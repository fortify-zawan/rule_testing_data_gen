"""Prompt strings for llm/sequence_generator.py."""

# SYSTEM = """You are generating realistic synthetic bank account transaction sequences for AML rule testing.
# Output ONLY valid JSON — no explanation, no markdown fences."""



SYSTEM = """You are generating realistic synthetic bank account transaction sequences for AML rule testing.
Output ONLY valid JSON — no explanation, no markdown fences."""


STATELESS_PROMPT = """Generate a set of test transactions for AML rule testing.

{schema_context}

Rule: {raw_expression}
Relevant attributes (use ONLY these canonical names from the schema above): {attributes}

Risky prototype (anchor for risky transactions):
{risky_proto}

Genuine prototype (anchor for genuine transactions):
{genuine_proto}

Generate exactly {n_risky} RISKY and {n_genuine} GENUINE transactions. No other transactions.

Requirements:
- Each risky transaction must reflect the risky prototype's character (values that trigger the rule).
- Each genuine transaction must reflect the genuine prototype's character (values that do NOT trigger).
- Use realistic dates in YYYY-MM-DD format for the created_at field (spread over 1-3 months).
- Vary the exact values naturally — don't make all transactions identical.
- Total count must be exactly {n_risky} + {n_genuine} = {n_total} transactions.
- IMPORTANT — Attribute keys: use ONLY the canonical attribute names listed in the schema above as JSON keys.
  Do NOT use aliases (e.g. use "send_amount" not "amount", "receive_country_code" not "country").
- IMPORTANT — Country values: use full country names exactly as they appear in the rule
  (e.g. "Iran" not "IR", "North Korea" not "KP"). The engine matches by exact string.

Output this exact JSON (a list of transactions):
[
  {{
    "id": "t-001",
    "tag": "risky" or "genuine",
    "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, "attr2": value, ...}}
  }},
  ...
]

All relevant attributes ({attributes}) must be present in every transaction's attributes dict.
Non-relevant fields can be omitted."""

# BEHAVIORAL_PROMPT = """Generate a realistic account transaction sequence for AML behavioral rule testing.

# --- TASK CONTEXT ---
# The following inputs define what you must generate:

# SCHEMA — canonical attribute names and allowed values you must use:
# {schema_context}

# RULE — the AML rule this sequence is being tested against:
# {raw_expression}

# RELEVANT ATTRIBUTES — only these fields (plus created_at) should appear in each transaction:
# {attributes}

# HIGH-RISK COUNTRIES — use these exact strings when setting country attributes:
# {high_risk_countries}

# SEQUENCE TYPE: {scenario_type}
#   - risky   → sequence must cause the rule to FIRE
#   - genuine → sequence must cause the rule to NOT FIRE

# {intent_section}
# {feedback_history_section}
# {feedback_section}
# --- END TASK CONTEXT ---

# --- SECTION A — Aggregate-first reasoning (follow these steps before generating) ---
# Think step by step:
# 1. Read the rule carefully. Identify the LOGICAL STRUCTURE.
#    - Is it a simple list of conditions (AND logic)?
#    - Is it a choice of conditions (OR logic)?
#    - For OR logic: You only need to satisfy ONE branch to make the rule FIRE (Risky).
#      You must FAIL ALL branches to make the rule NOT FIRE (Genuine).
#    - Decide which branch you will target for the scenario type.
# 2. For each aggregate condition, decide on concrete target values — how many transactions,
#    of what approximate sizes, are needed in the motif layer to satisfy or avoid the condition?
# 3. If the rule compares two aggregates from different time windows (e.g. recent 7d vs prior 30d),
#    remember that all windows are anchored at the date of the LAST transaction you generate.
#    Plan the final background transaction date first, then place filter-matching motif transactions
#    within the correct window relative to that anchor date. Do NOT mix up which transactions go
#    in which window — a "recent 7d" transaction must be within 7 days of the last transaction.
# 4. If any condition has a `group_by` attribute (e.g. group_by=recipient_id): the aggregate is
#    evaluated independently for each distinct group value. Generate transactions with at least
#    2–3 distinct values of the group_by attribute.
#    - For RISKY: concentrate enough motif transactions in ONE group so that group alone crosses
#      the threshold. Other groups should stay below the threshold for realism.
#    - For GENUINE: ensure NO single group accumulates enough to cross the threshold — spread
#      motif transactions across multiple groups.
# 5. If any condition uses `shared_distinct_count` (the raw_expression contains "shared_distinct_count"
#    or mentions "share email/phone" or similar):
#    - For RISKY: generate ≥2 distinct user_ids in the target recipient group with the SAME value
#      for at least one of the link attributes (e.g. user_A and user_B both have email="shared@x.com").
#      Other recipient groups should have senders with unique PII — no unintended sharing.
#    - For GENUINE: ensure every user_id in EVERY recipient group has a UNIQUE value for ALL link
#      attributes — no two senders share email, phone, or any other link attribute.
#    - Always include the link attribute fields (e.g. email, phone) in every transaction's attributes.
# 6. What does a realistic background for this account type look like?
# The rule fires on AGGREGATES — individual transactions should reflect real account history,
# not a direct demonstration of the rule.

