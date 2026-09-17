"""RSS / Atom feeds - the backbone of the collector."""

from __future__ import annotations

import logging
from calendar import timegm
from datetime import datetime, timezone
from typing import Any

import feedparser

from ainews.extract import extract_article, html_to_text, looks_paywalled
from ainews.models import Item
from ainews.sources.base import Source, register

log = logging.getLogger(__name__)


def entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if parsed:
            try:
                return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def entry_summary(entry: Any) -> str:
    raw = entry.get("summary") or ""
    if not raw and entry.get("content"):
        raw = entry["content"][0].get("value", "")
    return html_to_text(raw)[:4000]


@register
class RSSSource(Source):
    """A feed URL.

    Options:
        url:        feed URL (required)
        full_text:  fetch and extract each linked article (default false)
        include:    only keep entries whose title/summary matches one of these
                    case-insensitive substrings
        exclude:    drop entries matching any of these substrings
    """

    type_name = "rss"
    required_options = ("url",)

    def fetch(self, since: datetime) -> list[Item]:
        url = self.options.get("url", "")
        if not url:
            log.warning("%s: no url configured", self.name)
            return []

        resp = self.ctx.fetcher.get(url)
        if resp is None:
            return []
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log.warning("%s: could not parse feed (%s)", self.name, parsed.get("bozo_exception"))
            return []

        include = [s.lower() for s in self.options.get("include", [])]
        exclude = [s.lower() for s in self.options.get("exclude", [])]
        want_full_text = bool(self.options.get("full_text", False))

        items: list[Item] = []
        for entry in parsed.entries:
            if len(items) >= self.limit:
                break
            published = entry_datetime(entry)
            if published is not None and published < since:
                continue
            title = html_to_text(entry.get("title", "")).strip()
            link = entry.get("link") or ""
            if not title and not link:
                continue
            summary = entry_summary(entry)

            haystack = f"{title}\n{summary}".lower()
            if include and not any(term in haystack for term in include):
                continue
            if exclude and any(term in haystack for term in exclude):
                continue

            item = Item(
                source=self.name,
                source_type=self.type_name,
                title=title or link,
                url=link or None,
                published=published,
                author=entry.get("author"),
                summary=summary,
            )
            if want_full_text and link:
                item.text = self._article_text(item)
            items.append(item)

        return self.log_result(items)

    # -- full text ------------------------------------------------------------

    def _article_text(self, item: Item) -> str:
        """Fetch and extract the linked article, caching the result."""
        if not item.url:
            return ""
        cache_key = f"article:{item.key}"
        cached = self.ctx.store.get_cache(cache_key)
        if cached is not None:
            # Re-check the cached text: a stub gets cached like any other body,
            # so without this an item fetched while the cookies were expired
            # would look like a complete article on every later run.
            text = str(cached)
            if looks_paywalled(text):
                item.extra["paywalled"] = True
            return text

        html = self.ctx.fetcher.get_text(item.url)
        if not html:
            return ""
        _, text = extract_article(html)
        if looks_paywalled(text):
            log.info("%s: only a stub of %s is readable", self.name, item.url)
            item.extra["paywalled"] = True
        self.ctx.store.set_cache(cache_key, text)
        return text
