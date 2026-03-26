"""Prompt strings for llm/rule_parser.py."""
SYSTEM = """You are a specialized AML Rule Parsing Engine. Your function is to transform natural language \
Anti-Money Laundering rules into a strict, executable JSON intermediate representation.
You must output ONLY valid raw JSON. Do not use markdown fences, code blocks, or provide explanatory text."""

PROMPT_TEMPLATE = """
# 1. CONTEXT
Schema: {schema_context}
Aggregations: {aggregation_context}

---

# 2. CORE PARSING LOGIC

### STEP 1: Classify Rule Type
- **Stateless**: Per-transaction checks. NO aggregation keywords (sum, count, avg, total) and NO history context.
- **Behavioral**: Aggregations, patterns, or history. ALWAYS uses `computed_attrs`.

### STEP 2: Normalization
- Strip currency symbols (e.g., "$1000" → 1000).
- Convert percentages (e.g., "10%") to decimals (0.10).
- Maintain exact string casing for countries (e.g., "Iran" stays "Iran", never "IR").

### STEP 3: Execute Branch

#### BRANCH A: STATELESS RULES
1. Map rule clauses directly to `conditions`.
2. Set `attribute`, `operator`, `value` directly.
3. Set `computed_attrs` to `[]`.

#### BRANCH B: BEHAVIORAL RULES (Two-Phase Model)
**Phase 1 — Define Computed Attributes**
Identify every calculated value (sum, count, avg, age, days since first, etc.). Create an entry in `computed_attrs`:
- `name`: Descriptive label (e.g., "sum_to_iran").
- `aggregation`: Function type.
- `attribute`: Field to aggregate.
- `filters`: Scoped clauses.
- `window`/`group_by`: If specified.

**Phase 2 — Create Conditions & Link (CRITICAL)**
For every threshold check in the rule:
1. **Identify Target**: Find the Computed Attribute from Phase 1.
2. **Copy Name**: Paste its `name` into `computed_attr_name` on the condition.
3. **Nullify**: Set `attribute`, `aggregation`, `window`, `filters` to null on the condition.

---

# 3. SPECIAL PATTERNS

### 3.1 Account vs User Age
- **Account Age**: `aggregation: "days_since_first"`, `attribute: "created_at"`.
- **User Age**: `aggregation: "age_years"`, `attribute: "date_of_birth"`.

### 3.2 Group-By (Per-Entity Rules)
If rule scopes per entity ("per recipient", "any sender"):
1. Create **ONE Group CA** with `group_by`.
2. Create **ONE Condition** referencing that CA.

### 3.3 Shared PII
- Use `aggregation: "shared_distinct_count"` with `link_attribute`.

### 3.4 Derived Attributes (Ratio/Difference)
- Use `derived_from` to reference two earlier CAs.
- **CRITICAL for Ratios**: Ensure numerator and denominator are logically disjoint if comparing time periods.

### 3.5 Cross-Field Filters
- Use `value_field` instead of `value` when comparing two raw fields (e.g., `sender_country != recipient_country`).

### 3.6 Condition Groups
- Use `condition_group` for "(A AND B) OR (C AND D)" logic.

### 3.7 "New Entity" Logic (First Interaction)
To detect "new recipient", "new country", or "first interaction":
1. Create a CA with `aggregation: "days_since_first"`.
2. Set `group_by` to the entity dimension (e.g., `recipient_id` for "new recipient").
3. This CA returns 0 (or 1) for the first interaction.
4. In your main aggregation, **filter** on this CA: `days_since_first_ca == 0` (or `== 1`).

### 3.8 Filtering on Computed Attributes (Chaining)
**CRITICAL**: You can filter one Computed Attribute using the result of another.
- **How**: Define the dependency CA first (e.g., `sender_age`).
- **Link**: In the main CA's `filters`, set the `attribute` field to the **name** of the dependency CA.
- **Operators**: Use standard comparison operators (`>`, `==`, etc.). NEVER use "custom".

### 3.9 Window Exclusion ("last X months WITHOUT last M days")
Phrases like "without the last week", "excluding the last 7 days", or "prior period only" require `window_exclude`.
- Use `window_exclude` on a **single CA**.
- Do NOT use derived difference for window exclusion.

### 3.10 Divisibility Checks ("divisible by N")
Phrases like "amount divisible by 100" or "multiple of 50":
- Use the modulus operator `%` in the filter.
- **Format**: `attribute % N == 0`.
- Example: `receive_amount` divisible by 100 → `{{"attribute": "receive_amount", "operator": "%", "value": 0, "modulus_base": 100}}`.
- **Alternative if strict schema enforced**: Use `operator: "%"` and `value: 0` with `modulus_base: N` (if schema allows), otherwise map to `operator: "%", value: 100` (interpreted as `attribute % 100 == 0`). *If schema strictly enforces standard operators only and does not support modulus, you must handle this via logic or note limitation, but prefer `operator: "%"` if valid.*

### 3.11 Disjoint Time Windows in Ratios (CRITICAL)
When a rule compares a **recent period** to a **prior period** (e.g., "ratio of last week count to prior 6 months count"):
1. **Disjoint Windows**: The "prior" period usually implies **excluding** the "recent" period.
   - If Recent = 7 days, and Prior = 6 months.
   - The Prior window should be `window: "6m"` with `window_exclude: "7d"`.
   - If you do not exclude, the recent period is a subset of the prior period, making the ratio logic often invalid (e.g., ratio > 3 impossible if numerator is inside denominator).
2. **Filters Alignment**: Ensure the filters for the numerator (recent) and denominator (prior) match exactly unless the rule explicitly compares different transaction types.

---

# 4. OUTPUT JSON SCHEMA
{{
  "rule_type": "stateless" | "behavioral",
  "relevant_attributes": ["list of canonical attribute names involved"],
  "computed_attrs": [
    {{
      "name": "<string>",
      "aggregation": "count"|"sum"|"average"|"max"|"distinct_count"|"shared_distinct_count"|
                   "days_since_first"|"age_years"|"percentage_of_total"|"ratio"|"difference",
      "attribute": "<field>" | null,
      "filters": [
        {{
          "attribute": "<field_or_CA_name>",
          "operator": ">" | "<" | "==" | "!=" | "in" | "not_in" | "%", // Added modulus
          "value": <number|string|list> | null,
          "value_field": "<field>" | null,
          "modulus_base": <number> | null, // Used if operator is "%"
          "connector": "AND"
        }}
      ] | null,
      "window": "<7d>" | null,
      "window_exclude": "<1m>" | null,
      "group_by": "<field>" | null,
      "derived_from": [...] | null,
      "link_attribute": [...] | null
    }}
  ],
  "conditions": [
    {{
      "computed_attr_name": "<MUST_MATCH_CA_NAME>",
      "attribute": null,
      "operator": ">" | "<" | "==" | "!=" | "in",
      "value": <number|string|list>,
      "logical_connector": "AND" | "OR",
      "aggregation": null,
      "window": null,
      "filters": null,
      "condition_group": 0,
      "condition_group_connector": "OR" | "AND"
    }}
  ],
  "raw_expression": "<summary>",
  "high_risk_countries": ["..."]
}}

---

# 5. HARD REQUIREMENTS
- **LINKING ENFORCEMENT**: `computed_attr_name` is **MANDATORY** for behavioral conditions.
- **STRICT OPERATORS**: ONLY use standard comparison operators (`>`, `<`, `==`, `!=`, `in`, `not_in`, `%`). 
  - **FORBIDDEN**: `custom`, `matches`, `age >=`, or any natural language phrases in the operator field.
- **SEPARATION OF CONCERNS**: DO NOT set `aggregation` or `attribute` on behavioral conditions.
- **CHAINING**: If a filter requires a derived value, define it as a CA first.
- **DISJOINT RATIOS**: For ratios comparing time periods, ensure the denominator window excludes the numerator window (using `window_exclude`).

---

# 6. EXAMPLES

### Example 1: Behavioral with Filters (Scoped)
Description: "Alert if sum of completed transactions to Iran > $5,000"
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{
      "name": "completed_iran_sum", "aggregation": "sum", "attribute": "send_amount",
      "filters": [
        {{"attribute": "transaction_status", "operator": "==", "value": "completed", "connector": "AND"}},
        {{"attribute": "receive_country_code", "operator": "in", "value": ["Iran"], "connector": "AND"}}
      ]
    }}
  ],
  "conditions": [
    {{"computed_attr_name": "completed_iran_sum", "operator": ">", "value": 5000, "logical_connector": "AND", "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 2: Filter Chaining (User Age)
Description: "Alert if sum where sender age >= 60 > $10,000"
Reasoning: Define sender_age first, then filter the sum on it.
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{"name": "sender_age", "aggregation": "age_years", "attribute": "date_of_birth"}},
    {{
      "name": "elderly_sum", "aggregation": "sum", "attribute": "send_amount",
      "filters": [{{"attribute": "sender_age", "operator": ">=", "value": 60, "connector": "AND"}}]
    }}
  ],
  "conditions": [
    {{"computed_attr_name": "elderly_sum", "operator": ">", "value": 10000, "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 3: New Recipient Logic (days_since_first)
Description: "Alert if sum to a new recipient > $2000"
Reasoning: "New recipient" means days since first interaction with that recipient is 0.
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{
      "name": "days_since_first_recipient", "aggregation": "days_since_first", "attribute": "created_at",
      "group_by": "recipient_id"
    }},
    {{
      "name": "new_recipient_sum", "aggregation": "sum", "attribute": "send_amount",
      "filters": [{{"attribute": "days_since_first_recipient", "operator": "==", "value": 0, "connector": "AND"}}]
    }}
  ],
  "conditions": [
    {{"computed_attr_name": "new_recipient_sum", "operator": ">", "value": 2000, "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 4: Complex Chaining (Age + New Recipient + Cross-Field)
Description: "For this sender, if sum > 2000 to new recipients where sender >= 60 AND recipient country != sender country"
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{"name": "sender_age", "aggregation": "age_years", "attribute": "date_of_birth"}},
    {{"name": "days_since_first_recipient", "aggregation": "days_since_first", "attribute": "created_at", "group_by": "recipient_id"}},
    {{
      "name": "complex_sum", "aggregation": "sum", "attribute": "send_amount", "window": "30d",
      "filters": [
        {{"attribute": "sender_age", "operator": ">=", "value": 60, "connector": "AND"}},
        {{"attribute": "days_since_first_recipient", "operator": "==", "value": 0, "connector": "AND"}},
        {{"attribute": "receive_country_code", "operator": "!=", "value_field": "send_country_code", "connector": "AND"}}
      ]
    }}
  ],
  "conditions": [
    {{"computed_attr_name": "complex_sum", "operator": ">", "value": 2000, "logical_connector": "AND", "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 5: Cross-Field Filter
Description: "Alert if total sent where sender country differs from recipient country > $10,000"
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{
      "name": "cross_border_30d", "aggregation": "sum", "attribute": "send_amount", "window": "30d",
      "filters": [{{"attribute": "send_country_code", "operator": "!=", "value_field": "receive_country_code", "connector": "AND"}}]
    }}
  ],
  "conditions": [
    {{"computed_attr_name": "cross_border_30d", "operator": ">", "value": 10000, "logical_connector": "AND", "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 6: Behavioral Group-By
Description: "Alert if any recipient receives from more than 3 distinct senders in 30 days"
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{"name": "senders_per_recipient", "aggregation": "distinct_count", "attribute": "user_id", "group_by": "recipient_id", "window": "30d"}}
  ],
  "conditions": [
    {{"computed_attr_name": "senders_per_recipient", "operator": ">", "value": 3, "logical_connector": "AND", "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 7: Shared PII
Description: "Alert if any recipient has multiple senders sharing email or phone in 30 days"
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{"name": "shared_senders_per_recipient", "aggregation": "shared_distinct_count", "attribute": "user_id", "link_attribute": ["email", "phone"], "group_by": "recipient_id", "window": "30d"}}
  ],
  "conditions": [
    {{"computed_attr_name": "shared_senders_per_recipient", "operator": ">", "value": 1, "logical_connector": "AND", "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 8: Derived CA (Ratio with Disjoint Windows) - UPDATED
Description: "Alert if recent 7d spend is more than triple the prior 6m spend (excluding last 7d)"
Reasoning: 
- Numerator: Spend in last 7 days.
- Denominator: Spend in prior 6 months. "Prior" implies disjoint from recent.
- Use `window_exclude` on the denominator to ensure it does not overlap with the numerator.
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{"name": "spend_7d", "aggregation": "sum", "attribute": "send_amount", "window": "7d"}},
    {{"name": "spend_prior_6m", "aggregation": "sum", "attribute": "send_amount", "window": "6m", "window_exclude": "7d"}},
    {{"name": "spend_ratio", "aggregation": "ratio", "derived_from": ["spend_7d", "spend_prior_6m"]}}
  ],
  "conditions": [
    {{"computed_attr_name": "spend_ratio", "operator": ">", "value": 3.0, "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 9: Stateless
Description: "Transactions to Iran with send amount over $100"
Output:
{{
  "rule_type": "stateless",
  "computed_attrs": [],
  "conditions": [
    {{"attribute": "receive_country_code", "operator": "in", "value": ["Iran"], "logical_connector": "AND", "computed_attr_name": null}},
    {{"attribute": "send_amount", "operator": ">", "value": 100, "logical_connector": "AND", "computed_attr_name": null}}
  ]
}}

### Example 10: Condition Groups
Description: "Alert if account < 30 days AND sends to Iran, OR account > 180 days AND sends > $5000"
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{"name": "acct_age", "aggregation": "days_since_first", "attribute": "created_at"}},
    {{"name": "iran_count", "aggregation": "count", "attribute": "transaction_id", "filters": [{{"attribute": "receive_country_code", "operator": "in", "value": ["Iran"]}}]}},
    {{"name": "total_send", "aggregation": "sum", "attribute": "send_amount"}}
  ],
  "conditions": [
    {{"computed_attr_name": "acct_age", "operator": "<", "value": 30, "condition_group": 0, "condition_group_connector": "OR", "attribute": null, "aggregation": null, "window": null, "filters": null}},
    {{"computed_attr_name": "iran_count", "operator": ">", "value": 0, "condition_group": 0, "attribute": null, "aggregation": null, "window": null, "filters": null}},
    {{"computed_attr_name": "acct_age", "operator": ">=", "value": 180, "condition_group": 1, "attribute": null, "aggregation": null, "window": null, "filters": null}},
    {{"computed_attr_name": "total_send", "operator": ">", "value": 5000, "condition_group": 1, "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

### Example 11: Window Exclusion (window_exclude)
Description: "Alert if count of transactions in last 13 months excluding last 7 days = 0"
Reasoning: "excluding last 7 days" means use window_exclude on ONE CA.
Output:
{{
  "rule_type": "behavioral",
  "relevant_attributes": ["transaction_id", "created_at"],
  "computed_attrs": [
    {{"name": "txn_count_excl_last_7d", "aggregation": "count", "attribute": "transaction_id", "window": "13m", "window_exclude": "7d"}}
  ],
  "conditions": [
    {{"computed_attr_name": "txn_count_excl_last_7d", "operator": "==", "value": 0, "logical_connector": "AND", "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ],
  "raw_expression": "count of transactions in last 13 months excluding last 7 days == 0",
  "high_risk_countries": []
}}

### Example 12: Divisibility Filter - NEW
Description: "Alert if count of transactions where amount is divisible by 100 > 5"
Reasoning: "divisible by 100" maps to `attribute % 100 == 0`.
Output:
{{
  "rule_type": "behavioral",
  "computed_attrs": [
    {{
      "name": "divisible_count", "aggregation": "count", "attribute": "transaction_id",
      "filters": [{{"attribute": "send_amount", "operator": "%", "value": 0, "modulus_base": 100, "connector": "AND"}}]
    }}
  ],
  "conditions": [
    {{"computed_attr_name": "divisible_count", "operator": ">", "value": 5, "attribute": null, "aggregation": null, "window": null, "filters": null}}
  ]
}}

---

# 7. VERIFY BEFORE OUTPUT

**FOR BEHAVIORAL RULES:**
1. Check `computed_attrs` — is it non-empty? ✓
2. **Check `conditions` — does EVERY condition have `computed_attr_name` set?**
   - If `computed_attr_name` is null → **INVALID**.
3. **Check Filters — do they use STRICT operators?**
   - If `operator` is "custom" → **INVALID**. Use `==`, `>`, `%`, etc.
4. **Check Dependencies — are all CA names in filters defined?**
   - If filter uses `sender_age`, ensure a CA named `sender_age` exists.
5. **Check Window Exclusion — "without/excluding last X"?**
   - If rule says "excluding last X" or "without last X": use `window_exclude` on a **single CA**.
6. **Check Ratio Disjointness — if comparing recent vs prior periods:**
   - Does the denominator (prior) exclude the numerator (recent) window?
   - If ratio > 1.0 is expected, windows MUST be disjoint (use `window_exclude`).

---

# 8. TARGET DESCRIPTION
Parse the following rule:
{description}
"""