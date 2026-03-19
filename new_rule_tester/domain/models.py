from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class DerivedAttr:
    """One named intermediate computed value for a Tier 2 (derived) condition.

    Each DerivedAttr has its own independent window, filter, and aggregation.
    The engine computes each to a scalar, then combines them via derived_expression.
    """
    name: str                          # short label, e.g. "iran_7d_count"
    aggregation: str                   # "count", "sum", "average", "max"
    attribute: str                     # canonical schema field; use "transaction_id" for count
    window: str | None = None       # e.g. "7d", "30d"
    filter_attribute: str | None = None
    filter_operator: str | None = None
    filter_value: Any | None = None


@dataclass
class RuleCondition:
    attribute: str | None
    operator: str               # >, <, >=, <=, ==, !=, in, not_in
    value: Any
    aggregation: str | None = None   # sum, count, percentage_of_total, ratio, distinct_count, shared_distinct_count
    window: str | None = None        # e.g. "30d", "24h"
    logical_connector: str = "AND"      # AND or OR (how this connects to the NEXT condition)
    # For percentage_of_total, ratio (Pattern A), and filtered count:
    # defines which subset of transactions to compute over.
    filter_attribute: str | None = None
    filter_operator: str | None = None
    filter_value: Any | None = None
    group_by: str | None = None         # attribute to partition by before aggregating (e.g. "recipient_id")
    group_mode: str = "any"             # "any" = at least one group fires; "all" = every group must fire
    link_attribute: list[str] | None = None  # shared_distinct_count: attributes defining the "connection"
                                             # between primary values (OR semantics)
                                             # e.g. ["email", "phone"] — share any one = connected
    # Tier 2 (derived) condition fields.
    # When derived_attributes is set, the engine computes each DerivedAttr to a scalar
    # value, then combines them with derived_expression, and compares to value.
    derived_attributes: list[DerivedAttr] | None = None
    derived_expression: str | None = None   # "ratio" | "difference"
    window_mode: str | None = None          # "non_overlapping" | "independent" (Tier 2 only)

    def aggregate_key(self) -> str:
        """Consistent key for the aggregates dict, used by both compute and engine."""
        if self.derived_attributes:
            names = "/".join(da.name for da in self.derived_attributes)
            return f"{self.derived_expression or 'derived'}({names})"
        if self.link_attribute:
            base = f"{self.aggregation}({self.attribute}:{','.join(self.link_attribute)})"
        else:
            base = f"{self.aggregation}({self.attribute})"
        if self.group_by:
            return f"{base}_by_{self.group_by}"
        return base


@dataclass
class Rule:
    description: str
    rule_type: str              # "stateless" or "behavioral"
    relevant_attributes: list[str]
    conditions: list[RuleCondition]
    raw_expression: str         # human-readable summary of rule logic
    high_risk_countries: list[str] = field(default_factory=list)


@dataclass
class Prototype:
    scenario_type: str          # "risky" or "genuine"
    attributes: dict[str, Any]
    user_feedback_history: list[str] = field(default_factory=list)


@dataclass
class ConditionResult:
    attribute: str
    operator: str
    threshold: Any
    actual_value: Any
    passed: bool

    def label(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"{self.attribute} {self.operator} {self.threshold} (actual: {self.actual_value}) → {status}"


@dataclass
class ValidationResult:
    passed: bool
    expected_trigger: bool      # True = expected to trigger (risky), False = expected not to trigger (genuine)
    condition_results: list[ConditionResult] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL"


@dataclass
class Transaction:
    id: str
    tag: str                    # "risky", "genuine", "background"
    attributes: dict[str, Any]
    validation_result: ValidationResult | None = None


@dataclass
class BehavioralTestCase:
    id: str
    scenario_type: str          # "risky" or "genuine"
    intent: str | None = None
    transactions: list[Transaction] = field(default_factory=list)
    computed_aggregates: dict[str, Any] = field(default_factory=dict)
    validation_result: ValidationResult | None = None
    correction_attempts: int = 0
    user_feedback_history: list[str] = field(default_factory=list)


@dataclass
class TestSuggestion:
    id: str                         # "s-001", "s-002", ...
    scenario_type: str              # "risky" or "genuine"
    pattern_type: str               # e.g. "boundary_just_over", "near_miss_one_clause"
    title: str                      # short label
    description: str                # 2–3 sentences: what this tests and why
    focus_conditions: list[str]     # which conditions are specifically exercised
    suggested_intent: str           # pre-written intent string for the sequence generator
    expected_outcome: str           # "FIRE" or "NOT_FIRE" — derived from pattern_type, not LLM


@dataclass
class TestSuite:
    rule: Rule
    stateless_sequence: list[Transaction] | None = None
    behavioral_test_cases: list[BehavioralTestCase] = field(default_factory=list)
    prototypes: dict[str, Prototype] | None = None   # {"risky": Prototype, "genuine": Prototype}
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
