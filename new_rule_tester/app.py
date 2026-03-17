"""AML Rule Tester — Streamlit entry point.

Run with:
    streamlit run app.py
"""
import os
import sys

# Ensure the project root is on the path so all imports resolve correctly
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

from ui.pages import prototype_review, rule_input, test_case_builder, test_suite
from ui.state import init_state

st.set_page_config(
    page_title="AML Rule Tester",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Progress indicator in sidebar ────────────────────────────────────────────
init_state()

STEPS = {
    "rule_input": "1. Rule Input",
    "prototype_review": "2a. Prototype Review",
    "test_case_builder": "2b. Test Case Builder",
    "test_suite": "3. Test Suite",
}

with st.sidebar:
    st.markdown("## AML Rule Tester")
    st.markdown("---")
    current_step = st.session_state.step
    for key, label in STEPS.items():
        if key == current_step:
            st.markdown(f"**→ {label}**")
        else:
            st.markdown(f"&nbsp;&nbsp;&nbsp;{label}")

    # Show generation status log if present
    if st.session_state.status_log:
        st.markdown("---")
        st.markdown("**Generation Log**")
        for msg in st.session_state.status_log[-10:]:
            st.caption(msg)

# ── Page routing ──────────────────────────────────────────────────────────────
step = st.session_state.step

if step == "rule_input":
    rule_input.render()
elif step == "prototype_review":
    prototype_review.render()
elif step == "test_case_builder":
    test_case_builder.render()
elif step == "test_suite":
    test_suite.render()
else:
    st.error(f"Unknown step: {step}")
    st.session_state.step = "rule_input"
    st.rerun()
