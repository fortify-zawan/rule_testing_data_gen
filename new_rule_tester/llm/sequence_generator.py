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

    prompt = BEHAVIORAL_PROMPT.format(
        schema_context=format_attributes_for_prompt(show_aliases=False),
        raw_expression=rule.raw_expression,
        attributes=", ".join(rule.relevant_attributes),
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

    transactions = [
        Transaction(id=t["id"], tag=t.get("tag", scenario_type), attributes=_canonicalize_attrs(t["attributes"], rule.high_risk_countries))
        for t in raw_txns
    ]
    return transactions, conflict_dicts
