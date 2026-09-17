"""Thin wrapper around the Anthropic SDK: one place for model defaults,
structured-output calls and error handling."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
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
    # The `claude` CLI in non-interactive mode. It has its own WebSearch tool,
    # but not the Messages API's server-side one whose result blocks the
    # websearch source reads - so that source skips itself, as on Bedrock.
    "claude_cli": Capabilities(
        False, None, "the claude CLI does not expose the API's server-side web search"
    ),
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


#: Everything the CLI could otherwise reach. Triage and digest-writing are pure
#: text transformations - they have no business touching the filesystem, running
#: commands, or fetching URLs, and a tool call would also stall an unattended run
#: waiting for a permission prompt.
_CLI_DISALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,NotebookEdit,Glob,Grep,WebSearch,WebFetch,Task,TodoWrite"
)


@dataclass(frozen=True)
class ClaudeCliClient:
    """Runs the `claude` CLI non-interactively instead of calling the API.

    For organizations that allow Claude Code but not API or Console access: the
    CLI authenticates with Claude Code's own login, so there is no API key and
    no platform.claude.com round trip.

    Deliberately *not* an SDK-shaped client - it does not pretend to implement
    `messages.create`. `json_call` and `text_call` special-case it, because the
    CLI has no structured-output mode and no `output_config`, so those two
    differences have to be visible at the call site rather than hidden behind a
    lookalike interface.
    """

    binary: str = "claude"
    timeout: float = 1800.0

    def run(self, *, system: str, user: str, model: str, label: str = "claude-cli") -> str:
        """One non-interactive turn. Returns the reply text."""
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--system-prompt",
            system,
            "--disallowed-tools",
            _CLI_DISALLOWED_TOOLS,
        ]
        try:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise LLMCredentialsError(
                f"{label}: {self.binary!r} is not on PATH. Install Claude Code, or set "
                "llm.provider back to anthropic/bedrock/vertex/foundry."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"{label}: the claude CLI did not finish within {self.timeout:.0f}s") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400] or "no output"
            # The CLI exits non-zero when it isn't logged in, which is the one
            # failure worth naming precisely - it is fixed by `claude` login,
            # not by retrying.
            error = LLMCredentialsError if "login" in detail.lower() else LLMError
            raise error(f"{label}: claude CLI exited {proc.returncode}: {detail}")

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{label}: could not parse the CLI's JSON envelope: {exc}") from exc

        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise LLMError(
                f"{label}: claude CLI reported failure "
                f"({envelope.get('subtype')}: {envelope.get('api_error_status')})"
            )

        usage = envelope.get("usage") or {}
        log.info(
            "%s: %s in=%s out=%s cache_read=%s cost_usd=%s",
            label,
            model,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            envelope.get("total_cost_usd"),
        )

        text = (envelope.get("result") or "").strip()
        if not text:
            raise LLMError(f"{label}: the claude CLI returned an empty reply.")
        return text


def _system_text(system: str | list[dict[str, Any]]) -> str:
    """Flatten a system prompt to plain text.

    `text_call` is given a list of content blocks (so the API path can put a
    cache breakpoint on it); the CLI takes a single `--system-prompt` string.
    """
    if isinstance(system, str):
        return system
    return "\n\n".join(block.get("text", "") for block in system).strip()


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str, label: str) -> Any:
    """Parse a JSON object out of a model's reply.

    The API path constrains the response with `output_config.format`, so it can
    parse the text directly. The CLI has no equivalent, so the schema is only a
    request: the reply may arrive fenced, or with a sentence wrapped around it.
    Try strict, then a fenced block, then the outermost braces.
    """
    candidates = [text]
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
    raise LLMError(f"{label}: the claude CLI's reply was not valid JSON: {text[:200]!r}")


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
    if provider == "claude_cli":
        # Claude Code's own credentials; no API key, no Console access needed.
        return ClaudeCliClient(binary=config.claude_binary or "claude")
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
    if isinstance(client, ClaudeCliClient):
        # No `output_config` over the CLI, so the schema becomes part of the
        # prompt and the reply is parsed tolerantly. Same contract to callers,
        # weaker guarantee underneath - hence the explicit branch.
        instructed = (
            f"{_system_text(system)}\n\n"
            "Reply with a single JSON object and nothing else - no prose, no code "
            "fence - conforming to this JSON Schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        text = client.run(system=instructed, user=user, model=model, label=label)
        return _extract_json(text, label)

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
    if isinstance(client, ClaudeCliClient):
        return client.run(
            system=_system_text(system), user=user, model=model, label=label
        )

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
