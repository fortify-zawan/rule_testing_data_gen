"""Prompt strings for llm/sequence_corrector.py."""

# SYSTEM = """You are correcting AML test data so that transactions satisfy rule conditions exactly.
# Output ONLY valid JSON — no explanation, no markdown fences."""

SYSTEM = """You are correcting AML test data so that transactions satisfy rule conditions exactly.
Output ONLY valid JSON — no explanation, no markdown fences."""


STATELESS_CORRECT_PROMPT = """A transaction failed AML rule validation. Correct its attribute values.

Rule: {raw_expression}
Transaction tag: {tag} (risky = should trigger rule, genuine = should NOT trigger rule)
Current attribute values: {attributes}

Failed conditions:
{failed_conditions}

Anchor prototype (the intended character of this transaction type):
{prototype}

Instructions:
- Only change the attributes that caused the failure.
- Keep all other attributes as close to the current values as possible.
- The corrected transaction must {expectation}.

Output ONLY a JSON dict of the corrected attributes (same keys as current attributes):
{{"attr1": value, "attr2": value, ...}}"""

# BEHAVIORAL_CORRECT_PROMPT = """A behavioral AML rule test sequence failed validation. Repair it.

# --- TASK CONTEXT ---
# The following inputs define the sequence you are repairing:

# SCHEMA — canonical attribute names and allowed values you must use:
# {schema_context}

# RULE — the AML rule this sequence is being tested against:
# {raw_expression}

# SEQUENCE TYPE: {scenario_type}
#   - risky   → after repair, the sequence MUST cause the rule to FIRE
#   - genuine → after repair, the sequence MUST cause the rule to NOT FIRE

# HIGH-RISK COUNTRIES — use these exact strings when setting country attributes:
# {high_risk_countries}

# USER INTENT: {intent}
# --- END TASK CONTEXT ---

# --- VALIDATION RESULTS ---
# These are the aggregate values computed from the current sequence and the conditions that failed:

# Computed aggregates:
# {aggregates}

# Failed conditions (what must change):
# {failed_conditions}
# --- END VALIDATION RESULTS ---

# {repair_guidance_section}
# {feedback_history_section}

# --- SECTION A — Repair planning (think before changing anything) ---
# Before modifying any transaction:
# 1. Read the VALIDATION RESULTS above. Identify which transactions are MOTIF transactions
#    (those matching the rule's filter) and which are BACKGROUND.
# 2. Identify the LAST transaction in the sequence (most recent date) — this is the anchor.
#    All time windows are measured backwards from that date (latest_date). A "recent 7d" window
#    means transactions dated within 7 days BEFORE the last transaction's date.
# 3. Read the REPAIR GUIDANCE above (if present). Use the shortfall arithmetic to determine
#    exactly how much needs to change and in which time window.
# 4. Plan the minimum changes needed — adjust amounts or dates of existing motif transactions
#    before adding new ones. Only add new motif transactions if the shortfall cannot be covered
#    by adjusting what already exists.
# 5. Verify in your head that the planned changes satisfy the condition(s) after repair.
#    Do not proceed until your plan adds up.

# --- SECTION B — Preservation constraint (mandatory) ---
# The background transactions in the existing sequence represent realistic account noise and
# must be preserved. Do NOT replace, remove, or redate background transactions (those whose
# attributes do not match the rule's filter attribute/value and do not directly affect the
# failing aggregate).
# Only adjust MOTIF transactions — those that match the rule's filter and directly contribute
# to the failing aggregate. If the shortfall cannot be fixed by adjusting existing motif
# transactions, add as many new motif transactions as the shortfall requires — do not
# limit the number artificially.
# The account narrative, background activity pattern, and temporal spread must remain intact.
# Never modify background transactions regardless of period.

# If the failing condition uses `shared_distinct_count` (link_attribute is set):
# - RISKY fix: find the target recipient group. Give ≥2 distinct user_ids in that group the SAME
#   value for one link attribute (e.g. user_A and user_B both get email="shared@example.com").
#   Do NOT change their other attributes — only adjust the link attribute values.
# - GENUINE fix: ensure no two user_ids in any recipient group share any link attribute value.
#   Make each sender's email/phone/device_id unique across the whole sequence.

# If the failing condition uses GROUP BY (e.g. group_by=recipient_id): the validation reports
# the worst-case group value (max for >, min for <). To repair:
# - RISKY: concentrate enough motif transactions in ONE group to push that group's aggregate
#   above the threshold. Do not spread motif transactions evenly — focus them on a single group.
# - GENUINE: ensure no single group accumulates enough to trigger — if one group is too high,
#   move some of its motif transactions to a different group value (change the group_by attribute).

