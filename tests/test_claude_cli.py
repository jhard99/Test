"""The claude_cli provider: synthesis without an API key or Console access.

For organizations that permit Claude Code but block the API and
platform.claude.com, the CLI is the only route to the written digest. These
tests mock subprocess, so they make no model calls.

The CLI path gives up two things the API path has - structured outputs and
`output_config` - so most of what is worth pinning here is the tolerant JSON
parsing that replaces the schema guarantee, and the failure modes.
"""

import json
import subprocess

import pytest

from ainews.config import LLMConfig
from ainews.llm import (
    ClaudeCliClient,
    LLMCredentialsError,
    LLMError,
    _extract_json,
    _system_text,
    build_client,
    capabilities,
    json_call,
    text_call,
)


def envelope(result, **over):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "usage": {"input_tokens": 2, "output_tokens": 9},
        "total_cost_usd": 0.01,
    }
    payload.update(over)
    return json.dumps(payload)


class FakeRun:
    """Stands in for subprocess.run, recording how the CLI was invoked."""

    def __init__(self, stdout="", returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.cmd = None
        self.input = None

    def __call__(self, cmd, input=None, capture_output=None, text=None, timeout=None):
        self.cmd = cmd
        self.input = input
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)

    def flag(self, name):
        """Value passed for a flag, so tests don't depend on argument order."""
        return self.cmd[self.cmd.index(name) + 1]


def test_build_client_returns_the_cli_client():
    client = build_client(LLMConfig(provider="claude_cli"))
    assert isinstance(client, ClaudeCliClient)
    assert client.binary == "claude"


def test_a_custom_binary_path_is_honoured():
    client = build_client(LLMConfig(provider="claude_cli", claude_binary="/opt/claude"))
    assert client.binary == "/opt/claude"


def test_the_provider_declares_no_server_side_web_search():
    """The websearch source reads Messages-API web_search result blocks, which
    the CLI does not produce - so it must skip itself rather than half-work."""
    caps = capabilities("claude_cli")
    assert caps.web_search is False
    assert caps.note


def test_text_call_passes_the_prompt_on_stdin_and_returns_the_result(monkeypatch):
    fake = FakeRun(stdout=envelope("# Digest\n\nBody."))
    monkeypatch.setattr(subprocess, "run", fake)

    out = text_call(
        ClaudeCliClient(), model="claude-opus-5", system="be terse", user="write it"
    )
    assert out == "# Digest\n\nBody."
    assert fake.input == "write it"
    assert fake.flag("--model") == "claude-opus-5"
    assert fake.flag("--system-prompt") == "be terse"
    assert "-p" in fake.cmd and "--output-format" in fake.cmd


def test_the_cli_is_denied_every_tool(monkeypatch):
    """Triage and writing are pure text transformations. A tool call would also
    stall an unattended run waiting on a permission prompt."""
    fake = FakeRun(stdout=envelope("ok"))
    monkeypatch.setattr(subprocess, "run", fake)
    text_call(ClaudeCliClient(), model="m", system="s", user="u")

    denied = fake.flag("--disallowed-tools")
    for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch"):
        assert tool in denied


def test_a_block_list_system_prompt_is_flattened(monkeypatch):
    """text_call is handed content blocks so the API path can put a cache
    breakpoint on them; --system-prompt takes one string."""
    fake = FakeRun(stdout=envelope("ok"))
    monkeypatch.setattr(subprocess, "run", fake)
    text_call(
        ClaudeCliClient(),
        model="m",
        system=[{"type": "text", "text": "first", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "second"}],
        user="u",
    )
    assert fake.flag("--system-prompt") == "first\n\nsecond"


def test_system_text_handles_both_shapes():
    assert _system_text("plain") == "plain"
    assert _system_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\n\nb"


def test_json_call_puts_the_schema_in_the_prompt(monkeypatch):
    """There is no output_config over the CLI, so the schema can only be asked
    for - which is why the reply is parsed tolerantly below."""
    fake = FakeRun(stdout=envelope('{"items": [{"index": 0}]}'))
    monkeypatch.setattr(subprocess, "run", fake)

    schema = {"type": "object", "properties": {"items": {"type": "array"}}}
    got = json_call(
        ClaudeCliClient(), model="m", system="triage", user="[]", schema=schema
    )
    assert got == {"items": [{"index": 0}]}
    sent = fake.flag("--system-prompt")
    assert "triage" in sent
    assert "JSON Schema" in sent
    assert json.dumps(schema, ensure_ascii=False) in sent


@pytest.mark.parametrize(
    "reply",
    [
        '{"items": []}',                                  # clean
        '```json\n{"items": []}\n```',                    # fenced
        '```\n{"items": []}\n```',                        # fenced, unlabelled
        'Here you go:\n\n{"items": []}\n\nHope that helps!',  # wrapped in prose
    ],
)
def test_json_is_recovered_from_however_the_model_wrapped_it(reply, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout=envelope(reply)))
    got = json_call(
        ClaudeCliClient(), model="m", system="s", user="u", schema={"type": "object"}
    )
    assert got == {"items": []}


def test_unparseable_json_raises_with_a_usable_excerpt():
    with pytest.raises(LLMError, match="not valid JSON"):
        _extract_json("I'd rather not answer that.", "triage")


def test_a_missing_binary_is_a_credentials_error(monkeypatch):
    """Distinct from a transient failure: retrying will not help, and the
    message has to say what to install."""
    monkeypatch.setattr(subprocess, "run", FakeRun(exc=FileNotFoundError()))
    with pytest.raises(LLMCredentialsError, match="not on PATH"):
        text_call(ClaudeCliClient(), model="m", system="s", user="u")


def test_a_login_failure_is_a_credentials_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", FakeRun(returncode=1, stderr="Please run /login to continue")
    )
    with pytest.raises(LLMCredentialsError):
        text_call(ClaudeCliClient(), model="m", system="s", user="u")


def test_other_nonzero_exits_are_ordinary_errors(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=2, stderr="boom"))
    with pytest.raises(LLMError, match="exited 2"):
        text_call(ClaudeCliClient(), model="m", system="s", user="u")


def test_a_timeout_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", FakeRun(exc=subprocess.TimeoutExpired("claude", 1))
    )
    with pytest.raises(LLMError, match="did not finish"):
        text_call(ClaudeCliClient(timeout=1), model="m", system="s", user="u")


def test_an_error_envelope_is_not_treated_as_a_reply(monkeypatch):
    """The CLI can exit 0 and still report failure in the envelope."""
    monkeypatch.setattr(
        subprocess,
        "run",
        FakeRun(stdout=envelope("", is_error=True, subtype="error_during_execution")),
    )
    with pytest.raises(LLMError, match="reported failure"):
        text_call(ClaudeCliClient(), model="m", system="s", user="u")


def test_an_empty_result_is_an_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout=envelope("   ")))
    with pytest.raises(LLMError, match="empty reply"):
        text_call(ClaudeCliClient(), model="m", system="s", user="u")


def test_non_json_stdout_is_an_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="not json at all"))
    with pytest.raises(LLMError, match="JSON envelope"):
        text_call(ClaudeCliClient(), model="m", system="s", user="u")
