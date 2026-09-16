"""Open-web discovery using Claude's server-side web search tool.

This is the "scrape the internet" half of the agent: it finds stories that none
of your configured feeds carried, rather than only re-reading known sources.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import anthropic
from dateutil import parser as date_parser

from ainews.llm import build_client, supports_adaptive_thinking
from ainews.models import Item
from ainews.sources.base import Source, register

log = logging.getLogger(__name__)

_DEFAULT_QUERIES = [
    "major AI news this week",
    "new AI model release this week",
    "AI policy and regulation news this week",
    "AI funding and acquisitions this week",
    "AI safety research news this week",
]

_SYSTEM = (
    "You are a news scout. Use web search to find substantive, recent news "
    "articles matching the user's topic. Prefer primary sources and reputable "
    "outlets. Do not summarize at length - the searches themselves are the "
    "deliverable. Reply with at most three sentences noting what you found."
)


def _parse_page_age(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = date_parser.parse(value, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@register
class WebSearchSource(Source):
    """Options:
        queries:   search prompts (default: a general AI news set)
        model:     model to drive the searches (default: llm.triage_model)
        max_uses:  max searches per query, default 4
    """

    type_name = "websearch"

    def fetch(self, since: datetime) -> list[Item]:
        queries = self.options.get("queries") or _DEFAULT_QUERIES
        model = str(self.options.get("model") or self.ctx.config.llm.triage_model)
        max_uses = int(self.options.get("max_uses", 4))
        allowed = self.options.get("allowed_domains") or []
        blocked = self.options.get("blocked_domains") or []

        tool: dict[str, Any] = {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": max_uses,
        }
        # The API rejects both lists on one tool.
        if allowed:
            tool["allowed_domains"] = allowed
        elif blocked:
            tool["blocked_domains"] = blocked

        client = build_client()
        window = f"{since.date().isoformat()} to {datetime.now(timezone.utc).date().isoformat()}"
        items: dict[str, Item] = {}

        for query in queries:
            if len(items) >= self.limit:
                break
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": 4000,
                "system": _SYSTEM,
                "tools": [tool],
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Find news published between {window} about: {query}. "
                            "Run several searches with different phrasings."
                        ),
                    }
                ],
            }
            if supports_adaptive_thinking(model):
                kwargs["thinking"] = {"type": "adaptive"}

            try:
                response = client.messages.create(**kwargs)
            except anthropic.APIError as exc:
                log.warning("%s: web search failed for %r: %s", self.name, query, exc)
                continue

            for result in self._results(response):
                url = result.get("url")
                if not url or url in items:
                    continue
                published = _parse_page_age(result.get("page_age"))
                if published is not None and published < since:
                    continue
                items[url] = Item(
                    source=self.name,
                    source_type=self.type_name,
                    title=(result.get("title") or url).strip(),
                    url=url,
                    published=published,
                    summary="",
                    extra={"query": query, "page_age": result.get("page_age")},
                )
                if len(items) >= self.limit:
                    break

        return self.log_result(list(items.values()))

    @staticmethod
    def _results(response: Any) -> list[dict[str, Any]]:
        """Pull search hits out of the response.

        Server-tool failures come back as HTTP 200 with an error *object* where
        a successful call has a *list*, so branch on that before iterating.
        """
        out: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", "") != "web_search_tool_result":
                continue
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                code = getattr(content, "error_code", None)
                log.warning("web search returned an error: %s", code or content)
                continue
            for result in content:
                out.append(
                    {
                        "url": getattr(result, "url", None),
                        "title": getattr(result, "title", None),
                        "page_age": getattr(result, "page_age", None),
                    }
                )
        return out
