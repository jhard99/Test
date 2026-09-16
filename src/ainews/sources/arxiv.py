"""New arXiv preprints in the AI categories."""

from __future__ import annotations

import logging
from datetime import datetime

import feedparser

from ainews.extract import html_to_text
from ainews.models import Item
from ainews.sources.base import Source, register
from ainews.sources.rss import entry_datetime

log = logging.getLogger(__name__)

# HTTPS, and one of the endpoints in http.SANCTIONED_APIS: export.arxiv.org
# serves a blanket `Disallow: /` robots.txt aimed at page crawlers, while the
# arXiv API Terms of Use invite API clients at one request every three seconds.
_API = "https://export.arxiv.org/api/query"
_DEFAULT_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL"]


@register
class ArxivSource(Source):
    """Options:
        categories: arXiv category codes (default cs.AI, cs.LG, cs.CL)
        keywords:   optional title/abstract filter
        max_items:  how many of the newest submissions to consider
    """

    type_name = "arxiv"

    def fetch(self, since: datetime) -> list[Item]:
        categories = self.options.get("categories") or _DEFAULT_CATEGORIES
        keywords = [k.lower() for k in self.options.get("keywords", [])]
        search_query = " OR ".join(f"cat:{c}" for c in categories)

        resp = self.ctx.fetcher.get(
            _API,
            params={
                "search_query": search_query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": str(min(self.limit * 2, 200)),
            },
        )
        if resp is None:
            return []

        parsed = feedparser.parse(resp.content)
        items: list[Item] = []
        for entry in parsed.entries:
            if len(items) >= self.limit:
                break
            published = entry_datetime(entry)
            if published is not None and published < since:
                continue
            title = " ".join((entry.get("title") or "").split())
            abstract = html_to_text(entry.get("summary", ""))
            if keywords and not any(k in f"{title} {abstract}".lower() for k in keywords):
                continue
            authors = ", ".join(a.get("name", "") for a in entry.get("authors", [])[:5])
            items.append(
                Item(
                    source=self.name,
                    source_type=self.type_name,
                    title=title,
                    url=entry.get("link"),
                    published=published,
                    author=authors,
                    summary=abstract[:3000],
                    extra={"categories": [t.get("term") for t in entry.get("tags", [])]},
                )
            )

        return self.log_result(items)
