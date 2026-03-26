"""Deterministic aggregate computation from a transaction sequence.

Reads RuleCondition aggregation types and computes values from the transaction list.
No LLM involved — fully deterministic.

Supported aggregations (Tier 1 inline / ComputedAttr scalar): sum, count, percentage_of_total,
  ratio, distinct_count, shared_distinct_count, average, max, days_since_first, age_years

Tier 2 (derived conditions): each DerivedAttr is computed to a scalar, then
  combined with derived_expression ("ratio" or "difference").
  For ratio: non-overlapping windows — DA[0] = recent period, DA[1] = prior period
    (immediately before, no overlap).
  For difference: each DA uses its own independent window from latest.

For percentage_of_total, ratio (Pattern A), and filtered count:
  - If filters (list of FilterClause) are set on the condition,
    the subset is transactions that match all filter clauses (AND/OR chained).
  - If no filter is set, percentage_of_total and ratio fall back to filtering
    by high_risk_countries (backward-compatible default).

Window: if window is set on the condition (e.g. "30d", "24h"), transactions are
  first restricted to those within that window of the latest transaction date.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from domain.models import DerivedAttr, FilterClause, Rule, RuleCondition, Transaction
from logging_config import get_logger

log = get_logger(__name__)


# ─── Shared operator evaluation (local copy to avoid circular import) ─────────

def _eval_op(actual: Any, operator: str, threshold: Any) -> bool:
    try:
        if operator in (">", "<", ">=", "<="):
            return {
                ">": float(actual) > float(threshold),
                "<": float(actual) < float(threshold),
                ">=": float(actual) >= float(threshold),
                "<=": float(actual) <= float(threshold),
            }[operator]
        elif operator == "==":
            try:
                return float(actual) == float(threshold)
            except (TypeError, ValueError):
                return str(actual) == str(threshold)
        elif operator == "!=":
            try:
                return float(actual) != float(threshold)
            except (TypeError, ValueError):
                return str(actual) != str(threshold)
        elif operator == "in":
            vals = threshold if isinstance(threshold, list) else [threshold]
            return str(actual) in [str(v) for v in vals]
        elif operator == "not_in":
            vals = threshold if isinstance(threshold, list) else [threshold]
            return str(actual) not in [str(v) for v in vals]
        elif operator == "mod_eq_0":
            divisor = float(threshold)
            return divisor != 0 and float(actual) % divisor == 0
    except (TypeError, ValueError):
        pass
    return False


# ─── Window filtering ─────────────────────────────────────────────────────────

def _parse_window(window_str: str) -> timedelta | None:
    """Parse a window string like '30d' or '24h' into a timedelta."""
    if not window_str:
        return None
    window_str = window_str.strip().lower()
    try:
        if window_str.endswith("d"):
            return timedelta(days=int(window_str[:-1]))
        elif window_str.endswith("h"):
            return timedelta(hours=int(window_str[:-1]))
        elif window_str.endswith("m"):
            return timedelta(days=int(window_str[:-1]) * 30)
    except ValueError:
        pass
    return None


def _apply_window(transactions: list[Transaction], window_str: str | None) -> list[Transaction]:
    """
    Filter transactions to those within `window` of the latest transaction date.
    Transactions without a parseable 'date' attribute are kept (failsafe).
    If window is None or unparseable, returns all transactions unchanged.
    """
    delta = _parse_window(window_str) if window_str else None
    if delta is None:
        return transactions

    # Accept canonical "created_at" and legacy "date" key
    def _get_date_str(t: Transaction) -> str | None:
        return t.attributes.get("created_at") or t.attributes.get("date")

    dates = []
    for t in transactions:
        raw = _get_date_str(t)
        if raw:
            try:
                dates.append(datetime.strptime(str(raw)[:10], "%Y-%m-%d"))
            except ValueError:
                pass

    if not dates:
        return transactions  # no parseable dates — can't apply window

    latest = max(dates)
    cutoff = latest - delta

    result = []
    for t in transactions:
        raw = _get_date_str(t)
        if not raw:
            result.append(t)  # keep transactions with no date
            continue
        try:
            txn_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d")
            if txn_date >= cutoff:
                result.append(t)
        except ValueError:
            result.append(t)  # keep if date can't be parsed

    return result


def _apply_window_range(
    transactions: list[Transaction],
    outer_window: str,
    exclude_window: str,
) -> list[Transaction]:
    """Return transactions where (latest − outer) <= date < (latest − exclude).

    Used for "last N months excluding last M days" style CAs.
    Correct for ALL aggregation types (count, sum, average, distinct_count, etc.).
    """
    outer_delta = _parse_window(outer_window)
    exclude_delta = _parse_window(exclude_window)
    if outer_delta is None or exclude_delta is None:
        return transactions  # unparseable — fail safe

    def _get_date_str(t: Transaction) -> str | None:
        return t.attributes.get("created_at") or t.attributes.get("date")

    dates = []
    for t in transactions:
        raw = _get_date_str(t)
        if raw:
            try:
                dates.append(datetime.strptime(str(raw)[:10], "%Y-%m-%d"))
            except ValueError:
                pass

    if not dates:
        return transactions

    latest = max(dates)
    lower = latest - outer_delta
    upper = latest - exclude_delta

    result = []
    for t in transactions:
        raw = _get_date_str(t)
        if not raw:
            result.append(t)
            continue
        try:
            txn_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d")
            if lower <= txn_date < upper:
                result.append(t)
        except ValueError:
            result.append(t)
    return result


# ─── Filter matching ──────────────────────────────────────────────────────────

def _eval_clause(t: Transaction, fc: FilterClause) -> bool:
    """Evaluate one FilterClause against a transaction.

    If fc.value_field is set, the RHS is resolved from transaction.attributes[value_field]
    (cross-field comparison). Otherwise fc.value is used as a literal RHS.
    """
    rhs = t.attributes.get(fc.value_field) if fc.value_field else fc.value
    return _eval_op(t.attributes.get(fc.attribute), fc.operator, rhs)


def _matches_filters(t: Transaction, filters: list[FilterClause]) -> bool:
    """Evaluate a list of FilterClause objects against a transaction.

    Clauses are chained using each clause's connector field (AND/OR).
    The last clause's connector is ignored.
    Returns True if filters is empty (no restriction).
    """
    if not filters:
        return True
    result = _eval_clause(t, filters[0])
    for i in range(1, len(filters)):
        conn = (filters[i - 1].connector or "AND").upper()
        clause_result = _eval_clause(t, filters[i])
        result = result or clause_result if conn == "OR" else result and clause_result
    return result


def _is_high_risk(t: Transaction, rule: Rule) -> bool:
    if not rule.high_risk_countries:
        return False
    # Check canonical name first, fall back to legacy "country" key
    country = t.attributes.get("receive_country_code") or t.attributes.get("country", "")
    return str(country).strip() in rule.high_risk_countries


def _get_subset(
    transactions: list[Transaction],
    cond: RuleCondition,
    rule: Rule,
) -> list[Transaction]:
    """
    Return the subset of transactions relevant to a percentage/ratio/filtered-count condition.
    Uses the explicit filters list if set; falls back to high-risk country filter.
    """
    if cond.filters:
        return [t for t in transactions if _matches_filters(t, cond.filters)]
    # fallback — high-risk countries
    return [t for t in transactions if _is_high_risk(t, rule)]


# ─── Numeric value extraction ─────────────────────────────────────────────────

def _get_values(transactions: list[Transaction], attribute: str) -> list[float]:
    vals = []
    for t in transactions:
        v = t.attributes.get(attribute)
        if v is not None:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    return vals


# ─── Date range helpers (used by derived ratio conditions) ───────────────────

def _get_date(t: Transaction) -> datetime | None:
    """Parse the transaction date from created_at or date attribute."""
    raw = t.attributes.get("created_at") or t.attributes.get("date")
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _in_date_range(
    t: Transaction,
    start: datetime,
    end: datetime,
    inclusive_end: bool = True,
) -> bool:
    """Return True if the transaction date falls within [start, end] (or [start, end))."""
    d = _get_date(t)
    if d is None:
        return False
    return start <= d <= end if inclusive_end else start <= d < end


# ─── DerivedAttr computation helper ──────────────────────────────────────────

def _compute_da(da: DerivedAttr, pool: list[Transaction]) -> float:
    """Compute a single DerivedAttr's scalar value from an already-filtered pool."""
    if da.aggregation == "count":
        return float(len(pool))
    elif da.aggregation == "sum":
        return sum(_get_values(pool, da.attribute))
    elif da.aggregation == "average":
        vals = _get_values(pool, da.attribute)
        return sum(vals) / len(vals) if vals else 0.0
    elif da.aggregation == "max":
        vals = _get_values(pool, da.attribute)
        return max(vals) if vals else 0.0
    elif da.aggregation == "distinct_count":
        return float(len({str(t.attributes[da.attribute]) for t in pool if da.attribute in t.attributes}))
    return float(len(pool))  # safe fallback


