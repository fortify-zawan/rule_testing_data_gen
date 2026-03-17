"""Deterministic aggregate computation from a transaction sequence.

Reads RuleCondition aggregation types and computes values from the transaction list.
No LLM involved — fully deterministic.

Supported aggregations (Tier 1): sum, count, percentage_of_total, ratio, distinct_count,
  average, max, days_since_first

Tier 2 (derived conditions): each DerivedAttr is computed to a scalar, then
  combined with derived_expression ("ratio" or "difference").
  For ratio: non-overlapping windows — DA[0] = recent period, DA[1] = prior period
    (immediately before, no overlap).
  For difference: each DA uses its own independent window from latest.

For percentage_of_total, ratio (Pattern A), and filtered count:
  - If filter_attribute/filter_operator/filter_value are set on the condition,
    the subset is transactions that match that filter expression.
  - If no filter is set, percentage_of_total and ratio fall back to filtering
    by high_risk_countries (backward-compatible default).

Window: if window is set on the condition (e.g. "30d", "24h"), transactions are
  first restricted to those within that window of the latest transaction date.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from domain.models import DerivedAttr, Rule, RuleCondition, Transaction
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
            return str(actual) == str(threshold)
        elif operator == "!=":
            return str(actual) != str(threshold)
        elif operator == "in":
            vals = threshold if isinstance(threshold, list) else [threshold]
            return str(actual) in [str(v) for v in vals]
        elif operator == "not_in":
            vals = threshold if isinstance(threshold, list) else [threshold]
            return str(actual) not in [str(v) for v in vals]
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


# ─── Filter matching ──────────────────────────────────────────────────────────

def _matches_filter(t: Transaction, attr: str, op: str, val: Any) -> bool:
    return _eval_op(t.attributes.get(attr), op, val)


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
    Uses the explicit filter fields if set; falls back to high-risk country filter.
    """
    if cond.filter_attribute and cond.filter_operator is not None:
        return [t for t in transactions if _matches_filter(t, cond.filter_attribute, cond.filter_operator, cond.filter_value)]
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
    return float(len(pool))  # safe fallback


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

    for cond in rule.conditions:

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
                    if da0.filter_attribute and da0.filter_operator is not None:
                        num_pool = [t for t in num_pool if _matches_filter(
                            t, da0.filter_attribute, da0.filter_operator, da0.filter_value)]

                    den_pool = _apply_window(transactions, da1.window)
                    if da1.filter_attribute and da1.filter_operator is not None:
                        den_pool = [t for t in den_pool if _matches_filter(
                            t, da1.filter_attribute, da1.filter_operator, da1.filter_value)]

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

                    if da0.filter_attribute and da0.filter_operator is not None:
                        num_pool = [t for t in num_pool if _matches_filter(
                            t, da0.filter_attribute, da0.filter_operator, da0.filter_value)]
                    if da1.filter_attribute and da1.filter_operator is not None:
                        den_pool = [t for t in den_pool if _matches_filter(
                            t, da1.filter_attribute, da1.filter_operator, da1.filter_value)]

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
                    if da.filter_attribute and da.filter_operator is not None:
                        da_pool = [t for t in da_pool if _matches_filter(
                            t, da.filter_attribute, da.filter_operator, da.filter_value)]
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

        # Apply window first
        txns = _apply_window(transactions, cond.window)

        key = cond.aggregate_key()

        if cond.aggregation == "sum":
            if cond.filter_attribute and cond.filter_operator is not None:
                filtered = [t for t in txns if _matches_filter(t, cond.filter_attribute, cond.filter_operator, cond.filter_value)]
                results[key] = sum(_get_values(filtered, cond.attribute))
            else:
                results[key] = sum(_get_values(txns, cond.attribute))

        elif cond.aggregation == "count":
            # If a filter is specified, count only matching transactions
            if cond.filter_attribute and cond.filter_operator is not None:
                results[key] = sum(
                    1 for t in txns
                    if _matches_filter(t, cond.filter_attribute, cond.filter_operator, cond.filter_value)
                )
            else:
                results[key] = len(txns)

        elif cond.aggregation == "distinct_count":
            uniq = {str(t.attributes[cond.attribute]) for t in txns if cond.attribute in t.attributes}
            results[key] = len(uniq)

        elif cond.aggregation == "average":
            if cond.filter_attribute and cond.filter_operator is not None:
                filtered = [t for t in txns if _matches_filter(t, cond.filter_attribute, cond.filter_operator, cond.filter_value)]
                vals = _get_values(filtered, cond.attribute)
            else:
                vals = _get_values(txns, cond.attribute)
            results[key] = sum(vals) / len(vals) if vals else 0.0

        elif cond.aggregation == "max":
            if cond.filter_attribute and cond.filter_operator is not None:
                filtered = [t for t in txns if _matches_filter(t, cond.filter_attribute, cond.filter_operator, cond.filter_value)]
                vals = _get_values(filtered, cond.attribute)
            else:
                vals = _get_values(txns, cond.attribute)
            results[key] = max(vals) if vals else 0.0

        elif cond.aggregation == "percentage_of_total":
            all_vals = _get_values(txns, cond.attribute)
            total = sum(all_vals)
            if total == 0:
                results[key] = 0.0
            else:
                subset = _get_subset(txns, cond, rule)
                subset_total = sum(_get_values(subset, cond.attribute))
                results[key] = subset_total / total

        elif cond.aggregation == "ratio":
            # Pattern A: legacy subset ÷ complement (same window)
            all_vals = _get_values(txns, cond.attribute)
            total = sum(all_vals)
            subset = _get_subset(txns, cond, rule)
            subset_total = sum(_get_values(subset, cond.attribute))
            complement = total - subset_total
            results[key] = subset_total / complement if complement != 0 else float("inf")

        elif cond.aggregation == "days_since_first":
            # Parse all dates in the window
            def _parse_date(t: Transaction) -> datetime | None:
                raw = t.attributes.get("created_at") or t.attributes.get("date")
                if not raw:
                    return None
                try:
                    return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
                except ValueError:
                    return None

            all_dates = [d for t in txns if (d := _parse_date(t)) is not None]
            if not all_dates:
                results[key] = 0.0
            else:
                latest = max(all_dates)
                # If filter set, find earliest among matching transactions; else earliest of all
                if cond.filter_attribute and cond.filter_operator is not None:
                    filtered = [t for t in txns if _matches_filter(t, cond.filter_attribute, cond.filter_operator, cond.filter_value)]
                    target_dates = [d for t in filtered if (d := _parse_date(t)) is not None]
                else:
                    target_dates = all_dates
                earliest = min(target_dates) if target_dates else latest
                results[key] = float((latest - earliest).days)

        else:
            # Unsupported aggregation — raise clearly rather than silently returning 0
            raise ValueError(
                f"Unsupported aggregation '{cond.aggregation}' on attribute '{cond.attribute}'. "
                f"Supported: sum, count, average, max, percentage_of_total, ratio, distinct_count, days_since_first."
            )

        log.debug("compute_aggregates | %s = %s", key, results.get(key))

    log.debug("compute_aggregates | full results: %s", results)
    return results
