"""Subscription sites: cookie scoping, and noticing when a session expires.

The two things that can go quietly wrong once you wire up your own NYT/WSJ
login: a full browser cookie export leaking credentials for unrelated sites,
and the export expiring so every article silently becomes a paywall stub.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ainews.config import Config, HttpConfig, SourceConfig
from ainews.http import Fetcher
from ainews.sources import build_source
from ainews.sources.base import Context
from ainews.store import Store

from test_sources import FakeFetcher, FakeResponse, rss_bytes

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=7)

COOKIE_FILE = """# Netscape HTTP Cookie File
.nytimes.com\tTRUE\t/\tTRUE\t1900000000\tNYT-S\tsession
.wsj.com\tTRUE\t/\tTRUE\t1900000000\twsjregion\tvalue
.my-bank.example\tTRUE\t/\tTRUE\t1900000000\tSESSION\tmust-not-leak
.github.com\tTRUE\t/\tTRUE\t1900000000\tuser_session\tmust-not-leak
"""

PAYWALL_STUB = (
    "<html><body><article><p>The opening paragraph of the piece is visible. "
    "Subscribe to continue reading your article.</p></article></body></html>"
)
FULL_ARTICLE = (
    "<html><body><article><p>"
    + ("A real sentence of subscriber reporting with enough words to count. " * 30)
    + "</p></article></body></html>"
)


@pytest.fixture()
def cookie_jar(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(COOKIE_FILE)
    return path


def test_only_allowed_domains_receive_cookies(cookie_jar):
    """A full browser export must not hand unrelated sites' credentials to this
    tool. Everything outside http.cookie_domains is dropped at load time."""
    fetcher = Fetcher(
        HttpConfig(
            cookie_domains=["nytimes.com", "wsj.com", "theatlantic.com"],
            cookies_file=str(cookie_jar),
            requests_per_minute=0,
        )
    )
    held = {c.domain.lstrip(".") for c in fetcher.session.cookies}
    assert held == {"nytimes.com", "wsj.com"}
    assert not any("bank" in d or "github" in d for d in held)

    assert fetcher.has_credentials_for("nytimes.com")
    assert fetcher.has_credentials_for("www.nytimes.com")  # subdomains count
    assert not fetcher.has_credentials_for("github.com")
    assert fetcher.authenticated_domains == {"nytimes.com", "wsj.com"}
    fetcher.close()


def test_a_domain_with_no_cookies_is_not_authenticated(cookie_jar):
    """theatlantic.com is allowed but absent from the file."""
    fetcher = Fetcher(
        HttpConfig(
            cookie_domains=["nytimes.com", "theatlantic.com"],
            cookies_file=str(cookie_jar),
            requests_per_minute=0,
        )
    )
    assert not fetcher.has_credentials_for("theatlantic.com")
    fetcher.close()


def test_a_missing_cookie_file_degrades_instead_of_raising(tmp_path):
    fetcher = Fetcher(
        HttpConfig(cookie_domains=["nytimes.com"], cookies_file=str(tmp_path / "nope.txt"))
    )
    assert fetcher.authenticated_domains == set()
    fetcher.close()


def _ctx(tmp_path, fetcher, name="s"):
    return Context(config=Config(), fetcher=fetcher, store=Store(tmp_path / f"{name}.sqlite3"))


def _paywalled_source(ctx, url):
    return build_source(
        SourceConfig(
            type="paywalled",
            name="NYT",
            options={"url": url, "domain": "nytimes.com", "full_text": True},
        ),
        ctx,
    )


def test_expired_cookies_are_reported_once_not_per_article(tmp_path, caplog):
    """The failure this guards: cookies present, every article a stub, digest
    quietly thinner, and nothing saying the session died."""
    feed = "https://nyt.example/feed"
    entries = [(f"Story {i}", f"https://nyt.example/{i}", NOW, "teaser") for i in range(4)]
    responses = {feed: FakeResponse(rss_bytes(entries))}
    for _, url, _, _ in entries:
        responses[url] = FakeResponse(PAYWALL_STUB.encode())

    fetcher = FakeFetcher(responses, authenticated={"nytimes.com"})
    ctx = _ctx(tmp_path, fetcher, "expired")
    with caplog.at_level("WARNING"):
        items = _paywalled_source(ctx, feed).fetch(SINCE)

    assert len(items) == 4
    assert all(i.extra["paywalled"] for i in items)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("probably expired" in w and "re-export" in w for w in warnings), warnings
    ctx.store.close()


def test_working_cookies_produce_no_expiry_warning(tmp_path, caplog):
    feed = "https://nyt.example/feed"
    entries = [(f"Story {i}", f"https://nyt.example/{i}", NOW, "teaser") for i in range(4)]
    responses = {feed: FakeResponse(rss_bytes(entries))}
    for _, url, _, _ in entries:
        responses[url] = FakeResponse(FULL_ARTICLE.encode())

    fetcher = FakeFetcher(responses, authenticated={"nytimes.com"})
    ctx = _ctx(tmp_path, fetcher, "working")
    with caplog.at_level("WARNING"):
        items = _paywalled_source(ctx, feed).fetch(SINCE)

    assert all("A real sentence" in i.text for i in items)
    assert not any(i.extra.get("paywalled") for i in items)
    assert not [r for r in caplog.records if "expired" in r.getMessage()]
    ctx.store.close()


def test_no_warning_without_cookies_at_all(tmp_path, caplog):
    """Unauthenticated is the documented default, not a fault - it must not
    produce an expiry warning."""
    feed = "https://nyt.example/feed"
    fetcher = FakeFetcher(
        {feed: FakeResponse(rss_bytes([("Story", "https://nyt.example/1", NOW, "teaser")]))}
    )
    ctx = _ctx(tmp_path, fetcher, "anon")
    with caplog.at_level("WARNING"):
        items = _paywalled_source(ctx, feed).fetch(SINCE)
    assert items[0].extra["authenticated"] is False
    assert not [r for r in caplog.records if "expired" in r.getMessage()]
    ctx.store.close()


def test_a_cached_stub_is_still_flagged_as_paywalled(tmp_path):
    """A stub is cached like any other body, so on a later run the item would
    otherwise look like a complete article."""
    feed = "https://nyt.example/feed"
    article = "https://nyt.example/1"
    responses = {
        feed: FakeResponse(rss_bytes([("Story", article, NOW, "teaser")])),
        article: FakeResponse(PAYWALL_STUB.encode()),
    }
    fetcher = FakeFetcher(responses, authenticated={"nytimes.com"})
    ctx = _ctx(tmp_path, fetcher, "cached")

    first = _paywalled_source(ctx, feed).fetch(SINCE)
    assert first[0].extra["paywalled"] is True

    # Second run: the article body comes from the cache, not the network.
    before = len(fetcher.calls)
    second = _paywalled_source(ctx, feed).fetch(SINCE)
    assert second[0].extra["paywalled"] is True, "cached stub lost its paywalled flag"
    assert len(fetcher.calls) == before + 1  # feed refetched, article cached
    ctx.store.close()


def test_a_publisher_that_blocks_programs_is_reported_separately(tmp_path, caplog):
    """WSJ's robots.txt disallows article paths; NYT answers 403 to a non-browser
    client. Either way nothing comes back, the cookies are fine, and re-exporting
    them would not help - so this must not read as an expired session."""
    feed = "https://nyt.example/feed"
    entries = [(f"Story {i}", f"https://nyt.example/{i}", NOW, "teaser") for i in range(4)]
    # The feed resolves; the article URLs do not - the fetcher returns None for a
    # 403 exactly as it does for a robots.txt refusal.
    fetcher = FakeFetcher({feed: FakeResponse(rss_bytes(entries))}, authenticated={"nytimes.com"})
    ctx = _ctx(tmp_path, fetcher, "blocked")

    with caplog.at_level("WARNING"):
        items = _paywalled_source(ctx, feed).fetch(SINCE)

    assert len(items) == 4
    assert all(not (i.text or "").strip() for i in items)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("no article body for 4 of 4" in w for w in warnings), warnings
    assert any("Re-exporting cookies will not help" in w for w in warnings), warnings
    # Must not be mistaken for the expiry case.
    assert not any("probably expired" in w for w in warnings), warnings
    ctx.store.close()


def test_unfetchable_articles_are_not_reported_when_unauthenticated(tmp_path, caplog):
    """Without cookies, full_text is off and no body is expected - stay silent."""
    feed = "https://nyt.example/feed"
    entries = [(f"Story {i}", f"https://nyt.example/{i}", NOW, "teaser") for i in range(4)]
    fetcher = FakeFetcher({feed: FakeResponse(rss_bytes(entries))})
    ctx = _ctx(tmp_path, fetcher, "blocked-anon")
    with caplog.at_level("WARNING"):
        _paywalled_source(ctx, feed).fetch(SINCE)
    assert not [r for r in caplog.records if "no article body" in r.getMessage()]
    ctx.store.close()
