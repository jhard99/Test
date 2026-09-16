"""Thin wrapper around the Anthropic SDK: one place for model defaults,
structured-output calls and error handling."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when Claude could not produce a usable answer."""


def build_client(timeout: float = 600.0) -> anthropic.Anthropic:
    """Resolves credentials from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
    `ant auth login` profile."""
    return anthropic.Anthropic(timeout=timeout, max_retries=3)


def supports_adaptive_thinking(model: str) -> bool:
    """Adaptive thinking is the current-generation shape; Haiku 4.5 and older
    models still use the deprecated budget_tokens form, so we just omit it."""
    model = model.lower()
    if "haiku" in model:
        return False
    return not re.search(r"-(3|4|4-5)(-|$)", model)


def _thinking(model: str) -> dict[str, str] | None:
    return {"type": "adaptive"} if supports_adaptive_thinking(model) else None


def _first_text(response: Any) -> str:
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _check_stop(response: Any, label: str) -> None:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise LLMError(f"{label}: Claude declined this request (category={category}).")
    if response.stop_reason == "max_tokens":
        raise LLMError(f"{label}: response hit max_tokens - raise llm.max_output_tokens.")


def json_call(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    effort: str = "medium",
    max_tokens: int = 16000,
    label: str = "json_call",
) -> Any:
    """One structured-output request. Returns parsed JSON matching `schema`."""
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": schema},
        "effort": effort,
    }
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": output_config,
    }
    thinking = _thinking(model)
    if thinking:
        kwargs["thinking"] = thinking

    try:
        response = client.messages.create(**kwargs)
    except anthropic.APIStatusError as exc:
        raise LLMError(f"{label}: API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"{label}: could not reach the Claude API: {exc}") from exc

    _check_stop(response, label)
    text = _first_text(response)
    if not text:
        raise LLMError(f"{label}: empty response.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{label}: response was not valid JSON: {exc}") from exc


def text_call(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str | list[dict[str, Any]],
    user: str,
    effort: str = "high",
    max_tokens: int = 32000,
    label: str = "text_call",
) -> str:
    """One long-form request. Streams so a big max_tokens can't hit the HTTP
    timeout, and returns the assembled text."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort},
    }
    thinking = _thinking(model)
    if thinking:
        kwargs["thinking"] = thinking

    try:
        with client.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        raise LLMError(f"{label}: API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"{label}: could not reach the Claude API: {exc}") from exc

    _check_stop(response, label)
    usage = response.usage
    log.info(
        "%s: %s in=%s out=%s cache_read=%s",
        label,
        model,
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
    )
    text = _first_text(response)
    if not text:
        raise LLMError(f"{label}: empty response.")
    return text
