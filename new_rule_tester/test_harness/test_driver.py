"""AML Rule Parser Benchmark — Test Driver.

Reads benchmark.json, parses each rule_desc via parse_rule(), and compares
the returned Rule object against the ground-truth Rule stored in the file.

Usage (run from inside new_rule_tester/):
    python -m test_harness.test_driver
    python -m test_harness.test_driver --rules R01 R05 R10
    python -m test_harness.test_driver --output results.json

Or directly:
    python test_harness/test_driver.py
"""
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

# Allow running as a script or as a module from new_rule_tester/
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # new_rule_tester/
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from domain.models import DerivedAttr, Rule, RuleCondition  # noqa: E402
from llm.rule_parser import parse_rule  # noqa: E402

_BENCHMARK_DEFAULT = os.path.join(_ROOT, "benchmark.json")


# ── Hydration: benchmark JSON dict → domain objects ───────────────────────────

def _coerce_list(operator: str | None, value: Any) -> Any:
    """Ensure filter_value / condition value is a list when operator is in/not_in."""
    if operator in ("in", "not_in") and value is not None and not isinstance(value, list):
        return [value]
    return value


def _hydrate_da(raw: dict) -> DerivedAttr:
    fop = raw.get("filter_operator")
    return DerivedAttr(
        name=raw["name"],
        aggregation=raw.get("aggregation", "count"),
        attribute=raw.get("attribute", "transaction_id"),
        window=raw.get("window"),
        filter_attribute=raw.get("filter_attribute"),
        filter_operator=fop,
        filter_value=_coerce_list(fop, raw.get("filter_value")),
    )


def _hydrate_condition(raw: dict) -> RuleCondition:
    fop = raw.get("filter_operator")
    cop = raw.get("operator")
    return RuleCondition(
        attribute=raw.get("attribute"),
        operator=cop,
        value=_coerce_list(cop, raw["value"]),
        aggregation=raw.get("aggregation"),
        window=raw.get("window"),
        logical_connector=raw.get("logical_connector", "AND"),
        filter_attribute=raw.get("filter_attribute"),
        filter_operator=fop,
        filter_value=_coerce_list(fop, raw.get("filter_value")),
        group_by=raw.get("group_by"),
        group_mode=raw.get("group_mode", "any"),
        link_attribute=raw.get("link_attribute"),
        derived_attributes=(
            [_hydrate_da(da) for da in raw["derived_attributes"]]
            if raw.get("derived_attributes") else None
        ),
        derived_expression=raw.get("derived_expression"),
        window_mode=raw.get("window_mode"),
    )


def _hydrate_rule(raw: dict, description: str) -> Rule:
    return Rule(
        description=description,
        rule_type=raw["rule_type"],
        relevant_attributes=raw.get("relevant_attributes", []),
        conditions=[_hydrate_condition(c) for c in raw["conditions"]],
        raw_expression=raw.get("raw_expression", ""),
        high_risk_countries=raw.get("high_risk_countries", []),
    )


# ── Comparison helpers ─────────────────────────────────────────────────────────

def _vals_equal(expected: Any, actual: Any) -> bool:
    """Value equality: set-equality for lists, float tolerance for numbers, case-insensitive strings."""
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    if isinstance(expected, list) and isinstance(actual, list):
        return {str(v).lower().strip() for v in expected} == {str(v).lower().strip() for v in actual}
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) < 1e-6
    return str(expected).lower().strip() == str(actual).lower().strip()


def _set_overlap(expected: list, actual: list) -> dict:
    """Return {matched, missing, extra, score} for two unordered string lists."""
    exp = {str(v).lower().strip() for v in (expected or [])}
    act = {str(v).lower().strip() for v in (actual or [])}
    matched = exp & act
    missing = exp - act
    extra = act - exp
    score = len(matched) / len(exp) if exp else (1.0 if not act else 0.0)
    return {
        "matched": sorted(matched),
        "missing": sorted(missing),
        "extra": sorted(extra),
        "score": round(score, 3),
    }


# ── Per-DerivedAttr comparison ─────────────────────────────────────────────────

@dataclass
class DAComparison:
    index: int
    name_match: bool
    aggregation_match: bool
    attribute_match: bool
    window_match: bool
    filter_match: bool
    score: float
    notes: list[str] = field(default_factory=list)


