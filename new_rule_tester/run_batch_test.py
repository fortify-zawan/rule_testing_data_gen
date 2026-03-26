"""Batch rule tester — reads test_rules.csv, runs risky + genuine generation for each rule.

Usage (from inside new_rule_tester/):
    source venv/bin/activate
    export ANTHROPIC_API_KEY=sk-ant-...              # required
    python run_batch_test.py                         # all rules, 1 run each
    python run_batch_test.py --rules R01 R08         # specific rules only
    python run_batch_test.py --rules R01 --repeat 3  # run R01 three times; PASS only if all pass
    python run_batch_test.py --out my_report         # custom output base name
"""
import argparse
import csv
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Ensure new_rule_tester/ is on sys.path regardless of where the script is invoked from
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from domain.models import Rule                                              # noqa: E402
from llm.rule_parser import parse_rule                                      # noqa: E402
from orchestration.behavioral_orchestrator import run as orchestrator_run  # noqa: E402

CSV_PATH = _HERE / "test_rules.csv"
SCENARIOS = ["risky", "genuine"]


def _check_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        print("       export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)


def run_scenario(rule: Rule, scenario: str, intent: str) -> dict:
    """Run one scenario (risky or genuine) against an already-parsed rule."""
    result = {
        "passed": False,
        "correction_attempts": 0,
        "failed_conditions": [],
        "error": "",
    }

    print(f"  Generating {scenario.upper()} scenario...")

    def status_cb(msg: str):
        print(f"    {msg}")

    try:
        case = orchestrator_run(
            rule=rule,
            scenario_type=scenario,
            intent=intent,
            status_callback=status_cb,
        )
        result["passed"] = bool(case.validation_result and case.validation_result.passed)
        result["correction_attempts"] = case.correction_attempts
        if case.validation_result:
            result["failed_conditions"] = [
                f"{cr.attribute} {cr.operator} {cr.threshold} (actual={cr.actual_value})"
                for cr in case.validation_result.condition_results
                if not cr.passed
            ]
    except Exception as e:
        result["error"] = f"run_error: {e}"
        print(f"    ERROR: {e}")
        traceback.print_exc()

    status_str = "PASS" if result["passed"] else ("ERROR" if result["error"] else "FAIL")
    print(f"  {scenario.upper()} → {status_str} (corrections={result['correction_attempts']})")
    if result["failed_conditions"]:
        for fc in result["failed_conditions"]:
            print(f"    failed condition: {fc}")

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", nargs="*", help="Rule IDs to run (default: all)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="Run each rule N times; final status is PASS only if all runs pass (default: 1)")
    parser.add_argument("--scenario", choices=["risky", "genuine", "both"], default="both",
                        help="Which scenario to run: risky, genuine, or both (default: both)")
    parser.add_argument("--out", default="batch_report", help="Output file base name")
    args = parser.parse_args()

    _check_api_key()

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if args.rules:
        rows = [r for r in rows if r["rule_id"] in args.rules]
        if not rows:
            print(f"No rules matched: {args.rules}")
            sys.exit(1)

    scenarios = SCENARIOS if args.scenario == "both" else [args.scenario]
    repeat = max(1, args.repeat)
    total  = len(rows) * len(scenarios) * repeat
    print(f"\nBatch test — {len(rows)} rule(s) × {len(scenarios)} scenario(s) × {repeat} run(s) = {total} total\n")

    results = []  # one entry per (rule_id, scenario, run)

    for row in rows:
        rule_id   = row["rule_id"]
        rule_name = row["rule_name"]
        desc      = row["rule_description"]

        print(f"[{rule_id}] {rule_name}")

        # Parse the rule ONCE — reused across all runs and scenarios.
        print(f"  Parsing rule...")
        try:
            rule = parse_rule(desc)
            print(f"  Parsed OK — type={rule.rule_type}  conditions={len(rule.conditions)}")
        except Exception as e:
            print(f"  Parse FAILED: {e}")
            for scenario in scenarios:
                for run_n in range(1, repeat + 1):
                    results.append({
                        "rule_id": rule_id, "rule_name": rule_name,
                        "scenario": scenario, "run": run_n,
                        "parse_ok": False, "passed": False, "correction_attempts": 0,
                        "failed_conditions": [], "error": f"parse_error: {e}",
                    })
            print()
            continue

        for run_n in range(1, repeat + 1):
            if repeat > 1:
                print(f"  ── Run {run_n}/{repeat} ──")
            for scenario in scenarios:
                r = run_scenario(rule, scenario, intent=desc)
                results.append({
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                    "scenario": scenario,
                    "run": run_n,
                    "parse_ok": True,
                    **r,
                })

        print()

    # ── Summary table ──────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"] and not r["error"])
    errors = sum(1 for r in results if r["error"])

    print("=" * 70)
    print(f"SUMMARY  {passed} PASS / {failed} FAIL / {errors} ERROR  (total={total})")
    if repeat > 1:
        print(f"         Final PASS = ALL {repeat} runs passed for that rule/scenario")
    print("=" * 70)
    print(f"\n{'ID':<6}  {'Name':<40}  {'Risky':>14}  {'Genuine':>14}  {'Corrections':>11}")
    print("-" * 92)

    for row in rows:
        rid  = row["rule_id"]
        name = row["rule_name"][:39]

        def fmt_scenario(scenario: str) -> tuple[str, int]:
            runs = [r for r in results if r["rule_id"] == rid and r["scenario"] == scenario]
            if not runs:
                return "—", 0
            total_corr = sum(r["correction_attempts"] for r in runs if not r["error"])
            if any(r["error"] for r in runs):
                n_err = sum(1 for r in runs if r["error"])
                return f"ERROR({n_err}/{len(runs)})", total_corr
            n_pass = sum(1 for r in runs if r["passed"])
            if repeat == 1:
                label = "PASS" if n_pass == 1 else "FAIL"
            else:
                label = f"PASS({n_pass}/{len(runs)})" if n_pass == len(runs) else f"FAIL({n_pass}/{len(runs)})"
            return label, total_corr

        risky_label,   risky_corr   = fmt_scenario("risky")
        genuine_label, genuine_corr = fmt_scenario("genuine")
        total_corr = risky_corr + genuine_corr
        print(f"{rid:<6}  {name:<40}  {risky_label:>14}  {genuine_label:>14}  {total_corr:>11}")

    # ── Save reports ───────────────────────────────────────────────────────────
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv  = _HERE / f"{args.out}_{ts}.csv"
    out_json = _HERE / f"{args.out}_{ts}.json"

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rule_id", "rule_name", "scenario", "run", "parse_ok",
            "passed", "correction_attempts", "failed_conditions", "error",
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({**r, "failed_conditions": "; ".join(r["failed_conditions"])})

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nSaved:  {out_csv}")
    print(f"        {out_json}")


if __name__ == "__main__":
    main()
