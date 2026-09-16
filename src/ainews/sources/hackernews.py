"""Hacker News via the public Algolia search API."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ainews.models import Item
from ainews.sources.base import Source, register

log = logging.getLogger(__name__)

_API = "https://hn.algolia.com/api/v1/search"

_DEFAULT_QUERIES = [
    "AI",
    "LLM",
    "OpenAI",
    "Anthropic",
    "Claude",
    "Gemini",
    "machine learning",
    "neural network",
    "AI regulation",
]


@register
class HackerNewsSource(Source):
    """Options:
        queries:    search terms (default: a general AI set)
        min_points: minimum score, default 80
        max_items:  cap across all queries
    """

    type_name = "hackernews"

    def fetch(self, since: datetime) -> list[Item]:
        queries = self.options.get("queries") or _DEFAULT_QUERIES
        min_points = int(self.options.get("min_points", 80))
        since_ts = int(since.timestamp())

        items: dict[str, Item] = {}
        for query in queries:
            if len(items) >= self.limit:
                break
            params = {
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{since_ts},points>={min_points}",
                "hitsPerPage": "50",
            }
            resp = self.ctx.fetcher.get(_API, params=params)
            if resp is None:
                continue
            try:
                hits = resp.json().get("hits", [])
            except ValueError:
                log.warning("%s: non-JSON response for query %r", self.name, query)
                continue

            for hit in hits:
                object_id = str(hit.get("objectID", ""))
                if not object_id or object_id in items:
                    continue
                title = (hit.get("title") or "").strip()
                if not title:
                    continue
                discussion = f"https://news.ycombinator.com/item?id={object_id}"
                created = hit.get("created_at_i")
                published = (
                    datetime.fromtimestamp(int(created), tz=timezone.utc) if created else None
                )
                points = int(hit.get("points") or 0)
                comments = int(hit.get("num_comments") or 0)
                items[object_id] = Item(
                    source=self.name,
                    source_type=self.type_name,
                    title=title,
                    url=hit.get("url") or discussion,
                    published=published,
                    author=hit.get("author"),
                    summary=(hit.get("story_text") or "")[:1500]
                    or f"{points} points, {comments} comments on Hacker News.",
                    extra={
                        "points": points,
                        "comments": comments,
                        "discussion": discussion,
                        "matched_query": query,
                    },
                )

        ranked = sorted(items.values(), key=lambda i: i.extra.get("points", 0), reverse=True)
        return self.log_result(ranked[: self.limit])
