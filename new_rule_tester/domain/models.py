from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class FilterClause:
    """One filter predicate applied to a transaction before aggregation.

    If value_field is set, the RHS is resolved from transaction.attributes[value_field]
    (cross-field comparison). Otherwise value is used as a literal RHS.
    connector specifies how this clause chains to the NEXT clause (AND/OR); ignored on the last clause.
    """
    attribute: str                  # LHS canonical attribute name
    operator: str                   # >, <, >=, <=, ==, !=, in, not_in
    value: Any = None               # literal RHS value; ignored when value_field is set
    value_field: str | None = None  # RHS attribute name for cross-field comparison
    connector: str = "AND"          # how this clause chains to the NEXT; ignored on last


@dataclass
class DerivedAttr:
    """One named intermediate computed value for a Tier 2 (derived) condition.

    Each DerivedAttr has its own independent window, filter, and aggregation.
    The engine computes each to a scalar, then combines them via derived_expression.
    """
    name: str                          # short label, e.g. "iran_7d_count"
    aggregation: str                   # "count", "sum", "average", "max"
    attribute: str                     # canonical schema field; use "transaction_id" for count
    window: str | None = None          # e.g. "7d", "30d"
    filters: list[FilterClause] | None = None  # list of filter clauses (AND/OR chained)


@dataclass
class ComputedAttr:
    """A named intermediate value computed from a transaction sequence before condition evaluation.

    Defined at the Rule level so multiple conditions can reference it by name.
    The engine computes all ComputedAttrs once (in order) before evaluating any condition.

    All CAs inject their computed value into t.attributes[name] so later CAs can
    reference them in filters. Comparisons always live on conditions or filters — never
    baked into the CA itself.

    Three output modes:

      Scalar (group_by=None, derived_from=None):
        aggregation(attribute)[window] over filtered transactions → one value.
        Stored in aggregates[name] and injected into every t.attributes[name].
        Example: avg(send_amount)[30d] where country=Kenya → 342.5

      Group (group_by set):
        aggregation(attribute) computed per distinct group value in the windowed subset.
        The raw per-group aggregate is injected into each t.attributes[name].
        Later CA filters compare directly: e.g. {attribute: is_new_recipient, op: ==, value: 1}.
        Example: count(transaction_id) group_by=recipient_id
          → t.attributes["is_new_recipient"] = 1 for first-time recipients, 2 for second, etc.

      Derived (derived_from set, aggregation="ratio" or "difference"):
        Combines two previously computed scalar CAs using arithmetic.
        aggregation="ratio"      → derived_from[0] / derived_from[1]
        aggregation="difference" → derived_from[0] - derived_from[1]
        attribute/window/filters/group_by are ignored.
        Stored in aggregates[name] and injected into all t.attributes[name].

    CAs are computed in declaration order; later CAs can reference earlier CA names in
    their filters or derived_from (since earlier CAs have already injected their values).
    """
    name: str                              # label used as key in aggregates and t.attributes
    aggregation: str                       # count, sum, average, max, age_years, distinct_count,
                                           # shared_distinct_count, days_since_first,
                                           # ratio, difference (last two for derived mode only)
    attribute: str                         # canonical schema attribute; ignored in derived mode
    filters: list[FilterClause] | None = None  # optional pre-aggregation filter (can ref earlier CA names)
    group_by: str | None = None            # if set: inject per-group aggregate into t.attributes[name]
    window: str | None = None             # CA's own window, applied independently (e.g. "30d", "7d")
    window_exclude: str | None = None     # if set, restrict to (latest−window) ≤ date < (latest−window_exclude)
    derived_from: list[str] | None = None  # derived mode: names of exactly 2 earlier scalar CAs
    link_attribute: list[str] | None = None  # shared_distinct_count only: PII/link attrs (OR semantics)


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
    filters: list[FilterClause] | None = None  # list of filter clauses (AND/OR chained)
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
    condition_group: int = 0
    # Conditions sharing a group number are evaluated together using logical_connector.
    # Groups are combined in ascending order using condition_group_connector.
    condition_group_connector: str = "OR"
    # How THIS group's result connects to the NEXT group's result.
    # Read from the FIRST condition in each group; all others in the group should leave this at default "OR".
    # "OR" (default) or "AND". Irrelevant for the last group.
    computed_attr_name: str | None = None
    # If set, this condition evaluates aggregates[computed_attr_name] operator value.
    # All aggregation/attribute/window/filters fields are ignored — the value is pre-computed
    # by a ComputedAttr in rule.computed_attrs. This is the preferred path for new rules.

    def aggregate_key(self) -> str:
        """Consistent key for the aggregates dict, used by both compute and engine."""
        if self.computed_attr_name:
            return self.computed_attr_name
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
    computed_attrs: list[ComputedAttr] = field(default_factory=list)
    # Named intermediate values computed from the transaction sequence before condition evaluation.
    # Scalar CAs store their value in aggregates[name] and inject it into all t.attributes[name].
    # Boolean CAs (group_by set) inject True/False per transaction into t.attributes[name].
    # Computed in declaration order; conditions reference them via computed_attr_name or FilterClause.


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
