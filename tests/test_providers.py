"""Reaching Claude without a first-party API key."""

import anthropic
import pytest

from ainews.config import Config, LLMConfig
from ainews.llm import LLMError, build_client, capabilities, resolve_model


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