# --- SECTION B — Background + Motif composition ---
# Structure the sequence as two interleaved layers:
# - BACKGROUND: Normal account activity unrelated to the rule's filter. Use different
#   destinations, amounts, and patterns. Include enough background transactions to make
#   the account history look real — the right number depends on the rule, not a fixed ratio.
# - MOTIF: The rule-relevant subset. Let the rule's own thresholds determine how many motif
#   transactions are needed. If the rule requires counting 25 transactions to a single
#   recipient, generate at least 25 such transactions — do not cap motif count artificially.
#   For RISKY: sized and placed to push the aggregate past the threshold.
#   For GENUINE: sized to stay just below the threshold, but not obviously so.
# Interleave motif transactions across the timeline. Do NOT append them all at the end.

# --- SECTION C — Customer archetype ---
# If user intent is provided, infer a consistent customer profile (e.g. migrant worker,
# small business owner, student, retail trader). Hold this profile across the full sequence:
# transaction sizes, frequency, destinations, and timing should all be consistent with
# the account type. If no intent is provided, infer a plausible profile from the rule's
# attributes and generate accordingly.

# --- SECTION D — Value variance ---
# Do not cluster amounts near the threshold. Background amounts should vary freely.
# Motif amounts should vary naturally (some higher, some lower) — the aggregate target
# must be met in total, but individual values should look organic, not robotic.
# Exception: if the intent explicitly implies structuring, clustering is acceptable.

# --- SECTION E — Temporal realism ---
# Order transactions by date with realistic spacing:
# - Most activity on weekdays
# - Occasional same-day pairs (normal for active accounts)
# - Include 1-2 quiet periods of 3-7 days with no transactions
# - The timeline must be long enough to cover all of the rule's time windows. If the rule
#   has a single window (e.g. 30 days), span at least that many days. If it has multiple
#   non-overlapping windows (e.g. a recent period + a prior period), span their combined
#   length and place motif transactions in the correct window for each.
# - Background transactions can extend freely across the full timeline.
# - The LAST transaction in the sequence (most recent date) MUST be a background transaction.
#   IMPORTANT — window anchoring: all time windows are measured backwards from the date of
#   that final background transaction (latest_date). If the rule has a "recent 7d" window,
#   your filter-matching motif transactions for that window must be dated within 7 days BEFORE
#   the final background transaction — not 7 days before today or some other reference point.
#   Decide the final background transaction date FIRST, then work backwards to place motif
#   transactions in the correct windows relative to that date.

