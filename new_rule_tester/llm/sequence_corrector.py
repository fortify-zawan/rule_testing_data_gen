"""Internal correction of failed transactions (stateless) or sequences (behavioral)."""
import json

from config.schema_loader import (
    canonical_name,
    format_attributes_for_prompt,
    normalize_country_values,
)
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

            flt0 = (
                f"{da0.filter_attribute} {da0.filter_operator} {da0.filter_value}"
                if da0.filter_attribute else "all transactions"
            )
            flt1 = (
                f"{da1.filter_attribute} {da1.filter_operator} {da1.filter_value}"
                if da1.filter_attribute else "all transactions"
            )

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


# ─── Behavioral correction (regenerate full sequence) ────────────────────────

def correct_behavioral_sequence(
    rule: Rule,
    scenario_type: str,
    transactions: list[Transaction],
    aggregates: dict,
    failed_conditions: list[ConditionResult],
    intent: str = "",
    feedback_history: list[str] | None = None,
) -> list[Transaction]:
    """Returns a corrected transaction list for a behavioral sequence."""
    log.info(
        "corrector | scenario=%s failed_conditions=%d input_txns=%d",
        scenario_type, len(failed_conditions), len(transactions),
    )
    for fc in failed_conditions:
        log.info("corrector | failed: %s %s %s (actual=%s)", fc.attribute, fc.operator, fc.threshold, fc.actual_value)
    # Build a condition lookup so we can annotate group_by info on each failed condition
    cond_by_key = {c.aggregate_key(): c for c in rule.conditions if c.aggregation}
    failed_lines = []
    for r in failed_conditions:
        line = f"- {r.attribute} {r.operator} {r.threshold}: actual was {r.actual_value} → FAIL"
        cond = cond_by_key.get(r.attribute)
        if cond and cond.group_by:
            line += f"  [group_by={cond.group_by}, group_mode={cond.group_mode or 'any'}: actual={r.actual_value} is the {('max' if cond.operator not in ('<', '<=') else 'min')} across all {cond.group_by} groups]"
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

    derived = _format_derived_conditions(rule, scenario_type, aggregates)
    if derived:
        repair_guidance_section = (
            "--- REPAIR GUIDANCE ---\n"
            "The following section explains exactly how to fix the failing aggregate(s):\n"
            f"{derived}\n"
            "--- END REPAIR GUIDANCE ---"
        )
    else:
        repair_guidance_section = ""

    prompt = BEHAVIORAL_CORRECT_PROMPT.format(
        schema_context=format_attributes_for_prompt(show_aliases=False),
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
    result = [
        Transaction(id=t["id"], tag=t.get("tag", scenario_type), attributes=_canonicalize_attrs(t["attributes"], rule.high_risk_countries))
        for t in data
    ]
    log.info("corrector | returned %d transactions", len(result))
    return result