# ─── ComputedAttr pre-computation ─────────────────────────────────────────────

def _compute_ca_scalar(
    aggregation: str,
    attribute: str,
    txns: list[Transaction],
    link_attribute: list[str] | None = None,
    anchor_date: datetime | None = None,
) -> float:
    """Compute a scalar from a transaction list for a ComputedAttr.

    anchor_date: the latest date across the full (pre-filter) windowed set, used as the
    reference point for days_since_first so that filtered subsets still measure against
    the overall sequence anchor rather than their own latest date.
    """
    if aggregation == "count":
        return float(len(txns))
    if aggregation == "sum":
        return sum(_get_values(txns, attribute))
    if aggregation == "average":
        vals = _get_values(txns, attribute)
        return sum(vals) / len(vals) if vals else 0.0
    if aggregation == "max":
        vals = _get_values(txns, attribute)
        return max(vals) if vals else 0.0
    if aggregation == "distinct_count":
        return float(len({str(t.attributes[attribute]) for t in txns if attribute in t.attributes}))
    if aggregation == "shared_distinct_count":
        from collections import defaultdict
        link_attrs = link_attribute or []
        link_to_primaries: dict[tuple, set] = defaultdict(set)
        for t in txns:
            primary = t.attributes.get(attribute)
            if primary is None:
                continue
            for la in link_attrs:
                lv = t.attributes.get(la)
                if lv is not None:
                    link_to_primaries[(la, str(lv))].add(str(primary))
        shared: set[str] = set()
        for primaries in link_to_primaries.values():
            if len(primaries) > 1:
                shared.update(primaries)
        return float(len(shared))
    if aggregation == "days_since_first":
        dates = [d for t in txns if (d := _get_date(t)) is not None]
        if not dates:
            return 0.0
        ref = anchor_date if anchor_date is not None else max(dates)
        earliest = min(dates)
        return float((ref - earliest).days)
    if aggregation == "age_years":
        all_dates = [d for t in txns if (d := _get_date(t)) is not None]
        if not all_dates:
            return 0.0
        reference_date = max(all_dates)
        for t in txns:
            raw_dob = t.attributes.get(attribute)
            if raw_dob:
                try:
                    dob = datetime.strptime(str(raw_dob)[:10], "%Y-%m-%d")
                    return float((reference_date - dob).days // 365)
                except ValueError:
                    pass
        return 0.0
    return float(len(txns))  # safe fallback


def _compute_all_attrs(transactions: list[Transaction], rule: Rule, aggregates: dict) -> None:
    """Pre-compute all ComputedAttrs once before condition evaluation.

    Processes CAs in declaration order so later CAs can reference earlier CA outputs
    via t.attributes (since each CA injects its result into all relevant transactions).

    Scalar CA (no group_by):
      Computes aggregation(attribute)[window] over filtered transactions.
      Stores the raw scalar in aggregates[ca.name].
      Injects the scalar into every t.attributes[ca.name] so later CA filters can use it.

    Group CA (group_by set):
      Computes aggregation(attribute) per group in the windowed subset.
      Injects the raw per-group aggregate value into each t.attributes[ca.name].
      Later CAs and condition filters compare against this value directly
      (e.g. is_new_recipient == 1 to identify new recipients).
    """
    if not rule.computed_attrs:
        return

    for ca in rule.computed_attrs:

        # ── Derived mode: combine two previously computed scalar CAs ───────────
        if ca.derived_from:
            src_a = ca.derived_from[0] if len(ca.derived_from) > 0 else None
            src_b = ca.derived_from[1] if len(ca.derived_from) > 1 else None
            if src_a not in aggregates:
                log.warning(
                    "compute_all_attrs | derived CA '%s' references '%s' which is not in aggregates "
                    "(not yet computed or is a boolean CA). Check declaration order.",
                    ca.name, src_a,
                )
            if src_b and src_b not in aggregates:
                log.warning(
                    "compute_all_attrs | derived CA '%s' references '%s' which is not in aggregates "
                    "(not yet computed or is a boolean CA). Check declaration order.",
                    ca.name, src_b,
                )
            a = aggregates.get(src_a, 0.0) if src_a else 0.0
            b = aggregates.get(src_b, 0.0) if src_b else 0.0
            if ca.aggregation == "ratio":
                scalar = a / b if b != 0 else float("inf")
                log.debug("compute_all_attrs | %s (derived, ratio): %s / %s = %s", ca.name, a, b, scalar)
            else:  # "difference"
                scalar = a - b
                log.debug("compute_all_attrs | %s (derived, difference): %s - %s = %s", ca.name, a, b, scalar)
            aggregates[ca.name] = scalar
            for t in transactions:
                t.attributes[ca.name] = scalar
            continue

        if ca.window_exclude:
            windowed = _apply_window_range(transactions, ca.window, ca.window_exclude)
        else:
            windowed = _apply_window(transactions, ca.window)
        windowed_dates = [d for t in windowed if (d := _get_date(t)) is not None]
        anchor = max(windowed_dates) if windowed_dates else None

        if ca.group_by:
            # Group mode: compute per-group aggregate, inject raw value per transaction
            groups: dict[str, list[Transaction]] = {}
            for t in windowed:
                gval = str(t.attributes.get(ca.group_by, "__none__"))
                groups.setdefault(gval, []).append(t)

            group_aggs: dict[str, float] = {}
            for gval, group_txns in groups.items():
                filtered = (
                    [t for t in group_txns if _matches_filters(t, ca.filters)]
                    if ca.filters else group_txns
                )
                group_aggs[gval] = _compute_ca_scalar(
                    ca.aggregation, ca.attribute, filtered,
                    link_attribute=ca.link_attribute, anchor_date=anchor,
                )

            for t in windowed:
                gval = str(t.attributes.get(ca.group_by, "__none__"))
                t.attributes[ca.name] = group_aggs.get(gval, 0.0)
            # Store the max per-group value in aggregates so conditions can reference it directly.
            # max semantics = "any group fires" (the common case for group CAs in conditions).
            aggregates[ca.name] = max(group_aggs.values()) if group_aggs else 0.0
            log.info("compute_all_attrs | %s (group_by=%s): %d groups, values=%s, max=%s", ca.name, ca.group_by, len(groups), dict(list(group_aggs.items())[:5]), aggregates[ca.name])

        else:
            # Scalar mode: compute one value, store in aggregates, inject into all transactions
            if ca.filters:
                for j, fc in enumerate(ca.filters):
                    match_count = sum(1 for t in windowed if _eval_clause(t, fc))
                    sample_vals = [t.attributes.get(fc.attribute) for t in windowed[:3]]
                    log.info(
                        "compute_all_attrs | %s filter[%d] attr=%s op=%s val=%r field=%r: %d/%d match (sample vals: %s)",
                        ca.name, j, fc.attribute, fc.operator, fc.value, fc.value_field,
                        match_count, len(windowed), sample_vals,
                    )
            filtered = (
                [t for t in windowed if _matches_filters(t, ca.filters)]
                if ca.filters else windowed
            )
            scalar = _compute_ca_scalar(
                ca.aggregation, ca.attribute, filtered,
                link_attribute=ca.link_attribute, anchor_date=anchor,
            )
            aggregates[ca.name] = scalar
            for t in transactions:  # inject into ALL transactions so later CA filters can read it
                t.attributes[ca.name] = scalar
            log.info("compute_all_attrs | %s (scalar): windowed=%d filtered=%d value=%s", ca.name, len(windowed), len(filtered), scalar)


# ─── Tier 1 aggregation helper ────────────────────────────────────────────────

def _compute_tier1_agg(cond: RuleCondition, txns: list[Transaction], rule: Rule) -> float:
    """Compute a Tier 1 aggregate for an already-windowed transaction list."""
    if cond.aggregation == "sum":
        if cond.filters:
            filtered = [t for t in txns if _matches_filters(t, cond.filters)]
            return sum(_get_values(filtered, cond.attribute))
        return sum(_get_values(txns, cond.attribute))

    if cond.aggregation == "count":
        if cond.filters:
            return float(sum(1 for t in txns if _matches_filters(t, cond.filters)))
        return float(len(txns))

    if cond.aggregation == "distinct_count":
        uniq = {str(t.attributes[cond.attribute]) for t in txns if cond.attribute in t.attributes}
        return float(len(uniq))

    if cond.aggregation == "shared_distinct_count":
        from collections import defaultdict
        link_attrs = cond.link_attribute or []
        link_to_primaries: dict[tuple, set] = defaultdict(set)
        for t in txns:
            primary = t.attributes.get(cond.attribute)
            if primary is None:
                continue
            for la in link_attrs:
                lv = t.attributes.get(la)
                if lv is not None:
                    link_to_primaries[(la, str(lv))].add(str(primary))
        shared: set[str] = set()
        for primaries in link_to_primaries.values():
            if len(primaries) > 1:
                shared.update(primaries)
        return float(len(shared))

    if cond.aggregation == "average":
        if cond.filters:
            filtered = [t for t in txns if _matches_filters(t, cond.filters)]
            vals = _get_values(filtered, cond.attribute)
        else:
            vals = _get_values(txns, cond.attribute)
        return sum(vals) / len(vals) if vals else 0.0

    if cond.aggregation == "max":
        if cond.filters:
            filtered = [t for t in txns if _matches_filters(t, cond.filters)]
            vals = _get_values(filtered, cond.attribute)
        else:
            vals = _get_values(txns, cond.attribute)
        return max(vals) if vals else 0.0

    if cond.aggregation == "percentage_of_total":
        all_vals = _get_values(txns, cond.attribute)
        total = sum(all_vals)
        if total == 0:
            return 0.0
        subset = _get_subset(txns, cond, rule)
        subset_total = sum(_get_values(subset, cond.attribute))
        return subset_total / total

    if cond.aggregation == "ratio":
        all_vals = _get_values(txns, cond.attribute)
        total = sum(all_vals)
        subset = _get_subset(txns, cond, rule)
        subset_total = sum(_get_values(subset, cond.attribute))
        complement = total - subset_total
        return subset_total / complement if complement != 0 else float("inf")

    if cond.aggregation == "days_since_first":
        all_dates = [d for t in txns if (d := _get_date(t)) is not None]
        if not all_dates:
            return 0.0
        latest = max(all_dates)
        if cond.filters:
            filtered = [t for t in txns if _matches_filters(t, cond.filters)]
            target_dates = [d for t in filtered if (d := _get_date(t)) is not None]
        else:
            target_dates = all_dates
        earliest = min(target_dates) if target_dates else latest
        return float((latest - earliest).days)

    if cond.aggregation == "age_years":
        # Reference point: latest transaction date in the windowed sequence
        all_dates = [d for t in txns if (d := _get_date(t)) is not None]
        if not all_dates:
            return 0.0
        reference_date = max(all_dates)
        # Read date_of_birth from the first transaction that has it
        attr = cond.attribute or "date_of_birth"
        dob = None
        for t in txns:
            raw_dob = t.attributes.get(attr)
            if raw_dob:
                try:
                    dob = datetime.strptime(str(raw_dob)[:10], "%Y-%m-%d")
                    break
                except ValueError:
                    pass
        if dob is None:
            return 0.0
        return float((reference_date - dob).days // 365)

    raise ValueError(
        f"Unsupported aggregation '{cond.aggregation}' on attribute '{cond.attribute}'. "
        f"Supported: sum, count, average, max, percentage_of_total, ratio, distinct_count, "
        f"shared_distinct_count, days_since_first, age_years."
    )


# ─── Main computation ─────────────────────────────────────────────────────────

def compute_aggregates(rule: Rule, transactions: list[Transaction]) -> dict:
    """
    Compute all aggregate values referenced in the rule's conditions.
    Returns a dict mapping a descriptive key to the computed value.
    """
    all_dates = [
        datetime.strptime(str(t.attributes.get("created_at") or t.attributes.get("date", ""))[:10], "%Y-%m-%d")
        for t in transactions
        if t.attributes.get("created_at") or t.attributes.get("date")
    ]
    latest_date = max(all_dates).date() if all_dates else None
    log.info("compute_aggregates | txns=%d latest_date=%s", len(transactions), latest_date)

    results = {}

    # Pre-compute all ComputedAttrs in order before any condition evaluation.
    # Scalar CAs are stored in results and injected into t.attributes.
    # Boolean CAs (group_by) are injected into t.attributes only.
    _compute_all_attrs(transactions, rule, results)

    for cond in rule.conditions:

        # ── ComputedAttr-backed condition — already in results from pre-pass ──
        if cond.computed_attr_name:
            continue  # value already stored in results[cond.computed_attr_name] by _compute_all_attrs

        # ── Tier 2: derived condition ─────────────────────────────────────────
        if cond.derived_attributes is not None:
            key = cond.aggregate_key()
            expr = cond.derived_expression or "ratio"

            if expr == "ratio" and len(cond.derived_attributes) == 2:
                da0, da1 = cond.derived_attributes[0], cond.derived_attributes[1]
                window_mode = cond.window_mode or "non_overlapping"

                if window_mode == "independent":
                    # Each DA applies its window independently from latest_date.
                    # Used for same-window comparisons (different filters or different attributes).
                    num_pool = _apply_window(transactions, da0.window)
                    if da0.filters:
                        num_pool = [t for t in num_pool if _matches_filters(t, da0.filters)]

                    den_pool = _apply_window(transactions, da1.window)
                    if da1.filters:
                        den_pool = [t for t in den_pool if _matches_filters(t, da1.filters)]

                    numerator = _compute_da(da0, num_pool)
                    denominator = _compute_da(da1, den_pool)

                else:
                    # Non-overlapping window semantics (default):
                    #   DA[0] (numerator)  = [latest - window[0], latest]
                    #   DA[1] (denominator) = [latest - window[0] - window[1], latest - window[0])
                    delta0 = _parse_window(da0.window)
                    delta1 = _parse_window(da1.window)

                    all_dates = [d for t in transactions if (d := _get_date(t)) is not None]
                    if not all_dates or delta0 is None or delta1 is None:
                        results[key] = 0.0
                        continue

                    latest = max(all_dates)
                    period0_start = latest - delta0           # recent period start
                    period1_end = period0_start               # prior period ends where recent begins
                    period1_start = period0_start - delta1    # prior period start

                    num_pool = [t for t in transactions if _in_date_range(t, period0_start, latest)]
                    den_pool = [t for t in transactions if _in_date_range(t, period1_start, period1_end, inclusive_end=False)]

                    if da0.filters:
                        num_pool = [t for t in num_pool if _matches_filters(t, da0.filters)]
                    if da1.filters:
                        den_pool = [t for t in den_pool if _matches_filters(t, da1.filters)]

                    numerator = _compute_da(da0, num_pool)
                    denominator = _compute_da(da1, den_pool)

                results[key] = numerator / denominator if denominator != 0 else float("inf")
                # Also store intermediates so the corrector can see component values
                results[da0.name] = numerator
                results[da1.name] = denominator

            else:
                # difference and other expressions: independent windows from latest
                derived_values: dict[str, float] = {}
                for da in cond.derived_attributes:
                    da_pool = _apply_window(transactions, da.window)
                    if da.filters:
                        da_pool = [t for t in da_pool if _matches_filters(t, da.filters)]
                    derived_values[da.name] = _compute_da(da, da_pool)

                vals_list = [derived_values[da.name] for da in cond.derived_attributes]
                if expr == "difference":
                    results[key] = vals_list[0] - vals_list[1] if len(vals_list) >= 2 else 0.0
                else:
                    results[key] = vals_list[0] if vals_list else 0.0

            log.debug("compute_aggregates | %s = %s", key, results.get(key))
            continue

        # ── Tier 1: standard aggregation ─────────────────────────────────────
        if not cond.aggregation:
            continue  # stateless condition — skip

        # Apply window first (computed attrs already injected in pre-pass above)
        txns = _apply_window(transactions, cond.window)

        key = cond.aggregate_key()

        if cond.group_by:
            # Partition by group_by attribute, compute aggregate per group,
            # then reduce to a single decisive value:
            #   any + (> / >=) → max across groups (at least one group fires)
            #   any + (< / <=) → min across groups
            #   all + (> / >=) → min across groups (every group must fire)
            #   all + (< / <=) → max across groups
            groups: dict[str, list[Transaction]] = {}
            for t in txns:
                gval = str(t.attributes.get(cond.group_by, "__none__"))
                groups.setdefault(gval, []).append(t)
            group_values = {gval: _compute_tier1_agg(cond, gtxns, rule) for gval, gtxns in groups.items()}
            if not group_values:
                results[key] = 0.0
            else:
                is_any = (cond.group_mode or "any") == "any"
                lt_op = cond.operator in ("<", "<=")
                use_max = (is_any and not lt_op) or (not is_any and lt_op)
                results[key] = max(group_values.values()) if use_max else min(group_values.values())
            log.debug("compute_aggregates | %s = %s (groups=%d)", key, results[key], len(group_values))
        else:
            results[key] = _compute_tier1_agg(cond, txns, rule)
            log.debug("compute_aggregates | %s = %s", key, results.get(key))

    log.debug("compute_aggregates | full results: %s", results)
    return results
