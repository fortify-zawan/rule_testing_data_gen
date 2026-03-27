"""Background suggestion loader.

Starts a daemon thread that calls generate_suggestions() and stores the result
in a module-level dict keyed by Streamlit session ID.  The thread never touches
st.session_state, so there are no session-lock blocks or phantom reruns.
"""
import threading
from typing import Any

_lock = threading.Lock()
_store: dict[str, Any] = {}   # session_id -> "loading" | list[TestSuggestion]


def _session_key() -> str:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        ctx = get_script_run_ctx()
        if ctx:
            return ctx.session_id
    except Exception:
        pass
    return "default"


def start(rule) -> None:
    """Kick off a background thread to load suggestions for *rule*.

    No-op if loading is already in progress.  Call clear() first to force a reload.
    """
    key = _session_key()
    with _lock:
        if _store.get(key) == "loading":
            return
        _store[key] = "loading"

    def _worker():
        from llm.suggestion_generator import generate_suggestions
        try:
            results = generate_suggestions(rule)
        except Exception:
            results = []
        with _lock:
            _store[key] = results

    t = threading.Thread(target=_worker, daemon=True, name="suggestion-loader")
    t.start()


def poll() -> Any:
    """Return the current state for this session.

    Returns:
        None       — not started
        "loading"  — in progress
        list       — finished (may be empty if an error occurred)
    """
    key = _session_key()
    with _lock:
        return _store.get(key)


def clear() -> None:
    """Clear any cached or in-progress result for this session."""
    key = _session_key()
    with _lock:
        _store.pop(key, None)
