"""Prompt strings for llm/suggestion_generator.py."""

SYSTEM = """You are a test engineer generating AML rule test suggestions.
Output ONLY valid JSON — no explanation, no markdown fences."""

SUGGESTION_PROMPT = """You are a test engineer treating this AML detection rule as a software function \
to be tested with 95%+ coverage — analogous to unit testing in software engineering.

Your goal: suggest test scenarios that together cover all meaningful paths through the rule, \
both risky (FIRE) and genuine (NOT_FIRE). Each scenario should target a specific aspect of the \
rule that would reveal a bug if that aspect were implemented incorrectly.

Rule expression: {raw_expression}
Rule type: {rule_type} (stateless = evaluated per transaction; behavioral = aggregates across a sequence)

--- RULE ANATOMY ---
{rule_anatomy}

--- YOUR TASK ---
Think through the rule like an engineer stress-testing it:
- What is the core suspicious behaviour this rule is designed to catch?
- Which conditions are the hardest to satisfy simultaneously?
- What realistic innocent customer behaviour could look superficially similar but should NOT trigger?
- Where are the exact threshold, window, and filter boundaries that, if off-by-one, would cause \
false positives or false negatives?

Then generate one test scenario per pattern below. Each scenario must be meaningfully different \
from the others — do not generate near-duplicate scenarios with only minor value changes.

PATTERNS TO COVER:
{patterns_list}

FIELD RULES:
- title: Short label (max 10 words). Name the pattern and what makes it distinctive.
- description: 2-3 sentences. What specifically does this test, and what bug would it catch if \
the rule were implemented incorrectly?
- focus_conditions: List the specific condition or CA names this pattern exercises \
(e.g. "sum_new_recipient_elderly > 2000", "sender_age >= 60 filter").
- suggested_intent: Describe the account behaviour in plain English — the customer profile, \
what mix of transactions they make, and what narrative leads to the outcome. Be specific about \
which filters pass or fail and why, but do NOT hardcode threshold numbers or country names.
  BAD: "avg to Iran = $510, 3 transactions"
  GOOD: "Elderly account holder with established domestic transfer pattern suddenly sends to \
multiple new recipients in different cross-border destinations, each for the first time, \
accumulating just above the threshold."

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
