# AML Rule Tester

A Streamlit app that takes a natural-language Anti-Money Laundering (AML) rule, parses it into a structured representation, and generates realistic synthetic bank transaction sequences to test whether the rule fires correctly.

**LLM is used for:** rule parsing, transaction generation, sequence correction, coverage suggestions.
**Fully deterministic (no LLM):** all validation — aggregate computation and condition evaluation.

---

## Running the app

```bash
cd new_rule_tester
export ANTHROPIC_API_KEY=sk-ant-...   # required — get yours at console.anthropic.com
streamlit run app.py
```

Logs are written to `logs/aml_tester.log` (rotated daily).

---

## What it does

You give it a rule like:

> "Alert if total transfers to Iran in the last 30 days exceed $5,000"

The app:
1. Parses the rule into structured ComputedAttrs + conditions (aggregation, window, threshold, filters, OR groups)
2. Generates realistic transaction sequences that should — and should not — trigger the rule
3. Validates those sequences deterministically against the rule
4. Auto-corrects failures and shows you the results
5. Lets you build a test suite of approved cases and export it

---

## Two rule types

### Stateless
Evaluates each transaction in isolation. No time windows, no aggregation.

Example: *"Transaction to Iran with send amount > $100"*
→ Each transaction either passes all conditions or not.

### Behavioral
Evaluates patterns over a sequence of transactions. Requires aggregation, counting, or time windows.

Example: *"Sum of transfers to Iran in last 30 days > $5,000"*
→ The rule fires based on aggregate values computed across the full sequence.

---

## How rules are represented

### Computed attributes

The rule parser extracts all intermediate quantities as named **ComputedAttrs** before any condition is evaluated. Each CA is computed once (in order) and its value is injected into every transaction's attributes so later CAs and conditions can reference it.

Three CA modes:

**Scalar** — one aggregate over filtered transactions in a window:
```
iran_30d_sum   = sum(send_amount)[30d]  where receive_country_code in ["Iran"]
cash_7d_count  = count(transaction_id)[7d]  where transaction_type == "Cash"
prior_6m_count = count(transaction_id)[6m, exclude_last=7d]   ← prior period window
```

**Group** — aggregate computed per distinct value of a group-by attribute; the per-group result is injected back into each transaction so later filter clauses can reference it:
```
recipient_7d_sum = sum(send_amount)[7d]  group_by=recipient_id
# → t.attributes["recipient_7d_sum"] holds that recipient's 7d total for each txn
```

**Derived** — combines two scalar CAs with `ratio` or `difference`:
```
cash_ratio = ratio(cash_7d_count / prior_6m_count)
net_flow   = difference(inbound_30d_sum - outbound_30d_sum)
```

Conditions then compare a CA's value to a threshold:
```
iran_30d_sum > 5000
cash_ratio > 3.0
net_flow > 5000
```

### Filters and OR groups

Each CA can have a list of `FilterClause` predicates (AND/OR chained) instead of a single filter. Conditions can be grouped into OR groups: the rule fires if **any one group** has all its conditions satisfied.

---

## Full app flow

```
User enters NL rule
      ↓
Page 1 — Rule Input
  LLM parses rule → Rule object (conditions, aggregations, windows, thresholds)
  User can review and edit parsed conditions
      ↓
      ├── stateless rule ──→ Page 1b — Prototype Review
      │     User describes risky / genuine account character
      │     LLM generates Prototype attribute sets
      │     Stateless orchestrator: generate → validate → correct (up to 3 attempts)
      │     User reviews transactions, approves → saved to test suite
      │
      └── behavioral rule ──→ Page 2 — Test Case Builder
            Coverage suggestions pre-fetched during page transition (boundary cases,
              near-misses, window-edge tests, filter-empty cases, etc.)
            Left panel: user picks scenario type + optional intent
            Behavioral orchestrator: generate → validate → correct loop (up to 3 correction passes)
            User reviews transactions + aggregate results
            User can give feedback → regenerates with that feedback carried forward
            User approves → added to test suite
                ↓
Page 3 — Test Suite
  All approved cases with full transactions and validation results
  Export: CSV / JSON / XLSX
```

---

## The generate → validate → correct loop (behavioral)

This is the core engine. All three actors run on every case generation.

