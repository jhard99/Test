"""Collect -> dedupe -> triage -> cluster."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

import anthropic

from ainews.config import Config
from ainews.llm import LLMError, json_call, resolve_model
from ainews.models import Cluster, Item, ScoredItem, normalize_url
from ainews.sources.base import Context, build_source

log = logging.getLogger(__name__)

# Bump when the triage prompt changes, so cached verdicts are not reused.
TRIAGE_PROMPT_VERSION = "1"

TOPICS = [
    "models-and-releases",
    "research",
    "products-and-tools",
    "business-and-funding",
    "policy-and-regulation",
    "safety-and-alignment",
    "compute-and-infrastructure",
    "society-and-labor",
    "other",
]

_TRIAGE_SYSTEM = f"""You triage candidate news items for a weekly AI newsletter.

For each item decide:
- ai_related: true only if the item is substantively about artificial intelligence,
  machine learning, or their direct business/policy/societal consequences. A passing
  mention of "AI" in an unrelated story is false.
- importance: how much a well-informed AI professional would care.
  5 = field-defining (frontier model launch, landmark regulation, major lab shakeup)
  4 = significant (notable model/product release, major funding round, important paper)
  3 = worth knowing (solid product news, credible research result, notable analysis)
  2 = minor (incremental updates, small funding, routine commentary)
  1 = noise (marketing, listicles, rehashes, pure speculation)
- topic: exactly one of {', '.join(TOPICS)}
- one_liner: <= 25 words, factual, no hype, stating what actually happened.

Judge only from the text you are given. Do not speculate about content you cannot see.
Return one entry per input item, preserving the given index."""

_TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "ai_related": {"type": "boolean"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "topic": {"type": "string", "enum": TOPICS},
                    "one_liner": {"type": "string"},
                },
                "required": ["index", "ai_related", "importance", "topic", "one_liner"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_CLUSTER_SYSTEM = """You group news items that cover the same underlying story.

Two items belong together when they report the same event, launch, paper, deal or
decision - even with different angles or headlines. Items about merely similar
topics stay separate. Every item must appear in exactly one group; a story covered
by a single outlet is a group of one.

