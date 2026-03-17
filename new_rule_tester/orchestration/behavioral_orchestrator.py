"""Behavioral rule orchestrator.

Coordinates:
  1. Internal generation + validation loop (Loop B) — runs silently before user sees anything.
  2. Accepts user feedback and reruns the loop (Loop C entry point).
"""
from domain.models import BehavioralTestCase, Rule, Transaction
from llm.sequence_corrector import correct_behavioral_sequence
from llm.sequence_generator import generate_behavioral_sequence
from logging_config import get_logger
from validation.rule_engine import evaluate_behavioral_sequence

log = get_logger(__name__)

MAX_ATTEMPTS = 4  # 4 validation passes = 3 real correction attempts before giving up


# ── Realism checks (soft gates) ───────────────────────────────────────────────

def _check_motif_dominance(transactions: list[Transaction], rule: Rule) -> str | None:
    """Return a warning if rule-relevant (motif) transactions dominate the sequence."""
    if not transactions:
        return None
    for cond in rule.conditions:
        if cond.filter_attribute and cond.filter_value is not None:
            filter_val = str(cond.filter_value).lower()
            matching = sum(
                1 for t in transactions
                if str(t.attributes.get(cond.filter_attribute, "")).lower() == filter_val
            )
            ratio = matching / len(transactions)
            if ratio > 0.5:
                return (
                    f"Realism warning: {matching}/{len(transactions)} transactions match "
                    f"{cond.filter_attribute}={cond.filter_value!r} — motif is too dominant "
                    f"({ratio:.0%}). Add more background transactions with different destinations/values."
                )
    return None


def _check_threshold_clustering(transactions: list[Transaction], rule: Rule) -> str | None:
    """Return a warning if numeric amounts cluster tightly around a rule threshold."""
    if not transactions:
        return None
    for cond in rule.conditions:
        if not isinstance(cond.value, (int, float)):
            continue
        threshold = float(cond.value)
        if threshold == 0:
            continue
        amounts = [
            float(t.attributes[cond.attribute])
            for t in transactions
            if cond.attribute in t.attributes
            and isinstance(t.attributes[cond.attribute], (int, float))
        ]
        if len(amounts) < 3:
            continue
        near = sum(1 for a in amounts if abs(a - threshold) / threshold < 0.1)
        if near / len(amounts) > 0.6:
            return (
                f"Realism warning: {near}/{len(amounts)} values of {cond.attribute!r} "
                f"are within 10% of the threshold ({threshold}). "
                f"Vary amounts more naturally — spread them above and below."
            )
    return None


