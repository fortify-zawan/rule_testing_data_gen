"""Page 1 — Rule Input.

User enters a natural language AML rule. The LLM parses it into structured form.
User can edit the parsed output before proceeding.
"""
import streamlit as st

from domain.models import FilterClause, Rule, RuleCondition
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
                st.session_state.filter_clauses = {}  # reset per-condition filter state
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

    if "filter_clauses" not in st.session_state:
        st.session_state.filter_clauses = {}

    # Parse value back — try numeric, fall back to string/list
    import ast

    def _parse(raw):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw

    # Pre-compute which condition index is the first in each group (for connector display)
    _first_in_group: set[int] = set()
    _seen_groups: set[int] = set()
    for i, cond in enumerate(rule.conditions):
        gid = cond.condition_group or 0
        if gid not in _seen_groups:
            _first_in_group.add(i)
            _seen_groups.add(gid)

    for i, cond in enumerate(rule.conditions):
        if cond.computed_attr_name:
            cond_label = cond.computed_attr_name
        elif cond.derived_attributes:
            cond_label = cond.aggregate_key()
        else:
            cond_label = f"{cond.attribute} {cond.aggregation or ''}"
        with st.expander(f"Condition {i + 1}: {cond_label} {cond.operator} {cond.value}", expanded=True):

            if cond.derived_attributes is not None:
                # ── Tier 2 derived condition: read-only summary + editable operator/value ──
                st.caption("Derived condition — computed from named intermediate attributes")
                for da in cond.derived_attributes:
                    if da.filters:
                        parts = []
                        for k, fc in enumerate(da.filters):
                            if fc.value_field:
                                parts.append(f"{fc.attribute} {fc.operator} field({fc.value_field})")
                            else:
                                parts.append(f"{fc.attribute} {fc.operator} {fc.value}")
                            if k < len(da.filters) - 1:
                                parts.append(fc.connector)
                        da_filter = ", filter: " + " ".join(parts)
                    else:
                        da_filter = ""
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

                # Condition group fields
                st.caption("Condition group — use to express (A AND B) OR (C AND D) style logic")
                gcol1, gcol2 = st.columns(2)
                cg_val = gcol1.number_input(
                    "Condition group",
                    min_value=0, step=1,
                    value=cond.condition_group or 0,
                    help="Conditions sharing a group number are evaluated together. Default 0 = flat evaluation.",
                    key=f"cg_{i}",
                )
                if i in _first_in_group:
                    cgc_val = gcol2.selectbox(
                        "Group connector to next group",
                        ["OR", "AND"],
                        index=0 if (cond.condition_group_connector or "OR").upper() == "OR" else 1,
                        help='How this group\'s result connects to the next group. Only set on the first condition of each group.',
                        key=f"cgc_{i}",
                    )
                else:
                    gcol2.caption("Group connector set on first condition of this group")
                    cgc_val = cond.condition_group_connector or "OR"

                parsed_val = _parse(val)
                updated_conditions.append(RuleCondition(
                    attribute=cond.attribute,
                    operator=op,
                    value=parsed_val,
                    logical_connector=connector,
                    derived_attributes=cond.derived_attributes,
                    derived_expression=cond.derived_expression,
                    window_mode=cond.window_mode,
                    condition_group=int(cg_val),
                    condition_group_connector=cgc_val,
                ))

            else:
                # ── Tier 1 simple condition: fully editable ────────────────────────────
                col1, col2, col3 = st.columns(3)
                if cond.computed_attr_name:
                    col1.text_input("Computed Attr", value=cond.computed_attr_name, key=f"attr_{i}", disabled=True)
                    attr = None
                else:
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
                agg_options = ["", "sum", "count", "average", "max", "percentage_of_total", "ratio", "distinct_count", "shared_distinct_count", "days_since_first", "age_years"]
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

                # Filter clauses — multi-clause, shown for all aggregations
                if agg:
                    # Initialise from parsed condition on first render
                    if i not in st.session_state.filter_clauses:
                        if cond.filters:
                            st.session_state.filter_clauses[i] = [
                                {
                                    "attribute": fc.attribute,
                                    "operator": fc.operator,
                                    "value": str(fc.value) if fc.value is not None else "",
                                    "value_field": fc.value_field or "",
                                    "cross_field": bool(fc.value_field),
                                    "connector": fc.connector,
                                }
                                for fc in cond.filters
                            ]
                        else:
                            st.session_state.filter_clauses[i] = []

                    st.caption("Filters (optional) — restricts which transactions are included; add multiple clauses for compound conditions")
                    _op_opts = ["", ">", "<", ">=", "<=", "==", "!=", "in", "not_in"]
                    clauses = st.session_state.filter_clauses[i]

                    # Column header row (shown once when there are clauses)
                    if clauses:
                        h = st.columns([3, 1.5, 0.6, 3, 1.2])
                        h[0].caption("Attribute")
                        h[1].caption("Operator")
                        h[2].caption("⇄")
                        h[3].caption("Value  /  Compare-to field")
                        h[4].caption("Chain")

                    for j, clause in enumerate(clauses):
                        fc_cols = st.columns([3, 1.5, 0.6, 3, 1.2])
                        clause["attribute"] = fc_cols[0].text_input(
                            "attr", value=clause.get("attribute", ""), key=f"fattr_{i}_{j}",
                            placeholder="e.g. transaction_status",
                            label_visibility="collapsed",
                        )
                        op_idx = _op_opts.index(clause.get("operator", "")) if clause.get("operator", "") in _op_opts else 0
                        clause["operator"] = fc_cols[1].selectbox(
                            "op", _op_opts, index=op_idx, key=f"fop_{i}_{j}",
                            label_visibility="collapsed",
                        )
                        clause["cross_field"] = fc_cols[2].checkbox(
                            "⇄", value=clause.get("cross_field", False), key=f"fcf_{i}_{j}",
                            help="Toggle: compare against another field instead of a literal value",
                        )
                        if clause["cross_field"]:
                            clause["value_field"] = fc_cols[3].text_input(
                                "field", value=clause.get("value_field", ""), key=f"fvf_{i}_{j}",
                                placeholder="e.g. recipient_name",
                                label_visibility="collapsed",
                            )
                            clause["value"] = ""
                        else:
                            clause["value"] = fc_cols[3].text_input(
                                "value", value=clause.get("value", ""), key=f"fval_{i}_{j}",
                                placeholder='e.g. completed  or  ["Iran"]',
                                label_visibility="collapsed",
                            )
                            clause["value_field"] = ""
                        if j < len(clauses) - 1:
                            clause["connector"] = fc_cols[4].selectbox(
                                "chain", ["AND", "OR"],
                                index=0 if clause.get("connector", "AND") == "AND" else 1,
                                key=f"fconn_{i}_{j}",
                                label_visibility="collapsed",
                            )
                        else:
                            fc_cols[4].write("")

                    if st.button("＋ Add filter", key=f"fadd_{i}"):
                        st.session_state.filter_clauses[i].append(
                            {"attribute": "", "operator": "==", "value": "", "value_field": "", "cross_field": False, "connector": "AND"}
                        )
                        st.rerun()

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

                # Condition group fields
                st.caption("Condition group — use to express (A AND B) OR (C AND D) style logic")
                cgcol1, cgcol2 = st.columns(2)
                cg_val = cgcol1.number_input(
                    "Condition group",
                    min_value=0, step=1,
                    value=cond.condition_group or 0,
                    help="Conditions sharing a group number are evaluated together. Default 0 = flat evaluation.",
                    key=f"cg_{i}",
                )
                if i in _first_in_group:
                    cgc_val = cgcol2.selectbox(
                        "Group connector to next group",
                        ["OR", "AND"],
                        index=0 if (cond.condition_group_connector or "OR").upper() == "OR" else 1,
                        help='How this group\'s result connects to the next group. Only set on the first condition of each group.',
                        key=f"cgc_{i}",
                    )
                else:
                    cgcol2.caption("Group connector set on first condition of this group")
                    cgc_val = cond.condition_group_connector or "OR"

                # Build FilterClause list from session state
                built_filters = []
                for clause in st.session_state.filter_clauses.get(i, []):
                    fc_attr = (clause.get("attribute") or "").strip()
                    fc_op   = clause.get("operator") or ""
                    if not fc_attr or not fc_op:
                        continue
                    if clause.get("cross_field") and clause.get("value_field", "").strip():
                        built_filters.append(FilterClause(
                            attribute=fc_attr, operator=fc_op,
                            value=None, value_field=clause["value_field"].strip(),
                            connector=clause.get("connector", "AND"),
                        ))
                    else:
                        raw_v = clause.get("value", "")
                        pv = _parse(raw_v) if str(raw_v).strip() else None
                        if fc_op in ("in", "not_in") and pv is not None and not isinstance(pv, list):
                            pv = [pv]
                        built_filters.append(FilterClause(
                            attribute=fc_attr, operator=fc_op,
                            value=pv, value_field=None,
                            connector=clause.get("connector", "AND"),
                        ))
                parsed_val = _parse(val)

                updated_conditions.append(RuleCondition(
                    attribute=attr,
                    operator=op,
                    value=parsed_val,
                    aggregation=agg if agg else None,
                    window=window.strip() if window.strip() else None,
                    logical_connector=connector,
                    filters=built_filters if built_filters else None,
                    group_by=group_by_val.strip() if group_by_val.strip() else None,
                    group_mode=group_mode_val,
                    link_attribute=link_attribute_val,
                    condition_group=int(cg_val),
                    condition_group_connector=cgc_val,
                    computed_attr_name=cond.computed_attr_name,
                ))

    rule.conditions = updated_conditions
    st.session_state.rule = rule

    # Debug — serialized rule JSON
    import dataclasses, json

    def _rule_to_dict(r):
        return json.loads(json.dumps(dataclasses.asdict(r), default=str))

    with st.expander("Debug — parsed rule JSON"):
        st.json(_rule_to_dict(rule))

    st.divider()
    if st.button("Confirm and Continue", type="primary"):
        # TODO: suggestion generation temporarily disabled for testing
        # with st.spinner("Analysing rule for edge case suggestions..."):
        #     try:
        #         st.session_state.suggestions = generate_suggestions(rule)
        #     except Exception:
        #         st.session_state.suggestions = []
        st.session_state.suggestions = []
        if rule.rule_type == "stateless":
            go_to("prototype_review")
        else:
            go_to("test_case_builder")
        st.rerun()