# --- Hard requirements ---
# - Generate as many transactions as the rule requires. Let the rule's thresholds determine
#   the minimum — if a condition needs a count of 25 to a single recipient, you need at least
#   25 such transactions. Add enough background beyond that to make the account look realistic.
# - For RISKY: must make at least one OR-branch true (choose one branch and satisfy it fully).
# - For GENUINE: must make every OR-branch false (ensure each branch fails).
# - Use realistic dates in YYYY-MM-DD format for the created_at field.
# - IMPORTANT — Attribute keys: use ONLY the canonical attribute names listed in the schema above as JSON keys.
#   Do NOT use aliases (e.g. use "send_amount" not "amount", "receive_country_code" not "country" or "destination_country").
#   Only populate the relevant attributes ({attributes}) plus created_at.
# - IMPORTANT — Country values: use the EXACT same string as listed under HIGH-RISK COUNTRIES above
#   (e.g. if high_risk_countries = ["Iran"], set receive_country_code to "Iran" — NOT "IR" or "IRN").
#   The validation engine matches both attribute keys and country values by exact string comparison.

# Output a JSON list of transactions, ordered by date:
# [
#   {{
#     "id": "t-001",
#     "tag": "{scenario_type}",
#     "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, "attr2": value, ...}}
#   }},
#   ...
# ]"""

# CONFLICT_SECTION_TEMPLATE = """
# --- SECTION F — Feedback conflict check ---
# You have user instructions in the USER INSTRUCTIONS block above.
# Before finalising your output, ask yourself: given what this rule requires for a
# {scenario_type} outcome, does any instruction tell you to generate transactions that
# would push the sequence away from that outcome?

#   RISKY   → conflict = instruction makes it harder or impossible for the rule to FIRE
#   GENUINE → conflict = instruction makes it more likely for the rule to FIRE

# IMPORTANT — for RATIO conditions with non-overlapping windows (recent period / prior period):
#   The rule fires when RECENT aggregate / PRIOR aggregate > threshold.
#   Reducing the PRIOR period (denominator) → ratio goes UP → HELPS risky fire → NOT a conflict.
#   Increasing the PRIOR period (denominator) → ratio goes DOWN → HURTS risky → IS a conflict.
#   Reducing the RECENT period (numerator)   → ratio goes DOWN → HURTS risky → IS a conflict.
#   Think carefully about which time period an instruction affects before flagging it.

# Flag only clear directional conflicts — where following the instruction would materially
# prevent the expected validation outcome. Ignore style preferences, realism guidance, or
# value constraints that don't affect aggregate direction.

# IMPORTANT — still honor all user instructions when generating transactions. Do not
# silently override them. Just flag what conflicts so the user can be informed.

# Required output format when user instructions are present (wrap in an object, not a bare array):
# {{{{
#   "transactions": [ ... ],
#   "feedback_conflicts": [
#     {{{{
#       "feedback_instruction": "<the conflicting instruction, quoted verbatim>",
#       "conflicting_condition": "<rule condition affected, e.g. 'sum(receive_amount) > 500'>",
#       "explanation": "<one sentence: why following this instruction causes the {scenario_type} validation to fail>"
#     }}}}
#   ]
# }}}}
# Use [] for feedback_conflicts if there are none."""


