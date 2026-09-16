"""The no-API path: keyword triage and clustering."""

from datetime import datetime, timedelta, timezone

from ainews import heuristic
from ainews.config import Config
from ainews.models import Item

NOW = datetime(2026, 3, 3, tzinfo=timezone.utc)


def item(title, summary="", source="Wire", source_type="rss", published=NOW, **extra):
    return Item(
        source=source,
        source_type=source_type,
        title=title,
        url=f"https://example.com/{abs(hash(title)) % 10000}",
        summary=summary,
        published=published,
        extra=extra,
    )


def test_strong_term_marks_an_item_ai_related():
    scored = heuristic.score_item(item("Anthropic releases a new language model"), NOW)
    assert scored.ai_related


def test_unrelated_story_is_filtered_out():
    scored = heuristic.score_item(item("League expands playoff format", "Football news."), NOW)
    assert not scored.ai_related


def test_bare_ai_mention_alone_is_not_enough():
    # One weak term, no strong term, generic source -> not AI news.
    scored = heuristic.score_item(item("Retailer says AI helped holiday sales"), NOW)
    assert not scored.ai_related
    # Two weak terms clears the bar.
    assert heuristic.score_item(item("Nvidia chips power new data center"), NOW).ai_related


def test_topical_source_lowers_the_bar():
    scored = heuristic.score_item(item("Chip supply tightens", source="TechCrunch AI"), NOW)
    assert scored.ai_related


def test_arxiv_items_are_always_research():
    scored = heuristic.score_item(item("A study of gradient flows", source_type="arxiv"), NOW)
    assert scored.ai_related and scored.topic == "research"


def test_importance_rewards_events_multi_source_coverage_and_engagement():
    plain = heuristic.score_item(item("Thoughts on large language model design"), NOW)
    launch = heuristic.score_item(item("OpenAI launches GPT-6 with a new transformer stack"), NOW)
    popular = heuristic.score_item(
        item("OpenAI launches GPT-6 with a new transformer stack", points=900,
             also_seen_in=["The Verge", "Ars"]),
        NOW,
    )
    assert launch.importance > plain.importance
    assert popular.importance == 5


def test_importance_penalises_listicles_and_stale_items():
    listicle = heuristic.score_item(item("Best AI tools for machine learning in 2026"), NOW)
    assert listicle.importance <= 2

    headline = "Anthropic ships a language model"
    fresh = heuristic.score_item(item(headline), NOW)
    stale = heuristic.score_item(item(headline, published=NOW - timedelta(days=20)), NOW)
    assert stale.importance < fresh.importance


def test_topic_classification():
    cases = {
        "EU parliament passes AI regulation": "policy-and-regulation",
        "AI startup raises $200M in funding": "business-and-funding",
        "New data center for GPU clusters announced": "compute-and-infrastructure",
        "Researchers find jailbreak in safety guardrails": "safety-and-alignment",
    }
    for title, expected in cases.items():
        assert heuristic.score_item(item(title), NOW).topic == expected, title


def test_first_sentence_trims_cleanly():
    assert heuristic.first_sentence("One thing. Two thing.") == "One thing."
    assert heuristic.first_sentence("") == ""
    assert heuristic.first_sentence("word " * 200).endswith("…")


def test_cluster_groups_the_same_story_and_keeps_others_apart():
    scored = [
        heuristic.score_item(item("OpenAI launches GPT-6 with new transformer stack"), NOW),
        heuristic.score_item(item("GPT-6 launches: OpenAI unveils new transformer stack"), NOW),
        heuristic.score_item(item("EU parliament passes sweeping AI regulation"), NOW),
    ]
    clusters = heuristic.cluster(scored)
    assert len(clusters) == 2
    assert max(len(c.items) for c in clusters) == 2


def test_run_filters_ranks_and_clusters():
    cfg = Config()
    cfg.min_importance = 2
    items = [
        item("Anthropic launches Claude 6, its largest model", points=800),
        item("Claude 6 launch: Anthropic ships its largest model"),
        item("League expands playoff format", "Football."),
    ]
    clusters = heuristic.run(items, cfg)
    titles = [s.item.title for c in clusters for s in c.items]
    assert "League expands playoff format" not in titles
    assert len(clusters) == 1 and len(clusters[0].items) == 2
