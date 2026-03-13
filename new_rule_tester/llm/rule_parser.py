"""Parse a natural language AML rule into a structured Rule object."""
from llm.llm_wrapper import call_llm_json
from domain.models import Rule, RuleCondition, DerivedAttr
from config.schema_loader import (
    format_attributes_for_prompt,
    format_aggregations_for_prompt,
    canonical_name,
)
from logging_config import get_logger

log = get_logger(__name__)

SYSTEM = """You are a specialized AML Rule Parsing Engine. Your function is to transform natural language \
Anti-Money Laundering rules into a strict, executable JSON intermediate representation.
You must output ONLY valid raw JSON. Do not use markdown fences, code blocks, or provide explanatory text."""

PROMPT_TEMPLATE = """
# 1. KNOWLEDGE BASE
## Available Transaction Schema
{schema_context}

## Supported Aggregation Functions
{aggregation_context}

---

# 2. PARSING LOGIC & CONSTRAINTS
Follow these steps strictly.

### STEP 1: Identify Rule Type
- **stateless**: Evaluates a single transaction.
- **behavioral**: Evaluates patterns, counts, sums, averages, or account history.

### STEP 2: Parse Aggregations & Scopes (CRITICAL)

**FIRST: determine if an aggregation keyword is present** (sum, count, average, total, max, percentage, ratio).

**Case A — Aggregation keyword IS present** (behavioral scoping):
- The "to Iran" / "from UK" / "where country = X" clause binds as a FILTER on that aggregation, NOT a separate condition.
- Populate `filter_attribute`, `filter_operator`, and `filter_value` to define this scope.
- DO NOT create a separate condition for the scope.
  - Incorrect: `Condition 1: avg(amount) > 500`, `Condition 2: Country == Iran`.
  - Correct: `Condition 1: avg(amount) > 500` with `filter_attribute="receive_country_code"`, `filter_operator="in"`, `filter_value=["Iran"]`.

**Case B — NO aggregation keyword** (stateless per-transaction check):
- The country/entity mention is a SEPARATE direct condition on `receive_country_code` (or equivalent).
- "amount sent to Iran is greater than 500" → TWO conditions: `receive_country_code in ["Iran"]` AND `send_amount > 500`.
- Do NOT collapse them into a filtered aggregation. No aggregation means stateless rule type.

### STEP 3: Parse Account Age (CRITICAL)
- **Definition**: "Account Age" or "Young Account" is defined as the time span across the transaction history.
- **Negative Constraint**: NEVER use `operator: "<"` or `">"` directly on `created_at` to represent account age.
- **Mandatory Mapping**:
  - "account age <= 7 days" -> `aggregation: "days_since_first"`, `attribute: "created_at"`, `operator: "<="`, `value: 7`.
  - "young account (<= N days)" -> same pattern with `operator: "<="` and `value: N`.
  - "account younger than N days" -> `aggregation: "days_since_first"`, `operator: "<"`, `value: N`.

### STEP 4: Choose condition tier (CRITICAL)

TIER 1 (simple) — use for most conditions:
  Maps to a single aggregation over one attribute, one window, and one optional filter.
  Use for: sum, count, average, max, percentage_of_total, distinct_count, days_since_first,
  and ratio (Pattern A — subset ÷ complement within the same window).
  → Set: attribute, aggregation, window, filter fields as normal.
  → Leave derived_attributes, derived_expression, window_mode as null.

  Use Tier 1 percentage_of_total when: "X% of total goes to Y" — subset ÷ whole,
  single window, single attribute. The denominator is always "all transactions" (unfiltered).
  Use Tier 1 ratio (Pattern A) when: "ratio of subset to complement" — subset ÷ (whole − subset),
  single window, single attribute.

TIER 2 (derived) — use when the condition compares two independently computed aggregates.
  If you need to compute more than one scalar and then apply arithmetic across them → Tier 2.

  TRIGGER SIGNALS — if ANY of these is true, the condition is Tier 2:
    a) Different time windows: "last 7 days vs prior 30 days", "this week vs last month"
    b) Different filters on the same attribute: "cash transactions vs total transactions",
       "Iran transfers vs all transfers" (when NOT using percentage_of_total)
    c) Different attributes being compared: "inbound vs outbound", "send_amount vs receive_amount"
    d) Different aggregation functions: "max amount vs average amount"
    e) Explicit cross-comparison language: "compared to", "relative to", "ratio of A to B",
       "exceeds X by", "difference between A and B"

  DO NOT use Tier 2 for:
    - percentage_of_total: subset ÷ whole (single window, denominator = all txns) → Tier 1
    - ratio Pattern A: subset ÷ complement in the same window → Tier 1

  STRUCTURE:
  → Set attribute, aggregation, window, filter_attribute, filter_operator, filter_value ALL to null.
  → Set derived_attributes: a list of exactly 2 named intermediate computed values.
     Each has its OWN: name, aggregation, attribute, window, filter_attribute, filter_operator, filter_value.
     Name each descriptively (e.g. "iran_7d_count", "cash_7d_count", "inbound_30d_sum").
     For COUNT-based: aggregation="count", attribute="transaction_id".
     For SUM/AVG/MAX: use the relevant canonical field.
  → Set derived_expression: "ratio" (DA[0] / DA[1]) or "difference" (DA[0] − DA[1]).
  → Set window_mode:
     "non_overlapping" — when DAs have DIFFERENT windows representing sequential time periods
       (e.g. recent 7d vs prior 30d). Engine makes periods non-overlapping automatically:
         DA[0] period = [latest − window0, latest]
         DA[1] period = (latest − window0 − window1, latest − window0)
     "independent" — when DAs should each apply their window independently from the latest date.
       Use this when windows are the same, or the comparison is NOT about sequential periods
       (e.g. cash count vs total count both in last 7d, or inbound vs outbound both in last 30d).
     RULE: if windows differ AND they represent "recent vs prior period" → "non_overlapping".
           if windows are identical OR the comparison is within the same time range → "independent".

  DA ORDERING:
  → DA[0] = numerator or left operand (more recent or "target" quantity).
  → DA[1] = denominator or right operand (baseline or "reference" quantity).

### STEP 5: Normalization
- Strip currency symbols (e.g., "$1000" -> 1000).
- Convert percentages (e.g., "10%") to decimals (0.10).
- Maintain exact string casing for countries (e.g., "Iran" stays "Iran", never "IR").

---

# 3. OUTPUT JSON SCHEMA
{{
  "rule_type": "stateless" | "behavioral",
  "relevant_attributes": ["list of canonical attribute names involved"],
  "conditions": [
    {{
      "attribute": "canonical_field_name",
      "operator": ">" | "<" | ">=" | "<=" | "==" | "!=" | "in" | "not_in",
      "value": <number, string, or list>,
      "aggregation": null | <aggregation_name>,
      "window": null | <string like "24h", "30d">,
      "logical_connector": "AND" | "OR",
      "filter_attribute": null | <canonical_field_name>,
      "filter_operator": null | <operator>,
      "filter_value": null | <value or list>,
      "derived_attributes": null | [
        {{
          "name": "<short_label e.g. iran_7d_count>",
          "aggregation": "count" | "sum" | "average" | "max",
          "attribute": "<canonical field — use transaction_id for count-based>",
          "window": "<e.g. '7d', '30d'>",
          "filter_attribute": null | "<canonical_field_name>",
          "filter_operator": null | "<operator>",
          "filter_value": null | <value or list>
        }},
        ...
      ],
      "derived_expression": null | "ratio" | "difference",
      "window_mode": null | "non_overlapping" | "independent"
    }}
  ],
  "raw_expression": "A readable summary of the logic",
  "high_risk_countries": ["list of countries flagged as risky"]
}}

---

# 4. REFERENCE EXAMPLES

Example 1a — Stateless (direct per-transaction conditions, explicit phrasing)
Description: "Transactions to Iran with send amount over $100"
Output:
{{
  "rule_type": "stateless",
  "relevant_attributes": ["receive_country_code", "send_amount"],
  "conditions": [
    {{"attribute": "receive_country_code", "operator": "in", "value": ["Iran"], "aggregation": null, "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}},
    {{"attribute": "send_amount", "operator": ">", "value": 100.0, "aggregation": null, "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}}
  ],
  "raw_expression": "receive_country_code IN ['Iran'] AND send_amount > 100",
  "high_risk_countries": ["Iran"]
}}

Example 1b — Stateless (implicit country+amount, no aggregation keyword)
Description: "if amount sent to Iran is greater than 500"
Reasoning: No aggregation keyword (no "average", "sum", "total") → stateless rule, two direct conditions.
Output:
{{
  "rule_type": "stateless",
  "relevant_attributes": ["receive_country_code", "send_amount"],
  "conditions": [
    {{"attribute": "receive_country_code", "operator": "in", "value": ["Iran"], "aggregation": null, "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}},
    {{"attribute": "send_amount", "operator": ">", "value": 500.0, "aggregation": null, "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}}
  ],
  "raw_expression": "receive_country_code IN ['Iran'] AND send_amount > 500",
  "high_risk_countries": ["Iran"]
}}

Example 2 — Behavioral: Scoped Aggregation (the "to" clause binds to filter, NOT a separate condition)
Description: "Alert if avg send amount to Iran is greater than 500"
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["send_amount", "receive_country_code"],
  "conditions": [
    {{"attribute": "send_amount", "operator": ">", "value": 500, "aggregation": "average", "window": null, "logical_connector": "AND", "filter_attribute": "receive_country_code", "filter_operator": "in", "filter_value": ["Iran"]}}
  ],
  "raw_expression": "average(send_amount to Iran) > 500",
  "high_risk_countries": ["Iran"]
}}

Example 3 — Behavioral: Account Age (use days_since_first, NEVER a raw created_at comparison)
Description: "Alert if account is younger than 7 days"
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["created_at"],
  "conditions": [
    {{"attribute": "created_at", "operator": "<", "value": 7, "aggregation": "days_since_first", "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}}
  ],
  "raw_expression": "days_since_first(created_at) < 7",
  "high_risk_countries": []
}}

Example 4 — Behavioral: Combined scoped aggregation AND account age (both patterns together)
Description: "Alert if avg send amount to Iran > $500 for young accounts (<= 7 days)"
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["send_amount", "receive_country_code", "created_at"],
  "conditions": [
    {{"attribute": "send_amount", "operator": ">", "value": 500.0, "aggregation": "average", "window": null, "logical_connector": "AND", "filter_attribute": "receive_country_code", "filter_operator": "in", "filter_value": ["Iran"]}},
    {{"attribute": "created_at", "operator": "<=", "value": 7, "aggregation": "days_since_first", "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}}
  ],
  "raw_expression": "average(send_amount to Iran) > 500 AND days_since_first(created_at) <= 7",
  "high_risk_countries": ["Iran"]
}}

Example 5 — Behavioral: Percentage + unscoped sum
Description: "Alert if more than 10% of total outbound goes to North Korea AND total outbound > $10,000"
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["receive_country_code", "send_amount"],
  "conditions": [
    {{"attribute": "send_amount", "operator": ">", "value": 0.10, "aggregation": "percentage_of_total", "window": null, "logical_connector": "AND", "filter_attribute": "receive_country_code", "filter_operator": "in", "filter_value": ["North Korea"]}},
    {{"attribute": "send_amount", "operator": ">", "value": 10000.0, "aggregation": "sum", "window": null, "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}}
  ],
  "raw_expression": "percentage_of_total(send_amount to North Korea) > 0.10 AND sum(send_amount) > 10000",
  "high_risk_countries": ["North Korea"]
}}

Example 6 — Behavioral: Time window
Description: "Alert if average transaction amount exceeds $3,000 in the last 30 days"
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["send_amount"],
  "conditions": [
    {{"attribute": "send_amount", "operator": ">", "value": 3000.0, "aggregation": "average", "window": "30d", "logical_connector": "AND", "filter_attribute": null, "filter_operator": null, "filter_value": null}}
  ],
  "raw_expression": "average(send_amount) > 3000 within 30 days",
  "high_risk_countries": []
}}

Example 7 — Behavioral: Filtered days_since_first
Description: "Alert if days since first transaction to Iran > 30"
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["receive_country_code", "created_at"],
  "conditions": [
    {{"attribute": "created_at", "operator": ">", "value": 30, "aggregation": "days_since_first", "window": null, "logical_connector": "AND", "filter_attribute": "receive_country_code", "filter_operator": "in", "filter_value": ["Iran"]}}
  ],
  "raw_expression": "days_since_first(created_at to Iran) > 30",
  "high_risk_countries": ["Iran"]
}}

Example 8 — Behavioral: Tier 2 derived condition (different windows, non-overlapping)
Description: "Alert if number of transactions to Iran in the last 7 days is more than twice
              the number of transactions to Iran in the prior 30 days"
Reasoning:
  Trigger signal (a): different time windows representing sequential periods → Tier 2.
  DA[0] = iran_7d_count: count(transaction_id, window=7d, filter=Iran) ← recent period (numerator)
  DA[1] = iran_30d_count: count(transaction_id, window=30d, filter=Iran) ← prior period (denominator)
  derived_expression = "ratio", window_mode = "non_overlapping"
  Engine: numerator=[latest-7d, latest], denominator=[latest-37d, latest-7d)
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["receive_country_code", "transaction_id"],
  "conditions": [
    {{
      "attribute": null,
      "operator": ">",
      "value": 2.0,
      "aggregation": null,
      "window": null,
      "logical_connector": "AND",
      "filter_attribute": null,
      "filter_operator": null,
      "filter_value": null,
      "derived_attributes": [
        {{
          "name": "iran_7d_count",
          "aggregation": "count",
          "attribute": "transaction_id",
          "window": "7d",
          "filter_attribute": "receive_country_code",
          "filter_operator": "in",
          "filter_value": ["Iran"]
        }},
        {{
          "name": "iran_30d_count",
          "aggregation": "count",
          "attribute": "transaction_id",
          "window": "30d",
          "filter_attribute": "receive_country_code",
          "filter_operator": "in",
          "filter_value": ["Iran"]
        }}
      ],
      "derived_expression": "ratio",
      "window_mode": "non_overlapping"
    }}
  ],
  "raw_expression": "ratio(iran_7d_count / iran_30d_count) > 2.0",
  "high_risk_countries": ["Iran"]
}}

Example 9 — Behavioral: Tier 2 derived condition (same window, different filters)
Description: "Alert if ratio of cash transactions to total transactions in the last 7 days exceeds 0.8"
Reasoning:
  Trigger signal (b): different filters on the same attribute within the same window → Tier 2.
  Both DAs use window=7d but one filters on cash, the other has no filter.
  window_mode = "independent" because both windows are identical (same calendar period).
  NOT percentage_of_total because the denominator is a separately-defined group ("total"), not Tier 1.
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["transaction_type", "transaction_id"],
  "conditions": [
    {{
      "attribute": null,
      "operator": ">",
      "value": 0.8,
      "aggregation": null,
      "window": null,
      "logical_connector": "AND",
      "filter_attribute": null,
      "filter_operator": null,
      "filter_value": null,
      "derived_attributes": [
        {{
          "name": "cash_7d_count",
          "aggregation": "count",
          "attribute": "transaction_id",
          "window": "7d",
          "filter_attribute": "transaction_type",
          "filter_operator": "==",
          "filter_value": "cash"
        }},
        {{
          "name": "total_7d_count",
          "aggregation": "count",
          "attribute": "transaction_id",
          "window": "7d",
          "filter_attribute": null,
          "filter_operator": null,
          "filter_value": null
        }}
      ],
      "derived_expression": "ratio",
      "window_mode": "independent"
    }}
  ],
  "raw_expression": "ratio(cash_7d_count / total_7d_count) > 0.8",
  "high_risk_countries": []
}}

Example 10 — Behavioral: Tier 2 derived condition (same window, different attributes)
Description: "Alert if total inbound exceeds total outbound by more than $5,000 in the last 30 days"
Reasoning:
  Trigger signal (c): different attributes (receive_amount vs send_amount) → Tier 2.
  Both DAs use the same window=30d, so window_mode = "independent".
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["receive_amount", "send_amount"],
  "conditions": [
    {{
      "attribute": null,
      "operator": ">",
      "value": 5000.0,
      "aggregation": null,
      "window": null,
      "logical_connector": "AND",
      "filter_attribute": null,
      "filter_operator": null,
      "filter_value": null,
      "derived_attributes": [
        {{
          "name": "inbound_30d_sum",
          "aggregation": "sum",
          "attribute": "receive_amount",
          "window": "30d",
          "filter_attribute": null,
          "filter_operator": null,
          "filter_value": null
        }},
        {{
          "name": "outbound_30d_sum",
          "aggregation": "sum",
          "attribute": "send_amount",
          "window": "30d",
          "filter_attribute": null,
          "filter_operator": null,
          "filter_value": null
        }}
      ],
      "derived_expression": "difference",
      "window_mode": "independent"
    }}
  ],
  "raw_expression": "difference(inbound_30d_sum - outbound_30d_sum) > 5000",
  "high_risk_countries": []
}}

---

# 5. TARGET DESCRIPTION
Parse the following rule:
{description}"""


