"""Prompt strings for llm/sequence_generator.py."""

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

BEHAVIORAL_PROMPT = """Generate a realistic account transaction sequence for AML behavioral rule testing.

--- TASK CONTEXT ---
The following inputs define what you must generate:

SCHEMA — canonical attribute names and allowed values you must use:
{schema_context}

RULE — the AML rule this sequence is being tested against:
{raw_expression}

RELEVANT ATTRIBUTES — only these fields (plus created_at) should appear in each transaction:
{attributes}

HIGH-RISK COUNTRIES — use these exact strings when setting country attributes:
{high_risk_countries}

SEQUENCE TYPE: {scenario_type}
  - risky   → sequence must cause the rule to FIRE
  - genuine → sequence must cause the rule to NOT FIRE

{intent_section}
{feedback_history_section}
{feedback_section}
--- END TASK CONTEXT ---

--- SECTION A — Aggregate-first reasoning (follow these steps before generating) ---
Think step by step:
1. Read the rule carefully. Identify every aggregate condition it defines — what is being
   counted or summed, over what time period, with what filter, and what threshold must be
   crossed (risky) or stayed below (genuine).
2. For each aggregate condition, decide on concrete target values — how many transactions,
   of what approximate sizes, are needed in the motif layer to satisfy or avoid the condition?
3. If the rule compares two aggregates from different time windows (e.g. recent 7d vs prior 30d),
   remember that all windows are anchored at the date of the LAST transaction you generate.
   Plan the final background transaction date first, then place filter-matching motif transactions
   within the correct window relative to that anchor date. Do NOT mix up which transactions go
   in which window — a "recent 7d" transaction must be within 7 days of the last transaction.
4. What does a realistic background for this account type look like?
The rule fires on AGGREGATES — individual transactions should reflect real account history,
not a direct demonstration of the rule.

--- SECTION B — Background + Motif composition ---
Structure the sequence as two interleaved layers:
- BACKGROUND (70-80% of transactions): Normal account activity. Use different destinations,
  amounts, and patterns from the rule-relevant subset. Avoid attributes that would move
  the rule's aggregate (e.g. if the rule tracks transfers to a high-risk country, background
  transactions should go to other destinations).
- MOTIF (20-30% of transactions): The rule-relevant subset. For RISKY: sized and placed to
  push the aggregate past the threshold. For GENUINE: sized to stay just below the threshold,
  but not obviously so — the account should still look plausible.
Interleave motif transactions across the timeline. Do NOT append them all at the end.

--- SECTION C — Customer archetype ---
If user intent is provided, infer a consistent customer profile (e.g. migrant worker,
small business owner, student, retail trader). Hold this profile across the full sequence:
transaction sizes, frequency, destinations, and timing should all be consistent with
the account type. If no intent is provided, infer a plausible profile from the rule's
attributes and generate accordingly.

--- SECTION D — Value variance ---
Do not cluster amounts near the threshold. Background amounts should vary freely.
Motif amounts should vary naturally (some higher, some lower) — the aggregate target
must be met in total, but individual values should look organic, not robotic.
Exception: if the intent explicitly implies structuring, clustering is acceptable.

--- SECTION E — Temporal realism ---
Order transactions by date with realistic spacing:
- Most activity on weekdays
- Occasional same-day pairs (normal for active accounts)
- Include 1-2 quiet periods of 3-7 days with no transactions
- The timeline must be long enough to cover all of the rule's time windows. If the rule
  has a single window (e.g. 30 days), span at least that many days. If it has multiple
  non-overlapping windows (e.g. a recent period + a prior period), span their combined
  length and place motif transactions in the correct window for each.
- Background transactions can extend freely across the full timeline.
- The LAST transaction in the sequence (most recent date) MUST be a background transaction.
  IMPORTANT — window anchoring: all time windows are measured backwards from the date of
  that final background transaction (latest_date). If the rule has a "recent 7d" window,
  your filter-matching motif transactions for that window must be dated within 7 days BEFORE
  the final background transaction — not 7 days before today or some other reference point.
  Decide the final background transaction date FIRST, then work backwards to place motif
  transactions in the correct windows relative to that date.

--- Hard requirements ---
- Generate 10-20 transactions total. Use more transactions if the rule's windows are long
  or require a richer account history to look realistic.
- For RISKY: the sequence's aggregate values must trigger ALL rule conditions.
- For GENUINE: the sequence's aggregate values must NOT trigger the complete set of conditions.
- Use realistic dates in YYYY-MM-DD format for the created_at field.
- IMPORTANT — Attribute keys: use ONLY the canonical attribute names listed in the schema above as JSON keys.
  Do NOT use aliases (e.g. use "send_amount" not "amount", "receive_country_code" not "country" or "destination_country").
  Only populate the relevant attributes ({attributes}) plus created_at.
- IMPORTANT — Country values: use the EXACT same string as listed under HIGH-RISK COUNTRIES above
  (e.g. if high_risk_countries = ["Iran"], set receive_country_code to "Iran" — NOT "IR" or "IRN").
  The validation engine matches both attribute keys and country values by exact string comparison.

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
--- SECTION F — Feedback conflict check ---
You have user instructions in the USER INSTRUCTIONS block above.
Before finalising your output, ask yourself: given what this rule requires for a
{scenario_type} outcome, does any instruction tell you to generate transactions that
would push the sequence away from that outcome?

  RISKY   → conflict = instruction makes it harder or impossible for the rule to FIRE
  GENUINE → conflict = instruction makes it more likely for the rule to FIRE

Flag only clear directional conflicts — where following the instruction would materially
prevent the expected validation outcome. Ignore style preferences, realism guidance, or
value constraints that don't affect aggregate direction.

IMPORTANT — still honor all user instructions when generating transactions. Do not
silently override them. Just flag what conflicts so the user can be informed.

Required output format when user instructions are present (wrap in an object, not a bare array):
{{{{
  "transactions": [ ... ],
  "feedback_conflicts": [
    {{{{
      "feedback_instruction": "<the conflicting instruction, quoted verbatim>",
      "conflicting_condition": "<rule condition affected, e.g. 'sum(receive_amount) > 500'>",
      "explanation": "<one sentence: why following this instruction causes the {scenario_type} validation to fail>"
    }}}}
  ]
}}}}
Use [] for feedback_conflicts if there are none."""
