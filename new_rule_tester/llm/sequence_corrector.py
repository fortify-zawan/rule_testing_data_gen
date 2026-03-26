"""Internal correction of failed transactions (stateless) or sequences (behavioral)."""
import json

from config.schema_loader import (
    canonical_name,
    format_attributes_for_prompt,
    normalize_country_values,
)
from llm.sequence_generator import _rule_allowed_attrs
from domain.models import ConditionResult, Rule, Transaction
from llm.llm_wrapper import call_llm_json
from logging_config import get_logger
from prompts.sequence_corrector import (
    BEHAVIORAL_CORRECT_PROMPT,
    STATELESS_CORRECT_PROMPT,
    SYSTEM,
)

log = get_logger(__name__)


def _canonicalize_attrs(attrs: dict, high_risk_countries: list[str] | None = None) -> dict:
    renamed = {canonical_name(k): v for k, v in attrs.items()}
    return normalize_country_values(renamed, high_risk_countries)


def _window_days(w: str | None) -> int | None:
    if not w:
        return None
    w = w.strip().lower()
    try:
        if w.endswith("d"):
            return int(w[:-1])
        if w.endswith("h"):
            return max(1, int(w[:-1]) // 24)
        if w.endswith("m"):
            return int(w[:-1]) * 30
    except ValueError:
        pass
    return None


def _format_filter_desc(da) -> str:
    """Render a DerivedAttr's filters list as a human-readable string for the corrector prompt."""
    if not da.filters:
        return "all transactions"
    parts = []
    for k, fc in enumerate(da.filters):
        if fc.value_field:
            parts.append(f"{fc.attribute} {fc.operator} field({fc.value_field})")
        else:
            parts.append(f"{fc.attribute} {fc.operator} {fc.value}")
        if k < len(da.filters) - 1:
            parts.append(fc.connector)
    return " ".join(parts)


def _format_tier1_repair_guidance(
    rule: Rule,
    scenario_type: str,
    failed_conditions: list,
    aggregates: dict | None,
) -> str:
    """Generate explicit shortfall arithmetic for failing conditions (Tier 1 or CA-backed).

    Tells the LLM exactly how much needs to change and how.
    """
    if not aggregates or not failed_conditions:
        return ""

    failed_keys = {r.attribute for r in failed_conditions}
    parts = []

    # ── CA-backed conditions (computed_attr_name is set) ────────────────────
    ca_map = {ca.name: ca for ca in rule.computed_attrs} if rule.computed_attrs else {}
    for cond in rule.conditions:
        if not cond.computed_attr_name:
            continue
        key = cond.computed_attr_name
        if key not in failed_keys:
            continue
        ca = ca_map.get(key)
        current = aggregates.get(key, 0.0)
        threshold = cond.value
        op = cond.operator

        parts.append(f"\n--- COMPUTED ATTR REPAIR: {key} {op} {threshold} [FAIL, current={current}] ---")

        if ca is None:
            parts.append(f"No ComputedAttr definition found for '{key}'. Adjust the underlying source attributes.")
        elif ca.derived_from:
            # Derived CA — trace chain to source scalar CAs
            src_a_name = ca.derived_from[0] if len(ca.derived_from) > 0 else "?"
            src_b_name = ca.derived_from[1] if len(ca.derived_from) > 1 else "?"
            src_a = ca_map.get(src_a_name)
            src_b = ca_map.get(src_b_name)
            a_val = aggregates.get(src_a_name, 0.0)
            b_val = aggregates.get(src_b_name, 0.0)
            parts.append(f"Type: Derived CA ({ca.aggregation})")
            parts.append(f"  {key} = {src_a_name} {('/' if ca.aggregation == 'ratio' else '-')} {src_b_name}")
            parts.append(f"  Current: {src_a_name}={a_val}, {src_b_name}={b_val}, {key}={current}")
            if ca.aggregation == "ratio":
                if scenario_type == "risky":
                    # For > threshold: need a > threshold * b (strictly), so target slightly above
                    required_a = float(threshold) * float(b_val)
                    # Add a small buffer so the ratio strictly exceeds the threshold
                    target_a = required_a + max(1.0, required_a * 0.05)
                    shortfall = target_a - float(a_val)
                    parts.append(f"  Goal: {src_a_name} / {src_b_name} {op} {threshold}")
                    parts.append(f"  Need {src_a_name} > {threshold} × {b_val} = {required_a:.2f} (target: {target_a:.2f}, shortfall: {shortfall:.2f})")
                    if src_a:
                        flt = _format_filter_desc(src_a) if src_a.filters else "all transactions"
                        parts.append(f"  → Add filter-matching ({flt}) transactions to {src_a.window or 'full'} window.")
                        parts.append(f"  → Do NOT add transactions to {src_b_name}'s window — that raises the denominator.")
                    if src_b:
                        flt_b = _format_filter_desc(src_b) if src_b.filters else "all transactions"
                        parts.append(f"  → Optional: reduce ({flt_b}) transactions in {src_b.window or 'full'} window to lower denominator.")
                else:
                    allowed_a = float(threshold) * float(b_val)
                    excess = float(a_val) - allowed_a
                    parts.append(f"  Goal: keep {src_a_name} / {src_b_name} {op} {threshold}")
                    parts.append(f"  Current ratio {current:.2f} exceeds {threshold}. Reduce {src_a_name} by ~{excess:.2f}.")
                    if src_a:
                        flt = _format_filter_desc(src_a) if src_a.filters else "all transactions"
                        parts.append(f"  → Reduce or remove filter-matching ({flt}) transactions in {src_a.window or 'full'} window.")
            else:  # difference
                if scenario_type == "risky":
                    shortfall = float(threshold) - float(current) + (1 if op == ">" else 0)
                    parts.append(f"  Goal: {src_a_name} − {src_b_name} {op} {threshold} (shortfall: {shortfall:.2f})")
                    parts.append(f"  → Increase {src_a_name} or decrease {src_b_name} by ~{shortfall:.2f}.")
                else:
                    excess = float(current) - float(threshold)
                    parts.append(f"  Goal: keep {src_a_name} − {src_b_name} {op} {threshold} (excess: {excess:.2f})")
                    parts.append(f"  → Decrease {src_a_name} or increase {src_b_name} by ~{excess:.2f}.")
        elif ca.group_by:
            # Group CA — injects raw per-group aggregate value; condition has the comparison
            parts.append(f"Type: Group CA (group_by={ca.group_by})")
            parts.append(f"Engine: {ca.aggregation}({ca.attribute}) per {ca.group_by} → injects raw value per transaction")
            parts.append(f"Condition check: {ca.name} {op} {threshold}")
            if op in (">", ">="):
                parts.append(f"Goal (RISKY): ensure {ca.aggregation}({ca.attribute}) per group {op} {threshold}")
                parts.append(f"  → Increase per-group {ca.attribute} values or add more transactions per group.")
            else:
                parts.append(f"Goal (GENUINE): ensure {ca.aggregation}({ca.attribute}) per group does NOT satisfy {op} {threshold}")
                parts.append(f"  → Reduce per-group {ca.attribute} values or spread transactions across more groups.")
        else:
            # Scalar CA — one aggregate value stored and injected everywhere
            filter_desc = _format_filter_desc(ca) if ca.filters else "all transactions"
            if ca.window and ca.window_exclude:
                window_desc = f" in range (latest−{ca.window}) to (latest−{ca.window_exclude}) [excludes last {ca.window_exclude}]"
            elif ca.window:
                window_desc = f" within {ca.window}"
            else:
                window_desc = ""
            parts.append(f"Type: Scalar CA — {ca.aggregation}({ca.attribute}) over ({filter_desc}){window_desc}")
            parts.append(f"Current value: {current}  |  Condition: {key} {op} {threshold}")
            parts.append(f"Repair: adjust the SOURCE attributes that feed this computation ({ca.attribute}, filters: {filter_desc}).")
            if ca.window_exclude:
                parts.append(f"  IMPORTANT — This CA uses window_exclude={ca.window_exclude}.")
                parts.append(f"  Only transactions dated between (latest−{ca.window}) and (latest−{ca.window_exclude}) count.")
                parts.append(f"  Transactions in the last {ca.window_exclude} are EXCLUDED from this CA.")
                if op in (">", ">="):
                    parts.append(f"  → To increase the value: add transactions dated in the outer-but-not-inner band.")
                    parts.append(f"  → Do NOT place them in the last {ca.window_exclude} — they will not be counted.")
                else:
                    parts.append(f"  → To decrease the value: move transactions out of the outer-but-not-inner band")
                    parts.append(f"    (either older than {ca.window} or more recent than (latest−{ca.window_exclude})).")
            if op in (">", ">="):
                if ca.aggregation == "count":
                    need = int(threshold) + (1 if op == ">" else 0)
                    shortfall = need - int(current)
                    parts.append(f"  → Add {shortfall} more filter-matching transactions to push count to {need}.")
                elif ca.aggregation in ("sum", "average"):
                    shortfall = float(threshold) - float(current) + (1 if op == ">" else 0)
                    parts.append(f"  → Increase {ca.attribute} values on filter-matching transactions by ~{shortfall:.2f} in total.")
                else:
                    parts.append(f"  → Increase {ca.attribute} on the relevant transactions so the {ca.aggregation} exceeds {threshold}.")
            else:
                if ca.aggregation == "count":
                    need = int(threshold) - (1 if op == "<" else 0)
                    parts.append(f"  → Reduce filter-matching transactions to at most {need}.")
                elif ca.aggregation in ("sum", "average"):
                    excess = float(current) - float(threshold)
                    parts.append(f"  → Decrease {ca.attribute} values on filter-matching transactions by ~{excess:.2f} in total.")
                else:
                    parts.append(f"  → Decrease {ca.attribute} on the relevant transactions so the {ca.aggregation} stays below {threshold}.")
        parts.append("--- END COMPUTED ATTR REPAIR ---")

    # ── Tier 1 conditions (aggregation is set inline) ────────────────────────
    for cond in rule.conditions:
        if cond.derived_attributes:
            continue  # Tier 2 handled separately
        if cond.computed_attr_name:
            continue  # handled above
        if not cond.aggregation:
            continue
        key = cond.aggregate_key()
        if key not in failed_keys:
            continue

        current = aggregates.get(key, 0.0)
        threshold = float(cond.value)
        op = cond.operator
        filter_desc = _format_filter_desc(cond) if cond.filters else "all transactions"
        window_desc = f" within {cond.window}" if cond.window else ""

        parts.append(f"\n--- TIER 1 REPAIR: {key} {op} {threshold} [FAIL, current={current}] ---")

        if cond.aggregation == "count":
            if op in (">", ">="):
                need = int(threshold) + (1 if op == ">" else 0)
                shortfall = need - int(current)
                parts.append(f"You must generate a sequence with AT LEAST {need} transactions matching ({filter_desc}){window_desc}.")
                parts.append(f"Current count is {int(current)}. You need {shortfall} more matching transactions.")
            else:
                need = int(threshold) - (1 if op == "<" else 0)
                parts.append(f"You must generate a sequence with AT MOST {need} transactions matching ({filter_desc}){window_desc}.")

        elif cond.aggregation in ("sum", "average"):
            if op in (">", ">="):
                shortfall = threshold - current + (1 if op == ">" else 0)
                parts.append(f"Current {cond.aggregation}={current:.2f}, need {op} {threshold}. Shortfall: {shortfall:.2f}.")
                parts.append(f"Increase amounts or add more matching ({filter_desc}){window_desc} transactions to cover the gap.")
            else:
                excess = current - threshold
                parts.append(f"Current {cond.aggregation}={current:.2f}, need {op} {threshold}. Excess: {excess:.2f}.")
                parts.append(f"Reduce amounts or remove some matching ({filter_desc}){window_desc} transactions.")

        elif cond.aggregation == "distinct_count":
            if op in (">", ">="):
                need = int(threshold) + (1 if op == ">" else 0)
                shortfall = need - int(current)
                parts.append(f"Need {need} distinct values of {cond.attribute}{window_desc}, currently {int(current)}.")
                parts.append(f"Add {shortfall} transactions with NEW distinct values for {cond.attribute}.")

        # Detect computed attr filters and add targeted repair notes
        computed_attr_names = {ca.name for ca in rule.computed_attrs} if rule.computed_attrs else set()
        if cond.filters and computed_attr_names:
            ca_filter_names = [
                fc.attribute for fc in cond.filters
                if fc.attribute in computed_attr_names
            ]
            if ca_filter_names:
                parts.append("")
                parts.append("COMPUTED ATTRIBUTE NOTE:")
                for ca_name in ca_filter_names:
                    ca = next((c for c in rule.computed_attrs if c.name == ca_name), None)
                    if ca is None:
                        continue
                    if ca.group_by:
                        want_true = True  # default; check filter value
                        for fc in cond.filters:
                            if fc.attribute == ca_name:
                                want_true = str(fc.value).lower() in ("true", "1", "yes")
                                break
                        if want_true:
                            parts.append(
                                f"  {ca_name} (group_by={ca.group_by}) must be TRUE for motif transactions."
                            )
                            parts.append(
                                f"  Engine rule: {ca.aggregation}({ca.attribute}) per {ca.group_by} {ca.operator} {ca.value} → True"
                            )
                            parts.append(
                                f"  REPAIR: each motif {ca.group_by} must appear EXACTLY ONCE in the window."
                            )
                            parts.append(
                                f"  → Use a different {ca.group_by} for every new motif transaction."
                            )
                            parts.append(
                                f"  → Do NOT reuse existing {ca.group_by} values — that raises their count above 1 → False."
                            )
                        else:
                            parts.append(
                                f"  {ca_name} (group_by={ca.group_by}) must be FALSE for motif transactions."
                            )
                            parts.append(
                                f"  Engine rule: count per {ca.group_by} > 1 → False"
                            )
                            parts.append(
                                f"  → Ensure each motif {ca.group_by} appears 2+ times in the window."
                            )
                    else:
                        parts.append(
                            f"  {ca_name} (scalar, no group_by): {ca.aggregation}({ca.attribute}) {ca.operator} {ca.value}."
                        )
                        parts.append(
                            f"  → Modify {ca.attribute} on the first transaction to satisfy this condition."
                        )

        parts.append("--- END TIER 1 REPAIR ---")

    return "\n".join(parts)


def _format_derived_conditions(rule: Rule, scenario_type: str, aggregates: dict | None = None) -> str:
    """Serialize derived-condition DA details + concrete shortfall arithmetic into corrector prompt context."""
    parts = []
    for cond in rule.conditions:
        if not cond.derived_attributes:
            continue
        key = cond.aggregate_key()
        expr = cond.derived_expression or "ratio"

        if expr == "ratio" and len(cond.derived_attributes) == 2:
            da0, da1 = cond.derived_attributes[0], cond.derived_attributes[1]
            d0 = _window_days(da0.window)
            d1 = _window_days(da1.window)
            window_mode = cond.window_mode or "non_overlapping"

            flt0 = _format_filter_desc(da0)
            flt1 = _format_filter_desc(da1)

            parts.append(f"\n--- DERIVED CONDITION: {key} {cond.operator} {cond.value} ---")
            parts.append(f"Expression: {da0.name} / {da1.name} {cond.operator} {cond.value}")
            parts.append(f"Window mode: {window_mode}")
            parts.append("")

            if window_mode == "independent":
                parts.append(f"DA[0] = {da0.name}  [NUMERATOR]")
                parts.append(f"  aggregation : {da0.aggregation}({da0.attribute})")
                parts.append(f"  window      : {da0.window}  → [latest_date - {d0}d, latest_date]  (independent from latest)")
                parts.append(f"  filter      : {flt0}")
                parts.append("")
                parts.append(f"DA[1] = {da1.name}  [DENOMINATOR]")
                parts.append(f"  aggregation : {da1.aggregation}({da1.attribute})")
                parts.append(f"  window      : {da1.window}  → [latest_date - {d1}d, latest_date]  (independent from latest)")
                parts.append(f"  filter      : {flt1}")
                parts.append("")
                parts.append("PERIOD LAYOUT (both windows anchored independently at latest_date):")
                parts.append(f"  DA[0] covers: [latest_date - {d0}d, latest_date]")
                parts.append(f"  DA[1] covers: [latest_date - {d1}d, latest_date]")
            else:
                total_days = (d0 or 0) + (d1 or 0)
                parts.append(f"DA[0] = {da0.name}  [NUMERATOR — RECENT PERIOD]")
                parts.append(f"  aggregation : {da0.aggregation}({da0.attribute})")
                parts.append(f"  window      : {da0.window}  → [latest_date - {d0}d, latest_date]  (inclusive)")
                parts.append(f"  filter      : {flt0}")
                parts.append("")
                parts.append(f"DA[1] = {da1.name}  [DENOMINATOR — PRIOR PERIOD]")
                parts.append(f"  aggregation : {da1.aggregation}({da1.attribute})")
                parts.append(f"  window      : {da1.window}  → (latest_date - {total_days}d, latest_date - {d0}d)  (exclusive of recent period)")
                parts.append(f"  filter      : {flt1}")
                parts.append("")
                parts.append("PERIOD LAYOUT (non-overlapping, anchored at latest_date):")
                parts.append(f"  |←── prior {d1}d ──────→|←── recent {d0}d ──→| latest_date")
                parts.append(f"       DA[1] = {da1.name}      DA[0] = {da0.name}")
                parts.append(f"  Total timeline must span ≥ {total_days} days.")
            parts.append("")

            # If current aggregate values are known, give exact shortfall arithmetic
            if aggregates is not None:
                current_ratio = aggregates.get(key)
                da0_current = aggregates.get(da0.name)
                da1_current = aggregates.get(da1.name)

                if da0_current is not None and da1_current is not None:
                    parts.append("CURRENT COMPONENT VALUES (from last validation):")
                    parts.append(f"  {da0.name} (numerator)   = {da0_current}")
                    parts.append(f"  {da1.name} (denominator) = {da1_current}")
                    parts.append(f"  ratio = {current_ratio}")
                    parts.append("")

                    if scenario_type == "risky":
                        required_da0 = float(cond.value) * float(da1_current)
                        shortfall = required_da0 - float(da0_current)
                        parts.append("SHORTFALL ANALYSIS (risky must fire):")
                        parts.append(f"  Required: {da0.name} > {cond.value} × {da1_current} = {required_da0:.2f}")
                        parts.append(f"  Current : {da0.name} = {da0_current}")
                        parts.append(f"  Shortfall: need to ADD at least {shortfall:.2f} more to {da0.name}")
                        if window_mode == "independent":
                            if da0.aggregation == "sum":
                                parts.append(f"  → Add or increase filter-matching ({flt0}) transactions within the {da0.window} window.")
                            elif da0.aggregation == "count":
                                needed_count = int(required_da0) + 1 - int(da0_current)
                                parts.append(f"  → Add at least {needed_count} more filter-matching ({flt0}) transactions within the {da0.window} window.")
                        else:
                            d0_label = f"RECENT {d0}d"
                            if da0.aggregation == "sum":
                                parts.append(f"  MANDATORY: You MUST add filter-matching ({flt0}) transactions to the {d0_label} period — the numerator ({da0.name}) must be > 0.")
                                parts.append(f"  → Primary repair: Add or increase filter-matching ({flt0}) transactions in the {d0_label} period to cover the shortfall above.")
                                parts.append(f"  → Optional lever: You may also reduce or remove EXISTING filter-matching ({flt1}) transactions in the PRIOR {d1}d period — this lowers the denominator and reduces how much you need to add to the recent period.")
                                parts.append("  → Do NOT add NEW filter-matching transactions to the PRIOR period — that raises the denominator and worsens the ratio.")
                                parts.append(f"  → Reducing the prior period alone is NOT sufficient — you must have filter-matching ({flt0}) transactions in the recent period for all related conditions to pass.")
                            elif da0.aggregation == "count":
                                needed_count = int(required_da0) + 1 - int(da0_current)
                                parts.append(f"  MANDATORY: You MUST add filter-matching ({flt0}) transactions to the {d0_label} period — the numerator ({da0.name}) must be > 0.")
                                parts.append(f"  → Primary repair: Add at least {needed_count} more filter-matching ({flt0}) transactions dated within the {d0_label} period.")
                                parts.append(f"  → Optional lever: You may also reduce or remove EXISTING filter-matching ({flt1}) transactions in the PRIOR {d1}d period — fewer prior-period matches lowers the denominator.")
                                parts.append("  → Do NOT add NEW filter-matching transactions to the PRIOR period — that raises the denominator and worsens the ratio.")
                                parts.append(f"  → Reducing the prior period alone is NOT sufficient — you must have filter-matching ({flt0}) transactions in the recent period for all related conditions to pass.")
                    else:
                        allowed_da0 = float(cond.value) * float(da1_current)
                        excess = float(da0_current) - allowed_da0
                        parts.append("EXCESS ANALYSIS (genuine must not fire):")
                        parts.append(f"  Allowed: {da0.name} ≤ {cond.value} × {da1_current} = {allowed_da0:.2f}")
                        parts.append(f"  Current: {da0.name} = {da0_current}  (excess = {excess:.2f})")
                        if window_mode == "independent":
                            parts.append(f"  → Reduce filter-matching ({flt0}) transactions within the {da0.window} window by at least {excess:.2f}.")
                        else:
                            parts.append(f"  → Move or reduce filter-matching transactions in the RECENT {d0}d period by at least {excess:.2f}.")
            else:
                # No current aggregates — give general guidance
                if scenario_type == "risky":
                    parts.append("REPAIR GUIDANCE (risky must fire):")
                    parts.append(f"  {da0.name} must be > {cond.value} × {da1.name}")
                    if window_mode == "independent":
                        parts.append(f"  → Add or increase filter-matching ({flt0}) transactions within the {da0.window} window.")
                    else:
                        parts.append(f"  MANDATORY: You MUST add filter-matching ({flt0}) transactions to the RECENT {d0}d period — the numerator must be > 0.")
                        parts.append(f"  → Primary repair: Add or increase filter-matching ({flt0}) transactions in the RECENT {d0}d period.")
                        parts.append(f"  → Optional lever: You may also reduce or remove EXISTING filter-matching ({flt1}) transactions in the PRIOR {d1}d period — this lowers the denominator and improves the ratio.")
                        parts.append("  → Do NOT add NEW filter-matching transactions to the PRIOR period — that raises the denominator and worsens the ratio.")
                        parts.append("  → Reducing the prior period alone is NOT sufficient — you must have transactions in the recent period for all related conditions to pass.")
                else:
                    parts.append("REPAIR GUIDANCE (genuine must not fire):")
                    parts.append(f"  {da0.name} must be ≤ {cond.value} × {da1.name}")
                    if window_mode == "independent":
                        parts.append(f"  → Reduce filter-matching ({flt0}) transactions within the {da0.window} window.")
                    else:
                        parts.append(f"  → Reduce or move filter-matching transactions out of the RECENT {d0}d period.")

            parts.append("--- END DERIVED CONDITION ---")

        elif expr == "difference" and len(cond.derived_attributes) == 2:
            da0, da1 = cond.derived_attributes[0], cond.derived_attributes[1]
            parts.append(f"\n--- DERIVED CONDITION: {key} {cond.operator} {cond.value} ---")
            parts.append(f"Expression: {da0.name} - {da1.name} {cond.operator} {cond.value}")
            parts.append(f"DA[0] = {da0.name}: {da0.aggregation}({da0.attribute}), window={da0.window} (from latest)")
            parts.append(f"DA[1] = {da1.name}: {da1.aggregation}({da1.attribute}), window={da1.window} (from latest)")
            if aggregates is not None:
                da0_v = aggregates.get(da0.name)
                da1_v = aggregates.get(da1.name)
                if da0_v is not None and da1_v is not None:
                    parts.append(f"Current: {da0.name}={da0_v}, {da1.name}={da1_v}, difference={da0_v - da1_v}")
            parts.append("--- END DERIVED CONDITION ---")

    return "\n".join(parts)

# ─── Stateless correction (fix one transaction at a time) ────────────────────


def correct_stateless_transaction(
    rule: Rule,
    transaction: Transaction,
    failed_conditions: list[ConditionResult],
    prototype_attrs: dict,
) -> dict:
    """Returns corrected attribute dict for a single failed transaction."""
    failed_desc = "\n".join(
        f"- {r.attribute} {r.operator} {r.threshold}: actual value was {r.actual_value} → FAIL"
        for r in failed_conditions
    )
    expectation = (
        "trigger the rule (all conditions must pass)"
        if transaction.tag == "risky"
        else "NOT trigger the rule (at least one condition must fail)"
    )

    prompt = STATELESS_CORRECT_PROMPT.format(
        raw_expression=rule.raw_expression,
        tag=transaction.tag,
        attributes=json.dumps(transaction.attributes),
        failed_conditions=failed_desc,
        prototype=json.dumps(prototype_attrs),
        expectation=expectation,
    )
    return call_llm_json(prompt, system=SYSTEM)


# ─── Behavioral correction helpers ───────────────────────────────────────────

def _build_transaction_table(
    transactions: list[Transaction],
    allowed_attrs: set[str],
) -> tuple[str, str, str]:
    """Render transactions as a compact pipe-delimited table.

    Returns (table_str, anchor_date, next_id).
    anchor_date is the created_at of the last transaction by date.
    next_id is the next sequential ID after the highest existing one.
    """
    if not transactions:
        return "(no transactions)", "", "t-001"

    sorted_txns = sorted(transactions, key=lambda t: t.attributes.get("created_at", ""))

    # Columns: created_at first, then remaining allowed attrs alphabetically
    attr_cols = ["created_at"] + sorted(c for c in allowed_attrs if c != "created_at")
    all_cols = ["id"] + attr_cols

    header = " | ".join(all_cols)
    sep = "-" * len(header)
    rows = [header, sep]
    for t in sorted_txns:
        vals = [t.id] + [str(t.attributes.get(col, "")) for col in attr_cols]
        rows.append(" | ".join(vals))

    anchor_date = sorted_txns[-1].attributes.get("created_at", "")

    # Derive next ID from the highest numeric suffix seen
    next_id = "t-001"
    max_num = -1
    prefix = "t"
    for t in transactions:
        parts = t.id.rsplit("-", 1)
        if len(parts) == 2:
            try:
                n = int(parts[1])
                if n > max_num:
                    max_num = n
                    prefix = parts[0]
            except ValueError:
                pass
    if max_num >= 0:
        next_id = f"{prefix}-{max_num + 1:03d}"

    return "\n".join(rows), anchor_date, next_id


def _apply_delta(
    original: list[Transaction],
    delta: dict,
    scenario_type: str,
    allowed: set[str],
    high_risk_countries: list[str] | None,
) -> list[Transaction]:
    """Apply an add/modify delta to the original transaction list.

    Transactions not referenced in the delta are preserved unchanged.
    The result is sorted by created_at.
    """
    # Work from a mutable copy keyed by ID
    txn_map: dict[str, Transaction] = {t.id: t for t in original}

    # Apply partial attribute modifications to existing transactions
    for txn_id, changes in (delta.get("modify") or {}).items():
        if not isinstance(changes, dict):
            log.warning("corrector | modify[%s]: expected dict, got %s — skipping", txn_id, type(changes))
            continue
        if txn_id not in txn_map:
            log.warning("corrector | modify: unknown transaction id %s — skipping", txn_id)
            continue
        canonical_changes = {
            k: v
            for k, v in _canonicalize_attrs(changes, high_risk_countries).items()
            if k in allowed
        }
        existing = txn_map[txn_id]
        txn_map[txn_id] = Transaction(
            id=existing.id,
            tag=existing.tag,
            attributes={**existing.attributes, **canonical_changes},
        )

    # Insert new transactions
    for t in (delta.get("add") or []):
        if not isinstance(t, dict):
            continue
        txn_id = t.get("id") or t.get("transaction_id")
        if not txn_id:
            log.warning("corrector | add: transaction missing id — skipping")
            continue
        raw_attrs = t.get("attributes") or {
            k: v for k, v in t.items() if k not in ("id", "transaction_id", "tag")
        }
        canonical_attrs = {
            k: v
            for k, v in _canonicalize_attrs(raw_attrs, high_risk_countries).items()
            if k in allowed
        }
        txn_map[txn_id] = Transaction(
            id=txn_id,
            tag=t.get("tag", scenario_type),
            attributes=canonical_attrs,
        )

    result = list(txn_map.values())
    result.sort(key=lambda t: t.attributes.get("created_at", ""))
    return result


# ─── Behavioral correction ───────────────────────────────────────────────────

def correct_behavioral_sequence(
    rule: Rule,
    scenario_type: str,
    transactions: list[Transaction],
    aggregates: dict,
    failed_conditions: list[ConditionResult],
    intent: str = "",
    feedback_history: list[str] | None = None,
) -> list[Transaction]:
    """Returns a corrected transaction list for a behavioral sequence.

    Sends the existing sequence as a compact table and asks the LLM for a
    delta (add/modify) rather than a full sequence regeneration. Transactions
    not referenced in the delta are preserved unchanged, so background
    transactions are never inadvertently lost.
    Falls back to the original sequence if the LLM returns malformed output.
    """
    log.info(
        "corrector | scenario=%s failed_conditions=%d input_txns=%d",
        scenario_type, len(failed_conditions), len(transactions),
    )
    for fc in failed_conditions:
        log.info("corrector | failed: %s %s %s (actual=%s)", fc.attribute, fc.operator, fc.threshold, fc.actual_value)

    _allowed = _rule_allowed_attrs(rule)

    # Build compact table representation of the existing sequence
    transaction_table, anchor_date, next_id = _build_transaction_table(transactions, _allowed)

    # Annotate failed conditions with group_by context
    cond_by_key = {c.aggregate_key(): c for c in rule.conditions if c.aggregation}
    failed_lines = []
    for r in failed_conditions:
        line = f"- {r.attribute} {r.operator} {r.threshold}: actual was {r.actual_value} → FAIL"
        cond = cond_by_key.get(r.attribute)
        if cond and cond.group_by:
            direction = "max" if cond.operator not in ("<", "<=") else "min"
            line += (
                f"  [group_by={cond.group_by}, group_mode={cond.group_mode or 'any'}:"
                f" actual={r.actual_value} is the {direction} across all {cond.group_by} groups]"
            )
        failed_lines.append(line)
    failed_desc = "\n".join(failed_lines)
    agg_json = json.dumps(aggregates, indent=2)

    if feedback_history:
        history_lines = "\n".join(f"  - {f}" for f in feedback_history)
        feedback_history_section = (
            "--- PREVIOUS USER INSTRUCTIONS (all must be respected) ---\n"
            f"{history_lines}\n"
            "--- END PREVIOUS INSTRUCTIONS ---"
        )
    else:
        feedback_history_section = ""

    tier1 = _format_tier1_repair_guidance(rule, scenario_type, failed_conditions, aggregates)
    derived = _format_derived_conditions(rule, scenario_type, aggregates)
    combined = "\n".join(filter(None, [tier1, derived]))
    if combined:
        repair_guidance_section = (
            "--- REPAIR GUIDANCE ---\n"
            "The following section explains exactly how to fix the failing aggregate(s):\n"
            f"{combined}\n"
            "--- END REPAIR GUIDANCE ---"
        )
    else:
        repair_guidance_section = ""

    prompt = BEHAVIORAL_CORRECT_PROMPT.format(
        n_transactions=len(transactions),
        anchor_date=anchor_date,
        next_id=next_id,
        transaction_table=transaction_table,
        schema_context=format_attributes_for_prompt(show_aliases=False, allowed_attrs=_allowed),
        raw_expression=rule.raw_expression,
        scenario_type=scenario_type,
        high_risk_countries=", ".join(rule.high_risk_countries) if rule.high_risk_countries else "none",
        aggregates=agg_json,
        failed_conditions=failed_desc,
        intent=intent or "none",
        repair_guidance_section=repair_guidance_section,
        feedback_history_section=feedback_history_section,
    )

    log.debug("corrector | repair_guidance_section present: %s", bool(repair_guidance_section))
    data = call_llm_json(prompt, system=SYSTEM)

    # Validate that the LLM returned a delta dict with at least one of add/modify
    if not isinstance(data, dict) or ("add" not in data and "modify" not in data):
        log.warning(
            "corrector | unexpected output format (type=%s, keys=%s) — falling back to original sequence",
            type(data).__name__,
            list(data.keys()) if isinstance(data, dict) else "n/a",
        )
        return transactions

    result = _apply_delta(transactions, data, scenario_type, _allowed, rule.high_risk_countries)

    adds = len(data.get("add") or [])
    modifies = len(data.get("modify") or {})
    log.info("corrector | delta applied: %d added, %d modified → %d total transactions", adds, modifies, len(result))
    return result
