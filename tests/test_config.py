import pytest

from ainews.config import Config, ConfigError, expand_env, load_dotenv


def test_expand_env_uses_value_then_default(monkeypatch):
    monkeypatch.setenv("SET_VAR", "real")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert expand_env("${SET_VAR}") == "real"
    assert expand_env("${UNSET_VAR:-fallback}") == "fallback"
    assert expand_env("${UNSET_VAR}") == ""
    assert expand_env({"a": ["${SET_VAR}"]}) == {"a": ["real"]}


def test_empty_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BLANK", "")
    assert expand_env("${BLANK:-default}") == "default"


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('EXISTING=from_file\nNEW="quoted"\n# comment\n\n')
    monkeypatch.setenv("EXISTING", "from_shell")
    monkeypatch.delenv("NEW", raising=False)
    load_dotenv(env_file)
    import os

    assert os.environ["EXISTING"] == "from_shell"
    assert os.environ["NEW"] == "quoted"


def test_from_dict_parses_sources_and_nested_sections():
    cfg = Config.from_dict(
        {
            "window_days": 14,
            "llm": {"triage_model": "claude-sonnet-5", "triage_batch_size": "25"},
            "http": {"respect_robots": "false", "cookie_domains": ["nytimes.com"]},
            "delivery": {"smtp": {"host": "smtp.test", "port": "465", "sender": "a@b.c", "recipients": ["d@e.f"]}},
            "sources": [
                {"type": "rss", "name": "Feed", "url": "https://example.com/feed"},
                {"type": "arxiv", "enabled": False},
            ],
        }
    )
    assert cfg.window_days == 14
    assert cfg.llm.triage_batch_size == 25
    assert cfg.http.respect_robots is False
    assert cfg.smtp.port == 465 and cfg.smtp.configured
    assert [s.type for s in cfg.sources] == ["rss", "arxiv"]
    assert cfg.sources[0].options == {"url": "https://example.com/feed"}
    assert cfg.sources[1].name == "arxiv" and cfg.sources[1].enabled is False


def test_recipients_accepts_comma_separated_string():
    cfg = Config.from_dict({"delivery": {"smtp": {"host": "h", "sender": "s@x", "recipients": "a@x, b@x"}}})
    assert cfg.smtp.recipients == ["a@x", "b@x"]


def test_source_without_type_is_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict({"sources": [{"name": "nameless"}]})


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "nope.yaml")


def test_example_config_loads(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("SMTP_USER", "u@example.com")
    cfg = Config.load(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert cfg.sources and cfg.llm.model == "claude-opus-5"
    assert {"imap", "paywalled", "hackernews", "websearch"} <= {s.type for s in cfg.sources}