def _compare_da(idx: int, exp: DerivedAttr, act: DerivedAttr) -> DAComparison:
    notes = []

    name_match = (exp.name or "").lower() == (act.name or "").lower()
    agg_match  = (exp.aggregation or "").lower() == (act.aggregation or "").lower()
    attr_match = (exp.attribute or "").lower() == (act.attribute or "").lower()
    win_match  = (exp.window or "").lower() == (act.window or "").lower()
    filter_match = (
        (exp.filter_attribute or "").lower() == (act.filter_attribute or "").lower()
        and (exp.filter_operator or "") == (act.filter_operator or "")
        and _vals_equal(exp.filter_value, act.filter_value)
    )

    if not name_match:
        notes.append(f"name: expected={exp.name!r} got={act.name!r}")
    if not agg_match:
        notes.append(f"aggregation: expected={exp.aggregation!r} got={act.aggregation!r}")
    if not attr_match:
        notes.append(f"attribute: expected={exp.attribute!r} got={act.attribute!r}")
    if not win_match:
        notes.append(f"window: expected={exp.window!r} got={act.window!r}")
    if not filter_match:
        notes.append(
            f"filter: expected=({exp.filter_attribute} {exp.filter_operator} {exp.filter_value})"
            f" got=({act.filter_attribute} {act.filter_operator} {act.filter_value})"
        )

    fields = [name_match, agg_match, attr_match, win_match, filter_match]
    return DAComparison(idx, name_match, agg_match, attr_match, win_match, filter_match,
                        score=round(sum(fields) / len(fields), 3), notes=notes)


# ── Per-condition comparison ───────────────────────────────────────────────────

@dataclass
class ConditionComparison:
    index: int
    field_results: dict[str, bool]      # field_name → matched
    da_comparisons: list[DAComparison]
    score: float                         # 0.0 – 1.0
    notes: list[str] = field(default_factory=list)


# Field groups with weights used in scoring
_CORE_FIELDS   = ["attribute", "operator", "value", "aggregation", "window", "logical_connector"]
_FILTER_FIELDS = ["filter_attribute", "filter_operator", "filter_value"]
_DERIVED_FIELDS = ["derived_expression", "window_mode", "derived_attribute_count"]

_WEIGHTS = {f: 2.0 for f in _CORE_FIELDS}
_WEIGHTS.update({f: 1.5 for f in _FILTER_FIELDS})
_WEIGHTS.update({f: 2.0 for f in _DERIVED_FIELDS})
_DA_WEIGHT = 2.0


def _compare_condition(idx: int, exp: RuleCondition, act: RuleCondition) -> ConditionComparison:
    notes = []
    results: dict[str, bool] = {}

    # Core fields
    results["attribute"]          = (exp.attribute or "").lower() == (act.attribute or "").lower()
    results["operator"]           = (exp.operator or "") == (act.operator or "")
    results["value"]              = _vals_equal(exp.value, act.value)
    results["aggregation"]        = (exp.aggregation or "").lower() == (act.aggregation or "").lower()
    results["window"]             = (exp.window or "").lower() == (act.window or "").lower()
    results["logical_connector"]  = (
        (exp.logical_connector or "AND").upper() == (act.logical_connector or "AND").upper()
    )

    # Filter fields
    results["filter_attribute"]   = (exp.filter_attribute or "").lower() == (act.filter_attribute or "").lower()
    results["filter_operator"]    = (exp.filter_operator or "") == (act.filter_operator or "")
    results["filter_value"]       = _vals_equal(exp.filter_value, act.filter_value)

    # Derived expression fields
    results["derived_expression"] = (exp.derived_expression or "").lower() == (act.derived_expression or "").lower()
    results["window_mode"]        = (exp.window_mode or "").lower() == (act.window_mode or "").lower()

    exp_das = exp.derived_attributes or []
    act_das = act.derived_attributes or []
    results["derived_attribute_count"] = len(exp_das) == len(act_das)

    # Record mismatches
    for fname, matched in results.items():
        if not matched:
            exp_val = getattr(exp, fname, None) if fname != "derived_attribute_count" else len(exp_das)
            act_val = getattr(act, fname, None) if fname != "derived_attribute_count" else len(act_das)
            notes.append(f"{fname}: expected={exp_val!r}  got={act_val!r}")

    # Compare DerivedAttrs positionally
    da_comparisons = []
    for da_idx in range(max(len(exp_das), len(act_das))):
        if da_idx < len(exp_das) and da_idx < len(act_das):
            da_comparisons.append(_compare_da(da_idx, exp_das[da_idx], act_das[da_idx]))
        elif da_idx < len(exp_das):
            notes.append(f"DA[{da_idx}] ({exp_das[da_idx].name}): missing in actual")
            da_comparisons.append(DAComparison(da_idx, False, False, False, False, False, 0.0,
                                               ["missing in actual"]))
        else:
            notes.append(f"DA[{da_idx}] ({act_das[da_idx].name}): unexpected extra in actual")
            da_comparisons.append(DAComparison(da_idx, False, False, False, False, False, 0.0,
                                               ["unexpected in actual"]))

    # Weighted score across all fields
    all_fields = _CORE_FIELDS + _FILTER_FIELDS + _DERIVED_FIELDS
    earned = sum(_WEIGHTS[f] * results[f] for f in all_fields)
    total  = sum(_WEIGHTS[f] for f in all_fields)

    if da_comparisons:
        da_avg = sum(d.score for d in da_comparisons) / len(da_comparisons)
        earned += _DA_WEIGHT * da_avg
        total  += _DA_WEIGHT

    score = round(earned / total, 3) if total > 0 else 0.0
    return ConditionComparison(idx, results, da_comparisons, score, notes)