BEHAVIORAL_PROMPT = """Generate a realistic account transaction sequence for AML behavioral rule testing.

--- PRIORITY ORDER ---                                                        
1. Mechanical correctness: Aggregates MUST satisfy (risky) or avoid (genuine) ALL required conditions.
2. Realism: Make it look natural ONLY after correctness is guaranteed.
Never sacrifice correctness for realism.
--- END PRIORITY ORDER ---

--- TASK CONTEXT ---
SCHEMA: {schema_context}
RULE: {raw_expression}
RELEVANT ATTRIBUTES: {attributes} (plus created_at)
HIGH-RISK COUNTRIES: {high_risk_countries}
SEQUENCE TYPE: {scenario_type}
  - risky   → sequence must cause the rule to FIRE
  - genuine → sequence must cause the rule to NOT FIRE
{intent_section}
{feedback_history_section}
{feedback_section}
--- END TASK CONTEXT ---

--- SECTION A — Planning Steps (follow internally before generating) ---

STEP A1 — Parse all conditions:
List each condition: aggregate type (sum/count/average/distinct_count/ratio/difference), window, filters, threshold, group_by.

STEP A2 — Handle OR condition groups:
If rule has multiple condition groups connected by OR:
  - RISKY: pick the EASIEST group to satisfy (fewest transactions/lowest totals). 
    Only need to satisfy ALL conditions within ONE group.
    
    **To compare group difficulty**: Calculate minimum transactions and totals for each group, pick the lower one.
    
  - GENUINE: must FAIL every group. Stay below threshold in ALL groups.
If only one group (all AND): must satisfy/avoid every condition.

STEP A3 — Calculate exact targets:
For each condition in target group:
  - COUNT > N: need exactly N+1 or more matching transactions
  - SUM > X: need total strictly greater than X
  - AVERAGE > X: need average = sum/count > X
    * Set count first, then ensure sum > X × count
    * Add 5% safety margin: aim for average = threshold × 1.05
  - DISTINCT_COUNT > N: need N+1 distinct values
  - RATIO recent/prior > T: 
    * Ensure prior > 0 (denominator never zero)
    * For RISKY: set recent ≥ floor(T × prior) + 1
    * For GENUINE: keep recent < T × prior
  - DIFFERENCE A - B > N:
    * Ensure A > B + N
    * For RISKY: make A sufficiently larger than B

STEP A4 — Handle group_by logic:
If condition has group_by (e.g., group_by=recipient_id):
  - Aggregate evaluated independently per distinct group value
  - RISKY: pick ONE target group value (e.g., recipient_id="A"). 
    ALL motif transactions go to this SAME value.
  - GENUINE: spread transactions across 3+ group values so NO single group crosses threshold.

STEP A5 — Handle "new entity" conditions:
If condition checks "new recipient", "new country", "first transaction to X":
  - Implemented as days_since_first with group_by
  - "New" means entity appears EXACTLY ONCE in the sequence
  
  CRITICAL — Multiple "new" requirements:
  If rule requires BOTH "new recipient" AND "new country":
  - Each motif transaction needs a UNIQUE recipient_id AND a UNIQUE country
  - Transaction 1: recipient_A, country_X
  - Transaction 2: recipient_B, country_Y (different recipient AND country)
  - Transaction 3: recipient_C, country_Z (different recipient AND country)
  
  DO NOT repeat the same recipient_id or country across motif transactions.

STEP A6 — Handle multi-filter conditions:
If condition has multiple filters (e.g., sender_age >= 60 AND new_recipient AND new_country):
  - EVERY motif transaction must satisfy ALL filters simultaneously
  - A transaction failing ANY filter won't count toward the aggregate
  
  For each planned motif transaction, verify:
  * sender_age >= 60 → date_of_birth at least 60 years before transaction date
  * new_recipient → recipient_id appears only once in entire sequence
  * new_country → receive_country_code appears only once in entire sequence
  * nationality != country → nationality_code, send_country_code, receive_country_code all different

STEP A7 — Set sender attributes correctly:
For age-based filters:
  - Set date_of_birth to be at least 60 years before transaction dates
  - Example: transactions in 2024-01 → date_of_birth ≤ 1964-01
  - CRITICAL: ALL transactions must have SAME date_of_birth, nationality_code, send_country_code (same sender)

STEP A8 — Handle derived/difference computed attributes:
If condition uses DIFFERENCE of two distinct_count CAs (e.g., distinct_senders - distinct_addresses > 4):
  - This means multiple senders are SHARING the same attribute value
  - For RISKY: 
    * Need distinct_senders > distinct_attribute + threshold
    * Generate transactions from multiple different user_ids
    * Make some senders share the same address/email/phone/device_id
    * Example: distinct_senders - distinct_addresses > 4
      → Need 10 senders using 5 addresses → 10 - 5 = 5 > 4 ✓
  - For GENUINE:
    * Make each sender have unique attribute values (no sharing)

STEP A9 — Plan time windows:
  - LAST transaction in sequence = anchor date
  - All windows measured backwards from this anchor
  - Place motif transactions within correct window relative to anchor
  - For "new entity" checks: typically windowless (checks ALL history)

STEP A10 — Plan background:
  - Background must NOT satisfy any filter conditions
  - Use different recipients, countries, or failing filter values
  - Never interfere with motif hard targets from Step A3

--- SECTION B — Self-verification (MANDATORY before output) ---
Verify each check:

CHECK 1 — COUNT/SUM/AVERAGE TARGETS:
  Does motif count meet threshold? Does sum exceed threshold?
  For average: sum/count > threshold?

CHECK 2 — TIME WINDOW:
  Are all motif transactions within the correct window relative to last transaction date?

CHECK 3 — MULTI-FILTER COMPLETENESS:
  For each motif transaction: does it satisfy EVERY filter condition?
  If ANY filter fails, transaction won't count.

CHECK 4 — NEW ENTITY UNIQUENESS:
  For "new recipient": does each recipient_id appear EXACTLY ONCE?
  For "new country": does each country appear EXACTLY ONCE?
  Count occurrences — no duplicates allowed.

CHECK 5 — SENDER ATTRIBUTE CONSISTENCY:
  Is date_of_birth the same on ALL transactions?
  Is nationality_code the same on ALL transactions?
  Is send_country_code the same on ALL transactions?

CHECK 6 — GROUP ISOLATION:
  If group_by set: does only target group cross threshold (RISKY) or none (GENUINE)?

CHECK 7 — DERIVED CA CORRECTNESS:
  For difference of distinct_count: 
  - distinct_senders > distinct_attribute + threshold?
  - Multiple senders share same attribute value?

If ANY check fails, fix before outputting.

--- Hard requirements ---
- Use ONLY canonical attribute names from schema as JSON keys
- Use EXACT high-risk country strings (e.g., "Iran" not "IR")
- Dates in YYYY-MM-DD format
- "last month" = 30 days, "last 6 months" = 180 days
- Background must FAIL filter conditions (different status/country/etc.)
- Sender attributes (date_of_birth, nationality_code, send_country_code) must be SAME on all transactions
- Last transaction MUST be background (not motif)

Output a JSON list of transactions, ordered by date:
[
  {{
    "id": "t-001",
    "tag": "{scenario_type}",
    "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, "attr2": value, ...}}
  }},
  ...
]"""