def parse_rule(description: str) -> Rule:
    log.info("parse_rule | input_chars=%d", len(description))
    prompt = PROMPT_TEMPLATE.format(
        schema_context=format_attributes_for_prompt(),
        aggregation_context=format_aggregations_for_prompt(),
        description=description,
    )
    data = call_llm_json(prompt, system=SYSTEM)

    def _coerce_filter_value(fop, fval):
        """Ensure filter_value is a list when operator is 'in'/'not_in'."""
        if fval is None:
            return None
        if fop in ("in", "not_in") and not isinstance(fval, list):
            return [fval]
        return fval

    def _hydrate_derived_attr(raw: dict) -> DerivedAttr:
        """Construct a DerivedAttr from a raw JSON sub-object."""
        fop = raw.get("filter_operator")
        return DerivedAttr(
            name=raw["name"],
            aggregation=raw.get("aggregation", "count"),
            attribute=canonical_name(raw["attribute"]) if raw.get("attribute") else "transaction_id",
            window=raw.get("window"),
            filter_attribute=canonical_name(raw["filter_attribute"]) if raw.get("filter_attribute") else None,
            filter_operator=fop,
            filter_value=_coerce_filter_value(fop, raw.get("filter_value")),
        )

    conditions = [
        RuleCondition(
            attribute=canonical_name(c["attribute"]) if c.get("attribute") else None,
            operator=c["operator"],
            value=c["value"],
            aggregation=c.get("aggregation"),
            window=c.get("window"),
            logical_connector=c.get("logical_connector", "AND"),
            filter_attribute=canonical_name(c["filter_attribute"]) if c.get("filter_attribute") else None,
            filter_operator=c.get("filter_operator"),
            filter_value=_coerce_filter_value(c.get("filter_operator"), c.get("filter_value")),
            derived_attributes=[_hydrate_derived_attr(da) for da in c["derived_attributes"]]
                                if c.get("derived_attributes") else None,
            derived_expression=c.get("derived_expression"),
            window_mode=c.get("window_mode"),
        )
        for c in data["conditions"]
    ]

    relevant_attributes = [canonical_name(a) for a in data["relevant_attributes"]]

    rule = Rule(
        description=description,
        rule_type=data["rule_type"],
        relevant_attributes=relevant_attributes,
        conditions=conditions,
        raw_expression=data["raw_expression"],
        high_risk_countries=data.get("high_risk_countries", []),
    )
    log.info(
        "parse_rule | result: rule_type=%s conditions=%d high_risk_countries=%s raw_expression=%r",
        rule.rule_type,
        len(rule.conditions),
        rule.high_risk_countries,
        rule.raw_expression,
    )
    log.debug("parse_rule | full Rule: %r", rule)
    return rule