# ── Rule-level comparison ──────────────────────────────────────────────────────

@dataclass
class RuleComparison:
    rule_id: str
    label: str
    rule_desc: str
    passed: bool
    overall_score: float
    rule_type_match: bool
    relevant_attributes: dict       # {matched, missing, extra, score}
    high_risk_countries: dict       # {matched, missing, extra, score}
    condition_count_match: bool
    expected_condition_count: int
    actual_condition_count: int
    conditions: list[ConditionComparison]
    error: str = ""                 # populated if parse_rule() raised


# Thresholds for a rule-level PASS
_PASS_RULE_TYPE         = True          # rule_type must match exactly
_PASS_COND_COUNT        = True          # condition count must match exactly
_PASS_REL_ATTRS_SCORE   = 0.75          # relevant_attributes overlap ≥ 75%
_PASS_CONDITION_SCORE   = 0.75          # every individual condition ≥ 75%


def compare_rules(
    rule_id: str,
    label: str,
    desc: str,
    expected: Rule,
    actual: Rule,
) -> RuleComparison:
    rule_type_match = expected.rule_type == actual.rule_type
    rel_attrs       = _set_overlap(expected.relevant_attributes, actual.relevant_attributes)
    hrc             = _set_overlap(expected.high_risk_countries, actual.high_risk_countries)

    exp_conds, act_conds = expected.conditions, actual.conditions
    count_match = len(exp_conds) == len(act_conds)

    cond_comparisons = []
    for i in range(max(len(exp_conds), len(act_conds))):
        if i < len(exp_conds) and i < len(act_conds):
            cond_comparisons.append(_compare_condition(i, exp_conds[i], act_conds[i]))
        elif i < len(exp_conds):
            cond_comparisons.append(ConditionComparison(i, {}, [], 0.0,
                                                         [f"condition[{i}] missing in actual"]))
        else:
            cond_comparisons.append(ConditionComparison(i, {}, [], 0.0,
                                                         [f"condition[{i}] unexpected in actual"]))

    # Overall score: average of rule_type + rel_attrs + hrc + count + mean(condition scores)
    component_scores = [
        1.0 if rule_type_match else 0.0,
        rel_attrs["score"],
        hrc["score"] if expected.high_risk_countries else 1.0,
        1.0 if count_match else max(0.0, 1.0 - 0.25 * abs(len(exp_conds) - len(act_conds))),
    ]
    if cond_comparisons:
        component_scores.append(sum(c.score for c in cond_comparisons) / len(cond_comparisons))
    overall_score = round(sum(component_scores) / len(component_scores), 3)

    passed = (
        rule_type_match
        and count_match
        and rel_attrs["score"] >= _PASS_REL_ATTRS_SCORE
        and all(c.score >= _PASS_CONDITION_SCORE for c in cond_comparisons)
    )

    return RuleComparison(
        rule_id=rule_id,
        label=label,
        rule_desc=desc,
        passed=passed,
        overall_score=overall_score,
        rule_type_match=rule_type_match,
        relevant_attributes=rel_attrs,
        high_risk_countries=hrc,
        condition_count_match=count_match,
        expected_condition_count=len(exp_conds),
        actual_condition_count=len(act_conds),
        conditions=cond_comparisons,
    )


