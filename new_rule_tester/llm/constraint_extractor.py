"""Extract structured constraints from user feedback text.

The LLM reads the user's free-text feedback and extracts explicit constraints
into hard (MUST/MUST NOT) and soft (SHOULD) entries. These are added to the
ConstraintLedger so they persist across all future regeneration rounds.

The user confirms the extracted entries before they are committed, so any
LLM extraction errors can be caught and discarded.
"""
from domain.models import Rule, ConstraintLedger, ConstraintEntry
from llm.llm_wrapper import call_llm_json

SYSTEM = """You extract structured constraints from user feedback on AML test data.
Output ONLY valid JSON — no explanation, no markdown fences."""

EXTRACT_PROMPT = """A user gave feedback on a generated AML transaction sequence.
Extract every explicit constraint from their feedback as structured entries.

Rule: {raw_expression}
Scenario type: {scenario_type} (risky = should trigger rule, genuine = should NOT trigger)

Existing constraints already in the ledger (do NOT re-add these):
{existing_constraints}

User feedback:
"{feedback}"

For each distinct constraint in the feedback:
- constraint_type: "hard" if non-negotiable (words like must, must not, never, always, remove, do not include)
                   "soft" if a preference (words like prefer, should, try to, ideally, more, less)
- text: a clear, concise statement of the constraint in plain English
        Write it as a policy rule, not a command.
        GOOD: "Iran must not appear in any transaction's receive_country_code"
        GOOD: "At most 2 distinct destination countries across the sequence"
        GOOD: "Transaction amounts should vary between $50 and $500"
        BAD: "Remove Iran" (too vague — rephrase as a policy)
        BAD: "Make it better" (not a concrete constraint — skip it)

Rules:
- Only extract constraints that are concrete and enforceable.
- Do NOT re-extract anything already in the existing constraints list.
- If no new concrete constraints can be found, return an empty array.

Output JSON array:
[
  {{"constraint_type": "hard" | "soft", "text": "..."}},
  ...
]"""


def extract_constraints(
    feedback: str,
    rule: Rule,
    scenario_type: str,
    existing_ledger: ConstraintLedger,
) -> list[ConstraintEntry]:
    """Extract new constraint entries from user feedback.

    Returns a list of ConstraintEntry objects. Does NOT modify the ledger —
    the caller decides whether to commit them (after user confirmation).
    """
    existing_text = "\n".join(
        f"  [{e.constraint_type.upper()}] {e.text}"
        for e in existing_ledger.entries
    ) or "None yet."

    prompt = EXTRACT_PROMPT.format(
        raw_expression=rule.raw_expression,
        scenario_type=scenario_type,
        existing_constraints=existing_text,
        feedback=feedback,
    )

    raw = call_llm_json(prompt, system=SYSTEM)

    entries = []
    for item in raw:
        ct = item.get("constraint_type", "").lower()
        text = item.get("text", "").strip()
        if ct in ("hard", "soft") and text:
            entries.append(ConstraintEntry(constraint_type=ct, text=text))

    return entries
