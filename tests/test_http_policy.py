"""The robots.txt policy, and the documented-API carve-out around it.

These guard the two ways the fetcher can be wrong in opposite directions:
applying a crawler directive to a published API (which made the arXiv source
return nothing, forever, silently), and ignoring robots.txt for a host that
really does mean it (Reddit).
"""

from ainews.config import HttpConfig
from ainews.http import SANCTIONED_APIS, Fetcher, sanctioned_gap


def _fetcher(robots_body="User-agent: *\nDisallow: /"):
    """A Fetcher whose robots.txt fetch always returns `robots_body`."""
    cfg = HttpConfig(requests_per_minute=0)
    fetcher = Fetcher(cfg)

    class Resp:
        status_code = 200
        text = robots_body

    fetcher.session.get = lambda *a, **k: Resp()  # type: ignore[method-assign]
    return fetcher


def test_arxiv_api_is_exempt_from_a_blanket_disallow():
    """export.arxiv.org serves `Disallow: /` for crawlers while publishing an
    API; treating the API as a crawl target is what broke the arxiv source."""
    fetcher = _fetcher()
    assert fetcher._robots_allows("https://export.arxiv.org/api/query?search_query=cat:cs.AI")
    fetcher.close()


def test_a_non_api_path_on_the_same_host_still_obeys_robots():
    """The carve-out is per-endpoint, not per-host."""
    fetcher = _fetcher()
    assert not fetcher._robots_allows("https://export.arxiv.org/abs/2401.00001")
    fetcher.close()


def test_anonymous_reddit_is_not_exempt():
    """Reddit's `Disallow: /` covers its .json views under a stated content
    policy, so the old anonymous route must stay blocked."""
    fetcher = _fetcher()
    assert not fetcher._robots_allows("https://www.reddit.com/r/MachineLearning/top.json")
    assert not fetcher._robots_allows("https://www.reddit.com/r/LocalLLaMA/top")
    fetcher.close()


def test_reddits_oauth_api_is_exempt():
    fetcher = _fetcher()
    assert fetcher._robots_allows("https://oauth.reddit.com/r/MachineLearning/top")
    assert fetcher._robots_allows("https://www.reddit.com/api/v1/access_token")
    fetcher.close()


def test_respect_robots_false_allows_everything():
    cfg = HttpConfig(respect_robots=False, requests_per_minute=0)
    fetcher = Fetcher(cfg)
    assert fetcher._robots_allows("https://www.reddit.com/r/x/top.json")
    fetcher.close()


def test_sanctioned_gap_reports_the_publishers_documented_rate():
    # arXiv's API terms ask for one request every three seconds.
    assert sanctioned_gap("https://export.arxiv.org/api/query?x=1") == 3.0
    assert sanctioned_gap("https://oauth.reddit.com/r/x/top") == 1.0
    # Not a sanctioned API: None means "apply robots.txt".
    assert sanctioned_gap("https://www.reddit.com/r/x/top.json") is None
    assert sanctioned_gap("https://example.com/feed") is None


def test_every_sanctioned_entry_is_an_absolute_https_or_http_prefix():
    """A relative or bare-host entry would silently match too much."""
    for prefix, gap in SANCTIONED_APIS.items():
        assert prefix.startswith(("http://", "https://")), prefix
        assert gap >= 0, prefix


def test_throttle_honours_the_api_gap_even_with_rate_limiting_off():
    """requests_per_minute: 0 disables the per-host limit; a publisher's own
    documented gap still has to apply."""
    import time

    fetcher = Fetcher(HttpConfig(requests_per_minute=0))
    start = time.monotonic()
    fetcher._throttle("export.arxiv.org", 0.2)
    fetcher._throttle("export.arxiv.org", 0.2)
    assert time.monotonic() - start >= 0.2
    fetcher.close()