def _collect_realism_warnings(transactions: list[Transaction], rule: Rule) -> str:
    """Run both realism checks and return a combined warning string (empty if all clear)."""
    warnings = []
    w1 = _check_motif_dominance(transactions, rule)
    w2 = _check_threshold_clustering(transactions, rule)
    if w1:
        warnings.append(w1)
    if w2:
        warnings.append(w2)
    return "\n".join(warnings)


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run(
    rule: Rule,
    scenario_type: str,
    intent: str = "",
    user_feedback: str = "",
    previous_case: BehavioralTestCase = None,
    status_callback=None,
) -> BehavioralTestCase:
    """
    Run the internal generation + validation loop for one behavioral test case.

    On first call: pass scenario_type + intent.
    On user feedback call: also pass user_feedback + previous_case.
    All prior feedback strings from previous_case.user_feedback_history are passed
    to every generator and corrector call so earlier instructions are never lost.
    Returns a BehavioralTestCase (validated internally).
    """
    def status(msg):
        if status_callback:
            status_callback(msg)

    case_id = (previous_case.id if previous_case else f"tc-{scenario_type[:1]}-1")
    correction_attempts = 0

    # Accumulated feedback from all prior rounds (not including the current one)
    prior_feedback = list(previous_case.user_feedback_history) if previous_case else []

    # Build previous aggregates context for the generator
    prev_agg_json = ""
    if previous_case:
        import json
        prev_agg_json = json.dumps(previous_case.computed_aggregates, indent=2)

    log.info(
        "orchestrator.run | case_id=%s scenario=%s intent=%r feedback=%r is_retry=%s",
        case_id, scenario_type, intent[:80] if intent else "", user_feedback[:80] if user_feedback else "",
        previous_case is not None,
    )

    status("Generating behavioral sequence...")
    transactions, conflict_dicts = generate_behavioral_sequence(
        rule=rule,
        scenario_type=scenario_type,
        intent=intent,
        feedback=user_feedback,
        previous_sequence_json=prev_agg_json,
        feedback_history=prior_feedback,
    )
    log.info("orchestrator | generated %d transactions", len(transactions))

    if conflict_dicts:
        lines = ["⚠️ Note: one or more of your instructions may conflict with the rule and could be overridden by auto-correction:"]
        for c in conflict_dicts:
            lines.append(f"  • \"{c.get('feedback_instruction', '')}\"")
            lines.append(f"    → {c.get('explanation', '')} (affects: {c.get('conflicting_condition', '')})")
        status("\n".join(lines))
        log.warning("orchestrator | feedback conflicts detected: %s", conflict_dicts)

    # Internal loop B
    for attempt in range(MAX_ATTEMPTS):
        status(f"Validating aggregates (attempt {attempt + 1})...")
        validation_result, aggregates = evaluate_behavioral_sequence(rule, transactions, scenario_type)

        # Run realism checks regardless of validation outcome — warnings feed into next correction
        realism_warnings = _collect_realism_warnings(transactions, rule)
        if realism_warnings:
            status(f"Realism check: {realism_warnings}")
            log.warning("orchestrator | realism warning on attempt %d: %s", attempt + 1, realism_warnings)

        if validation_result.passed:
            status("Sequence passed validation.")
            log.info("orchestrator | validation PASSED on attempt %d", attempt + 1)
            break

        failed_keys = [r.attribute for r in validation_result.condition_results if not r.passed]
        log.info(
            "orchestrator | validation FAILED on attempt %d | failed_conditions=%s",
            attempt + 1, failed_keys,
        )

        correction_attempts += 1
        if attempt < MAX_ATTEMPTS - 1:
            status(f"Correcting sequence — attempt {attempt + 1}...")
            log.info("orchestrator | starting correction attempt %d/%d", attempt + 1, MAX_ATTEMPTS - 1)
            failed_conditions = [r for r in validation_result.condition_results if not r.passed]
            import json
            corrector_intent = intent
            if realism_warnings:
                corrector_intent = (intent + "\n\nAdditional realism constraints:\n" + realism_warnings).strip()
            # Pass full feedback history so corrector also respects all prior user instructions
            corrector_history = prior_feedback + ([user_feedback] if user_feedback else [])
            transactions = correct_behavioral_sequence(
                rule=rule,
                scenario_type=scenario_type,
                transactions=transactions,
                aggregates=aggregates,
                failed_conditions=failed_conditions,
                intent=corrector_intent,
                feedback_history=corrector_history,
            )
        else:
            status(f"Warning: sequence did not converge after {MAX_ATTEMPTS} attempts.")
            log.warning("orchestrator | did not converge after %d attempts — returning last result", MAX_ATTEMPTS)

    # Final evaluation for the case record
    validation_result, aggregates = evaluate_behavioral_sequence(rule, transactions, scenario_type)

    log.info(
        "orchestrator | final result: passed=%s correction_attempts=%d aggregates=%s",
        validation_result.passed,
        correction_attempts,
        {k: round(v, 4) if isinstance(v, float) else v for k, v in aggregates.items()},
    )

    # Store all feedback (prior + current) in the returned case
    updated_feedback_history = prior_feedback + ([user_feedback] if user_feedback else [])

    return BehavioralTestCase(
        id=case_id,
        scenario_type=scenario_type,
        intent=intent,
        transactions=transactions,
        computed_aggregates=aggregates,
        validation_result=validation_result,
        correction_attempts=correction_attempts,
        user_feedback_history=updated_feedback_history,
    )
