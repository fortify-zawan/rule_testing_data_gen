"""Page 2b — Test Case Builder (behavioral rules only).

Loop B runs silently inside the orchestrator before the user sees the sequence.
Loop C: user can give feedback, which triggers Loop B again on the updated sequence.
User can add multiple test cases per rule.

Coverage Suggestions panel auto-generates on first render and provides pre-written
intents for boundary, near-miss, window-edge, and other edge-case scenarios.

Feedback History: all prior feedback strings are accumulated on the test case and
passed to every regeneration so earlier instructions are never forgotten.
"""
import dataclasses
import json

import streamlit as st

from domain.models import BehavioralTestCase, Rule, TestSuggestion
from export.exporter import export_csv, export_json, export_xlsx
from llm.suggestion_generator import generate_suggestions
from orchestration.behavioral_orchestrator import run as run_behavioral
from ui.state import clear_status_log, go_to, log_status
import ui.suggestion_loader as suggestion_loader

# ── Suggestion panel helpers ──────────────────────────────────────────────────

_SCENARIO_BADGE = {
    "risky":   ":red[RISKY]",
    "genuine": ":green[GENUINE]",
}

_OUTCOME_BADGE = {
    "FIRE":     ":red[FIRE]",
    "NOT_FIRE": ":green[NOT FIRE]",
}

_PATTERN_LABEL = {
    "typical_trigger":       "Typical Trigger",
    "boundary_just_over":    "Boundary — just over",
    "boundary_at_threshold": "Boundary — at threshold",
    "near_miss_one_clause":  "Near-miss — one clause fails",
    "or_branch_trigger":     "OR branch — one path triggers",
    "or_branch_all_fail":    "OR branch — all paths fail",
    "window_edge_inside":    "Window edge — inside",
    "filter_empty":          "Filter empty",
}


def _auto_generate_suggestions(rule: Rule):
    with st.spinner("Analysing rule..."):
        try:
            suggestions = generate_suggestions(rule)
            st.session_state.suggestions = suggestions
        except Exception as e:
            st.session_state.suggestions = []
            st.warning(f"Could not generate suggestions: {e}")


def _render_suggestions_content(rule: Rule):
    """Inner content for the suggestions expander."""
    suggestions: list[TestSuggestion] | None = st.session_state.get("suggestions")

    # Pick up results from the background thread if they have arrived
    if suggestions is None:
        bg = suggestion_loader.poll()
        if isinstance(bg, list):
            suggestions = bg
            st.session_state.suggestions = suggestions
        elif bg == "loading":
            st.caption("⏳ Suggestions loading in background…")
            if st.button("↻ Check", key="refresh_suggestions", use_container_width=True):
                st.rerun()
            return
        else:
            # Thread not started — fall back to manual generation
            st.caption("Generate edge-case suggestions for this rule.")
            if st.button("Generate Suggestions", key="gen_suggestions", use_container_width=True):
                _auto_generate_suggestions(rule)
                st.rerun()
            return

    st.caption("Auto-generated edge cases for this rule. Click **Use** to pre-fill the form.")

    if not suggestions:
        st.info("No suggestions available.")
        return

    for s in suggestions:
        pattern_label = _PATTERN_LABEL.get(s.pattern_type, s.pattern_type)
        scenario_badge = _SCENARIO_BADGE.get(s.scenario_type, s.scenario_type)
        outcome_badge = _OUTCOME_BADGE.get(s.expected_outcome, s.expected_outcome)

        title = s.title if len(s.title) <= 60 else s.title[:57] + "..."
        desc = s.description if len(s.description) <= 180 else s.description[:177] + "..."
        with st.container(border=True):
            st.markdown(
                f"{scenario_badge} &nbsp; {outcome_badge}  \n"
                f"**{title}**"
            )
            st.caption(f"*{pattern_label}*")
            st.caption(desc)
            if s.focus_conditions:
                st.caption("Focus: " + " · ".join(s.focus_conditions))
            if st.button("Use this suggestion", key=f"use_{s.id}", use_container_width=True):
                st.session_state.prefill_scenario_type = s.scenario_type
                st.session_state.prefill_intent = s.suggested_intent
                st.session_state.prefill_expected_outcome = s.expected_outcome
                st.rerun()


