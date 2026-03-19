"""Page 1 — Rule Input.

User enters a natural language AML rule. The LLM parses it into structured form.
User can edit the parsed output before proceeding.
"""
import streamlit as st

from domain.models import Rule, RuleCondition
from llm.rule_parser import parse_rule
from llm.suggestion_generator import generate_suggestions
from ui.state import go_to


def render():
    st.title("AML Rule Tester")
    st.subheader("Step 1 — Enter your AML rule")

    description = st.text_area(
        "Rule description",
        placeholder='e.g. "Alert if a customer sends more than $10,000 to Iran or North Korea in a single transaction"',
        height=120,
    )

    if st.button("Parse Rule", type="primary", disabled=not description.strip()):
        with st.spinner("Parsing rule..."):
            try:
                rule = parse_rule(description.strip())
                st.session_state.rule = rule
                st.session_state.risky_proto = None
                st.session_state.genuine_proto = None
                st.session_state.stateless_sequence = None
                st.session_state.behavioral_cases = []
                # Clear suggestion cache so new suggestions are generated for this rule
                st.session_state.suggestions = None
                st.session_state.prefill_scenario_type = None
                st.session_state.prefill_intent = None
                st.session_state.prefill_expected_outcome = None
            except Exception as e:
                st.error(f"Failed to parse rule: {e}")
                return

    rule: Rule = st.session_state.get("rule")
    if not rule:
        return

    st.divider()
    st.subheader("Parsed Rule — confirm or edit before continuing")

    # Rule type toggle
    rule_type = st.radio(
        "Rule type",
        ["stateless", "behavioral"],
        index=0 if rule.rule_type == "stateless" else 1,
        horizontal=True,
        help="Stateless: each transaction evaluated independently. Behavioral: aggregates across transactions.",
    )
    rule.rule_type = rule_type

    # Relevant attributes
    attrs_input = st.text_input(
        "Relevant attributes (comma-separated)",
        value=", ".join(rule.relevant_attributes),
    )
    rule.relevant_attributes = [a.strip() for a in attrs_input.split(",") if a.strip()]

    # High-risk countries
    hrc_input = st.text_input(
        "High-risk countries (comma-separated, leave blank if none)",
        value=", ".join(rule.high_risk_countries),
    )
    rule.high_risk_countries = [c.strip() for c in hrc_input.split(",") if c.strip()]

    # Raw expression (editable)
    raw_expr = st.text_input("Rule expression (human-readable)", value=rule.raw_expression)
    rule.raw_expression = raw_expr

    # Conditions table — display as editable rows
    st.markdown("**Conditions**")
    updated_conditions = []

    # Parse value back — try numeric, fall back to string/list
    import ast

    def _parse(raw):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw

    for i, cond in enumerate(rule.conditions):
        cond_label = cond.aggregate_key() if cond.derived_attributes else f"{cond.attribute} {cond.aggregation or ''}"
        with st.expander(f"Condition {i + 1}: {cond_label} {cond.operator} {cond.value}", expanded=True):

            if cond.derived_attributes is not None:
                # ── Tier 2 derived condition: read-only summary + editable operator/value ──
                st.caption("Derived condition — computed from named intermediate attributes")
                for da in cond.derived_attributes:
                    da_filter = (
                        f", filter: {da.filter_attribute} {da.filter_operator} {da.filter_value}"
                        if da.filter_attribute else ""
                    )
                    st.markdown(
                        f"- **{da.name}** = `{da.aggregation}({da.attribute})`"
                        f"{', window=' + da.window if da.window else ''}{da_filter}"
                    )
                wm = cond.window_mode or "non_overlapping"
                wm_label = "non-overlapping periods (DA[1] shifted back by DA[0] window)" if wm == "non_overlapping" else "independent (each DA anchored at latest_date)"
                st.markdown(f"Expression: **{cond.derived_expression}** → compared `{cond.operator} {cond.value}`")
                st.markdown(f"Window mode: `{wm}` — {wm_label}")

                col_op, col_val, col_conn = st.columns(3)
                op = col_op.selectbox(
                    "Operator",
                    [">", "<", ">=", "<=", "==", "!=", "in", "not_in"],
                    index=[">", "<", ">=", "<=", "==", "!=", "in", "not_in"].index(cond.operator)
                    if cond.operator in [">", "<", ">=", "<=", "==", "!=", "in", "not_in"] else 0,
                    key=f"op_{i}",
                )
                val = col_val.text_input("Value", value=str(cond.value), key=f"val_{i}")
                connector = col_conn.selectbox(
                    "Connector to next",
                    ["AND", "OR"],
                    index=0 if cond.logical_connector == "AND" else 1,
                    key=f"conn_{i}",
                )

                parsed_val = _parse(val)
                updated_conditions.append(RuleCondition(
                    attribute=cond.attribute,
                    operator=op,
                    value=parsed_val,
                    logical_connector=connector,
                    derived_attributes=cond.derived_attributes,
                    derived_expression=cond.derived_expression,
                    window_mode=cond.window_mode,
                ))

            else:
                # ── Tier 1 simple condition: fully editable ────────────────────────────
                col1, col2, col3 = st.columns(3)
                attr = col1.text_input("Attribute", value=cond.attribute or "", key=f"attr_{i}")
                op = col2.selectbox(
                    "Operator",
                    [">", "<", ">=", "<=", "==", "!=", "in", "not_in"],
                    index=[">", "<", ">=", "<=", "==", "!=", "in", "not_in"].index(cond.operator)
                    if cond.operator in [">", "<", ">=", "<=", "==", "!=", "in", "not_in"]
                    else 0,
                    key=f"op_{i}",
                )
                val = col3.text_input("Value", value=str(cond.value), key=f"val_{i}")

                col4, col5, col6 = st.columns(3)
                agg_options = ["", "sum", "count", "average", "max", "percentage_of_total", "ratio", "distinct_count", "shared_distinct_count", "days_since_first"]
                agg = col4.selectbox(
                    "Aggregation",
                    agg_options,
                    index=agg_options.index(cond.aggregation) if cond.aggregation in agg_options else 0,
                    key=f"agg_{i}",
                )
                window = col5.text_input(
                    "Window",
                    value=cond.window or "",
                    placeholder="e.g. 30d, 24h",
                    key=f"window_{i}",
                )
                connector = col6.selectbox(
                    "Connector to next",
                    ["AND", "OR"],
                    index=0 if cond.logical_connector == "AND" else 1,
                    key=f"conn_{i}",
                )

                # Filter fields — shown for all aggregations
                if agg:
                    st.caption("Filter (optional) — restricts which transactions are included in this aggregation (e.g. country = Iran)")
                    col7, col8, col9 = st.columns(3)
                    filter_attr = col7.text_input("Filter attribute", value=cond.filter_attribute or "", key=f"fattr_{i}")
                    filter_op_options = ["", ">", "<", ">=", "<=", "==", "!=", "in", "not_in"]
                    filter_op = col8.selectbox(
                        "Filter operator",
                        filter_op_options,
                        index=filter_op_options.index(cond.filter_operator) if cond.filter_operator in filter_op_options else 0,
                        key=f"fop_{i}",
                    )
                    filter_val_raw = col9.text_input(
                        "Filter value",
                        value=str(cond.filter_value) if cond.filter_value is not None else "",
                        key=f"fval_{i}",
                    )
                else:
                    filter_attr = ""
                    filter_op = ""
                    filter_val_raw = ""

                # Group-by field
                if agg:
                    st.caption("Group by (optional) — evaluates the condition per distinct value of this attribute (e.g. recipient_id, account_id)")
                    gcol1, gcol2 = st.columns(2)
                    group_by_val = gcol1.text_input(
                        "Group by attribute",
                        value=cond.group_by or "",
                        placeholder="e.g. recipient_id, account_id",
                        key=f"groupby_{i}",
                    )
                    if group_by_val.strip():
                        group_mode_val = gcol2.selectbox(
                            "Group mode",
                            ["any", "all"],
                            index=0 if (cond.group_mode or "any") == "any" else 1,
                            help='"any" = alert if at least one group fires; "all" = alert only if every group fires',
                            key=f"gmode_{i}",
                        )
                    else:
                        group_mode_val = "any"
                else:
                    group_by_val = ""
                    group_mode_val = "any"

                # Link attribute — shown only for shared_distinct_count
                if agg == "shared_distinct_count":
                    st.caption("Link attribute(s) — comma-separated; senders sharing ANY of these are counted (OR semantics)")
                    link_attr_raw = st.text_input(
                        "Link attribute(s)",
                        value=", ".join(cond.link_attribute or []),
                        placeholder="e.g. email, phone, device_id",
                        key=f"linkattr_{i}",
                    )
                    link_attribute_val = [a.strip() for a in link_attr_raw.split(",") if a.strip()] or None
                else:
                    link_attribute_val = None

                parsed_val = _parse(val)
                parsed_filter_val = _parse(filter_val_raw) if filter_val_raw.strip() else None

                updated_conditions.append(RuleCondition(
                    attribute=attr,
                    operator=op,
                    value=parsed_val,
                    aggregation=agg if agg else None,
                    window=window.strip() if window.strip() else None,
                    logical_connector=connector,
                    filter_attribute=filter_attr.strip() if filter_attr.strip() else None,
                    filter_operator=filter_op if filter_op else None,
                    filter_value=parsed_filter_val,
                    group_by=group_by_val.strip() if group_by_val.strip() else None,
                    group_mode=group_mode_val,
                    link_attribute=link_attribute_val,
                ))

    rule.conditions = updated_conditions
    st.session_state.rule = rule

    st.divider()
    if st.button("Confirm and Continue", type="primary"):
        with st.spinner("Analysing rule for edge case suggestions..."):
            try:
                st.session_state.suggestions = generate_suggestions(rule)
            except Exception:
                st.session_state.suggestions = []
        if rule.rule_type == "stateless":
            go_to("prototype_review")
        else:
            go_to("test_case_builder")
        st.rerun()
