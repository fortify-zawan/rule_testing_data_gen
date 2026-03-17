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
            return str(actual) == str(threshold)
        elif operator == "!=":
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
    """Combine per-condition pass/fail using AND/OR connectors."""
    if not condition_results:
        return True

    # Build a boolean expression respecting AND/OR connectors
    # logical_connector on condition[i] connects condition[i] to condition[i+1]
    result = condition_results[0].passed
    for i in range(1, len(condition_results)):
        connector = conditions[i - 1].logical_connector.upper()
        if connector == "OR":
            result = result or condition_results[i].passed
        else:  # AND (default)
            result = result and condition_results[i].passed
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
