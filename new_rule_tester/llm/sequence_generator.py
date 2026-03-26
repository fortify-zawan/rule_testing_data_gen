"""Generate full transaction sequences for stateless and behavioral rules."""
import json

from config.schema_loader import (
    canonical_name,
    format_attributes_for_prompt,
    normalize_country_values,
)
from domain.models import Prototype, Rule, Transaction
from llm.llm_wrapper import call_llm_json
from prompts.sequence_generator import (
    BEHAVIORAL_PROMPT,
    CONFLICT_SECTION_TEMPLATE,
    STATELESS_PROMPT,
    SYSTEM,
)


def _canonicalize_attrs(attrs: dict, high_risk_countries: list[str] | None = None) -> dict:
    """Resolve alias keys to canonical names and normalize ISO country codes to full names."""
    renamed = {canonical_name(k): v for k, v in attrs.items()}
    return normalize_country_values(renamed, high_risk_countries)


def _rule_allowed_attrs(rule: Rule) -> set[str]:
    """Return the full set of attribute names the LLM needs to generate for this rule.

    Includes relevant_attributes declared by the parser, all filter attributes from
    every condition, derived-attribute filter, and computed-attr filter (so e.g.
    transaction_status is always present even when the parser omits it from
    relevant_attributes), plus fixed display columns that always appear in the UI table.
    """
    attrs = set(rule.relevant_attributes)

    # CA names are not raw fields — exclude them when walking CA filters
    ca_names = {ca.name for ca in rule.computed_attrs}

    for ca in rule.computed_attrs:
        for fc in (ca.filters or []):
            if fc.attribute and fc.attribute not in ca_names:
                attrs.add(fc.attribute)
            if fc.value_field and fc.value_field not in ca_names:
                attrs.add(fc.value_field)
        if ca.group_by and ca.group_by not in ca_names:
            attrs.add(ca.group_by)
        if ca.link_attribute:
            attrs.update(la for la in ca.link_attribute if la not in ca_names)

    for cond in rule.conditions:
        for fc in (cond.filters or []):
            if fc.attribute:
                attrs.add(fc.attribute)
        for da in (cond.derived_attributes or []):
            for fc in (da.filters or []):
                if fc.attribute:
                    attrs.add(fc.attribute)

    attrs |= {"created_at", "send_amount", "send_currency"}
    # transaction_id is the row identity (t.id), not an attribute to generate
    attrs.discard("transaction_id")
    return attrs



# ─── Stateless ────────────────────────────────────────────────────────────────

def generate_stateless_sequence(
    rule: Rule,
    risky_proto: Prototype,
    genuine_proto: Prototype,
    n_risky: int,
    n_genuine: int,
) -> list[Transaction]:
    prompt = STATELESS_PROMPT.format(
        schema_context=format_attributes_for_prompt(show_aliases=False),
        raw_expression=rule.raw_expression,
        attributes=", ".join(rule.relevant_attributes),
        risky_proto=json.dumps(risky_proto.attributes),
        genuine_proto=json.dumps(genuine_proto.attributes),
        n_risky=n_risky,
        n_genuine=n_genuine,
        n_total=n_risky + n_genuine,
    )

    data = call_llm_json(prompt, system=SYSTEM)
    return [
        Transaction(id=t["id"], tag=t["tag"], attributes=_canonicalize_attrs(t["attributes"], rule.high_risk_countries))
        for t in data
    ]


# ─── Behavioral ───────────────────────────────────────────────────────────────

def generate_behavioral_sequence(
    rule: Rule,
    scenario_type: str,
    intent: str = "",
    feedback: str = "",
    previous_sequence_json: str = "",
    aggregate_feedback: str = "",
    feedback_history: list[str] | None = None,
) -> tuple[list[Transaction], list[dict]]:
    intent_section = f"User intent: {intent}" if intent else "No specific intent provided — generate based on rule alone."

    # Combine all user instructions (prior rounds + current round) under one strong block
    all_feedback = (list(feedback_history) if feedback_history else []) + ([feedback] if feedback else [])
    if all_feedback:
        instruction_lines = "\n".join(f"  - {f}" for f in all_feedback)
        feedback_history_section = (
            "--- USER INSTRUCTIONS (all must be respected) ---\n"
            f"{instruction_lines}\n"
            "--- END USER INSTRUCTIONS ---"
        )
    else:
        feedback_history_section = ""

    # Previous aggregates context (informational only, not user instructions)
    feedback_parts = []
    if previous_sequence_json:
        feedback_parts.append(f"Previous sequence aggregates:\n{previous_sequence_json}")
    if aggregate_feedback:
        feedback_parts.append(f"What needs to change:\n{aggregate_feedback}")
    feedback_section = "\n\n".join(feedback_parts)

    conflict_section = ""
    if all_feedback:
        conflict_section = CONFLICT_SECTION_TEMPLATE.format(scenario_type=scenario_type)

    _allowed = _rule_allowed_attrs(rule)
    prompt = BEHAVIORAL_PROMPT.format(
        schema_context=format_attributes_for_prompt(show_aliases=False, allowed_attrs=_allowed),
        raw_expression=rule.raw_expression,
        attributes=", ".join(sorted(_allowed - {"created_at"})),
        high_risk_countries=", ".join(rule.high_risk_countries) if rule.high_risk_countries else "none specified",
        scenario_type=scenario_type,
        intent_section=intent_section,
        feedback_history_section=feedback_history_section,
        feedback_section=feedback_section,
    ) + conflict_section

    data = call_llm_json(prompt, system=SYSTEM)

    if isinstance(data, list):
        raw_txns, conflict_dicts = data, []
    else:
        raw_txns = data.get("transactions", [])
        conflict_dicts = data.get("feedback_conflicts", [])

    transactions = []
    for t in raw_txns:
        if not isinstance(t, dict):
            continue
        txn_id = t.get("id") or t.get("transaction_id")
        if not txn_id:
            continue
        raw_attrs = t.get("attributes") or {k: v for k, v in t.items() if k not in ("id", "transaction_id", "tag")}
        transactions.append(Transaction(
            id=txn_id,
            tag=t.get("tag", scenario_type),
            attributes={
                k: v for k, v in _canonicalize_attrs(raw_attrs, rule.high_risk_countries).items()
                if k in _allowed
            },
        ))
    return transactions, conflict_dicts