# ── Report printer ─────────────────────────────────────────────────────────────

_SEP  = "─" * 80
_SEP2 = "═" * 80
_TICK = {True: "✓", False: "✗"}


def _t(b: bool) -> str:
    return _TICK[b]


def print_report(comparisons: list[RuleComparison]) -> None:
    passed_count = sum(1 for r in comparisons if r.passed)

    print(f"\n{_SEP2}")
    print(f"  AML RULE PARSER BENCHMARK  —  {passed_count}/{len(comparisons)} rules passed")
    print(f"{_SEP2}\n")

    for r in comparisons:
        status = "PASS ✓" if r.passed else "FAIL ✗"
        print(_SEP)
        print(f"[{r.rule_id}]  {r.label}")
        print(f"  Status : {status}   Overall score: {r.overall_score:.2f}")
        print(f"  Desc   : {r.rule_desc[:100]}")

        if r.error:
            print(f"  ERROR  : {r.error}")
            print()
            continue

        print()
        print(f"  {_t(r.rule_type_match)} rule_type")
        ra = r.relevant_attributes
        print(f"  {_t(ra['score'] >= 1.0)} relevant_attributes  score={ra['score']:.2f}"
              f"  matched={ra['matched']}  missing={ra['missing']}  extra={ra['extra']}")
        hrc = r.high_risk_countries
        print(f"  {_t(hrc['score'] >= 1.0)} high_risk_countries  score={hrc['score']:.2f}"
              f"  matched={hrc['matched']}  missing={hrc['missing']}  extra={hrc['extra']}")
        print(f"  {_t(r.condition_count_match)} condition_count"
              f"  expected={r.expected_condition_count}  got={r.actual_condition_count}")

        for c in r.conditions:
            c_ok = c.score >= _PASS_CONDITION_SCORE
            print(f"\n  {_t(c_ok)} Condition[{c.index}]  score={c.score:.2f}")

            if c.field_results:
                groups = [
                    ("core   ", _CORE_FIELDS),
                    ("filter ", _FILTER_FIELDS),
                    ("derived", _DERIVED_FIELDS),
                ]
                for gname, gfields in groups:
                    relevant = {f: c.field_results[f] for f in gfields if f in c.field_results}
                    all_pass = all(relevant.values())
                    fails = [f for f, v in relevant.items() if not v]
                    tick = _t(all_pass)
                    fail_str = f"  FAIL: {', '.join(fails)}" if fails else ""
                    print(f"      {tick} {gname} {fail_str}")

            for note in c.notes:
                print(f"          ↳  {note}")

            for da in c.da_comparisons:
                da_ok = da.score >= 1.0
                issues = f"  →  {'; '.join(da.notes)}" if da.notes else ""
                print(f"      {_t(da_ok)} DA[{da.index}]  score={da.score:.2f}{issues}")

        print()

    print(_SEP2)
    pct = 100 * passed_count / len(comparisons) if comparisons else 0
    print(f"  SUMMARY: {passed_count}/{len(comparisons)} PASSED  ({pct:.0f}%)")
    print(_SEP2)

    failed = [r for r in comparisons if not r.passed]
    if failed:
        print("\n  Failed rules:")
        for r in failed:
            print(f"    [{r.rule_id}]  score={r.overall_score:.2f}  —  {r.label}")
    print()


# ── JSON serialiser (for --output) ────────────────────────────────────────────

def _to_dict(cmp: RuleComparison) -> dict:
    def _da_dict(d: DAComparison) -> dict:
        return {
            "index": d.index,
            "name_match": d.name_match,
            "aggregation_match": d.aggregation_match,
            "attribute_match": d.attribute_match,
            "window_match": d.window_match,
            "filter_match": d.filter_match,
            "score": d.score,
            "notes": d.notes,
        }

    def _cond_dict(c: ConditionComparison) -> dict:
        return {
            "index": c.index,
            "score": c.score,
            "field_results": c.field_results,
            "notes": c.notes,
            "da_comparisons": [_da_dict(d) for d in c.da_comparisons],
        }

    return {
        "rule_id": cmp.rule_id,
        "label": cmp.label,
        "rule_desc": cmp.rule_desc,
        "passed": cmp.passed,
        "overall_score": cmp.overall_score,
        "rule_type_match": cmp.rule_type_match,
        "relevant_attributes": cmp.relevant_attributes,
        "high_risk_countries": cmp.high_risk_countries,
        "condition_count_match": cmp.condition_count_match,
        "expected_condition_count": cmp.expected_condition_count,
        "actual_condition_count": cmp.actual_condition_count,
        "conditions": [_cond_dict(c) for c in cmp.conditions],
        "error": cmp.error,
    }


