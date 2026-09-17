"""Reaching Claude without a first-party API key."""

import anthropic
import pytest

from ainews.config import Config, LLMConfig
from ainews.llm import (
    LLMError,
    build_client,
    capabilities,
    credentials_source,
    profile_credentials,
    resolve_model,
)


def test_default_provider_is_the_first_party_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = build_client(LLMConfig())
    assert isinstance(client, anthropic.Anthropic)


def test_bedrock_client_needs_no_anthropic_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    client = build_client(LLMConfig(provider="bedrock", aws_region="us-west-2"))
    assert isinstance(client, anthropic.AnthropicBedrockMantle)


def test_vertex_client_needs_a_project(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="vertex_project"):
        build_client(LLMConfig(provider="vertex"))


def test_unknown_provider_is_rejected():
    with pytest.raises(LLMError, match="Unknown llm.provider"):
        build_client(LLMConfig(provider="my-own-gateway"))


def test_bedrock_model_ids_are_prefixed():
    assert resolve_model("bedrock", "claude-opus-5") == "anthropic.claude-opus-5"
    assert resolve_model("bedrock", "anthropic.claude-opus-5") == "anthropic.claude-opus-5"
    assert resolve_model("anthropic", "claude-opus-5") == "claude-opus-5"
    assert resolve_model("vertex", "claude-opus-5") == "claude-opus-5"


def test_capabilities_match_platform_support():
    # Bedrock has no server-side web search; Vertex only the basic variant.
    assert capabilities("bedrock").web_search is False
    assert capabilities("vertex").web_search_tool_type == "web_search_20250305"
    assert capabilities("anthropic").web_search_tool_type == "web_search_20260209"


def test_websearch_source_skips_itself_when_unsupported(tmp_path):
    from ainews.sources import build_source
    from ainews.sources.base import Context
    from ainews.config import SourceConfig
    from ainews.store import Store
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "s.sqlite3")
    try:
        for llm_cfg in (LLMConfig(provider="bedrock"), LLMConfig(enabled=False)):
            cfg = Config()
            cfg.llm = llm_cfg
            ctx = Context(config=cfg, fetcher=None, store=store)
            source = build_source(SourceConfig(type="websearch", name="Web", options={}), ctx)
            # Returns empty instead of raising - no client is ever constructed.
            assert source.fetch(datetime.now(timezone.utc) - timedelta(days=7)) == []
    finally:
        store.close()


def test_config_parses_provider_settings():
    cfg = Config.from_dict({"llm": {"provider": "bedrock", "enabled": "false", "aws_region": "eu-west-1"}})
    assert cfg.llm.provider == "bedrock"
    assert cfg.llm.enabled is False
    assert cfg.llm.aws_region == "eu-west-1"


# --- credential precedence (subscription login vs API key) -------------------
#
# The SDK consults an `ant auth login` profile only when no API key is set, and
# *membership* is what counts, not truthiness. These pin the reporting that
# `ainews check` does, because a shadowed profile otherwise looks like a
# working one right up until the run fails to authenticate.


def _profile(tmp_path, name="default"):
    creds = tmp_path / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / f"{name}.json").write_text("{}")
    return tmp_path


def test_profile_is_found_when_no_key_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(_profile(tmp_path)))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert profile_credentials() is not None
    source, note = credentials_source()
    assert "ant profile" in source
    assert "no API key needed" in note


def test_no_credentials_at_all_reports_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))  # no credentials/ dir
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert profile_credentials() is None
    assert credentials_source() == ("", "")


def test_an_api_key_shadows_the_profile_and_check_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(_profile(tmp_path)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    source, note = credentials_source()
    assert source == "ANTHROPIC_API_KEY"
    assert "shadows" in note


def test_an_empty_api_key_still_wins_its_slot(tmp_path, monkeypatch):
    """The nastiest case: `ANTHROPIC_API_KEY=` in a .env authenticates with an
    empty key and beats a perfectly good profile."""
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(_profile(tmp_path)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    source, note = credentials_source()
    assert source == "empty ANTHROPIC_API_KEY"
    assert "unset it" in note


def test_named_profile_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(_profile(tmp_path, "work")))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_PROFILE", "work")
    assert profile_credentials() is not None
    assert "work" in credentials_source()[0]
    # The default profile does not exist in that dir.
    monkeypatch.setenv("ANTHROPIC_PROFILE", "default")
    assert profile_credentials() is None


def test_auth_token_beats_a_profile_but_not_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(_profile(tmp_path)))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-token")
    assert credentials_source()[0] == "ANTHROPIC_AUTH_TOKEN"
