"""Prompt strings for llm/sequence_corrector.py."""

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

BEHAVIORAL_CORRECT_PROMPT = """A behavioral AML rule test sequence failed validation. Repair it.

--- TASK CONTEXT ---
The following inputs define the sequence you are repairing:

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
--- END TASK CONTEXT ---

--- VALIDATION RESULTS ---
These are the aggregate values computed from the current sequence and the conditions that failed:

Computed aggregates:
{aggregates}

Failed conditions (what must change):
{failed_conditions}
--- END VALIDATION RESULTS ---

{repair_guidance_section}
{feedback_history_section}

--- SECTION A — Repair planning (think before changing anything) ---
Before modifying any transaction:
1. Read the VALIDATION RESULTS above. Identify which transactions are MOTIF transactions
   (those matching the rule's filter) and which are BACKGROUND.
2. Identify the LAST transaction in the sequence (most recent date) — this is the anchor.
   All time windows are measured backwards from that date (latest_date). A "recent 7d" window
   means transactions dated within 7 days BEFORE the last transaction's date.
3. Read the REPAIR GUIDANCE above (if present). Use the shortfall arithmetic to determine
   exactly how much needs to change and in which time window.
4. Plan the minimum changes needed — adjust amounts or dates of existing motif transactions
   before adding new ones. Only add new motif transactions if the shortfall cannot be covered
   by adjusting what already exists.
5. Verify in your head that the planned changes satisfy the condition(s) after repair.
   Do not proceed until your plan adds up.

--- SECTION B — Preservation constraint (mandatory) ---
The background transactions in the existing sequence represent realistic account noise and
must be preserved. Do NOT replace, remove, or redate background transactions (those whose
attributes do not match the rule's filter attribute/value and do not directly affect the
failing aggregate).
Only adjust MOTIF transactions — those that match the rule's filter and directly contribute
to the failing aggregate. If the shortfall cannot be fixed by adjusting existing motif
transactions, you may add 1-2 new motif transactions or adjust their amounts.
The account narrative, background activity pattern, and temporal spread must remain intact.
Never modify background transactions regardless of period.

--- SECTION C — Customer archetype ---
Maintain a consistent customer profile across the repaired sequence. Transaction sizes,
frequency, destinations, and timing should remain consistent with the account type
established in the original sequence.

--- SECTION D — Value variance ---
Motif amounts should still vary naturally after correction — do not set all motif
transactions to the same amount just to hit the aggregate target. The aggregate must
be met in total, but individual values should look organic.

--- SECTION E — Temporal realism ---
Preserve the existing date structure. Do not compress transactions into a shorter window
or make the spacing uniform. If adding new motif transactions, slot them into gaps in
the existing timeline.

--- Hard requirements ---
- For RISKY: all aggregate conditions must be satisfied after repair.
- For GENUINE: the complete set of conditions must NOT be satisfied after repair.
- The LAST transaction in the sequence (most recent date) MUST be a background transaction.
  This anchors the reference date used for all window calculations to a neutral account event.
- IMPORTANT — use ONLY the canonical attribute names from the schema above as JSON keys.

Output the full repaired transaction list (same JSON format, all transactions), ordered by date:
[
  {{
    "id": "t-001",
    "tag": "{scenario_type}",
    "attributes": {{"created_at": "YYYY-MM-DD", "attr1": value, ...}}
  }},
  ...
]"""
