"""Source-level tests with stubbed network responses."""

from datetime import datetime, timedelta, timezone

import pytest

from ainews.config import Config, SourceConfig
from ainews.sources import build_source
from ainews.sources.base import Context
from ainews.store import Store

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=7)


class FakeResponse:
    def __init__(self, content=b"", payload=None, headers=None):
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self._payload = payload
        self.headers = headers or {"Content-Type": "text/html"}
        self.status_code = 200

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeFetcher:
    """Stands in for ainews.http.Fetcher."""

    def __init__(self, responses=None, authenticated=()):
        self.responses = responses or {}
        self.calls = []
        self.authenticated = set(authenticated)

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.get(url)

    def post(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.get(url)

    def get_text(self, url):
        resp = self.get(url)
        return resp.text if resp else ""

    def has_credentials_for(self, domain):
        return domain in self.authenticated


@pytest.fixture()
def make_ctx(tmp_path):
    stores = []

    def _make(fetcher):
        store = Store(tmp_path / f"state{len(stores)}.sqlite3")
        stores.append(store)
        return Context(config=Config(), fetcher=fetcher, store=store)

    yield _make
    for store in stores:
        store.close()


def rss_bytes(entries):
    items = "".join(
        f"<item><title>{t}</title><link>{link}</link>"
        f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{desc}</description></item>"
        for t, link, when, desc in entries
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>Feed</title>{items}</channel></rss>".encode()


def test_rss_source_filters_by_date_and_include(make_ctx):
    feed_url = "https://example.com/feed"
    body = rss_bytes(
        [
            ("New AI model launches", "https://example.com/ai", NOW - timedelta(days=1), "About a model"),
            ("Sports roundup", "https://example.com/sport", NOW - timedelta(days=1), "Football"),
            ("Old AI story", "https://example.com/old", NOW - timedelta(days=30), "Stale"),
        ]
    )
    ctx = make_ctx(FakeFetcher({feed_url: FakeResponse(body)}))
    source = build_source(
        SourceConfig(type="rss", name="Feed", options={"url": feed_url, "include": ["ai"]}), ctx
    )
    items = source.fetch(SINCE)
    assert [i.title for i in items] == ["New AI model launches"]
    assert items[0].url == "https://example.com/ai"


def test_rss_source_fetches_full_text_and_caches_it(make_ctx):
    feed_url = "https://example.com/feed"
    article_url = "https://example.com/story"
    article_html = "<html><body><article>" + "<p>%s</p>" % ("A real sentence with enough words to count. " * 20) + "</article></body></html>"
    fetcher = FakeFetcher(
        {
            feed_url: FakeResponse(rss_bytes([("Story", article_url, NOW, "teaser")])),
            article_url: FakeResponse(article_html.encode()),
        }
    )
    ctx = make_ctx(fetcher)
    cfg = SourceConfig(type="rss", name="Feed", options={"url": feed_url, "full_text": True})

    first = build_source(cfg, ctx).fetch(SINCE)
    assert "A real sentence" in first[0].text

    before = len(fetcher.calls)
    second = build_source(cfg, ctx).fetch(SINCE)
    assert second[0].text == first[0].text
    # Feed refetched, article served from cache.
    assert len(fetcher.calls) == before + 1


def test_paywalled_source_skips_full_text_without_cookies(make_ctx):
    feed_url = "https://nyt.example/feed"
    article_url = "https://nyt.example/story"
    fetcher = FakeFetcher({feed_url: FakeResponse(rss_bytes([("Story", article_url, NOW, "teaser")]))})
    ctx = make_ctx(fetcher)
    source = build_source(
        SourceConfig(
            type="paywalled",
            name="NYT",
            options={"url": feed_url, "domain": "nytimes.com", "full_text": True},
        ),
        ctx,
    )
    items = source.fetch(SINCE)
    assert items[0].text == ""
    assert items[0].extra["authenticated"] is False
    assert article_url not in fetcher.calls


def test_paywalled_source_reads_full_text_with_cookies(make_ctx):
    feed_url = "https://nyt.example/feed"
    article_url = "https://nyt.example/story"
    html = "<html><body><article><p>" + ("Subscriber-only reporting sentence. " * 40) + "</p></article></body></html>"
    fetcher = FakeFetcher(
        {
            feed_url: FakeResponse(rss_bytes([("Story", article_url, NOW, "teaser")])),
            article_url: FakeResponse(html.encode()),
        },
        authenticated={"nytimes.com"},
    )
    ctx = make_ctx(fetcher)
    source = build_source(
        SourceConfig(
            type="paywalled",
            name="NYT",
            options={"url": feed_url, "domain": "nytimes.com", "full_text": True},
        ),
        ctx,
    )
    items = source.fetch(SINCE)
    assert "Subscriber-only reporting" in items[0].text
    assert items[0].extra["authenticated"] is True


def test_hackernews_source_parses_hits_and_sorts_by_points(make_ctx):
    payload = {
        "hits": [
            {"objectID": "1", "title": "Small story", "url": "https://a.com", "points": 120,
             "num_comments": 10, "created_at_i": int((NOW - timedelta(days=1)).timestamp())},
            {"objectID": "2", "title": "Big story", "url": "https://b.com", "points": 900,
             "num_comments": 400, "created_at_i": int((NOW - timedelta(days=2)).timestamp())},
        ]
    }

    class QueryFetcher(FakeFetcher):
        def get(self, url, **kwargs):
            self.calls.append(url)
            return FakeResponse(payload=payload)

    ctx = make_ctx(QueryFetcher())
    source = build_source(
        SourceConfig(type="hackernews", name="HN", options={"queries": ["AI"], "min_points": 100}), ctx
    )
    items = source.fetch(SINCE)
    assert [i.title for i in items] == ["Big story", "Small story"]
    assert items[0].extra["discussion"].endswith("id=2")


REDDIT_CREDS = {"client_id": "id", "client_secret": "secret"}


def _reddit_fetcher():
    """A fetcher that answers the OAuth token POST and the listing GET."""
    payload = {
        "data": {
            "children": [
                {"data": {"title": "Popular", "score": 500, "permalink": "/r/x/1", "num_comments": 9,
                          "created_utc": (NOW - timedelta(days=1)).timestamp()}},
                {"data": {"title": "Unpopular", "score": 5, "permalink": "/r/x/2", "num_comments": 0,
                          "created_utc": (NOW - timedelta(days=1)).timestamp()}},
            ]
        }
    }
    return FakeFetcher(
        {
            "https://www.reddit.com/api/v1/access_token": FakeResponse(payload={"access_token": "t"}),
            "https://oauth.reddit.com/r/x/top": FakeResponse(payload=payload),
        }
    )


def test_reddit_source_applies_score_floor(make_ctx):
    fetcher = _reddit_fetcher()
    ctx = make_ctx(fetcher)
    source = build_source(
        SourceConfig(
            type="reddit",
            name="Reddit",
            options={"subreddits": ["x"], "min_score": 100, **REDDIT_CREDS},
        ),
        ctx,
    )
    items = source.fetch(SINCE)
    assert [i.title for i in items] == ["Popular"]
    # Went through the OAuth API, not the robots-disallowed anonymous route.
    assert "https://www.reddit.com/api/v1/access_token" in fetcher.calls
    assert "https://oauth.reddit.com/r/x/top" in fetcher.calls
    assert not any("top.json" in url for url in fetcher.calls)


def test_reddit_source_sits_out_without_credentials(make_ctx):
    """Reddit disallows anonymous reads, so no credentials must mean no items
    and a visible `missing_options` - not a silent empty result."""
    fetcher = _reddit_fetcher()
    ctx = make_ctx(fetcher)
    source = build_source(
        SourceConfig(type="reddit", name="Reddit", options={"subreddits": ["x"]}), ctx
    )
    assert source.fetch(SINCE) == []
    assert source.missing_options() == ["client_id", "client_secret"]
    assert fetcher.calls == []


def test_reddit_source_returns_nothing_if_the_token_is_refused(make_ctx):
    # Fetcher.post returns None for a 4xx, which is what a bad secret produces.
    ctx = make_ctx(FakeFetcher({}))
    source = build_source(
        SourceConfig(
            type="reddit", name="Reddit", options={"subreddits": ["x"], **REDDIT_CREDS}
        ),
        ctx,
    )
    assert source.fetch(SINCE) == []


def test_unreachable_source_returns_no_items_instead_of_raising(make_ctx):
    ctx = make_ctx(FakeFetcher({}))
    source = build_source(
        SourceConfig(type="rss", name="Dead feed", options={"url": "https://dead.example/feed"}), ctx
    )
    assert source.fetch(SINCE) == []


def test_unknown_source_type_is_rejected(make_ctx):
    ctx = make_ctx(FakeFetcher({}))
    with pytest.raises(ValueError, match="Unknown source type"):
        build_source(SourceConfig(type="carrier-pigeon", name="?", options={}), ctx)
