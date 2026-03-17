"""Stateless rule orchestrator.

Coordinates:
  1. Sequence generation from approved prototypes.
  2. Internal correction loop (Loop B) — no user involvement.
"""
from config.schema_loader import canonical_name, normalize_country_values
from domain.models import Prototype, Rule, Transaction
from llm.sequence_corrector import correct_stateless_transaction
from llm.sequence_generator import generate_stateless_sequence
from validation.rule_engine import evaluate_stateless_sequence


def _canonicalize_attrs(attrs: dict, high_risk_countries: list[str] | None = None) -> dict:
    renamed = {canonical_name(k): v for k, v in attrs.items()}
    return normalize_country_values(renamed, high_risk_countries)

MAX_CORRECTION_ATTEMPTS = 3


def run(
    rule: Rule,
    risky_proto: Prototype,
    genuine_proto: Prototype,
    n_risky: int,
    n_genuine: int,
    status_callback=None,
) -> list[Transaction]:
    """
    Generate and validate a stateless sequence.
    status_callback(msg): optional callable to surface progress messages in the UI.
    Returns the final sequence (all transactions, with validation results on tagged ones).
    """
    def log(msg):
        if status_callback:
            status_callback(msg)

    log("Generating transaction sequence...")
    transactions = generate_stateless_sequence(rule, risky_proto, genuine_proto, n_risky, n_genuine)

    log("Validating tagged transactions...")
    transactions = evaluate_stateless_sequence(rule, transactions)

    # Loop B — correction loop
    for attempt in range(MAX_CORRECTION_ATTEMPTS):
        failed = [t for t in transactions if t.tag != "background" and t.validation_result and not t.validation_result.passed]
        if not failed:
            log("All tagged transactions passed validation.")
            break

        log(f"Correction round {attempt + 1}: {len(failed)} transaction(s) need fixing...")

        for t in failed:
            proto_attrs = (
                risky_proto.attributes if t.tag == "risky" else genuine_proto.attributes
            )
            failed_conditions = [r for r in t.validation_result.condition_results if not r.passed]
            corrected_attrs = _canonicalize_attrs(correct_stateless_transaction(rule, t, failed_conditions, proto_attrs), rule.high_risk_countries)
            t.attributes.update(corrected_attrs)

        # Re-validate after corrections
        transactions = evaluate_stateless_sequence(rule, transactions)

    # Mark any remaining failures as unresolvable (already noted in validation_result)
    still_failed = [t for t in transactions if t.tag != "background" and t.validation_result and not t.validation_result.passed]
    if still_failed:
        log(f"Warning: {len(still_failed)} transaction(s) could not be resolved after {MAX_CORRECTION_ATTEMPTS} attempts.")
    else:
        log("Sequence generation complete.")

    return transactions


def run_single(
    rule: Rule,
    proto: Prototype,
    scenario_type: str,
    n: int,
    status_callback=None,
) -> list[Transaction]:
    """Generate and validate cases for a single prototype type.

    Calls the existing run() with n=0 for the other type, then filters by tag.
    Uses proto as both risky and genuine to satisfy the sequence generator's signature.
    """
    if scenario_type == "risky":
        transactions = run(rule, proto, proto, n_risky=n, n_genuine=0, status_callback=status_callback)
    else:
        transactions = run(rule, proto, proto, n_risky=0, n_genuine=n, status_callback=status_callback)
    return [t for t in transactions if t.tag == scenario_type]
