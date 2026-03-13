"""Page 2b — Test Case Builder (behavioral rules only).

Loop B runs silently inside the orchestrator before the user sees the sequence.
Loop C: user can give feedback, which triggers Loop B again on the updated sequence.
User can add multiple test cases per rule.

Coverage Suggestions panel auto-generates on first render and provides pre-written
intents for boundary, near-miss, window-edge, and other edge-case scenarios.

Feedback History: all prior feedback strings are accumulated on the test case and
passed to every regeneration so earlier instructions are never forgotten.
"""
import streamlit as st
from domain.models import Rule, BehavioralTestCase, TestSuggestion
from orchestration.behavioral_orchestrator import run as run_behavioral
from llm.suggestion_generator import generate_suggestions
from ui.state import go_to, log_status, clear_status_log
from export.exporter import export_csv, export_json, export_xlsx


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
    if suggestions is None:
        _auto_generate_suggestions(rule)
        suggestions = st.session_state.get("suggestions", [])

    st.caption("Auto-generated edge cases for this rule. Click **Use** to pre-fill the form.")

    if not suggestions:
        st.info("No suggestions available.")
        return

    for s in suggestions:
        pattern_label = _PATTERN_LABEL.get(s.pattern_type, s.pattern_type)
        scenario_badge = _SCENARIO_BADGE.get(s.scenario_type, s.scenario_type)
        outcome_badge = _OUTCOME_BADGE.get(s.expected_outcome, s.expected_outcome)

        with st.container(border=True):
            st.markdown(
                f"{scenario_badge} &nbsp; {outcome_badge}  \n"
                f"**{s.title}**"
            )
            st.caption(f"*{pattern_label}*")
            st.caption(s.description)
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
            btn_label = f"▲ Hide transactions" if show_txns else f"▼ View {len(case.transactions)} transactions"
            if st.button(btn_label, key=f"toggle_txns_{i}", use_container_width=True):
                st.session_state[toggle_key] = not show_txns
                st.rerun()
            if show_txns:
                display_attrs = list(dict.fromkeys(_FIXED_COLS + list(rule.relevant_attributes)))
                rows = []
                for t in sorted(case.transactions, key=lambda t: t.attributes.get("created_at") or ""):
                    row = {"id": t.id, "tag": t.tag}
                    for col in display_attrs:
                        row[col] = t.attributes.get(col, "")
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



# ── Main render ───────────────────────────────────────────────────────────────

def render():
    import pandas as pd

    rule: Rule = st.session_state.rule
    cases: list[BehavioralTestCase] = st.session_state.behavioral_cases

    # ── Page header (full width) ──────────────────────────────────────────────
    st.title("AML Rule Tester")
    st.subheader("Step 2 — Test Case Builder")
    st.info(f"**Rule:** {rule.raw_expression}")

    if st.button("← Back to Rule Input"):
        go_to("rule_input")
        st.rerun()

    st.divider()

    # ── Two-column layout: main content | right panel ─────────────────────────
    main_col, suggestions_col = st.columns([3, 1.4], gap="large")

    with suggestions_col:
        _render_right_panel(rule)

    with main_col:

        current_case: BehavioralTestCase = st.session_state.get("current_case")

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
        st.dataframe(df, use_container_width=True, hide_index=True)

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
                        f"{', filter: ' + da.filter_attribute + ' ' + (da.filter_operator or '') + ' ' + str(da.filter_value) if da.filter_attribute else ''}"
                        for j, da in enumerate(cond.derived_attributes)
                    )
                    st.code(
                        f"Condition {i+1} [DERIVED]: {cond.aggregate_key()} {cond.operator} {cond.value}\n"
                        f"  derived_expression: {cond.derived_expression!r}\n"
                        f"  derived_attributes:\n{da_lines}",
                        language="text",
                    )
                else:
                    st.code(
                        f"Condition {i+1}: {cond.attribute} {cond.operator} {cond.value}\n"
                        f"  aggregation:      {cond.aggregation!r}\n"
                        f"  filter_attribute: {cond.filter_attribute!r}\n"
                        f"  filter_operator:  {cond.filter_operator!r}\n"
                        f"  filter_value:     {cond.filter_value!r}  (type: {type(cond.filter_value).__name__})",
                        language="text",
                    )
            st.markdown("**Transaction attributes (relevant columns):**")
            for t in case.transactions:
                fa = rule.conditions[0].filter_attribute if rule.conditions else None
                attr = rule.conditions[0].attribute if rule.conditions else None
                fa_val = t.attributes.get(fa) if fa else "—"
                attr_val = t.attributes.get(attr) if attr else "—"
                st.text(
                    f"  {t.id} | {fa}={fa_val!r}  |  {attr}={attr_val!r}  |  keys: {list(t.attributes.keys())}"
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
