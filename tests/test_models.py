from datetime import datetime, timezone

from ainews.models import Cluster, Item, ScoredItem, normalize_url


def test_normalize_url_strips_tracking_and_case():
    a = normalize_url("https://WWW.Example.com/story/?utm_source=nl&id=7&fbclid=xyz")
    b = normalize_url("http://example.com/story?id=7")
    assert a == b == "https://example.com/story?id=7"


def test_normalize_url_handles_empty_and_relative():
    assert normalize_url(None) == ""
    assert normalize_url("") == ""
    assert normalize_url("/just/a/path") == "/just/a/path"


def test_same_story_from_two_sources_shares_a_key():
    one = Item(source="A", source_type="rss", title="X launches Y", url="https://site.com/a?utm_medium=rss")
    two = Item(source="B", source_type="rss", title="Totally different headline", url="https://www.site.com/a/")
    assert one.key == two.key


def test_key_falls_back_to_title_when_no_url():
    item = Item(source="A", source_type="imap", title="Weekly roundup")
    assert item.key and len(item.key) == 16


def test_excerpt_marks_truncation():
    item = Item(source="A", source_type="rss", title="t", text="word " * 200)
    excerpt = item.excerpt(50)
    assert "truncated from" in excerpt
    assert len(excerpt) < len(item.text)
    assert item.excerpt(0) == item.text.strip()


def test_body_prefers_full_text():
    item = Item(source="A", source_type="rss", title="t", summary="short", text="long body")
    assert item.body == "long body"
    assert Item(source="A", source_type="rss", title="t", summary="short").body == "short"


def test_naive_published_is_treated_as_utc():
    item = Item(source="A", source_type="rss", title="t", published=datetime(2026, 1, 1, 12))
    assert item.published.tzinfo is timezone.utc
    assert item.age_days(datetime(2026, 1, 3, 12, tzinfo=timezone.utc)) == 2


def test_cluster_sources_are_unique_and_ordered():
    def scored(source: str) -> ScoredItem:
        return ScoredItem(
            item=Item(source=source, source_type="rss", title="t", url=f"https://x/{source}"),
            ai_related=True,
            importance=3,
            topic="research",
            one_liner="",
        )

    cluster = Cluster(headline="h", topic="research", importance=3, items=[scored("B"), scored("A"), scored("B")])
    assert cluster.sources == ["B", "A"]
