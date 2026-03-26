"""Deterministic rule engine — evaluates conditions against transactions or aggregates.

For stateless rules: evaluates each individual transaction.
For behavioral rules: evaluates pre-computed aggregates.
"""
from domain.models import (
    ConditionResult,
    Rule,
    RuleCondition,
    Transaction,
    ValidationResult,
)
from logging_config import get_logger
from validation.aggregate_compute import compute_aggregates

log = get_logger(__name__)


# ─── Operator evaluation ──────────────────────────────────────────────────────

def _evaluate_operator(actual, operator: str, threshold) -> bool:
    try:
        if operator == ">":
            return float(actual) > float(threshold)
        elif operator == "<":
            return float(actual) < float(threshold)
        elif operator == ">=":
            return float(actual) >= float(threshold)
        elif operator == "<=":
            return float(actual) <= float(threshold)
        elif operator == "==":
            try:
                return float(actual) == float(threshold)
            except (TypeError, ValueError):
                return str(actual) == str(threshold)
        elif operator == "!=":
            try:
                return float(actual) != float(threshold)
            except (TypeError, ValueError):
                return str(actual) != str(threshold)
        elif operator == "in":
            vals = threshold if isinstance(threshold, list) else [threshold]
            return str(actual) in [str(v) for v in vals]
        elif operator == "not_in":
            vals = threshold if isinstance(threshold, list) else [threshold]
            return str(actual) not in [str(v) for v in vals]
    except (TypeError, ValueError):
        return False
    return False


def _combine_results(condition_results: list[ConditionResult], conditions: list[RuleCondition]) -> bool:
    """Combine per-condition results respecting condition_group and condition_group_connector.

    Within each group: conditions combined left-to-right using logical_connector (AND/OR).
    Across groups: groups combined in ascending group-number order using each group's
    condition_group_connector (taken from the first condition in that group).
    """
    if not condition_results:
        return True

    from collections import defaultdict
    groups: dict[int, list] = defaultdict(list)
    group_connector: dict[int, str] = {}

    for cr, cond in zip(condition_results, conditions):
        gid = cond.condition_group
        groups[gid].append((cr, cond))
        if gid not in group_connector:
            group_connector[gid] = (cond.condition_group_connector or "OR").upper()
        elif (cond.condition_group_connector or "OR").upper() != group_connector[gid]:
            log.warning(
                "condition_group_connector mismatch in group %d: expected %s, got %s — using first value",
                gid, group_connector[gid], cond.condition_group_connector,
            )

    def _eval_group(items) -> bool:
        result = items[0][0].passed
        for i in range(1, len(items)):
            connector = (items[i - 1][1].logical_connector or "AND").upper()
            result = result or items[i][0].passed if connector == "OR" else result and items[i][0].passed
        return result

    sorted_ids = sorted(groups)
    result = _eval_group(groups[sorted_ids[0]])
    for i in range(1, len(sorted_ids)):
        prev_id = sorted_ids[i - 1]
        conn = group_connector.get(prev_id, "OR")
        next_result = _eval_group(groups[sorted_ids[i]])
        result = result or next_result if conn == "OR" else result and next_result

    return result


# ─── Stateless evaluation (per transaction) ───────────────────────────────────

def evaluate_transaction(rule: Rule, transaction: Transaction) -> ValidationResult:
    """Evaluate a single transaction against all stateless rule conditions."""
    expected_trigger = transaction.tag == "risky"
    condition_results = []

    for cond in rule.conditions:
        actual = transaction.attributes.get(cond.attribute)
        passed = _evaluate_operator(actual, cond.operator, cond.value)
        condition_results.append(ConditionResult(
            attribute=cond.attribute,
            operator=cond.operator,
            threshold=cond.value,
            actual_value=actual,
            passed=passed,
        ))

    rule_triggered = _combine_results(condition_results, rule.conditions)

    # Validation passes if: risky → rule fires, genuine → rule doesn't fire
    validation_passed = (rule_triggered == expected_trigger)

    return ValidationResult(
        passed=validation_passed,
        expected_trigger=expected_trigger,
        condition_results=condition_results,
    )


def evaluate_stateless_sequence(rule: Rule, transactions: list[Transaction]) -> list[Transaction]:
    """Evaluate all tagged (non-background) transactions. Returns transactions with results attached."""
    for t in transactions:
        if t.tag != "background":
            t.validation_result = evaluate_transaction(rule, t)
    return transactions


# ─── Behavioral evaluation (aggregates) ───────────────────────────────────────

def evaluate_behavioral_sequence(
    rule: Rule,
    transactions: list[Transaction],
    scenario_type: str,
) -> tuple[ValidationResult, dict]:
    """
    Compute aggregates and evaluate behavioral conditions.
    Returns (ValidationResult, computed_aggregates_dict).
    """
    aggregates = compute_aggregates(rule, transactions)
    expected_trigger = scenario_type == "risky"
    condition_results = []

    for cond in rule.conditions:
        # ComputedAttr-backed condition — value pre-computed in aggregates by _compute_all_attrs
        if cond.computed_attr_name:
            key = cond.computed_attr_name
            actual = aggregates.get(key, 0)
            passed = _evaluate_operator(actual, cond.operator, cond.value)
            condition_results.append(ConditionResult(
                attribute=key,
                operator=cond.operator,
                threshold=cond.value,
                actual_value=actual,
                passed=passed,
            ))
            continue

        # Tier 2: derived condition — look up by auto-generated key
        if cond.derived_attributes is not None:
            key = cond.aggregate_key()
            actual = aggregates.get(key, 0)
            passed = _evaluate_operator(actual, cond.operator, cond.value)
            condition_results.append(ConditionResult(
                attribute=key,
                operator=cond.operator,
                threshold=cond.value,
                actual_value=actual,
                passed=passed,
            ))
            continue

        if not cond.aggregation:
            # Non-aggregated condition (e.g. account_age <= 7): evaluate directly
            # against the first transaction's attributes — account-level attributes
            # are consistent across the sequence.
            actual = transactions[0].attributes.get(cond.attribute) if transactions else None
            passed = _evaluate_operator(actual, cond.operator, cond.value)
            condition_results.append(ConditionResult(
                attribute=cond.attribute,
                operator=cond.operator,
                threshold=cond.value,
                actual_value=actual,
                passed=passed,
            ))
            continue

        key = cond.aggregate_key()
        actual = aggregates.get(key, 0)
        passed = _evaluate_operator(actual, cond.operator, cond.value)
        condition_results.append(ConditionResult(
            attribute=key,
            operator=cond.operator,
            threshold=cond.value,
            actual_value=actual,
            passed=passed,
        ))

    rule_triggered = _combine_results(condition_results, rule.conditions)
    validation_passed = (rule_triggered == expected_trigger)

    for cr in condition_results:
        status = "PASS" if cr.passed else "FAIL"
        log.info(
            "rule_engine | condition %s %s %s: actual=%s → %s",
            cr.attribute, cr.operator, cr.threshold, cr.actual_value, status,
        )
    log.info(
        "rule_engine | overall: rule_triggered=%s expected_trigger=%s validation_passed=%s scenario=%s",
        rule_triggered, expected_trigger, validation_passed, scenario_type,
    )

    return ValidationResult(
        passed=validation_passed,
        expected_trigger=expected_trigger,
        condition_results=condition_results,
    ), aggregates
