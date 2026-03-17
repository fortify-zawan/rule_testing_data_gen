"""Prompt strings for llm/suggestion_generator.py."""

SYSTEM = """You are a test engineer generating AML rule test suggestions.
Output ONLY valid JSON — no explanation, no markdown fences."""

SUGGESTION_PROMPT = """You are a test engineer analysing an AML detection rule as a software function to be tested.

Rule expression: {raw_expression}
Rule type: {rule_type} (stateless = evaluated per transaction; behavioral = aggregates across a sequence)
Conditions:
{conditions_detail}

Generate test suggestions for each of the following coverage patterns.
For each pattern, produce exactly the fields listed below.

PATTERNS TO COVER:
{patterns_list}

FIELD RULES:
- title: Short label (max 10 words). Name the pattern and what makes it distinctive.
- description: 2-3 sentences. What exactly does this test, and why does it matter for this specific rule?
- focus_conditions: List the condition summaries (e.g. "send_amount sum > 10000") that this pattern specifically exercises.
- suggested_intent: Describe the account behaviour in plain English — what the account looks like, what mix of transactions it has, and what narrative leads to the outcome. Do NOT hardcode threshold numbers or country names. The intent will be fed to a transaction sequence generator.
  BAD: "avg to Iran = $510, 3 transactions"
  GOOD: "Account makes regular small domestic transfers with a cluster of higher-value transfers to one high-risk destination, pushing the average just over the rule threshold."

Output a JSON array — one object per pattern:
[
  {{
    "pattern_type": "<pattern_type from the list above>",
    "title": "...",
    "description": "...",
    "focus_conditions": ["...", "..."],
    "suggested_intent": "..."
  }},
  ...
]"""