For each group give:
- headline: a neutral, specific headline (<= 14 words) describing what happened
- topic: the topic shared by its members
- importance: the highest importance among its members
- members: the indexes of its items, most substantial first"""

_CLUSTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "topic": {"type": "string", "enum": TOPICS},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "members": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["headline", "topic", "importance", "members"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}


# --- collection --------------------------------------------------------------


def collect(cfg: Config, ctx: Context, since: datetime) -> list[Item]:
    """Run every enabled source. A broken source never fails the whole run."""
    items: list[Item] = []
    for source_cfg in cfg.sources:
        if not source_cfg.enabled:
            log.info("%s: disabled, skipping", source_cfg.name)
            continue
        try:
            source = build_source(source_cfg, ctx)
        except ValueError as exc:
            log.error("%s", exc)
            continue
        missing = source.missing_options()
        if missing:
            log.warning("%s: skipping, missing options: %s", source_cfg.name, ", ".join(missing))
            continue
        try:
            items.extend(source.fetch(since))
        except Exception:  # a third-party feed should not take the digest down
            log.exception("%s: fetch failed", source_cfg.name)
    return items


def dedupe(items: list[Item]) -> list[Item]:
    """Collapse items that share a normalized URL (or source+title), keeping the
    richest copy and remembering which other sources carried it."""
    merged: dict[str, Item] = {}
    for item in items:
        key = item.key
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        keep, drop = (item, existing) if len(item.body) > len(existing.body) else (existing, item)
        also = keep.extra.setdefault("also_seen_in", [])
        for name in [drop.source, *drop.extra.get("also_seen_in", [])]:
            if name != keep.source and name not in also:
                also.append(name)
        if keep.published is None:
            keep.published = drop.published
        merged[key] = keep
    return list(merged.values())


def split_newsletters(items: list[Item]) -> tuple[list[Item], list[Item]]:
    """Newsletters are roundups, not single stories - they get their own lane."""
    stories = [i for i in items if i.source_type != "imap"]
    newsletters = [i for i in items if i.source_type == "imap"]
    newsletters.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=None), reverse=True)
    return stories, newsletters


def drop_seen(items: list[Item], ctx: Context) -> list[Item]:
    seen = ctx.store.seen_keys(i.key for i in items)
    fresh = [i for i in items if i.key not in seen]
    if len(fresh) != len(items):
        log.info("skipped %d items covered in an earlier digest", len(items) - len(fresh))
    return fresh


# --- triage ------------------------------------------------------------------


def _triage_cache_key(item: Item, model: str) -> str:
    raw = f"{TRIAGE_PROMPT_VERSION}|{model}|{item.key}|{item.title}|{item.summary[:200]}"
    return "triage:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _triage_payload(index: int, item: Item) -> dict[str, Any]:
    return {
        "index": index,
        "title": item.title,
        "source": item.source,
        "domain": item.domain,
        "published": item.published.date().isoformat() if item.published else None,
        "abstract": item.excerpt(1200),
    }


def triage(
    items: list[Item], cfg: Config, ctx: Context, client: anthropic.Anthropic
) -> list[ScoredItem]:
    """Classify every candidate; cached per item so re-runs cost nothing."""
    scored: list[ScoredItem] = []
    pending: list[Item] = []

    for item in items:
        cached = ctx.store.get_cache(_triage_cache_key(item, cfg.llm.triage_model))
        if cached:
            scored.append(
                ScoredItem(
                    item=item,
                    ai_related=bool(cached["ai_related"]),
                    importance=int(cached["importance"]),
                    topic=str(cached["topic"]),
                    one_liner=str(cached["one_liner"]),
                )
            )
        else:
            pending.append(item)

    batch_size = max(1, cfg.llm.triage_batch_size)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        payload = [_triage_payload(i, item) for i, item in enumerate(batch)]
        try:
            result = json_call(
                client,
                model=resolve_model(cfg.llm.provider, cfg.llm.triage_model),
                system=_TRIAGE_SYSTEM,
                user=json.dumps(payload, ensure_ascii=False),
                schema=_TRIAGE_SCHEMA,
                effort=cfg.llm.triage_effort,
                label=f"triage[{start // batch_size + 1}]",
            )
        except LLMError as exc:
            log.error("%s - keeping this batch unscored", exc)
            continue

        by_index = {int(entry["index"]): entry for entry in result.get("items", [])}
        for offset, item in enumerate(batch):
            entry = by_index.get(offset)
            if entry is None:
                log.warning("triage returned no verdict for %r", item.title[:60])
                continue
            verdict = {
                "ai_related": bool(entry["ai_related"]),
                "importance": int(entry["importance"]),
                "topic": str(entry["topic"]),
                "one_liner": str(entry["one_liner"]),
            }
            ctx.store.set_cache(_triage_cache_key(item, cfg.llm.triage_model), verdict)
            scored.append(ScoredItem(item=item, **verdict))

    scored.sort(key=lambda s: (s.importance, s.item.published or datetime.min), reverse=True)
    return scored


def select(scored: list[ScoredItem], cfg: Config) -> list[ScoredItem]:
    return [s for s in scored if s.ai_related and s.importance >= cfg.min_importance]


# --- clustering --------------------------------------------------------------


def cluster(
    scored: list[ScoredItem], cfg: Config, ctx: Context, client: anthropic.Anthropic
) -> list[Cluster]:
    """Group items covering the same story so the digest doesn't repeat itself."""
    if not scored:
        return []
    if len(scored) == 1:
        only = scored[0]
        return [Cluster(headline=only.item.title, topic=only.topic, importance=only.importance, items=[only])]

    payload = [
        {
            "index": i,
            "title": s.item.title,
            "source": s.item.source,
            "topic": s.topic,
            "importance": s.importance,
            "one_liner": s.one_liner,
        }
        for i, s in enumerate(scored)
    ]
    try:
        result = json_call(
            client,
            model=resolve_model(cfg.llm.provider, cfg.llm.triage_model),
            system=_CLUSTER_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
            schema=_CLUSTER_SCHEMA,
            effort=cfg.llm.triage_effort,
            max_tokens=24000,
            label="cluster",
        )
    except LLMError as exc:
        log.error("%s - falling back to one story per item", exc)
        return [
            Cluster(headline=s.item.title, topic=s.topic, importance=s.importance, items=[s])
            for s in scored
        ]

    clusters: list[Cluster] = []
    assigned: set[int] = set()
    for group in result.get("groups", []):
        members = [i for i in group.get("members", []) if 0 <= i < len(scored) and i not in assigned]
        if not members:
            continue
        assigned.update(members)
        clusters.append(
            Cluster(
                headline=str(group.get("headline") or scored[members[0]].item.title),
                topic=str(group.get("topic") or scored[members[0]].topic),
                importance=int(group.get("importance") or scored[members[0]].importance),
                items=[scored[i] for i in members],
            )
        )

    # Anything the model forgot still deserves a place.
    for i, s in enumerate(scored):
        if i not in assigned:
            clusters.append(Cluster(headline=s.item.title, topic=s.topic, importance=s.importance, items=[s]))

    clusters.sort(key=lambda c: (c.importance, len(c.items)), reverse=True)
    return clusters


def merge_urls(clusters: list[Cluster]) -> set[str]:
    """Normalized URLs represented in the clusters - used for reporting."""
    return {normalize_url(s.item.url) for c in clusters for s in c.items if s.item.url}