# --- SECTION C — Customer archetype ---
# Maintain a consistent customer profile across the repaired sequence. Transaction sizes,
# frequency, destinations, and timing should remain consistent with the account type
# established in the original sequence.

# --- SECTION D — Value variance ---
# Motif amounts should still vary naturally after correction — do not set all motif
# transactions to the same amount just to hit the aggregate target. The aggregate must
# be met in total, but individual values should look organic.

# --- SECTION E — Temporal realism ---
# Preserve the existing date structure. Do not compress transactions into a shorter window
# or make the spacing uniform. If adding new motif transactions, slot them into gaps in
# the existing timeline.

# --- Hard requirements ---
# - For RISKY: all aggregate conditions must be satisfied after repair.
# - For GENUINE: the complete set of conditions must NOT be satisfied after repair.
# - The LAST transaction in the sequence (most recent date) MUST be a background transaction.
#   This anchors the reference date used for all window calculations to a neutral account event.
# - IMPORTANT — use ONLY the canonical attribute names from the schema above as JSON keys.

# Output the full repaired transaction list (same JSON format, all transactions), ordered by date:
# [
#   {{
#     "id": "t-001",
#     "tag": "{scenario_type}",
#     "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, ...}}
#   }},
#   ...
# ]"""



BEHAVIORAL_CORRECT_PROMPT = """You are correcting a failed AML transaction sequence by outputting ONLY the changes needed.

--- EXISTING SEQUENCE ({n_transactions} transactions, anchor date: {anchor_date}, next available ID: {next_id}) ---
{transaction_table}
--- END EXISTING SEQUENCE ---

--- TASK ---
SCHEMA — canonical attribute names and allowed values you must use:
{schema_context}

RULE — the AML rule this sequence is being tested against:
{raw_expression}

SEQUENCE TYPE: {scenario_type}
  - risky   → after repair, the sequence MUST cause the rule to FIRE
  - genuine → after repair, the sequence MUST cause the rule to NOT FIRE

HIGH-RISK COUNTRIES — use these exact strings when setting country attributes:
{high_risk_countries}

USER INTENT: {intent}
--- END TASK ---

--- CURRENT VALIDATION STATE ---
Computed aggregates (from the existing sequence above):
{aggregates}

Failed conditions (what must change):
{failed_conditions}
--- END CURRENT VALIDATION STATE ---

{repair_guidance_section}
{feedback_history_section}

--- SECTION A — Repair planning (MANDATORY INTERNAL CHECKLIST — do not output any of this section) ---

STEP A1 — Understand the condition groups:
  Read the failed conditions. Determine if the rule has OR groups:
  - If YES:
    * RISKY: you only need to fix ONE group. Pick the group requiring the fewest changes.
    * GENUINE: you must ensure ALL groups fail (each group has at least one failed condition).
  - If NO: fix every failed condition.
  Internal decision (do not output): choose target OR-group [X] and the reason.

STEP A2 — Calculate exact shortfalls:
  Using computed aggregates above:
  - COUNT shortfall: (threshold + 1) - current_count
  - SUM shortfall: (threshold + 0.01) - current_sum (or otherwise ensure strictly > threshold)
  - AVERAGE shortfall: calculate required sum > threshold × count
    * Add 5% buffer: aim for sum = threshold × count × 1.05
    * Fix: increase amounts on existing matching transactions
  - DISTINCT_COUNT shortfall: (threshold + 1) - current_distinct
  - DIFFERENCE shortfall: (threshold + 1) - (current_A - current_B)
  Internal arithmetic (do not output): record gaps per condition.

STEP A2b — Diagnose root cause:

DIAGNOSIS 1 — "New entity" failures (days_since_first == 0):
  If the rule implies "new recipient", "new country", "first time":
  
  FAILURE MODE A — Newness contamination:
    - Symptom: aggregates are much lower than expected (often 0 or 1).
    - Cause: supposed "new" entity values appear on a date BEFORE the motif day.
    - RISKY fix:
      * Choose a motif_day within the window.
      * Ensure ZERO earlier-day transactions use the motif entity values.
      * MODIFY earlier transactions to use different entity values if needed.
      * Add motif transactions on motif_day to satisfy count/sum threshold.
    - GENUINE fix:
      * Ensure there IS an earlier-day transaction to the entity.

DIAGNOSIS 2 — Multi-filter aggregate is 0:
  - Root cause: NO transactions satisfy ALL filter conditions simultaneously.
  - Check each filter: age, country, status, etc.
  - Fix: Add transactions meeting EVERY filter.

DIAGNOSIS 3 — Age condition failing:
  - Fix: change date_of_birth consistently across ALL transactions.

DIAGNOSIS 4 — Country relationship failing:
  - Fix: adjust nationality_code / send_country_code / receive_country_code.

DIAGNOSIS 5 — Average too low (count OK):
  - Cause: Sum too low relative to count
  - Fix: Increase amounts on existing matching transactions
  - Calculate: required sum > threshold × count × 1.05

DIAGNOSIS 6 — Difference of distinct_count failing:
  Example: distinct_senders - distinct_addresses > 4
  - Check: distinct_senders = ? distinct_addresses = ?
  - Cause: Not enough senders, OR too many distinct addresses
  - Fix:
    * Add more distinct senders (new user_ids)
    * Make senders share same address/email/phone/device
    * Example: 10 senders, 10 addresses → diff = 0
      Change to: 10 senders, 5 addresses → diff = 5 > 4 ✓

STEP A3 — Decide repair strategy per condition:
  A) COUNT shortfall: Add new matching transactions.
     If newness required, add multiple on SAME motif_day.

  B) SUM shortfall: Increase amounts of existing matching motif transactions.
     Otherwise add new matching motif transactions.

  C) AVERAGE shortfall: Increase amounts on existing matching transactions.
     Calculate required sum with 5% buffer.

  D) DISTINCT_COUNT shortfall: Add transactions with new distinct values.

  E) DIFFERENCE of distinct_count shortfall:
     - Add more distinct senders (new user_ids)
     - Make senders share attribute values

  F) Multi-filter aggregate = 0: Add transactions satisfying EVERY filter.

STEP A4 — Plan group_by targeting:
  If the failing condition uses group_by:
  - For RISKY: concentrate fixes in one group value.
  - For GENUINE: disperse across group values.

STEP A5 — Plan dates for new transactions:
  - Anchor date is {anchor_date}. All windows measured backwards from it.
  - All added motif transactions must be within the relevant time window.
  - If newness is required:
    * Choose one motif_day within the window.
    * Cluster multiple motif transactions on motif_day.
    * Ensure no earlier-day transaction uses the motif entity values.

STEP A6 — Verify your plan adds up:
  Re-check arithmetic: current aggregate + changes crosses (risky) or avoids (genuine) thresholds.

--- SECTION B — Scope of changes ---
Only output changes to transactions that directly affect the failing aggregates.
Do NOT include transactions you are not changing — they are preserved automatically.

--- SECTION B2 — Computed attribute repair ---
If failing condition references computed attributes, never set CA names as transaction keys.
Repair by modifying underlying source attributes.

For difference of distinct_count:
- Add transactions with different user_ids
- Set same address/email/phone/device for multiple senders to create sharing

--- SECTION C — Self-verification (MANDATORY INTERNAL CHECKLIST — do not output) ---
CHECK 1 — COUNT: after changes, does matching motif count meet (risky) or stay below (genuine) threshold?
CHECK 2 — SUM: after changes, does matching motif sum meet (risky) or stay below (genuine) threshold?
CHECK 3 — AVERAGE: after changes, does average = sum/count > threshold?
CHECK 4 — WINDOW: are all matching motif transactions within the window?
CHECK 5 — FILTERS: do matching motif transactions satisfy ALL required filters simultaneously?
CHECK 6 — NEWNESS: if newness required, are there ZERO earlier-day transactions with motif entity values?
CHECK 7 — DIFFERENCE: for difference of distinct_count, is distinct_senders > distinct_attribute + threshold?
CHECK 8 — ANCHOR INTACT: {anchor_date} remains the latest date.

If ANY check fails, revise before outputting.

--- Hard requirements ---
- For RISKY: all aggregate conditions in at least one OR-group must be satisfied.
- For GENUINE: no OR-group should be fully satisfied.
- The LAST transaction in the sequence (most recent date) MUST NOT be a motif transaction.
- Use ONLY canonical attribute names from schema above as JSON keys.
- New transaction IDs must start at {next_id} and continue sequentially.
- Add as many transactions as needed — do not cap artificially.
- Sender-level attributes (date_of_birth, nationality_code, send_country_code) must remain consistent across all transactions.

IMPORTANT OUTPUT RULE:
Output ONLY the following JSON object. No explanation, no markdown fences, no extra keys.
{{
  "add": [
    {{"id": "{next_id}", "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, "...": "..."}}}},
    ...
  ],
  "modify": {{
    "t-NNN": {{"attr1": new_value, "...": "..."}},
    ...
  }}
}}
- "add": NEW transactions to insert (with full attributes).
- "modify": EXISTING transaction ID → only attributes that change (partial update).
- If no additions are needed, use "add": [].
- If no modifications are needed, use "modify": {{}}.
- Do NOT include transactions you are not changing."""