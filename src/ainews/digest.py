"""Turn clustered stories into the weekly write-up."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import anthropic

from ainews.config import Config
from ainews.llm import text_call
from ainews.models import Cluster, Item

log = logging.getLogger(__name__)

# Rough guard so a bumper week can't blow past a sane request size.
MAX_INPUT_CHARS = 500_000

_SYSTEM = """You write a weekly AI news digest for one busy, technically literate reader.

Ground rules:
- Use ONLY the material provided. Never add facts, numbers, dates or quotes from
  memory. If the provided text does not say it, do not write it.
- Every story must link to at least one source URL from the material, as a
  markdown link.
- Where an item is marked truncated or paywalled, you saw only part of it: report
  what the headline and abstract support and say the detail is behind a paywall.
- Where several sources cover one story, synthesise them and cite the best two or
  three rather than listing all.
- Be specific and concrete: model names, version numbers, dollar figures, dates.
  No hype, no "game-changing", no filler transitions.
- Signal over completeness. A short digest of real news beats a long one padded
  with routine announcements.

Output format - GitHub-flavored markdown, no title heading (the caller adds one):

## The short version
4-7 bullets, one line each, covering the week's most consequential developments.

## Top stories
One `###` subsection per story, in descending importance (at most the number of
stories the user asks for). Each has:
- a specific headline
- 2-4 sentences: what happened, then why it matters
- a final line: `Sources: [Name](url), [Name](url)`

## Research worth reading
Up to 5 bullets on papers/preprints, each one line plus a link. Omit the whole
section if there is nothing substantive.

## Policy and money
Up to 6 bullets covering regulation, funding, deals and org changes, each with a
link. Omit if empty.

## From your newsletters
Up to 5 bullets on what your subscribed newsletters emphasised that the sections
above did not already cover, naming each newsletter. Omit if there were none.

## Also noted
A compact list of up to 10 remaining links worth a click: `- [Headline](url) - source, 8-word note.`

Close with a one-line `_Coverage: N stories from M sources._` note using the
counts given to you."""


@dataclass
class DigestInput:
    clusters: list[Cluster]
    newsletters: list[Item]
    start: date
    end: date
    total_candidates: int
    source_names: list[str]


def _cluster_payload(cluster: Cluster, per_item_chars: int) -> dict[str, Any]:
    entries = []
    for scored in cluster.items:
        item = scored.item
        body = item.excerpt(per_item_chars)
        entries.append(
            {
                "title": item.title,
                "source": item.source,
                "url": item.url,
                "published": item.published.date().isoformat() if item.published else None,
                "one_liner": scored.one_liner,
                "paywalled": bool(item.extra.get("paywalled")),
                "also_seen_in": item.extra.get("also_seen_in", []),
                "engagement": {
                    k: item.extra[k] for k in ("points", "score", "comments") if k in item.extra
                }
                or None,
                "body": body,
            }
        )
    return {
        "headline": cluster.headline,
        "topic": cluster.topic,
        "importance": cluster.importance,
        "coverage": entries,
    }


def build_payload(data: DigestInput, cfg: Config) -> dict[str, Any]:
    """Assemble the writer's input, shrinking per-item bodies if it is too big."""
    per_item_chars = cfg.per_item_chars
    for _ in range(4):
        payload = {
            "week": {"start": data.start.isoformat(), "end": data.end.isoformat()},
            "requested_top_stories": cfg.max_stories,
            "counts": {
                "candidates_collected": data.total_candidates,
                "stories_after_triage": sum(len(c.items) for c in data.clusters),
                "clusters": len(data.clusters),
                "sources": len(data.source_names),
            },
            "sources": data.source_names,
            "stories": [_cluster_payload(c, per_item_chars) for c in data.clusters],
            "newsletters": [
                {
                    "name": n.source,
                    "subject": n.title,
                    "received": n.published.date().isoformat() if n.published else None,
                    "body": n.excerpt(min(per_item_chars, 6000)),
                }
                for n in data.newsletters
            ],
        }
        size = len(json.dumps(payload, ensure_ascii=False))
        if size <= MAX_INPUT_CHARS:
            return payload
        per_item_chars = max(800, per_item_chars // 2)
        log.warning(
            "digest input is %d chars; trimming item bodies to %d chars", size, per_item_chars
        )
    return payload


def write_digest(
    data: DigestInput, cfg: Config, client: anthropic.Anthropic
) -> str:
    """Ask Claude for the digest markdown."""
    payload = build_payload(data, cfg)
    user = (
        "Here is this week's material as JSON. Write the digest described in your "
        f"instructions, covering at most {cfg.max_stories} top stories.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n```"
    )
    return text_call(
        client,
        model=cfg.llm.model,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        user=user,
        effort=cfg.llm.writer_effort,
        max_tokens=cfg.llm.max_output_tokens,
        label="write_digest",
    )


def fallback_digest(data: DigestInput) -> str:
    """A plain listing, used with --no-llm or when the writer call fails, so a
    run always produces something readable."""
    lines = ["## Collected stories", ""]
    for cluster in data.clusters:
        lines.append(f"### {cluster.headline}")
        lines.append(f"_{cluster.topic} · importance {cluster.importance}_")
        for scored in cluster.items:
            item = scored.item
            when = item.published.date().isoformat() if item.published else "undated"
            link = f"[{item.title}]({item.url})" if item.url else item.title
            lines.append(f"- {link} — {item.source}, {when}")
            if scored.one_liner:
                lines.append(f"  - {scored.one_liner}")
        lines.append("")
    if data.newsletters:
        lines += ["## Newsletters received", ""]
        for item in data.newsletters:
            when = item.published.date().isoformat() if item.published else "undated"
            lines.append(f"- **{item.title}** — {item.source}, {when}")
        lines.append("")
    stories = sum(len(c.items) for c in data.clusters)
    lines.append(f"_Coverage: {plural(stories, 'story', 'stories')} from {plural(len(data.source_names), 'source')}._")
    return "\n".join(lines)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def week_label(end: date) -> str:
    iso = end.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def now_utc() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