CONFLICT_SECTION_TEMPLATE = """
--- SECTION C — Feedback conflict check ---                                    
You have user instructions in the USER INSTRUCTIONS block above.
Before finalising your output, ask yourself: given what this rule requires for a
{scenario_type} outcome, does any instruction tell you to generate transactions that
would push the sequence away from that outcome?

  RISKY   → conflict = instruction makes it harder or impossible for the rule to FIRE
  GENUINE → conflict = instruction makes it more likely for the rule to FIRE

IMPORTANT — for RATIO conditions with non-overlapping windows (recent period / prior period):
  The rule fires when RECENT aggregate / PRIOR aggregate > threshold.
  Reducing the PRIOR period (denominator) → ratio goes UP → HELPS risky fire → NOT a conflict.
  Increasing the PRIOR period (denominator) → ratio goes DOWN → HURTS risky → IS a conflict.
  Reducing the RECENT period (numerator)   → ratio goes DOWN → HURTS risky → IS a conflict.
  Think carefully about which time period an instruction affects before flagging it.

Flag only clear directional conflicts — where following the instruction would materially
prevent the expected validation outcome. Ignore style preferences, realism guidance, or
value constraints that don't affect aggregate direction.

IMPORTANT — still honor all user instructions when generating transactions. Do not
silently override them. Just flag what conflicts so the user can be informed.

Required output format when user instructions are present (wrap in an object, not a bare array):
{{
  "transactions": [ ... ],
  "feedback_conflicts": [
    {{
      "feedback_instruction": "<the conflicting instruction, quoted verbatim>",
      "conflicting_condition": "<rule condition affected, e.g. 'sum(receive_amount) > 500'>",
      "explanation": "<one sentence: why following this instruction causes the {scenario_type} validation to fail>"
    }}
  ]
}}
Use [] for feedback_conflicts if there are none."""