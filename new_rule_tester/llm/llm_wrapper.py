"""Thin wrapper around the Anthropic SDK."""
import json
import time
import anthropic
from logging_config import get_logger

log = get_logger(__name__)

_client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from environment


def call_llm(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 4096,
) -> str:
    """Call the LLM and return the raw text response."""
    log.info("LLM call | model=%s max_tokens=%d prompt_chars=%d", model, max_tokens, len(prompt))
    log.debug("LLM prompt:\n%s", prompt)

    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system

    t0 = time.monotonic()
    response = _client.messages.create(**kwargs)
    elapsed = time.monotonic() - t0

    text = response.content[0].text
    log.info("LLM response | chars=%d elapsed=%.2fs", len(text), elapsed)
    log.debug("LLM raw response:\n%s", text)
    return text


def call_llm_json(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 4096,
) -> dict:
    """Call the LLM expecting a JSON response. Strips markdown fences if present."""
    raw = call_llm(prompt, system=system, model=model, max_tokens=max_tokens)
    text = raw.strip()
    # Strip markdown code fences if the model wraps the JSON
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("LLM JSON parse failed: %s | raw_text_preview=%r", exc, text[:300])
        raise
