"""Page 2a — Prototype Review (stateless rules only).

Each prototype (risky / genuine) is refined independently with accumulated feedback.
Once approved the user sets a count and generates cases for that prototype.
After reviewing cases, "Add to Suite" locks them in and resets the draft so another
prototype of the same type can be started.
Generated cases accumulate in the right-hand panel alongside coverage suggestions.
"""
import pandas as pd
import streamlit as st

from domain.models import Prototype, Rule, TestSuggestion, Transaction
from export.exporter import export_csv, export_json, export_xlsx
from llm.prototype_generator import generate_prototypes, generate_single_prototype
from llm.suggestion_generator import generate_suggestions
from orchestration.stateless_orchestrator import run_single
from ui.state import clear_status_log, go_to, log_status

# ── Shared badges ──────────────────────────────────────────────────────────────

_SCENARIO_BADGE = {"risky": ":red[RISKY]", "genuine": ":green[GENUINE]"}

_PATTERN_LABEL = {
    "typical_trigger":       "Typical Trigger",
    "boundary_just_over":    "Boundary — just over",
    "boundary_at_threshold": "Boundary — at threshold",
    "near_miss_one_clause":  "Near-miss — one clause fails",
    "or_branch_trigger":     "OR branch — one path triggers",
    "or_branch_all_fail":    "OR branch — all paths fail",
    "window_edge_inside":    "Window edge — inside",
    "window_edge_outside":   "Window edge — outside",
    "filter_partial_match":  "Filter — partial match",
    "filter_empty":          "Filter empty",
}

_OUTCOME_BADGE = {"FIRE": ":red[FIRE]", "NOT_FIRE": ":green[NOT FIRE]"}


# ── Right-panel helpers ────────────────────────────────────────────────────────

def _auto_generate_suggestions(rule: Rule):
    with st.spinner("Analysing rule for suggestions..."):
        try:
            st.session_state.suggestions = generate_suggestions(rule)
        except Exception as e:
            st.session_state.suggestions = []
            st.warning(f"Could not generate suggestions: {e}")


def _render_suggestions_content(rule: Rule):
    suggestions: list[TestSuggestion] | None = st.session_state.get("suggestions")
    if suggestions is None:
        _auto_generate_suggestions(rule)
        suggestions = st.session_state.get("suggestions", [])

    st.caption("Auto-generated edge cases for this rule.")

    if not suggestions:
        st.info("No suggestions available.")
        return

    for s in suggestions:
        with st.container(border=True):
            scenario_badge = _SCENARIO_BADGE.get(s.scenario_type, s.scenario_type)
            outcome_badge = _OUTCOME_BADGE.get(s.expected_outcome, s.expected_outcome)
            pattern_label = _PATTERN_LABEL.get(s.pattern_type, s.pattern_type)
            st.markdown(f"{scenario_badge} &nbsp; {outcome_badge}  \n**{s.title}**")
            st.caption(f"*{pattern_label}*")
            st.caption(s.description)
            if s.focus_conditions:
                st.caption("Focus: " + " · ".join(s.focus_conditions))
            if st.button("Use this suggestion", key=f"use_proto_{s.id}", use_container_width=True):
                st.session_state.prefill_proto_scenario_type = s.scenario_type
                st.session_state.prefill_proto_intent = s.suggested_intent
                _reset_draft(s.scenario_type)
                st.rerun()


