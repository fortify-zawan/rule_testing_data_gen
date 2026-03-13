"""Session state manager — centralises all st.session_state access."""
import streamlit as st


def init_state():
    """Initialise all session state keys with defaults on first load."""
    defaults = {
        "step": "rule_input",       # rule_input | prototype_review | test_case_builder | test_suite
        "rule": None,               # domain.models.Rule
        "risky_proto": None,        # domain.models.Prototype
        "genuine_proto": None,      # domain.models.Prototype
        "risky_proto_approved": False,   # True after user approves risky prototype
        "genuine_proto_approved": False, # True after user approves genuine prototype
        "risky_cases": None,        # list[Transaction] for the current draft (not yet added to suite)
        "genuine_cases": None,      # list[Transaction] for the current draft (not yet added to suite)
        "risky_case_groups": [],    # list[list[Transaction]] — all approved risky prototype groups
        "genuine_case_groups": [],  # list[list[Transaction]] — all approved genuine prototype groups
        "stateless_sequence": None, # list[Transaction] (flattened all groups, for export)
        "behavioral_cases": [],     # list[BehavioralTestCase]
        "current_case": None,       # BehavioralTestCase being reviewed
        "status_log": [],           # list of progress messages shown during generation
        "suggestions": None,        # list[TestSuggestion] | None — None means not yet generated
        "prefill_scenario_type": None,        # behavioral: set by "Use this suggestion"
        "prefill_intent": None,               # behavioral: set by "Use this suggestion"
        "prefill_expected_outcome": None,     # behavioral: "FIRE" or "NOT_FIRE"
        "prefill_proto_scenario_type": None,  # stateless: set by "Use this suggestion"
        "prefill_proto_intent": None,         # stateless: set by "Use this suggestion"
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def go_to(step: str):
    st.session_state.step = step


def log_status(msg: str):
    st.session_state.status_log.append(msg)


def clear_status_log():
    st.session_state.status_log = []
