"""Generate edge-case test suggestions for a parsed AML rule.

The LLM writes human-friendly text (title, description, focus_conditions, suggested_intent).
All logic — which patterns apply, expected_outcome, scenario_type — is determined in Python.
"""
from domain.models import FilterClause, Rule, TestSuggestion
from llm.llm_wrapper import call_llm_json
from prompts.suggestion_generator import SUGGESTION_PROMPT, SYSTEM

# Maps pattern_type → (scenario_type, expected_outcome)
PATTERN_OUTCOMES = {
    "typical_trigger":       ("risky",   "FIRE"),
    "volume_structuring":    ("risky",   "FIRE"),
    "boundary_just_over":    ("risky",   "FIRE"),
    "boundary_at_threshold": ("genuine", "NOT_FIRE"),
    "near_miss_one_clause":  ("genuine", "NOT_FIRE"),
    "or_branch_trigger":     ("risky",   "FIRE"),
    "or_branch_all_fail":    ("genuine", "NOT_FIRE"),
    "window_edge_inside":    ("risky",   "FIRE"),
    "window_edge_outside":   ("genuine", "NOT_FIRE"),
    "filter_partial_match":  ("genuine", "NOT_FIRE"),
    "group_isolation":       ("risky",   "FIRE"),
    "filter_empty":          ("genuine", "NOT_FIRE"),
}

PATTERN_DESCRIPTIONS = {
    "typical_trigger": (
        "A clear risky case with comfortable margin above every threshold. "
        "Tests the core detection path with a realistic mixed-activity sequence."
    ),
    "volume_structuring": (
        "Many individually small transactions that together push the aggregate above the threshold. "
        "Tests whether the rule catches distributed activity spread across time rather than a few large transactions."
    ),
    "boundary_just_over": (
        "Aggregate sits barely above the threshold — the minimum activity needed to fire. "
        "Tests whether the rule correctly detects marginal breaches."
    ),
    "boundary_at_threshold": (
        "Aggregate sits exactly at the threshold value. "
        "Tests operator strictness (> vs >=) and boundary handling — the rule must NOT fire."
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
        "Tests that window filtering correctly includes activity at the edge."
    ),
    "window_edge_outside": (
        "The rule-relevant transactions fall just outside the time window boundary. "
        "Tests that window filtering correctly excludes stale activity — the rule must NOT fire."
    ),
    "filter_partial_match": (
        "Transactions match some but not all filter conditions on a computed attribute. "
        "Confirms that multi-filter AND logic works correctly — partial filter matches must not count."
    ),
    "group_isolation": (
        "Only one group (by the group_by attribute) accumulates enough to cross the threshold; "
        "all other groups stay well below. Tests that group-level aggregation fires correctly on a single hot group."
    ),
    "filter_empty": (
        "No transactions match the filter conditions, so the filtered aggregate is zero. "
        "Tests how the rule handles an empty subset — important for sum, count, and ratio aggregations."
    ),
}


def _format_filter_clauses(filters: list[FilterClause]) -> str:
    parts = []
    for i, fc in enumerate(filters):
        if fc.value_field:
            clause = f"{fc.attribute} {fc.operator} {fc.value_field} (cross-field)"
        else:
            clause = f"{fc.attribute} {fc.operator} {fc.value}"
        if i < len(filters) - 1:
            clause += f" {fc.connector}"
        parts.append(clause)
    return " | ".join(parts)


def _format_rule_anatomy(rule: Rule) -> str:
    parts = []

    if rule.computed_attrs:
        parts.append("Computed Attributes (pre-computed before condition evaluation, injected into each transaction):")
        for ca in rule.computed_attrs:
            line = f"  {ca.name} = {ca.aggregation}({ca.attribute})"
            if ca.window:
                line += f" within {ca.window}"
            if ca.group_by:
                line += f" grouped by {ca.group_by}"
            if ca.filters:
                line += f"\n    filters: {_format_filter_clauses(ca.filters)}"
            parts.append(line)
        parts.append("")

    parts.append("Conditions:")
    for i, c in enumerate(rule.conditions):
        attr = c.computed_attr_name or c.attribute
        line = f"  Condition {i+1}: {attr} {c.operator} {c.value}"
        if c.aggregation:
            line += f" [{c.aggregation}]"
        if c.window:
            line += f" [window: {c.window}]"
        if c.filters:
            line += f"\n    filters: {_format_filter_clauses(c.filters)}"
        if c.group_by:
            line += f" [group_by: {c.group_by}]"
        if i < len(rule.conditions) - 1:
            line += f"  → {c.logical_connector}"
        parts.append(line)

    return "\n".join(parts)


def _applicable_patterns(rule: Rule) -> list[str]:
    """Determine which coverage patterns apply to this rule, inspecting both conditions and CAs."""
    patterns = ["typical_trigger", "volume_structuring"]

    has_numeric = any(isinstance(c.value, (int, float)) for c in rule.conditions)

    has_window = (
        any(c.window for c in rule.conditions)
        or any(ca.window for ca in rule.computed_attrs)
    )

    has_filter = (
        any(c.filters for c in rule.conditions)
        or any(ca.filters for ca in rule.computed_attrs)
    )

    has_multi_filter = (
        any(ca.filters and len(ca.filters) >= 2 for ca in rule.computed_attrs)
        or any(c.filters and len(c.filters) >= 2 for c in rule.conditions)
    )

    has_group_by = (
        any(c.group_by for c in rule.conditions)
        or any(ca.group_by for ca in rule.computed_attrs)
    )

    multi_condition = len(rule.conditions) >= 2
    has_and = multi_condition and any(
        c.logical_connector == "AND" for c in rule.conditions[:-1]
    )
    has_or = any(c.logical_connector == "OR" for c in rule.conditions[:-1])
    has_or_groups = len({c.condition_group for c in rule.conditions}) > 1

    if has_numeric:
        patterns += ["boundary_just_over", "boundary_at_threshold"]
    if has_and and multi_condition:
        patterns.append("near_miss_one_clause")
    if has_or or has_or_groups:
        patterns += ["or_branch_trigger", "or_branch_all_fail"]
    if has_window:
        patterns += ["window_edge_inside", "window_edge_outside"]
    if has_multi_filter:
        patterns.append("filter_partial_match")
    if has_filter and not has_multi_filter:
        patterns.append("filter_empty")
    elif has_filter:
        patterns.append("filter_empty")
    if has_group_by:
        patterns.append("group_isolation")

    # Deduplicate preserving order, cap at 10
    seen: set[str] = set()
    result = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result[:10]


def generate_suggestions(rule: Rule) -> list[TestSuggestion]:
    """Generate edge-case test suggestions for the given rule."""
    applicable = _applicable_patterns(rule)

    patterns_list = "\n".join(
        f"- {p}: {PATTERN_DESCRIPTIONS[p]}" for p in applicable
    )

    prompt = SUGGESTION_PROMPT.format(
        raw_expression=rule.raw_expression,
        rule_type=rule.rule_type,
        rule_anatomy=_format_rule_anatomy(rule),
        patterns_list=patterns_list,
    )

    raw = call_llm_json(prompt, system=SYSTEM)

    suggestions = []
    seen_patterns: set[str] = set()
    for i, item in enumerate(raw):
        pt = item.get("pattern_type", "")
        if pt not in PATTERN_OUTCOMES:
            continue
        if pt in seen_patterns:
            continue
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
