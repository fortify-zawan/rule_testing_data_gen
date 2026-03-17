"""Generate risky + genuine prototype examples for stateless rules."""
from domain.models import Prototype, Rule
from llm.llm_wrapper import call_llm_json
from prompts.prototype_generator import (
    CONFLICT_SECTION_TEMPLATE,
    PROMPT_TEMPLATE,
    SINGLE_PROTO_TEMPLATE,
    SYSTEM,
)


def generate_prototypes(
    rule: Rule,
    current_risky: dict = None,
    current_genuine: dict = None,
    feedback: str = "",
) -> tuple[Prototype, Prototype]:
    """Generate or regenerate prototype pair. Returns (risky_prototype, genuine_prototype)."""

    feedback_section = ""
    if feedback and current_risky:
        feedback_section = f"""Previous examples:
Risky: {current_risky}
Genuine: {current_genuine}

User feedback: {feedback}

Regenerate both examples incorporating this feedback while keeping the rule logic correct."""

    prompt = PROMPT_TEMPLATE.format(
        raw_expression=rule.raw_expression,
        attributes=", ".join(rule.relevant_attributes),
        high_risk_countries=", ".join(rule.high_risk_countries) if rule.high_risk_countries else "none specified",
        feedback_section=feedback_section,
    )

    data = call_llm_json(prompt, system=SYSTEM)

    risky = Prototype(scenario_type="risky", attributes=data["risky"])
    genuine = Prototype(scenario_type="genuine", attributes=data["genuine"])

    if feedback:
        risky.user_feedback_history = [feedback]
        genuine.user_feedback_history = [feedback]

    return risky, genuine


def generate_single_prototype(
    rule: Rule,
    scenario_type: str,
    feedback_history: list[str] = None,
    current_attrs: dict = None,
) -> tuple[Prototype, list[dict]]:
    """Generate or regenerate a single prototype (risky OR genuine) with accumulated feedback.

    feedback_history: all prior feedback strings accumulated across iterations.
    current_attrs: the prototype attributes from the last iteration (required when feedback exists).
    """
    feedback_history = feedback_history or []

    if scenario_type == "risky":
        scenario_instruction = (
            "The RISKY example MUST satisfy ALL rule conditions — it would trigger the rule."
        )
    else:
        scenario_instruction = (
            "The GENUINE example must NOT satisfy the complete set of conditions — it would NOT trigger the rule."
        )

    feedback_section = ""
    if feedback_history and current_attrs:
        lines = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(feedback_history))
        feedback_section = (
            f"Previous example:\n{scenario_type}: {current_attrs}\n\n"
            f"Accumulated feedback (apply ALL of these):\n{lines}\n\n"
            f"Regenerate incorporating all feedback while keeping the rule logic correct."
        )
    elif feedback_history:
        # Initial generation with intent guidance (no previous example yet)
        lines = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(feedback_history))
        feedback_section = f"Guidance for generation (apply all of these):\n{lines}"

    conflict_section = ""
    if feedback_history:
        conflict_section = CONFLICT_SECTION_TEMPLATE.format(scenario_type=scenario_type)

    prompt = SINGLE_PROTO_TEMPLATE.format(
        scenario_upper=scenario_type.upper(),
        raw_expression=rule.raw_expression,
        attributes=", ".join(rule.relevant_attributes),
        high_risk_countries=(
            ", ".join(rule.high_risk_countries) if rule.high_risk_countries else "none specified"
        ),
        scenario_instruction=scenario_instruction,
        scenario_type=scenario_type,
        feedback_section=feedback_section,
    ) + conflict_section

    data = call_llm_json(prompt, system=SYSTEM)
    conflict_dicts = data.pop("feedback_conflicts", []) if isinstance(data, dict) else []
    proto = Prototype(scenario_type=scenario_type, attributes=data[scenario_type])
    proto.user_feedback_history = list(feedback_history)
    return proto, conflict_dicts
