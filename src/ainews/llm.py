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


class LLMCredentialsError(LLMError):
    """Raised when no usable credentials could be resolved for the provider."""


CREDENTIALS_HINT = (
    "no usable credentials. Set ANTHROPIC_API_KEY, or run `ant auth login` to use a "
    "Claude subscription, or set llm.enabled: false (equivalently pass --no-llm) to "
    "run with keyword triage and no API calls."
)


def profile_credentials(profile: str | None = None) -> Path | None:
    """Path to the `ant auth login` credentials file for a profile, if it exists.

    A Claude subscription logged in with `ant auth login` needs no API key - the
    SDK finds the profile on disk by itself. Locating it matters for reporting:
    profiles are only consulted when no API key is set, so `ainews check` has to
    be able to say which source will actually win.
    """
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR")
    if not config_dir:
        appdata = os.environ.get("APPDATA")  # Windows
        config_dir = (
            str(Path(appdata) / "Anthropic") if appdata else os.path.expanduser("~/.config/anthropic")
        )
    name = profile or os.environ.get("ANTHROPIC_PROFILE") or "default"
    path = Path(config_dir) / "credentials" / f"{name}.json"
    return path if path.is_file() else None


def credentials_source() -> tuple[str, str]:
    """Which credential the SDK will use for the first-party API, and a note.

    Mirrors the SDK's own precedence: ANTHROPIC_API_KEY, then
    ANTHROPIC_AUTH_TOKEN, then the active `ant auth login` profile. Membership
    is what counts, not truthiness - an empty ANTHROPIC_API_KEY still claims its
    slot and authenticates with an empty key, which is the single most common
    way a working profile gets shadowed.
    """
    profile_name = os.environ.get("ANTHROPIC_PROFILE") or "default"
    profile = profile_credentials()

    if "ANTHROPIC_API_KEY" in os.environ:
        if not os.environ["ANTHROPIC_API_KEY"]:
            return "empty ANTHROPIC_API_KEY", (
                "an empty ANTHROPIC_API_KEY still wins over everything else and will "
                "fail to authenticate - unset it entirely"
            )
        shadowed = (
            f"; this shadows your `ant auth login` profile {profile_name!r} - unset it to use the profile"
            if profile
            else ""
        )
        return "ANTHROPIC_API_KEY", f"from the environment or .env{shadowed}"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN", "from the environment or .env"
    if profile:
        return f"ant profile {profile_name!r}", f"a Claude subscription login; no API key needed ({profile})"
    return "", ""


def is_credentials_error(exc: BaseException) -> bool:
    """True for the SDK's "could not resolve authentication" failure.

    The SDK raises a bare `TypeError` from header validation when no auth method
    resolves, and it does so at request time rather than at construction - so
    there is nothing to catch when the client is built, and the type alone is
    too broad to catch safely. Match on the message instead.
    """
    return isinstance(exc, TypeError) and "authentication method" in str(exc)


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
    except anthropic.APIStatusError as exc:
        raise LLMError(f"{label}: API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"{label}: could not reach the Claude API: {exc}") from exc
    except TypeError as exc:
        if is_credentials_error(exc):
            raise LLMCredentialsError(f"{label}: {CREDENTIALS_HINT}") from exc
        raise

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
    except TypeError as exc:
        if is_credentials_error(exc):
            raise LLMCredentialsError(f"{label}: {CREDENTIALS_HINT}") from exc
        raise

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
