"""Internal correction of failed transactions (stateless) or sequences (behavioral)."""
import json
from llm.llm_wrapper import call_llm_json
from domain.models import Rule, Transaction, ConditionResult
from config.schema_loader import format_attributes_for_prompt, canonical_name, normalize_country_values
from logging_config import get_logger

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
    from datetime import timedelta
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
                parts.append(f"PERIOD LAYOUT (both windows anchored independently at latest_date):")
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
                parts.append(f"PERIOD LAYOUT (non-overlapping, anchored at latest_date):")
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
                    parts.append(f"CURRENT COMPONENT VALUES (from last validation):")
                    parts.append(f"  {da0.name} (numerator)   = {da0_current}")
                    parts.append(f"  {da1.name} (denominator) = {da1_current}")
                    parts.append(f"  ratio = {current_ratio}")
                    parts.append("")

                    if scenario_type == "risky":
                        required_da0 = float(cond.value) * float(da1_current)
                        shortfall = required_da0 - float(da0_current)
                        parts.append(f"SHORTFALL ANALYSIS (risky must fire):")
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
                                parts.append(f"  → Do NOT add NEW filter-matching transactions to the PRIOR period — that raises the denominator and worsens the ratio.")
                                parts.append(f"  → Reducing the prior period alone is NOT sufficient — you must have filter-matching ({flt0}) transactions in the recent period for all related conditions to pass.")
                            elif da0.aggregation == "count":
                                needed_count = int(required_da0) + 1 - int(da0_current)
                                parts.append(f"  MANDATORY: You MUST add filter-matching ({flt0}) transactions to the {d0_label} period — the numerator ({da0.name}) must be > 0.")
                                parts.append(f"  → Primary repair: Add at least {needed_count} more filter-matching ({flt0}) transactions dated within the {d0_label} period.")
                                parts.append(f"  → Optional lever: You may also reduce or remove EXISTING filter-matching ({flt1}) transactions in the PRIOR {d1}d period — fewer prior-period matches lowers the denominator.")
                                parts.append(f"  → Do NOT add NEW filter-matching transactions to the PRIOR period — that raises the denominator and worsens the ratio.")
                                parts.append(f"  → Reducing the prior period alone is NOT sufficient — you must have filter-matching ({flt0}) transactions in the recent period for all related conditions to pass.")
                    else:
                        allowed_da0 = float(cond.value) * float(da1_current)
                        excess = float(da0_current) - allowed_da0
                        parts.append(f"EXCESS ANALYSIS (genuine must not fire):")
                        parts.append(f"  Allowed: {da0.name} ≤ {cond.value} × {da1_current} = {allowed_da0:.2f}")
                        parts.append(f"  Current: {da0.name} = {da0_current}  (excess = {excess:.2f})")
                        if window_mode == "independent":
                            parts.append(f"  → Reduce filter-matching ({flt0}) transactions within the {da0.window} window by at least {excess:.2f}.")
                        else:
                            parts.append(f"  → Move or reduce filter-matching transactions in the RECENT {d0}d period by at least {excess:.2f}.")
            else:
                # No current aggregates — give general guidance
                if scenario_type == "risky":
                    parts.append(f"REPAIR GUIDANCE (risky must fire):")
                    parts.append(f"  {da0.name} must be > {cond.value} × {da1.name}")
                    if window_mode == "independent":
                        parts.append(f"  → Add or increase filter-matching ({flt0}) transactions within the {da0.window} window.")
                    else:
                        parts.append(f"  MANDATORY: You MUST add filter-matching ({flt0}) transactions to the RECENT {d0}d period — the numerator must be > 0.")
                        parts.append(f"  → Primary repair: Add or increase filter-matching ({flt0}) transactions in the RECENT {d0}d period.")
                        parts.append(f"  → Optional lever: You may also reduce or remove EXISTING filter-matching ({flt1}) transactions in the PRIOR {d1}d period — this lowers the denominator and improves the ratio.")
                        parts.append(f"  → Do NOT add NEW filter-matching transactions to the PRIOR period — that raises the denominator and worsens the ratio.")
                        parts.append(f"  → Reducing the prior period alone is NOT sufficient — you must have transactions in the recent period for all related conditions to pass.")
                else:
                    parts.append(f"REPAIR GUIDANCE (genuine must not fire):")
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

SYSTEM = """You are correcting AML test data so that transactions satisfy rule conditions exactly.
Output ONLY valid JSON — no explanation, no markdown fences."""

# ─── Stateless correction (fix one transaction at a time) ────────────────────