```
Generator (LLM)
  Input:  rule description + schema + scenario type + intent + feedback history
  Output: 10–20 transactions with realistic dates, amounts, countries
  Approach: reads the rule text and reasons about aggregates itself —
            no pre-computed arithmetic injected at this stage
      ↓
Validator (deterministic — no LLM)
  Input:  Rule object (the "recipe") + transaction list (the "numbers")
  Output: computed aggregates dict + per-condition PASS/FAIL + overall pass/fail
  Example output:
    { "sum(send_amount)[30d]": 3200.0 }
    Condition: sum(send_amount)[30d] > 5000 → FAIL (actual: 3200.0)
      ↓ if passed → done
      ↓ if failed (up to 3 attempts)
Corrector (LLM)
  Input:  same schema + rule + current transactions + exact aggregate values
          + shortfall arithmetic for CA-backed conditions (computed by Python)
          + preservation constraint: don't touch background transactions
  Output: repaired transaction list
  Approach: diagnostic-first — "here are the exact numbers, here is the gap, fix it"
      ↓
Validator again (same deterministic logic)
      ↓ repeat up to MAX_ATTEMPTS = 4 (= 3 real correction passes)
```

**Key asymmetry:** the generator is rule-description-first (creative, flexible); the corrector is diagnostic-first (surgical, precise). The corrector receives pre-computed shortfall values from Python — it doesn't have to do the arithmetic itself.

> **Why `MAX_ATTEMPTS = 4` gives 3 correction passes:** the loop runs 4 validation checks. On attempts 0, 1, and 2 a correction call follows a failed validation. On attempt 3 the final validation result is recorded and the loop exits without a further correction call.

### Shortfall arithmetic (CA-backed corrector)

For a rule like *"recent 30d cash sum > 2× prior 90d cash sum"*, the corrector receives pre-computed guidance from Python:

```
COMPUTED ATTR REPAIR: cash_ratio > 2.0 [FAIL, current=0.857]
Type: Derived CA (ratio)
  cash_ratio = cash_7d_count / prior_6m_count
  Current: cash_7d_count=1800.0, prior_6m_count=2100.0, cash_ratio=0.857
  Required: cash_7d_count > 2.0 × 2100.0 = 4200.00 (with 5% buffer → aim for 4410.00)
  Shortfall: +2610.00 needed in cash_7d_count
  → Add filter-matching transactions in the RECENT 7d window.
  → Do NOT add filter-matching transactions to the PRIOR period — raises the denominator.
```

This is computed by `sequence_corrector._format_tier1_repair_guidance()` from live aggregate values stored by the validator.

### Feedback history

When a user gives feedback and clicks "Regenerate", that string is appended to `BehavioralTestCase.user_feedback_history`. On the next run, all prior feedback strings are passed to both the generator and every corrector call. Earlier instructions are never dropped.

---

## Coverage suggestions

When the user clicks **Confirm and Continue** on Page 1, suggestions are fetched immediately (with a visible spinner) before navigating to the next page. They are cached in session state so the next page renders instantly. The suggestion list covers:

| Pattern | Scenario | Description |
|---|---|---|
| `typical_trigger` | risky | Comfortable margin above all thresholds |
| `boundary_just_over` | risky | Aggregate barely exceeds threshold |
| `boundary_at_threshold` | genuine | Aggregate exactly at threshold (should not fire) |
| `near_miss_one_clause` | genuine | All conditions met except one |
| `or_branch_trigger` | risky | Only one OR branch fires |
| `or_branch_all_fail` | genuine | All OR branches stay below threshold |
| `window_edge_inside` | risky | Activity concentrated at the edge of the time window |
| `filter_empty` | genuine | No transactions match the rule's filter |

Each suggestion pre-fills the scenario type and intent field. `expected_outcome` (FIRE / NOT_FIRE) is determined by Python from the pattern type — never by the LLM.

---

## Project structure

