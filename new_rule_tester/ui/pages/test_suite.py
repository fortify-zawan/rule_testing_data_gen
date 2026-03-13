"""Page 3 — Test Suite.

Shows the complete validated test suite.
Stateless: one sequence with tagged transactions.
Behavioral: multiple test cases with aggregates.
Export: CSV, JSON, XLSX.
"""
import streamlit as st
import pandas as pd
from domain.models import Rule, Transaction, BehavioralTestCase
from export.exporter import export_csv, export_json, export_xlsx
from ui.state import go_to


def render():
    rule: Rule = st.session_state.rule

    st.title("AML Rule Tester")
    st.subheader("Test Suite")
    st.info(f"**Rule:** {rule.raw_expression}  |  **Type:** {rule.rule_type.capitalize()}")

    col_back, col_new = st.columns([1, 4])
    with col_back:
        if rule.rule_type == "stateless":
            if st.button("← Back to Prototype Review"):
                go_to("prototype_review")
                st.rerun()
        else:
            if st.button("← Back to Test Case Builder"):
                go_to("test_case_builder")
                st.rerun()
    with col_new:
        if st.button("Start New Rule"):
            st.session_state.rule = None
            st.session_state.risky_proto = None
            st.session_state.genuine_proto = None
            st.session_state.stateless_sequence = None
            st.session_state.behavioral_cases = []
            st.session_state.current_case = None
            go_to("rule_input")
            st.rerun()

    st.divider()

    if rule.rule_type == "stateless":
        _render_stateless(rule)
    else:
        _render_behavioral(rule)


# ─── Stateless view ───────────────────────────────────────────────────────────

def _render_stateless(rule: Rule):
    sequence: list[Transaction] = st.session_state.stateless_sequence
    if not sequence:
        st.warning("No sequence available.")
        return

    risky_txns = [t for t in sequence if t.tag == "risky"]
    genuine_txns = [t for t in sequence if t.tag == "genuine"]
    failed = [t for t in sequence if t.validation_result and not t.validation_result.passed]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total transactions", len(sequence))
    col2.metric("Risky", len(risky_txns))
    col3.metric("Genuine", len(genuine_txns))

    if failed:
        st.warning(f"{len(failed)} transaction(s) could not be resolved and are marked FAIL.")

    # Filter
    filter_option = st.radio("Filter", ["All", "Risky", "Genuine", "Failed"], horizontal=True)
    if filter_option == "Risky":
        show = risky_txns
    elif filter_option == "Genuine":
        show = genuine_txns
    elif filter_option == "Failed":
        show = failed
    else:
        show = sequence

    rows = []
    for t in show:
        row = {"ID": t.id, "Tag": t.tag}
        row.update(t.attributes)
        if t.validation_result:
            row["Expected"] = "trigger" if t.validation_result.expected_trigger else "no_trigger"
            row["Validation"] = "PASS" if t.validation_result.passed else "FAIL"
        else:
            row["Expected"] = "—"
            row["Validation"] = "—"
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.apply(
            lambda col: [
                "background-color: #d4edda" if v == "PASS"
                else "background-color: #f8d7da" if v == "FAIL"
                else ""
                for v in col
            ] if col.name == "Validation" else [""] * len(col),
            axis=0,
        ),
        use_container_width=True,
        hide_index=True,
    )

    # Expand per-condition breakdown
    st.markdown("**Condition Detail**")
    for t in [x for x in show if x.validation_result]:
        with st.expander(f"{t.id} — {t.tag.upper()} — {t.validation_result.summary()}"):
            for cr in t.validation_result.condition_results:
                icon = "✅" if cr.passed else "❌"
                st.markdown(f"{icon} {cr.label()}")

    _render_exports(rule)


# ─── Behavioral view ──────────────────────────────────────────────────────────

def _render_behavioral(rule: Rule):
    cases: list[BehavioralTestCase] = st.session_state.behavioral_cases
    if not cases:
        st.warning("No test cases available.")
        return

    n_risky = sum(1 for c in cases if c.scenario_type == "risky")
    n_genuine = sum(1 for c in cases if c.scenario_type == "genuine")
    st.markdown(f"**{len(cases)} test cases total** ({n_risky} risky, {n_genuine} genuine)")

    # Summary table
    rows = []
    for i, case in enumerate(cases):
        vr = case.validation_result
        row = {
            "TC": i + 1,
            "Type": case.scenario_type,
            "Transactions": len(case.transactions),
            "Expected": "trigger" if (vr and vr.expected_trigger) else "no_trigger",
            "Validation": "PASS" if (vr and vr.passed) else "FAIL",
        }
        # Add aggregate values
        for k, v in case.computed_aggregates.items():
            row[k] = f"{v:.4f}" if isinstance(v, float) else v
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.apply(
            lambda col: [
                "background-color: #d4edda" if v == "PASS"
                else "background-color: #f8d7da" if v == "FAIL"
                else ""
                for v in col
            ] if col.name == "Validation" else [""] * len(col),
            axis=0,
        ),
        use_container_width=True,
        hide_index=True,
    )

    # Expand each test case
    for i, case in enumerate(cases):
        with st.expander(f"TC {i+1} — {case.scenario_type.upper()} — {len(case.transactions)} transactions"):
            if case.intent:
                st.markdown(f"*Intent: {case.intent}*")

            # Condition breakdown
            if case.validation_result:
                for cr in case.validation_result.condition_results:
                    icon = "✅" if cr.passed else "❌"
                    val_str = f"{cr.actual_value:.4f}" if isinstance(cr.actual_value, float) else str(cr.actual_value)
                    st.markdown(f"{icon} `{cr.attribute} {cr.operator} {cr.threshold}` — actual: **{val_str}**")

            # Transaction list
            txn_rows = [{"id": t.id, "tag": t.tag, **t.attributes}
                        for t in sorted(case.transactions, key=lambda t: t.attributes.get("created_at") or "")]
            st.dataframe(pd.DataFrame(txn_rows), use_container_width=True, hide_index=True)

    # Add more test cases button
    st.divider()
    if st.button("+ Add Another Test Case"):
        st.session_state.current_case = None
        go_to("test_case_builder")
        st.rerun()

    _render_exports(rule)


# ─── Export controls ──────────────────────────────────────────────────────────

def _render_exports(rule: Rule):
    st.divider()
    st.markdown("**Export**")
    col1, col2, col3 = st.columns(3)

    sequence = st.session_state.stateless_sequence
    cases = st.session_state.behavioral_cases

    with col1:
        csv_data = export_csv(rule, sequence, cases)
        st.download_button("Export CSV", csv_data, file_name="test_suite.csv", mime="text/csv")

    with col2:
        json_data = export_json(rule, sequence, cases)
        st.download_button("Export JSON", json_data, file_name="test_suite.json", mime="application/json")

    with col3:
        xlsx_data = export_xlsx(rule, sequence, cases)
        st.download_button("Export XLSX", xlsx_data, file_name="test_suite.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
