from datetime import datetime, timezone

import pytest

from ainews.config import Config
from ainews.models import Item, ScoredItem
from ainews.pipeline import cluster, dedupe, select, split_newsletters, triage
from ainews.sources.base import Context
from ainews.store import Store


@pytest.fixture()
def ctx(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    yield Context(config=Config(), fetcher=None, store=store)
    store.close()


def item(source="A", title="t", url=None, text="", published=None, source_type="rss"):
    return Item(
        source=source,
        source_type=source_type,
        title=title,
        url=url,
        text=text,
        published=published,
    )


def test_dedupe_keeps_richest_copy_and_records_other_sources():
    thin = item(source="Wire", title="Lab ships model", url="https://x.com/a", text="short")
    rich = item(source="Ars", title="Lab ships model, explained", url="https://x.com/a?utm_source=rss", text="x" * 500)
    merged = dedupe([thin, rich])
    assert len(merged) == 1
    assert merged[0].source == "Ars"
    assert merged[0].extra["also_seen_in"] == ["Wire"]


def test_dedupe_backfills_missing_date():
    when = datetime(2026, 3, 1, tzinfo=timezone.utc)
    dated = item(url="https://x.com/a", published=when)
    undated = item(url="https://x.com/a", text="y" * 100)
    assert dedupe([dated, undated])[0].published == when


def test_split_newsletters_separates_imap_and_sorts_newest_first():
    old = item(source_type="imap", title="old", published=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = item(source_type="imap", title="new", published=datetime(2026, 2, 1, tzinfo=timezone.utc))
    story = item(title="story", url="https://x.com/s")
    stories, newsletters = split_newsletters([old, story, new])
    assert [s.title for s in stories] == ["story"]
    assert [n.title for n in newsletters] == ["new", "old"]


def test_select_applies_relevance_and_importance_floor():
    cfg = Config()
    cfg.min_importance = 3
    scored = [
        ScoredItem(item=item(title="a"), ai_related=True, importance=4, topic="research", one_liner=""),
        ScoredItem(item=item(title="b"), ai_related=True, importance=2, topic="research", one_liner=""),
        ScoredItem(item=item(title="c"), ai_related=False, importance=5, topic="other", one_liner=""),
    ]
    assert [s.item.title for s in select(scored, cfg)] == ["a"]


def test_triage_uses_cache_and_skips_the_api(ctx, monkeypatch):
    cfg = Config()
    target = item(title="Cached story", url="https://x.com/cached")

    def explode(*args, **kwargs):  # the API must not be called
        raise AssertionError("json_call should not run for a cached item")

    from ainews import pipeline

    pipeline.ctx_cache_probe = None
    ctx.store.set_cache(
        pipeline._triage_cache_key(target, cfg.llm.triage_model),
        {"ai_related": True, "importance": 4, "topic": "research", "one_liner": "cached"},
    )
    monkeypatch.setattr(pipeline, "json_call", explode)

    scored = triage([target], cfg, ctx, client=None)
    assert len(scored) == 1 and scored[0].one_liner == "cached" and scored[0].importance == 4


def test_triage_records_verdicts_in_the_cache(ctx, monkeypatch):
    cfg = Config()
    from ainews import pipeline

    calls = []

    def fake_json_call(client, **kwargs):
        calls.append(kwargs)
        return {
            "items": [
                {"index": 0, "ai_related": True, "importance": 5, "topic": "models-and-releases", "one_liner": "ok"}
            ]
        }

    monkeypatch.setattr(pipeline, "json_call", fake_json_call)
    target = item(title="Fresh story", url="https://x.com/fresh")
    scored = triage([target], cfg, ctx, client=None)

    assert scored[0].importance == 5
    assert len(calls) == 1
    assert ctx.store.get_cache(pipeline._triage_cache_key(target, cfg.llm.triage_model))["one_liner"] == "ok"


def test_cluster_falls_back_to_one_story_per_item_on_error(ctx, monkeypatch):
    from ainews import pipeline
    from ainews.llm import LLMError

    def boom(*args, **kwargs):
        raise LLMError("nope")

    monkeypatch.setattr(pipeline, "json_call", boom)
    scored = [
        ScoredItem(item=item(title=f"s{i}", url=f"https://x/{i}"), ai_related=True, importance=3, topic="research", one_liner="")
        for i in range(3)
    ]
    clusters = cluster(scored, Config(), ctx, client=None)
    assert len(clusters) == 3
    assert all(len(c.items) == 1 for c in clusters)


def test_cluster_assigns_every_item_exactly_once(ctx, monkeypatch):
    from ainews import pipeline

    def fake_json_call(client, **kwargs):
        # Groups 0+1 together, mentions an out-of-range index, and forgets item 2.
        return {
            "groups": [
                {"headline": "Grouped", "topic": "research", "importance": 4, "members": [0, 1, 99]},
            ]
        }

    monkeypatch.setattr(pipeline, "json_call", fake_json_call)
    scored = [
        ScoredItem(item=item(title=f"s{i}", url=f"https://x/{i}"), ai_related=True, importance=3, topic="research", one_liner="")
        for i in range(3)
    ]
    clusters = cluster(scored, Config(), ctx, client=None)
    assigned = [s.item.title for c in clusters for s in c.items]
    assert sorted(assigned) == ["s0", "s1", "s2"]
    assert clusters[0].headline == "Grouped" and len(clusters[0].items) == 2


def test_store_marks_and_filters_seen_items(ctx):
    from ainews.pipeline import drop_seen

    first = item(title="one", url="https://x.com/1")
    second = item(title="two", url="https://x.com/2")
    ctx.store.mark_seen([first], "2026-W10")
    assert [i.title for i in drop_seen([first, second], ctx)] == ["two"]