def _render_generated_cases_content(rule: Rule):
    risky_groups: list[list[Transaction]] = st.session_state.get("risky_case_groups", [])
    genuine_groups: list[list[Transaction]] = st.session_state.get("genuine_case_groups", [])

    if not risky_groups and not genuine_groups:
        st.caption("No cases added yet. Approve a prototype and generate cases.")
        return

    _FIXED_COLS = ["created_at", "send_amount", "currency"]
    display_attrs = list(dict.fromkeys(_FIXED_COLS + list(rule.relevant_attributes)))

    for tag, groups in [("risky", risky_groups), ("genuine", genuine_groups)]:
        if not groups:
            continue
        badge = _SCENARIO_BADGE.get(tag, tag)
        total = sum(len(g) for g in groups)
        st.markdown(f"**{badge} — {len(groups)} prototype(s), {total} transactions**")

        for gi, group in enumerate(groups):
            n_pass = sum(1 for t in group if t.validation_result and t.validation_result.passed)
            n_fail = len(group) - n_pass
            status_str = f"✅ {n_pass} pass" + (f"  ❌ {n_fail} fail" if n_fail else "")

            toggle_key = f"show_{tag}_group_{gi}"
            show = st.session_state.get(toggle_key, False)
            btn_label = "▲ Hide" if show else f"▼ {len(group)} transactions · {status_str}"
            with st.container(border=True):
                st.caption(f"Prototype {gi + 1}")
                if st.button(btn_label, key=f"toggle_{tag}_grp_{gi}", use_container_width=True):
                    st.session_state[toggle_key] = not show
                    st.rerun()
                if show:
                    rows = []
                    for t in sorted(group, key=lambda t: t.attributes.get("created_at") or ""):
                        row = {"id": t.id, "tag": t.tag}
                        for col in display_attrs:
                            row[col] = t.id if col == "transaction_id" else t.attributes.get(col, "")
                        if t.validation_result:
                            row["validation"] = "PASS" if t.validation_result.passed else "FAIL"
                        rows.append(row)
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Export
    sequence = st.session_state.get("stateless_sequence") or []
    if sequence:
        st.divider()
        st.markdown("**Export**")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button(
                "CSV", export_csv(rule, sequence, []),
                file_name="test_suite.csv", mime="text/csv", use_container_width=True,
            )
        with col2:
            st.download_button(
                "JSON", export_json(rule, sequence, []),
                file_name="test_suite.json", mime="application/json", use_container_width=True,
            )
        with col3:
            st.download_button(
                "XLSX", export_xlsx(rule, sequence, []),
                file_name="test_suite.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )


def _render_right_panel(rule: Rule):
    risky_groups = st.session_state.get("risky_case_groups", [])
    genuine_groups = st.session_state.get("genuine_case_groups", [])
    total = sum(len(g) for g in risky_groups + genuine_groups)
    has_cases = total > 0

    with st.expander("Coverage Suggestions", expanded=not has_cases):
        _render_suggestions_content(rule)

    with st.expander(f"Generated Cases ({total})", expanded=has_cases):
        _render_generated_cases_content(rule)


# ── Per-prototype section ──────────────────────────────────────────────────────

def _render_feedback_history(proto: Prototype, scenario_type: str):
    history = proto.user_feedback_history
    if not history:
        return
    with st.expander(f"Feedback History ({len(history)} instructions)", expanded=True):
        st.caption("All prior feedback is passed to every regeneration.")
        for i, text in enumerate(history):
            col_text, col_remove = st.columns([8, 1])
            with col_text:
                st.markdown(f"**{i + 1}.** {text}")
            with col_remove:
                if st.button("×", key=f"remove_{scenario_type}_feedback_{i}"):
                    proto.user_feedback_history.pop(i)
                    st.session_state[f"{scenario_type}_proto"] = proto
                    st.rerun()


def _reset_draft(scenario_type: str):
    """Clear the current draft so a new prototype can be started."""
    st.session_state[f"{scenario_type}_proto"] = None
    st.session_state[f"{scenario_type}_proto_approved"] = False
    st.session_state[f"{scenario_type}_cases"] = None


def _render_prototype_section(rule: Rule, scenario_type: str):
    proto_key = f"{scenario_type}_proto"
    approved_key = f"{scenario_type}_proto_approved"
    cases_key = f"{scenario_type}_cases"
    groups_key = f"{scenario_type}_case_groups"

    proto: Prototype | None = st.session_state.get(proto_key)
    is_approved: bool = st.session_state.get(approved_key, False)
    cases: list[Transaction] | None = st.session_state.get(cases_key)
    groups: list[list[Transaction]] = st.session_state.get(groups_key, [])

    badge = _SCENARIO_BADGE.get(scenario_type, scenario_type)
    n_groups = len(groups)
    st.markdown(f"#### {badge} Prototype")
    if n_groups:
        st.caption(f"{n_groups} version(s) already added to suite.")

    # No draft — show button to start one (auto-trigger if a suggestion was used)
    if proto is None:
        prefill_type = st.session_state.get("prefill_proto_scenario_type")
        prefill_intent = st.session_state.get("prefill_proto_intent")
        # Auto-generate if a suggestion targets this type
        if prefill_type == scenario_type and prefill_intent:
            st.session_state.prefill_proto_scenario_type = None
            st.session_state.prefill_proto_intent = None
            with st.spinner(f"Generating {scenario_type} prototype from suggestion..."):
                try:
                    new_proto, conflicts = generate_single_prototype(
                        rule, scenario_type=scenario_type,
                        feedback_history=[prefill_intent],
                    )
                    st.session_state[proto_key] = new_proto
                    st.session_state[f"{scenario_type}_proto_conflicts"] = conflicts
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to generate prototype: {e}")
            return

        label = f"+ Start {'another' if n_groups else 'a'} {scenario_type} prototype"
        if st.button(label, key=f"start_{scenario_type}"):
            with st.spinner(f"Generating {scenario_type} prototype..."):
                try:
                    new_proto, _ = generate_single_prototype(rule, scenario_type=scenario_type)
                    st.session_state[proto_key] = new_proto
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to generate prototype: {e}")
        return

    # Show current prototype attributes
    if scenario_type == "risky":
        st.caption("This transaction WOULD trigger the rule.")
    else:
        st.caption("This transaction would NOT trigger the rule.")

    with st.container(border=True):
        for attr, val in proto.attributes.items():
            st.markdown(f"- **{attr}:** {val}")

    conflicts = st.session_state.get(f"{scenario_type}_proto_conflicts", [])
    if conflicts and not is_approved:
        lines = ["⚠️ Note: one or more of your instructions may conflict with the rule and could be overridden during sequence generation:"]
        for c in conflicts:
            lines.append(f"  • \"{c.get('feedback_instruction', '')}\"")
            lines.append(f"    → {c.get('explanation', '')} (affects: {c.get('conflicting_condition', '')})")
        st.warning("\n".join(lines))

    if not is_approved:
        # Refinement controls
        _render_feedback_history(proto, scenario_type)

        feedback = st.text_area(
            "Feedback (optional)",
            key=f"{scenario_type}_feedback",
            placeholder='e.g. "Use a UK account sending to Iran just above the threshold"',
            height=70,
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Regenerate", key=f"regen_{scenario_type}",
                         disabled=not (feedback or "").strip()):
                with st.spinner("Regenerating..."):
                    try:
                        new_history = list(proto.user_feedback_history) + [feedback.strip()]
                        updated, conflicts = generate_single_prototype(
                            rule, scenario_type=scenario_type,
                            feedback_history=new_history, current_attrs=proto.attributes,
                        )
                        st.session_state[proto_key] = updated
                        st.session_state[f"{scenario_type}_proto_conflicts"] = conflicts
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to regenerate: {e}")

        with col2:
            if st.button("Discard", key=f"discard_{scenario_type}"):
                st.session_state[f"{scenario_type}_proto_conflicts"] = []
                _reset_draft(scenario_type)
                st.rerun()

        with col3:
            if st.button("Approve", key=f"approve_{scenario_type}", type="primary"):
                st.session_state[f"{scenario_type}_proto_conflicts"] = []
                st.session_state[approved_key] = True
                st.rerun()

    elif cases is None:
        # Approved — set count and generate
        n = st.number_input(
            f"Number of {scenario_type} transactions to generate",
            key=f"{scenario_type}_count",
            min_value=1, max_value=20, value=3,
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Generate Cases", key=f"gen_{scenario_type}", type="primary"):
                clear_status_log()
                status_placeholder = st.empty()
                log_lines = []

                def update_status(msg):
                    log_lines.append(msg)
                    status_placeholder.info("\n\n".join(log_lines))
                    log_status(msg)

                with st.spinner(f"Generating {scenario_type} transactions..."):
                    try:
                        generated = run_single(
                            rule=rule, proto=proto,
                            scenario_type=scenario_type, n=n,
                            status_callback=update_status,
                        )
                        st.session_state[cases_key] = generated
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to generate cases: {e}")
        with col2:
            if st.button("← Back to edit", key=f"unapprove_{scenario_type}"):
                st.session_state[approved_key] = False
                st.rerun()

    else:
        # Cases generated — show summary with validation, offer Add to Suite or Re-generate
        n_pass = sum(1 for t in cases if t.validation_result and t.validation_result.passed)
        n_fail = len(cases) - n_pass
        if n_fail:
            st.warning(f"{len(cases)} transactions generated — ✅ {n_pass} pass, ❌ {n_fail} fail")
        else:
            st.success(f"{len(cases)} transactions generated — all ✅ pass")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Re-generate", key=f"regen_cases_{scenario_type}"):
                st.session_state[cases_key] = None
                st.rerun()
        with col2:
            if st.button("Add to Suite ✓", key=f"add_suite_{scenario_type}", type="primary"):
                groups.append(cases)
                st.session_state[groups_key] = groups
                # Rebuild stateless_sequence from all groups
                all_risky = [t for g in st.session_state.get("risky_case_groups", []) for t in g]
                all_genuine = [t for g in st.session_state.get("genuine_case_groups", []) for t in g]
                st.session_state.stateless_sequence = all_risky + all_genuine
                _reset_draft(scenario_type)
                st.rerun()


# ── Main render ────────────────────────────────────────────────────────────────

def render():
    rule: Rule = st.session_state.rule

    st.title("AML Rule Tester")
    st.subheader("Step 2a — Prototype Review")
    st.info(f"**Rule:** {rule.raw_expression}")

    if st.button("← Back to Rule Input"):
        _reset_draft("risky")
        _reset_draft("genuine")
        st.session_state.risky_case_groups = []
        st.session_state.genuine_case_groups = []
        go_to("rule_input")
        st.rerun()

    st.divider()

    # First load — generate both together if nothing exists yet
    risky_proto = st.session_state.get("risky_proto")
    genuine_proto = st.session_state.get("genuine_proto")
    no_groups = (
        not st.session_state.get("risky_case_groups")
        and not st.session_state.get("genuine_case_groups")
    )
    if risky_proto is None and genuine_proto is None and no_groups:
        with st.spinner("Generating prototype examples..."):
            try:
                risky, genuine = generate_prototypes(rule)
                st.session_state.risky_proto = risky
                st.session_state.genuine_proto = genuine
            except Exception as e:
                st.error(f"Failed to generate prototypes: {e}")
                return

    # Two-column layout
    main_col, right_col = st.columns([3, 1.4], gap="large")

    with right_col:
        _render_right_panel(rule)

    with main_col:
        _render_prototype_section(rule, "risky")
        st.divider()
        _render_prototype_section(rule, "genuine")

        # Navigation — show once at least one group exists
        risky_groups = st.session_state.get("risky_case_groups", [])
        genuine_groups = st.session_state.get("genuine_case_groups", [])
        if risky_groups or genuine_groups:
            st.divider()
            if st.button("Full Suite & Export →", type="primary"):
                go_to("test_suite")
                st.rerun()
