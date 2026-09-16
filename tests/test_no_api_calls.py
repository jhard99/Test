"""`fetch` and `--no-llm` are documented as making no API calls.

The websearch source reaches Claude during *collection*, which happens before
triage - so disabling the model around the triage/write stages was not enough,
and both commands were quietly issuing API requests (and crashing with a bare
TypeError when no credentials were set).
"""

import anthropic
import pytest

from ainews.config import Config, LLMConfig, SourceConfig
from ainews.llm import (
    CREDENTIALS_HINT,
    LLMCredentialsError,
    is_credentials_error,
    json_call,
    text_call,
)
from ainews.sources import build_source
from ainews.sources.base import Context
from ainews.store import Store


class ExplodingClient:
    """Stands in for a client with no resolvable credentials, which is what the
    SDK does: a bare TypeError raised at request time, not at construction."""

    def __init__(self):
        self.messages = self

    def _boom(self, **kwargs):
        raise TypeError(
            '"Could not resolve authentication method. Expected one of api_key, '
            'auth_token, or credentials to be set."'
        )

    create = _boom
    stream = _boom


def _ctx(tmp_path, llm):
    cfg = Config()
    cfg.llm = llm
    return Context(config=cfg, fetcher=None, store=Store(tmp_path / "s.sqlite3"))


def test_websearch_source_makes_no_call_when_the_model_is_disabled(tmp_path):
    ctx = _ctx(tmp_path, LLMConfig(enabled=False))
    source = build_source(SourceConfig(type="websearch", name="Web search", options={}), ctx)
    # No fetcher and no client are wired up, so any attempt to reach out would
    # raise rather than return an empty list.
    assert source.fetch(__import__("datetime").datetime.now(__import__("datetime").timezone.utc)) == []
    ctx.store.close()


def test_credentials_error_is_recognised():
    exc = TypeError('"Could not resolve authentication method. Expected one of api_key"')
    assert is_credentials_error(exc)
    assert not is_credentials_error(TypeError("unrelated type problem"))
    assert not is_credentials_error(ValueError("not even a TypeError"))


def test_json_call_turns_missing_credentials_into_an_actionable_error():
    with pytest.raises(LLMCredentialsError) as caught:
        json_call(
            ExplodingClient(),
            model="claude-opus-5",
            system="s",
            user="u",
            schema={"type": "object"},
        )
    assert "ANTHROPIC_API_KEY" in str(caught.value)
    assert "--no-llm" in str(caught.value)


def test_text_call_turns_missing_credentials_into_an_actionable_error():
    with pytest.raises(LLMCredentialsError):
        text_call(ExplodingClient(), model="claude-opus-5", system="s", user="u")


def test_an_unrelated_type_error_is_not_swallowed():
    class OtherBoom:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            raise TypeError("genuinely a bug in our own call")

    with pytest.raises(TypeError, match="genuinely a bug"):
        json_call(
            OtherBoom(),
            model="claude-opus-5",
            system="s",
            user="u",
            schema={"type": "object"},
        )


def test_credentials_hint_names_every_escape_route():
    for expected in ("ANTHROPIC_API_KEY", "ant auth login", "llm.enabled", "--no-llm"):
        assert expected in CREDENTIALS_HINT
