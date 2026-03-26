"""Thin wrapper around the Anthropic SDK."""
import json
import os
import time

import anthropic

from logging_config import get_logger

log = get_logger(__name__)

_client = None


def _get_client() -> anthropic.Anthropic:
    """Return an Anthropic client, using the session-state key if available."""
    global _client

    # Try to read the key from Streamlit session state first
    api_key = None
    try:
        import streamlit as st

        api_key = st.session_state.get("api_key", "").strip() or None
    except Exception:
        pass

    # Fall back to the environment variable
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY")

    # Fall back to Streamlit secrets
    if not api_key:
        try:
            import streamlit as st

            api_key = st.secrets.get("ANTHROPIC_API_KEY", "").strip() or None
        except Exception:
            pass

    if not api_key:
        raise ValueError(
            "No API key configured. Set ANTHROPIC_API_KEY in the sidebar Settings, "
            "as an environment variable, or in .streamlit/secrets.toml."
        )

    # Re-create client if key changed or client doesn't exist
    if _client is None or _client.api_key != api_key:
        _client = anthropic.Anthropic(api_key=api_key)

    return _client


def call_llm(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 4096,
) -> str:
    """Call the LLM and return the raw text response."""
    log.info("LLM call | model=%s max_tokens=%d prompt_chars=%d", model, max_tokens, len(prompt))
    log.debug("LLM prompt:\n%s", prompt)

    client = _get_client()
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    t0 = time.monotonic()
    response = client.messages.create(**kwargs)
    elapsed = time.monotonic() - t0

    text = response.content[0].text
    log.info("LLM response | chars=%d elapsed=%.2fs", len(text), elapsed)
    log.debug("LLM raw response:\n%s", text)
    return text


def call_llm_json(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 8192,
    max_retries: int = 2,
) -> dict:
    """Call the LLM expecting a JSON response. Strips markdown fences and trailing text.

    Retries up to max_retries times on empty or unparseable responses before raising.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        raw = call_llm(prompt, system=system, model=model, max_tokens=max_tokens)
        text = raw.strip()
        # Strip markdown code fences if the model wraps the JSON
        if text.startswith("```"):
            lines = text.splitlines()
            inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            text = "\n".join(inner)

        if not text:
            last_exc = json.JSONDecodeError("LLM returned empty response", "", 0)
            log.warning(
                "LLM returned empty response (attempt %d/%d) — will retry",
                attempt + 1, max_retries + 1,
            )
        else:
            try:
                # raw_decode parses the first complete JSON value and ignores any trailing
                # text, which handles the case where the model appends an explanation.
                obj, _ = json.JSONDecoder().raw_decode(text)
                if attempt > 0:
                    log.info("LLM JSON parse succeeded on retry attempt %d", attempt + 1)
                return obj
            except json.JSONDecodeError as exc:
                last_exc = exc
                log.warning(
                    "LLM JSON parse failed (attempt %d/%d): %s | raw_text_preview=%r",
                    attempt + 1, max_retries + 1, exc, text[:300],
                )

    log.error(
        "LLM JSON parse failed after %d attempt(s). Last error: %s",
        max_retries + 1, last_exc,
    )
    raise last_exc