STATELESS_CORRECT_PROMPT = """A transaction failed AML rule validation. Correct its attribute values.

Rule: {raw_expression}
Transaction tag: {tag} (risky = should trigger rule, genuine = should NOT trigger rule)
Current attribute values: {attributes}

Failed conditions:
{failed_conditions}

Anchor prototype (the intended character of this transaction type):
{prototype}

Instructions:
- Only change the attributes that caused the failure.
- Keep all other attributes as close to the current values as possible.
- The corrected transaction must {expectation}.

Output ONLY a JSON dict of the corrected attributes (same keys as current attributes):
{{"attr1": value, "attr2": value, ...}}"""


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

BEHAVIORAL_CORRECT_PROMPT = """A behavioral AML rule test sequence failed validation. Repair it.

--- TASK CONTEXT ---
The following inputs define the sequence you are repairing:

SCHEMA — canonical attribute names and allowed values you must use:
{schema_context}

RULE — the AML rule this sequence is being tested against:
{raw_expression}

SEQUENCE TYPE: {scenario_type}
  - risky   → after repair, the sequence MUST cause the rule to FIRE
  - genuine → after repair, the sequence MUST cause the rule to NOT FIRE

HIGH-RISK COUNTRIES — use these exact strings when setting country attributes:
{high_risk_countries}

USER INTENT: {intent}
--- END TASK CONTEXT ---

--- VALIDATION RESULTS ---
These are the aggregate values computed from the current sequence and the conditions that failed:

Computed aggregates:
{aggregates}

Failed conditions (what must change):
{failed_conditions}
--- END VALIDATION RESULTS ---

{repair_guidance_section}
{feedback_history_section}

--- SECTION A — Repair planning (think before changing anything) ---
Before modifying any transaction:
1. Read the VALIDATION RESULTS above. Identify which transactions are MOTIF transactions
   (those matching the rule's filter) and which are BACKGROUND.
2. Identify the LAST transaction in the sequence (most recent date) — this is the anchor.
   All time windows are measured backwards from that date (latest_date). A "recent 7d" window
   means transactions dated within 7 days BEFORE the last transaction's date.
3. Read the REPAIR GUIDANCE above (if present). Use the shortfall arithmetic to determine
   exactly how much needs to change and in which time window.
4. Plan the minimum changes needed — adjust amounts or dates of existing motif transactions
   before adding new ones. Only add new motif transactions if the shortfall cannot be covered
   by adjusting what already exists.
5. Verify in your head that the planned changes satisfy the condition(s) after repair.
   Do not proceed until your plan adds up.

--- SECTION B — Preservation constraint (mandatory) ---
The background transactions in the existing sequence represent realistic account noise and
must be preserved. Do NOT replace, remove, or redate background transactions (those whose
attributes do not match the rule's filter attribute/value and do not directly affect the
failing aggregate).
Only adjust MOTIF transactions — those that match the rule's filter and directly contribute
to the failing aggregate. If the shortfall cannot be fixed by adjusting existing motif
transactions, you may add 1-2 new motif transactions or adjust their amounts.
The account narrative, background activity pattern, and temporal spread must remain intact.
Never modify background transactions regardless of period.

--- SECTION C — Customer archetype ---
Maintain a consistent customer profile across the repaired sequence. Transaction sizes,
frequency, destinations, and timing should remain consistent with the account type
established in the original sequence.

--- SECTION D — Value variance ---
Motif amounts should still vary naturally after correction — do not set all motif
transactions to the same amount just to hit the aggregate target. The aggregate must
be met in total, but individual values should look organic.

--- SECTION E — Temporal realism ---
Preserve the existing date structure. Do not compress transactions into a shorter window
or make the spacing uniform. If adding new motif transactions, slot them into gaps in
the existing timeline.

--- Hard requirements ---
- For RISKY: all aggregate conditions must be satisfied after repair.
- For GENUINE: the complete set of conditions must NOT be satisfied after repair.
- The LAST transaction in the sequence (most recent date) MUST be a background transaction.
  This anchors the reference date used for all window calculations to a neutral account event.
- IMPORTANT — use ONLY the canonical attribute names from the schema above as JSON keys.

Output the full repaired transaction list (same JSON format, all transactions), ordered by date:
[
  {{
    "id": "t-001",
    "tag": "{scenario_type}",
    "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, ...}}
  }},
  ...
]"""


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
    failed_desc = "\n".join(
        f"- {r.attribute} {r.operator} {r.threshold}: actual was {r.actual_value} → FAIL"
        for r in failed_conditions
    )
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
