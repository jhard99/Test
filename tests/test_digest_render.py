import json
from datetime import date, datetime, timezone

from ainews.config import Config, SmtpConfig
from ainews.deliver import build_message
from ainews.digest import DigestInput, build_payload, fallback_digest, week_label
from ainews.models import Cluster, Item, ScoredItem
from ainews.render import to_html, to_markdown


def make_input(body_chars=200, clusters=2):
    groups = []
    for n in range(clusters):
        item = Item(
            source=f"Source {n}",
            source_type="rss",
            title=f"Story {n}",
            url=f"https://example.com/{n}",
            text="x" * body_chars,
            published=datetime(2026, 3, 2, tzinfo=timezone.utc),
        )
        item.extra["paywalled"] = n == 0
        groups.append(
            Cluster(
                headline=f"Story {n} happened",
                topic="models-and-releases",
                importance=5 - n,
                items=[ScoredItem(item=item, ai_related=True, importance=5 - n, topic="models-and-releases", one_liner=f"line {n}")],
            )
        )
    newsletter = Item(
        source="newsletters: Import AI",
        source_type="imap",
        title="Import AI 400",
        text="newsletter body",
        published=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    return DigestInput(
        clusters=groups,
        newsletters=[newsletter],
        start=date(2026, 2, 24),
        end=date(2026, 3, 3),
        total_candidates=120,
        source_names=["Source 0", "Source 1", "newsletters: Import AI"],
    )


def test_build_payload_shape_and_counts():
    payload = build_payload(make_input(), Config())
    assert payload["week"] == {"start": "2026-02-24", "end": "2026-03-03"}
    assert payload["counts"]["candidates_collected"] == 120
    assert payload["counts"]["clusters"] == 2
    assert payload["stories"][0]["coverage"][0]["paywalled"] is True
    assert payload["newsletters"][0]["subject"] == "Import AI 400"


def test_build_payload_shrinks_oversized_input(monkeypatch):
    from ainews import digest

    monkeypatch.setattr(digest, "MAX_INPUT_CHARS", 5000)
    payload = build_payload(make_input(body_chars=20_000, clusters=4), Config())
    assert len(json.dumps(payload)) < 60_000  # trimmed from ~80k of bodies
    assert "truncated from" in payload["stories"][0]["coverage"][0]["body"]


def test_fallback_digest_lists_every_story_with_links():
    text = fallback_digest(make_input())
    assert "[Story 0](https://example.com/0)" in text
    assert "Import AI 400" in text
    assert "_Coverage: 2 stories from 3 sources._" in text


def test_week_label_is_iso_week():
    assert week_label(date(2026, 3, 3)) == "2026-W10"


def test_to_markdown_and_html_round_trip():
    md = to_markdown("## The short version\n\n- [Thing](https://x.com) happened", date(2026, 2, 24), date(2026, 3, 3), "2026-03-03 13:00 UTC")
    assert md.startswith("# AI weekly")
    html = to_html(md, footer="Assembled from 3 sources")
    assert "<h1>" in html and 'href="https://x.com"' in html
    assert "Assembled from 3 sources" in html
    assert html.startswith("<!doctype html>")


def test_build_message_is_multipart_with_text_and_html():
    cfg = SmtpConfig(host="smtp.test", sender="me@example.com", recipients=["me@example.com", "other@example.com"])
    msg = build_message(cfg, "AI weekly", "plain body", "<p>html body</p>")
    assert msg["To"] == "me@example.com, other@example.com"
    assert msg["Subject"] == "AI weekly"
    types = {part.get_content_type() for part in msg.walk()}
    assert {"text/plain", "text/html"} <= types