# ── TestDriver ─────────────────────────────────────────────────────────────────

class TestDriver:
    """
    Reads benchmark.json, parses each rule_desc with parse_rule(), and compares
    the returned Rule against the ground-truth Rule in the benchmark file.

    Usage:
        driver = TestDriver()
        results = driver.run()                        # all rules
        results = driver.run(rule_ids=["R01", "R10"]) # subset

    A rule PASSES when:
        - rule_type matches exactly
        - condition count matches exactly
        - relevant_attributes overlap score >= 0.75
        - every condition scores >= 0.75 (weighted field match)
    """

    def __init__(self, benchmark_path: str | None = None):
        self.benchmark_path = benchmark_path or _BENCHMARK_DEFAULT

    def _load(self) -> list[dict]:
        with open(self.benchmark_path) as f:
            return json.load(f)["benchmark_data"]

    def _rule_id(self, entry: dict, index: int) -> str:
        label = entry.get("label", "")
        # Label format: "R01 — ..." — extract leading R-id if present
        if " — " in label:
            candidate = label.split(" — ")[0].strip()
            if candidate.startswith("R") and candidate[1:].isdigit():
                return candidate
        return f"R{index + 1:02d}"

    def run(
        self,
        rule_ids: list[str] | None = None,
        output_path: str | None = None,
        verbose: bool = True,
    ) -> list[RuleComparison]:
        """
        Run the benchmark.

        Args:
            rule_ids:    Optional filter — only run these R-ids (e.g. ["R01", "R05"]).
            output_path: If set, write full results to this JSON file.
            verbose:     Print the report to stdout (default True).

        Returns:
            List of RuleComparison objects.
        """
        entries = self._load()
        results: list[RuleComparison] = []

        for i, entry in enumerate(entries):
            rule_id = self._rule_id(entry, i)
            label   = entry.get("label", rule_id)
            desc    = entry["rule_desc"]

            if rule_ids and rule_id not in rule_ids:
                continue

            print(f"[{rule_id}] Parsing: {desc[:80]}...")

            expected = _hydrate_rule(entry["rule"], desc)

            try:
                actual = parse_rule(desc)
            except Exception as exc:
                results.append(RuleComparison(
                    rule_id=rule_id, label=label, rule_desc=desc,
                    passed=False, overall_score=0.0,
                    rule_type_match=False,
                    relevant_attributes={"matched": [], "missing": [], "extra": [], "score": 0.0},
                    high_risk_countries={"matched": [], "missing": [], "extra": [], "score": 0.0},
                    condition_count_match=False,
                    expected_condition_count=len(expected.conditions),
                    actual_condition_count=0,
                    conditions=[],
                    error=str(exc),
                ))
                continue

            results.append(compare_rules(rule_id, label, desc, expected, actual))

        if verbose:
            print_report(results)

        if output_path:
            with open(output_path, "w") as f:
                json.dump(
                    {
                        "summary": {
                            "total": len(results),
                            "passed": sum(1 for r in results if r.passed),
                            "failed": sum(1 for r in results if not r.passed),
                        },
                        "results": [_to_dict(r) for r in results],
                    },
                    f, indent=2,
                )
            print(f"Results written to {output_path}")

        return results


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="AML rule parser benchmark")
    ap.add_argument(
        "--rules", nargs="*", metavar="R01",
        help="Run only specific rule IDs, e.g. --rules R01 R05 R10",
    )
    ap.add_argument(
        "--benchmark", metavar="PATH",
        help=f"Path to benchmark.json (default: {_BENCHMARK_DEFAULT})",
    )
    ap.add_argument(
        "--output", metavar="PATH",
        help="Write full results to a JSON file, e.g. --output results.json",
    )
    args = ap.parse_args()

    driver = TestDriver(benchmark_path=args.benchmark)
    driver.run(rule_ids=args.rules, output_path=args.output)
