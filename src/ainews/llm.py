"""Thin wrapper around the Anthropic SDK: one place for model defaults,
structured-output calls and error handling."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic

if TYPE_CHECKING:
    from ainews.config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when Claude could not produce a usable answer."""


class CredentialsMissing(LLMError):
    """No usable credentials for the configured provider.

    Separate from LLMError because callers treat it differently: a failed
    request is worth retrying or reporting, but missing credentials mean the
    whole model path is unavailable and the run should degrade, not die.
    """


def credentials_available(config: "LLMConfig | None" = None) -> bool:
    """Best-effort check for credentials, before any request is made.

    For the cloud providers we return True and let their own credential chains
    speak for themselves at call time - second-guessing boto3 or ADC here would
    produce worse errors than they do.
    """
    from ainews.config import LLMConfig

    config = config or LLMConfig()
    if (config.provider or "anthropic").lower() != "anthropic":
        return True
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # An `ant auth login` profile, which the SDK picks up with no env var set.
    profile_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    return profile_dir.is_dir() and any(profile_dir.iterdir())


@dataclass(frozen=True)
class Capabilities:
    """What a given platform can do, so sources can adapt instead of erroring."""

    web_search: bool
    web_search_tool_type: str | None = None
    note: str = ""


_CAPABILITIES = {
    # First-party API: everything.
    "anthropic": Capabilities(True, "web_search_20260209"),
    # Bedrock has no server-side web search at all.
    "bedrock": Capabilities(False, None, "Amazon Bedrock does not offer server-side web search"),
    # Vertex has the basic variant only (no dynamic filtering).
    "vertex": Capabilities(True, "web_search_20250305", "Vertex AI supports basic web search only"),
    # Foundry's server tools are in beta.
    "foundry": Capabilities(True, "web_search_20250305", "Microsoft Foundry server tools are in beta"),
}

PROVIDERS = tuple(_CAPABILITIES)


def capabilities(provider: str) -> Capabilities:
    return _CAPABILITIES.get(provider, _CAPABILITIES["anthropic"])


def resolve_model(provider: str, model: str) -> str:
    """Bedrock model ids carry an `anthropic.` prefix; the others do not."""
    if provider == "bedrock" and not model.startswith("anthropic."):
        return f"anthropic.{model}"
    return model


def build_client(config: "LLMConfig | None" = None, timeout: float = 600.0):
    """Build the client for the configured platform.

    Default (`anthropic`) resolves credentials from ANTHROPIC_API_KEY,
    ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile - so a Claude
    subscription logged in with the `ant` CLI works with no API key set.
    The other providers authenticate with that cloud's own credentials.
    """
    from ainews.config import LLMConfig

    config = config or LLMConfig()
    provider = (config.provider or "anthropic").lower()
    common = {"timeout": timeout, "max_retries": 3}

    if provider == "anthropic":
        return anthropic.Anthropic(**common)
    if provider == "bedrock":
        # AWS credentials come from the usual boto3 chain (env, profile, role).
        return anthropic.AnthropicBedrockMantle(aws_region=config.aws_region, **common)
    if provider == "vertex":
        if not config.vertex_project:
            raise LLMError("provider 'vertex' needs llm.vertex_project (your GCP project id).")
        return anthropic.AnthropicVertex(
            project_id=config.vertex_project, region=config.vertex_region, **common
        )
    if provider == "foundry":
        if not config.foundry_resource:
            raise LLMError("provider 'foundry' needs llm.foundry_resource.")
        return anthropic.AnthropicFoundry(resource=config.foundry_resource, **common)
    raise LLMError(f"Unknown llm.provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}.")


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
    except anthropic.AuthenticationError as exc:
        raise CredentialsMissing(f"{label}: credentials were rejected: {exc}") from exc
    except TypeError as exc:
        # The SDK raises a bare TypeError when no credential source resolves.
        if "authentication" in str(exc).lower():
            raise CredentialsMissing(f"{label}: {exc}") from exc
        raise
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
    except anthropic.AuthenticationError as exc:
        raise CredentialsMissing(f"{label}: credentials were rejected: {exc}") from exc
    except TypeError as exc:
        if "authentication" in str(exc).lower():
            raise CredentialsMissing(f"{label}: {exc}") from exc
        raise
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
