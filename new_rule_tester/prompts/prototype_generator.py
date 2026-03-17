"""Prompt strings for llm/prototype_generator.py."""

SYSTEM = """You are generating minimal transaction examples for AML rule testing.
Output ONLY valid JSON — no explanation, no markdown fences."""

PROMPT_TEMPLATE = """Generate exactly one RISKY and one GENUINE transaction example for this AML rule.

Rule: {raw_expression}
Relevant attributes: {attributes}
High-risk countries (if applicable): {high_risk_countries}

{feedback_section}

Rules:
- Only populate the relevant attributes listed above.
- The RISKY example must satisfy ALL rule conditions (it would trigger the rule).
- The GENUINE example must NOT satisfy the complete set of conditions (it would NOT trigger).
- Use realistic, specific values (not placeholders).

Output this exact JSON:
{{
  "risky": {{"attr1": value1, "attr2": value2, ...}},
  "genuine": {{"attr1": value1, "attr2": value2, ...}}
}}"""

SINGLE_PROTO_TEMPLATE = """Generate exactly one {scenario_upper} transaction example for this AML rule.

Rule: {raw_expression}
Relevant attributes: {attributes}
High-risk countries (if applicable): {high_risk_countries}

{scenario_instruction}

{feedback_section}

Rules:
- Only populate the relevant attributes listed above.
- Use realistic, specific values (not placeholders).

Output this exact JSON:
{{"{scenario_type}": {{"attr1": value1, "attr2": value2, ...}}}}"""

CONFLICT_SECTION_TEMPLATE = """
--- SECTION F — Feedback conflict check ---
You have user feedback above.
Ask yourself: given what this rule requires for a {scenario_type} outcome, does any
feedback instruction ask for attribute values that would push this prototype away from
that outcome?

  RISKY   → conflict = feedback makes the prototype less likely to trigger the rule
  GENUINE → conflict = feedback makes the prototype more likely to trigger the rule

Flag only clear directional conflicts. Ignore style or realism preferences.

IMPORTANT — still honor all feedback when generating the prototype. Just flag conflicts
so the user can be informed.

Required output format when feedback is present:
{{{{"{scenario_type}": {{{{"attr1": val, ...}}}}, "feedback_conflicts": [
  {{{{
    "feedback_instruction": "<conflicting instruction, quoted verbatim>",
    "conflicting_condition": "<rule condition affected, e.g. 'receive_amount > 500'>",
    "explanation": "<one sentence: why this makes the {scenario_type} validation fail>"
  }}}}
]}}}}
Use [] for feedback_conflicts if there are none."""