def _render_test_cases_content(rule: Rule, cases: list[BehavioralTestCase]):
    """Inner content for the test cases expander."""
    import pandas as pd

    if not cases:
        st.caption("No test cases approved yet.")
        return

    _FIXED_COLS = ["created_at", "send_amount", "currency"]

    for i, case in enumerate(cases):
        vr = case.validation_result
        status = "PASS" if (vr and vr.passed) else "FAIL"
        status_badge = f":green[{status}]" if status == "PASS" else f":red[{status}]"
        scenario_badge = _SCENARIO_BADGE.get(case.scenario_type, case.scenario_type)
        exp_label = "FIRE" if (vr and vr.expected_trigger) else "NOT FIRE"

        with st.container(border=True):
            st.markdown(f"**TC {i+1}** &nbsp; {scenario_badge} &nbsp; {status_badge}")
            st.caption(
                f"{len(case.transactions)} transactions · expected {exp_label}"
                + (f"\n_{case.intent}_" if case.intent else "")
            )
            if vr:
                for cr in vr.condition_results:
                    icon = "✅" if cr.passed else "❌"
                    try:
                        actual = f"{cr.actual_value:.4f}"
                    except (TypeError, ValueError):
                        actual = str(cr.actual_value)
                    st.caption(f"{icon} `{cr.attribute} {cr.operator} {cr.threshold}` → {actual}")

            toggle_key = f"show_txns_{i}"
            show_txns = st.session_state.get(toggle_key, False)
            btn_label = "▲ Hide transactions" if show_txns else f"▼ View {len(case.transactions)} transactions"
            if st.button(btn_label, key=f"toggle_txns_{i}", use_container_width=True):
                st.session_state[toggle_key] = not show_txns
                st.rerun()
            if show_txns:
                display_attrs = list(dict.fromkeys(_FIXED_COLS + list(rule.relevant_attributes)))
                rows = []
                for t in sorted(case.transactions, key=lambda t: t.attributes.get("created_at") or ""):
                    row = {"id": t.id, "tag": t.tag}
                    for col in display_attrs:
                        row[col] = t.id if col == "transaction_id" else t.attributes.get(col, "")
                    rows.append(row)
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("**Export**")
    sequence = st.session_state.get("stateless_sequence")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "CSV", export_csv(rule, sequence, cases),
            file_name="test_suite.csv", mime="text/csv", use_container_width=True,
        )
    with col2:
        st.download_button(
            "JSON", export_json(rule, sequence, cases),
            file_name="test_suite.json", mime="application/json", use_container_width=True,
        )
    with col3:
        st.download_button(
            "XLSX", export_xlsx(rule, sequence, cases),
            file_name="test_suite.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )


def _render_right_panel(rule: Rule):
    """Two collapsible sections: Coverage Suggestions + Test Cases."""
    cases: list[BehavioralTestCase] = st.session_state.behavioral_cases
    has_cases = len(cases) > 0

    with st.expander("Coverage Suggestions", expanded=not has_cases):
        _render_suggestions_content(rule)

    with st.expander(f"Test Cases ({len(cases)})", expanded=has_cases):
        _render_test_cases_content(rule, cases)


# ── Feedback History helpers ───────────────────────────────────────────────────

def _render_feedback_history(case: BehavioralTestCase):
    """Show accumulated feedback with remove buttons."""
    history = case.user_feedback_history
    if not history:
        return
    with st.expander(f"Feedback History ({len(history)} instructions)", expanded=True):
        st.caption(
            "All prior feedback is passed to every regeneration — earlier instructions won't be forgotten. "
            "Click × to remove one."
        )
        for i, text in enumerate(history):
            col_text, col_remove = st.columns([8, 1])
            with col_text:
                st.markdown(f"**{i + 1}.** {text}")
            with col_remove:
                if st.button("×", key=f"remove_feedback_{i}"):
                    case.user_feedback_history.pop(i)
                    st.session_state.current_case = case
                    st.rerun()


# ── Feedback report ────────────────────────────────────────────────────────────

def _build_feedback_report(rule: Rule, current_case: BehavioralTestCase | None) -> str:
    """Build a markdown debug/feedback report for the current session state."""
    rule_dict = json.loads(json.dumps(dataclasses.asdict(rule), default=str))

    lines: list[str] = []

    lines += [
        "# AML Rule Tester — Feedback Report",
        "",
        "---",
        "",
    ]

    # ── Section 1: Rule summary ──────────────────────────────────────────────
    lines += [
        "## 1. Rule",
        "",
        f"**Expression:** {rule.raw_expression}",
        f"**Type:** `{rule.rule_type}`",
        f"**Description:** {rule.description}",
    ]
    if rule.relevant_attributes:
        lines.append(f"**Relevant Attributes:** `{', '.join(rule.relevant_attributes)}`")
    if rule.high_risk_countries:
        lines.append(f"**High-Risk Countries:** {', '.join(rule.high_risk_countries)}")
    lines += ["", "---", ""]

    # ── Section 2: Computed Attributes ──────────────────────────────────────
    lines.append("## 2. Computed Attributes")
    lines.append("")
    if rule.computed_attrs:
        for ca in rule.computed_attrs:
            lines.append(f"### `{ca.name}`")
            lines.append(f"- **Aggregation:** `{ca.aggregation}({ca.attribute})`")
            if ca.window:
                lines.append(f"- **Window:** `{ca.window}`")
            if ca.window_exclude:
                lines.append(f"- **Window Exclude (prior period):** `{ca.window_exclude}`")
            if ca.group_by:
                lines.append(f"- **Group By:** `{ca.group_by}`")
            if ca.derived_from:
                lines.append(f"- **Derived From:** `{', '.join(ca.derived_from)}`")
            if ca.filters:
                parts = []
                for k, fc in enumerate(ca.filters):
                    clause = (
                        f"`{fc.attribute} {fc.operator} field({fc.value_field})`"
                        if fc.value_field
                        else f"`{fc.attribute} {fc.operator} {fc.value}`"
                    )
                    if k < len(ca.filters) - 1:
                        clause += f" **{fc.connector}**"
                    parts.append(clause)
                lines.append(f"- **Filters:** {' '.join(parts)}")
            lines.append("")
    else:
        lines += ["*(No computed attributes)*", ""]
    lines += ["---", ""]

    # ── Section 3: Conditions ────────────────────────────────────────────────
    lines.append("## 3. Conditions")
    lines.append("")
    for i, cond in enumerate(rule.conditions):
        attr = cond.computed_attr_name or cond.attribute or ""
        lines.append(f"### Condition {i + 1}: `{attr} {cond.operator} {cond.value}`")
        if cond.aggregation:
            lines.append(f"- **Aggregation:** `{cond.aggregation}`")
        if cond.window:
            lines.append(f"- **Window:** `{cond.window}`")
        if cond.filters:
            parts = []
            for k, fc in enumerate(cond.filters):
                clause = (
                    f"`{fc.attribute} {fc.operator} field({fc.value_field})`"
                    if fc.value_field
                    else f"`{fc.attribute} {fc.operator} {fc.value}`"
                )
                if k < len(cond.filters) - 1:
                    clause += f" **{fc.connector}**"
                parts.append(clause)
            lines.append(f"- **Filters:** {' '.join(parts)}")
        if cond.group_by:
            lines.append(f"- **Group By:** `{cond.group_by}` (mode: `{cond.group_mode}`)")
        if cond.condition_group:
            lines.append(f"- **Condition Group:** {cond.condition_group} (connector: `{cond.condition_group_connector}`)")
        if i < len(rule.conditions) - 1:
            lines.append(f"- **Connector to next:** `{cond.logical_connector}`")
        lines.append("")
    lines += ["---", ""]

    # ── Section 4: Full Rule JSON ────────────────────────────────────────────
    lines += [
        "## 4. Full Rule JSON",
        "",
        "```json",
        json.dumps(rule_dict, indent=2),
        "```",
        "",
        "---",
        "",
    ]

    # ── Section 5: Current Test Case ─────────────────────────────────────────
    lines.append("## 5. Current Test Case")
    lines.append("")
    if current_case is None:
        lines += ["*(No active test case — generate one first)*", "", "---", ""]
    else:
        vr = current_case.validation_result
        expected = "FIRE" if (vr and vr.expected_trigger) else "NOT FIRE"
        lines += [
            f"| Field | Value |",
            f"|---|---|",
            f"| **Case ID** | `{current_case.id}` |",
            f"| **Scenario** | `{current_case.scenario_type}` |",
            f"| **Expected Outcome** | **{expected}** |",
        ]
        if current_case.intent:
            lines.append(f"| **Intent** | {current_case.intent} |")
        lines += [
            f"| **Correction Attempts** | {current_case.correction_attempts} |",
            f"| **Transactions** | {len(current_case.transactions)} |",
            "",
        ]
        if current_case.user_feedback_history:
            lines.append("### Feedback History")
            for j, fb in enumerate(current_case.user_feedback_history, 1):
                lines.append(f"{j}. {fb}")
            lines.append("")
        lines += ["---", ""]

        # ── Section 6: Validation Result ──────────────────────────────────
        lines.append("## 6. Validation Result")
        lines.append("")
        if vr is None:
            lines += ["*(No validation result)*", "", "---", ""]
        else:
            overall = "✅ PASS" if vr.passed else "❌ FAIL"
            lines += [
                f"**Overall:** {overall}",
                f"**Expected trigger:** {'Yes (FIRE)' if vr.expected_trigger else 'No (NOT FIRE)'}",
                "",
            ]

            if current_case.computed_aggregates:
                lines += ["### Computed Aggregates", "", "| Aggregate | Value |", "|---|---|"]
                for agg_key, agg_val in current_case.computed_aggregates.items():
                    try:
                        fmt = f"{agg_val:.4f}" if isinstance(agg_val, float) else str(agg_val)
                    except Exception:
                        fmt = str(agg_val)
                    lines.append(f"| `{agg_key}` | `{fmt}` |")
                lines.append("")

            lines += ["### Per-Condition Results", "", "| Condition | Threshold | Actual Value | Status |", "|---|---|---|---|"]
            for cr in vr.condition_results:
                status = "✅ PASS" if cr.passed else "❌ FAIL"
                try:
                    actual = f"{cr.actual_value:.4f}" if isinstance(cr.actual_value, float) else str(cr.actual_value)
                except Exception:
                    actual = str(cr.actual_value)
                lines.append(f"| `{cr.attribute} {cr.operator}` | `{cr.threshold}` | `{actual}` | {status} |")
            lines.append("")

            if not vr.passed:
                if vr.expected_trigger:
                    # Risky case that did NOT fire — show which conditions fell short
                    shortfall = [cr for cr in vr.condition_results if not cr.passed]
                    lines += [
                        "### ⚠ Conditions That Prevented Firing (RISKY — expected FIRE)",
                        "",
                        "These conditions did not reach their threshold:",
                        "",
                    ]
                    for cr in shortfall:
                        try:
                            actual = f"{cr.actual_value:.4f}" if isinstance(cr.actual_value, float) else str(cr.actual_value)
                        except Exception:
                            actual = str(cr.actual_value)
                        lines.append(f"- ❌ `{cr.attribute} {cr.operator} {cr.threshold}` → actual: `{actual}`")
                else:
                    # Genuine case that incorrectly fired — show which conditions passed (triggered the rule)
                    triggered = [cr for cr in vr.condition_results if cr.passed]
                    lines += [
                        "### ⚠ Conditions That Caused Unexpected Trigger (GENUINE — expected NOT FIRE)",
                        "",
                        "These conditions evaluated to True and caused the rule to fire when it should not have:",
                        "",
                    ]
                    for cr in triggered:
                        try:
                            actual = f"{cr.actual_value:.4f}" if isinstance(cr.actual_value, float) else str(cr.actual_value)
                        except Exception:
                            actual = str(cr.actual_value)
                        lines.append(f"- ✅ `{cr.attribute} {cr.operator} {cr.threshold}` → actual: `{actual}`")
                lines.append("")
            lines += ["---", ""]

            # ── Section 7: Transactions ────────────────────────────────────
            lines.append("## 7. Transactions")
            lines.append("")
            if not current_case.transactions:
                lines += ["*(No transactions)*", ""]
            else:
                fixed = ["created_at", "send_amount", "currency"]
                extra = [a for a in rule.relevant_attributes if a not in fixed]
                cols = ["tag"] + fixed + extra
                lines.append("| # | " + " | ".join(cols) + " |")
                lines.append("|---|" + "|".join(["---"] * len(cols)) + "|")
                for idx, t in enumerate(
                    sorted(current_case.transactions, key=lambda tx: tx.attributes.get("created_at") or ""), 1
                ):
                    vals = [t.tag] + [str(t.attributes.get(c, "")) for c in fixed + extra]
                    lines.append(f"| {idx} | " + " | ".join(vals) + " |")
                lines.append("")

    return "\n".join(lines)

# ── Main render ───────────────────────────────────────────────────────────────

def render():
    import pandas as pd

    rule: Rule = st.session_state.rule
    cases: list[BehavioralTestCase] = st.session_state.behavioral_cases

    # ── Page header (full width) ──────────────────────────────────────────────
    st.title("AML Rule Tester")
    st.subheader("Step 2 — Test Case Builder")
    st.info(f"**Rule:** {rule.raw_expression}")

    current_case: BehavioralTestCase = st.session_state.get("current_case")

    col_back, col_report = st.columns([1, 1])
    with col_back:
        if st.button("← Back to Rule Input"):
            go_to("rule_input")
            st.rerun()
    with col_report:
        st.download_button(
            "Share Feedback",
            data=_build_feedback_report(rule, current_case),
            file_name="aml_feedback_report.md",
            mime="text/markdown",
            use_container_width=True,
            key="share_feedback_btn",
        )

    st.divider()

    # ── Two-column layout: main content | right panel ─────────────────────────
    main_col, suggestions_col = st.columns([3, 1.4], gap="large")

    with suggestions_col:
        _render_right_panel(rule)

    with main_col:
        # ── Form to create a new test case ────────────────────────────────────
        if current_case is None:
            st.subheader(f"New Test Case #{len(cases) + 1}")

            prefill_scenario = st.session_state.get("prefill_scenario_type")
            prefill_intent_val = st.session_state.get("prefill_intent") or ""
            prefill_outcome = st.session_state.get("prefill_expected_outcome")

            scenario_options = ["risky", "genuine"]
            scenario_index = scenario_options.index(prefill_scenario) if prefill_scenario in scenario_options else 0

            scenario_type = st.radio(
                "Scenario type",
                scenario_options,
                index=scenario_index,
                horizontal=True,
            )
            intent = st.text_area(
                "Intent (optional)",
                value=prefill_intent_val,
                placeholder='e.g. "Account slowly routing funds to Iran over 30 days, total just over $10k with ~15% to high-risk"',
                height=80,
            )

            if prefill_outcome:
                outcome_label = "FIRE" if prefill_outcome == "FIRE" else "NOT FIRE"
                st.caption(f"Expected outcome from suggestion: **{outcome_label}**")

            col_gen, col_finish = st.columns(2)
            with col_gen:
                if st.button("Generate Test Case", type="primary"):
                    st.session_state.prefill_scenario_type = None
                    st.session_state.prefill_intent = None
                    st.session_state.prefill_expected_outcome = None

                    clear_status_log()
                    status_placeholder = st.empty()
                    log_lines = []

                    def update_status(msg):
                        log_lines.append(msg)
                        status_placeholder.info("\n\n".join(log_lines))
                        log_status(msg)

                    with st.spinner("Generating and validating sequence..."):
                        try:
                            tc_id = f"tc-{len(cases)+1}"
                            case = run_behavioral(
                                rule=rule,
                                scenario_type=scenario_type,
                                intent=intent.strip(),
                                status_callback=update_status,
                            )
                            case.id = tc_id
                            st.session_state.current_case = case
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to generate test case: {e}")
            with col_finish:
                if cases:
                    if st.button("Full Suite & Export →", type="primary"):
                        go_to("test_suite")
                        st.rerun()
            return

        # ── Review the generated test case ────────────────────────────────────
        case: BehavioralTestCase = current_case

        st.subheader(f"Review — Test Case #{len(cases) + 1} ({case.scenario_type.upper()})")

        if case.intent:
            st.markdown(f"*Intent: {case.intent}*")

        # Feedback history panel
        _render_feedback_history(case)

        # Transactions table
        st.markdown("**Transactions**")
        _FIXED_COLS = ["created_at", "send_amount", "currency"]
        display_attrs = list(dict.fromkeys(_FIXED_COLS + list(rule.relevant_attributes)))

        rows = []
        sorted_transactions = sorted(
            case.transactions,
            key=lambda t: t.attributes.get("created_at") or "",
        )
        for t in sorted_transactions:
            row = {"id": t.id, "tag": t.tag}
            for col in display_attrs:
                row[col] = t.attributes.get(col, "")
            rows.append(row)

        df = pd.DataFrame(rows)
        st.table(df)

        # Computed aggregates + validation
        st.markdown("**Computed Aggregates & Validation**")
        vr = case.validation_result
        for cr in (vr.condition_results if vr else []):
            icon = "✅" if cr.passed else "❌"
            try:
                actual_display = f"{cr.actual_value:.4f}"
            except (TypeError, ValueError):
                actual_display = str(cr.actual_value)
            st.markdown(f"{icon} `{cr.attribute} {cr.operator} {cr.threshold}` — actual: **{actual_display}**")

        if vr:
            overall = "PASS" if vr.passed else "FAIL"
            exp = "TRIGGER" if vr.expected_trigger else "NO TRIGGER"
            color = "green" if vr.passed else "red"
            st.markdown(f"**Expected outcome:** {exp} | **Validation:** :{color}[{overall}]")

        if case.correction_attempts > 0:
            st.caption(f"Internal correction attempts: {case.correction_attempts}")

        # ── Debug panel ───────────────────────────────────────────────────────
        with st.expander("🔍 Debug: Rule conditions & transaction attributes", expanded=False):
            st.markdown("**Rule conditions (from session state):**")
            for i, cond in enumerate(rule.conditions):
                if cond.derived_attributes is not None:
                    da_lines = "\n".join(
                        f"    [{j}] {da.name}: {da.aggregation}({da.attribute})"
                        f"{', window=' + da.window if da.window else ''}"
                        f"{(', filter: ' + ' '.join((fc.attribute + ' ' + fc.operator + ' field(' + fc.value_field + ')') if fc.value_field else (fc.attribute + ' ' + fc.operator + ' ' + str(fc.value)) for fc in da.filters)) if da.filters else ''}"
                        for j, da in enumerate(cond.derived_attributes)
                    )
                    st.code(
                        f"Condition {i+1} [DERIVED]: {cond.aggregate_key()} {cond.operator} {cond.value}\n"
                        f"  derived_expression: {cond.derived_expression!r}\n"
                        f"  derived_attributes:\n{da_lines}",
                        language="text",
                    )
                else:
                    filter_str = (
                        " ".join(
                            (f"{fc.attribute} {fc.operator} field({fc.value_field})" if fc.value_field else f"{fc.attribute} {fc.operator} {fc.value}")
                            + (f" {fc.connector}" if k < len(cond.filters) - 1 else "")
                            for k, fc in enumerate(cond.filters)
                        )
                        if cond.filters else "none"
                    )
                    st.code(
                        f"Condition {i+1}: {cond.attribute} {cond.operator} {cond.value}\n"
                        f"  aggregation: {cond.aggregation!r}\n"
                        f"  filters:     {filter_str}",
                        language="text",
                    )
            st.markdown("**Transaction attributes (relevant columns):**")
            for t in case.transactions:
                first_cond = rule.conditions[0] if rule.conditions else None
                first_filter_attr = (first_cond.filters[0].attribute if first_cond and first_cond.filters else None)
                attr = first_cond.attribute if first_cond else None
                fa_val = t.attributes.get(first_filter_attr) if first_filter_attr else "—"
                attr_val = t.attributes.get(attr) if attr else "—"
                st.text(
                    f"  {t.id} | {first_filter_attr}={fa_val!r}  |  {attr}={attr_val!r}  |  keys: {list(t.attributes.keys())}"
                )

        st.divider()

        # ── Feedback → Regenerate (Loop C) ────────────────────────────────────
        st.markdown("**Give feedback to refine this test case**")
        st.caption(
            "Constraints persist across all future regenerations — earlier instructions won't be forgotten."
        )
        feedback = st.text_area(
            "Feedback",
            placeholder='e.g. "Iran should not appear in the sequence" or "Reduce country variation to 2 destinations"',
            height=80,
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Regenerate with Feedback", disabled=not feedback.strip(), type="primary"):
                clear_status_log()
                status_placeholder = st.empty()
                log_lines = []

                def update_status(msg):
                    log_lines.append(msg)
                    status_placeholder.info("\n\n".join(log_lines))
                    log_status(msg)

                with st.spinner("Regenerating..."):
                    try:
                        updated_case = run_behavioral(
                            rule=rule,
                            scenario_type=case.scenario_type,
                            intent=case.intent or "",
                            user_feedback=feedback.strip(),
                            previous_case=case,
                            status_callback=update_status,
                        )
                        updated_case.id = case.id
                        st.session_state.current_case = updated_case
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to regenerate: {e}")

        with col2:
            if st.button("Approve this Test Case", type="primary"):
                cases.append(case)
                st.session_state.behavioral_cases = cases
                st.session_state.current_case = None
                st.rerun()

        st.divider()
        col_add, col_finish = st.columns(2)
        with col_finish:
            if st.button("Full Suite & Export →", type="primary"):
                if cases:
                    go_to("test_suite")
                    st.rerun()
                else:
                    st.warning("Approve at least one test case first.")
