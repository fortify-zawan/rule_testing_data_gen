"""Generate edge-case test suggestions for a parsed AML rule.

The LLM writes human-friendly text (title, description, focus_conditions, suggested_intent).
All logic — which patterns apply, expected_outcome, scenario_type — is determined in Python.
"""
import json
from domain.models import Rule, RuleCondition, TestSuggestion
from llm.llm_wrapper import call_llm_json

# Maps pattern_type → (scenario_type, expected_outcome)
PATTERN_OUTCOMES = {
    "typical_trigger":       ("risky",   "FIRE"),
    "boundary_just_over":    ("risky",   "FIRE"),
    "boundary_at_threshold": ("genuine", "NOT_FIRE"),
    "near_miss_one_clause":  ("genuine", "NOT_FIRE"),
    "or_branch_trigger":     ("risky",   "FIRE"),
    "or_branch_all_fail":    ("genuine", "NOT_FIRE"),
    "window_edge_inside":    ("risky",   "FIRE"),
    "filter_empty":          ("genuine", "NOT_FIRE"),
}

PATTERN_DESCRIPTIONS = {
    "typical_trigger": (
        "A clear risky case with comfortable margin above every threshold. "
        "Tests the core detection path with a realistic mixed-activity sequence."
    ),
    "boundary_just_over": (
        "Aggregate sits barely above the threshold — the minimum activity needed to fire. "
        "Tests whether the rule correctly detects marginal breaches."
    ),
    "boundary_at_threshold": (
        "Aggregate sits exactly at the threshold value. "
        "Tests operator strictness (> vs >=) and boundary handling."
    ),
    "near_miss_one_clause": (
        "All AND conditions pass except one key clause, which just falls short. "
        "Confirms AND logic is correctly enforced and the rule does not fire on partial matches."
    ),
    "or_branch_trigger": (
        "Only one OR branch satisfies its conditions; the other branches do not. "
        "Tests that a single satisfied OR branch is sufficient to trigger the rule."
    ),
    "or_branch_all_fail": (
        "Every OR branch falls short of its threshold. "
        "Confirms the rule correctly does not fire when no branch is satisfied."
    ),
    "window_edge_inside": (
        "The rule-relevant transactions fall just inside the time window boundary. "
        "Tests that window filtering correctly includes recent activity."
    ),
    "filter_empty": (
        "No transactions match the filter attribute/value, so the filtered aggregate is zero or null. "
        "Tests how the rule handles an empty subset — important for percentage and ratio aggregations."
    ),
}


def _applicable_patterns(rule: Rule) -> list[str]:
    """Determine which coverage patterns apply to this rule. Always returns at least 1."""
    patterns = ["typical_trigger"]  # always applicable

    has_numeric = any(isinstance(c.value, (int, float)) for c in rule.conditions)
    multi_condition = len(rule.conditions) >= 2
    has_and = multi_condition and any(
        c.logical_connector == "AND" for c in rule.conditions[:-1]
    )
    has_or = any(c.logical_connector == "OR" for c in rule.conditions[:-1])
    has_window = any(c.window for c in rule.conditions)
    has_filter = any(c.filter_attribute for c in rule.conditions)

    if has_numeric:
        patterns += ["boundary_just_over", "boundary_at_threshold"]
    if has_and and multi_condition:
        patterns.append("near_miss_one_clause")
    if has_or:
        patterns += ["or_branch_trigger", "or_branch_all_fail"]
    if has_window:
        patterns.append("window_edge_inside")
    if has_filter:
        patterns.append("filter_empty")

    return patterns[:8]  # cap at 8


def _format_conditions(rule: Rule) -> str:
    parts = []
    for i, c in enumerate(rule.conditions):
        line = f"  Condition {i+1}: {c.attribute} {c.operator} {c.value}"
        if c.aggregation:
            line += f"  [aggregation: {c.aggregation}]"
        if c.window:
            line += f"  [window: {c.window}]"
        if c.filter_attribute:
            line += f"  [filter: {c.filter_attribute} {c.filter_operator} {c.filter_value}]"
        if i < len(rule.conditions) - 1:
            line += f"  → {c.logical_connector}"
        parts.append(line)
    return "\n".join(parts)


SYSTEM = """You are a test engineer generating AML rule test suggestions.
Output ONLY valid JSON — no explanation, no markdown fences."""

SUGGESTION_PROMPT = """You are a test engineer analysing an AML detection rule as a software function to be tested.

Rule expression: {raw_expression}
Rule type: {rule_type} (stateless = evaluated per transaction; behavioral = aggregates across a sequence)
Conditions:
{conditions_detail}

Generate test suggestions for each of the following coverage patterns.
For each pattern, produce exactly the fields listed below.

PATTERNS TO COVER:
{patterns_list}

FIELD RULES:
- title: Short label (max 10 words). Name the pattern and what makes it distinctive.
- description: 2-3 sentences. What exactly does this test, and why does it matter for this specific rule?
- focus_conditions: List the condition summaries (e.g. "send_amount sum > 10000") that this pattern specifically exercises.
- suggested_intent: Describe the account behaviour in plain English — what the account looks like, what mix of transactions it has, and what narrative leads to the outcome. Do NOT hardcode threshold numbers or country names. The intent will be fed to a transaction sequence generator.
  BAD: "avg to Iran = $510, 3 transactions"
  GOOD: "Account makes regular small domestic transfers with a cluster of higher-value transfers to one high-risk destination, pushing the average just over the rule threshold."

Output a JSON array — one object per pattern:
[
  {{
    "pattern_type": "<pattern_type from the list above>",
    "title": "...",
    "description": "...",
    "focus_conditions": ["...", "..."],
    "suggested_intent": "..."
  }},
  ...
]"""


def generate_suggestions(rule: Rule) -> list[TestSuggestion]:
    """Generate 6–8 edge-case test suggestions for the given rule."""
    applicable = _applicable_patterns(rule)

    patterns_list = "\n".join(
        f"- {p}: {PATTERN_DESCRIPTIONS[p]}" for p in applicable
    )

    prompt = SUGGESTION_PROMPT.format(
        raw_expression=rule.raw_expression,
        rule_type=rule.rule_type,
        conditions_detail=_format_conditions(rule),
        patterns_list=patterns_list,
    )

    raw = call_llm_json(prompt, system=SYSTEM)

    # Validate and build TestSuggestion objects — pattern_type controls scenario_type and expected_outcome
    suggestions = []
    seen_patterns = set()
    for i, item in enumerate(raw):
        pt = item.get("pattern_type", "")
        if pt not in PATTERN_OUTCOMES:
            continue  # discard unknown patterns the LLM invented
        if pt in seen_patterns:
            continue  # deduplicate
        seen_patterns.add(pt)

        scenario_type, expected_outcome = PATTERN_OUTCOMES[pt]
        suggestions.append(TestSuggestion(
            id=f"s-{i+1:03d}",
            scenario_type=scenario_type,
            pattern_type=pt,
            title=item.get("title", pt.replace("_", " ").title()),
            description=item.get("description", ""),
            focus_conditions=item.get("focus_conditions", []),
            suggested_intent=item.get("suggested_intent", ""),
            expected_outcome=expected_outcome,
        ))

    return suggestions