```
new_rule_tester/
├── app.py                         Streamlit entry point, page router, sidebar
├── logging_config.py              Centralised logging setup (get_logger)
├── requirements.txt
│
├── domain/
│   └── models.py                  All dataclasses: Rule, RuleCondition, ComputedAttr,
│                                  DerivedAttr, FilterClause, Transaction,
│                                  BehavioralTestCase, Prototype, TestSuggestion,
│                                  ValidationResult, ConditionResult
│
├── config/
│   ├── schema.yml                 Canonical attribute names, types, aliases, allowed values
│   └── schema_loader.py           canonical_name(), normalize_country_values(),
│                                  format_attributes_for_prompt()
│
├── llm/
│   ├── llm_wrapper.py             call_llm_json() — single entry point to Anthropic API
│   ├── rule_parser.py             NL rule → Rule object
│   ├── prototype_generator.py     NL description → Prototype attributes (stateless only)
│   ├── sequence_generator.py      Generates transaction sequences (stateless + behavioral)
│   ├── sequence_corrector.py      Repairs failed sequences; contains shortfall arithmetic
│   └── suggestion_generator.py    Generates TestSuggestion list for a rule
│
├── prompts/
│   ├── rule_parser.py             SYSTEM, PROMPT_TEMPLATE
│   ├── prototype_generator.py     SYSTEM, PROMPT_TEMPLATE, SINGLE_PROTO_TEMPLATE, CONFLICT_SECTION_TEMPLATE
│   ├── sequence_generator.py      SYSTEM, STATELESS_PROMPT, BEHAVIORAL_PROMPT, CONFLICT_SECTION_TEMPLATE
│   ├── sequence_corrector.py      SYSTEM, STATELESS_CORRECT_PROMPT, BEHAVIORAL_CORRECT_PROMPT
│   └── suggestion_generator.py    SYSTEM, SUGGESTION_PROMPT
│
├── validation/
│   ├── aggregate_compute.py       Deterministic aggregate computation (no LLM)
│   └── rule_engine.py             Evaluates Rule against transactions or aggregates
│
├── orchestration/
│   ├── stateless_orchestrator.py  Stateless: generate → validate → correct loop
│   └── behavioral_orchestrator.py Behavioral: generate → validate → correct loop;
│                                  feedback history accumulation
│
├── ui/
│   ├── state.py                   Session state init, go_to(), log_status()
│   └── pages/
│       ├── rule_input.py          Page 1 — rule entry + condition editor
│       ├── prototype_review.py    Page 1b — stateless prototype review
│       ├── test_case_builder.py   Page 2 — behavioral test case builder + suggestions panel
│       └── test_suite.py          Page 3 — test suite viewer + export
│
├── export/
│   └── exporter.py                CSV / JSON / XLSX export
│
└── logs/
    └── aml_tester.log             Daily-rotating debug log (git-ignored)
```

---

## Schema and attribute naming

`config/schema.yml` is the single source of truth for all transaction attribute names. The LLM is always given the canonical names and instructed not to use aliases.

Key canonical names:

| Canonical name | Type | Notes |
|---|---|---|
| `send_amount` | numeric | Amount sent |
| `receive_amount` | numeric | Amount received |
| `send_country_code` | categorical | Full country name, e.g. `"United Kingdom"` |
| `receive_country_code` | categorical | Full country name matching `rule.high_risk_countries` exactly |
| `transaction_type` | categorical | e.g. `"cash_withdrawal"`, `"bank_transfer"` |
| `payin_method` | categorical | e.g. `"card"`, `"wallet"` |
| `transaction_id` | string | Used as the attribute for `count` aggregations |
| `created_at` | datetime | ISO date `YYYY-MM-DD`; anchors all window calculations |

Country values must match the strings in `rule.high_risk_countries` exactly (e.g. `"Iran"` not `"IR"`). `schema_loader.normalize_country_values()` converts ISO codes to full names after LLM generation.

---

## Logging

All modules use `from logging_config import get_logger`. Logs go to `logs/aml_tester.log`.

| Level | Used for |
|---|---|
| DEBUG | Full prompt text, full LLM responses, per-aggregate computed values |
| INFO | LLM call metadata (model, chars, elapsed), rule parse result, validation pass/fail per condition, orchestrator attempt count, corrector shortfall details |
| WARNING | Failed convergence, condition_group_connector mismatches |
| ERROR | LLM JSON parse failures with raw response preview |

```bash
# Tail during a run
tail -f logs/aml_tester.log

# Filter to failures only
grep "FAIL\|ERROR\|WARNING" logs/aml_tester.log
```

---

## Key design decisions

**Generator is rule-description-first.** The generator prompt does not inject pre-computed arithmetic or DA period layouts. It gives the LLM the rule text and instructs it to reason about aggregates itself. This keeps the generator prompt simple and model-agnostic.

**Corrector is diagnostic-first.** The corrector receives exact current aggregate values and pre-computed shortfall arithmetic from Python. It never has to do the math itself. The corrector is the precision instrument; the generator just needs to be close enough.

**Validation is 100% deterministic.** No LLM is involved in deciding whether a sequence passes. `aggregate_compute.py` runs pure Python arithmetic over transaction attributes. This means pass/fail results are reproducible and trustworthy regardless of LLM behaviour.

**Background transactions are preserved.** The corrector has an explicit constraint not to modify background transactions. Only motif transactions (those matching the rule's filter) are adjusted. This keeps the account narrative intact across correction rounds.

**Feedback accumulates.** All prior user feedback strings travel with the test case through every generator and corrector call. The user never has to re-state earlier instructions.

**Prompts are separated from logic.** All LLM prompt strings live in `prompts/` — one file per LLM module. The `llm/` files contain only assembly logic and function signatures. This keeps logic files readable and makes prompt edits easy to find and review without touching Python logic.

**`call_llm_json` uses `raw_decode`.** The JSON parser uses `json.JSONDecoder().raw_decode()` rather than `json.loads()`, which gracefully handles cases where the model appends explanation text after the JSON object. `max_tokens` is set to 8,192 to avoid truncation on long behavioral sequences.
