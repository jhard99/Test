"""Subreddit top posts via Reddit's public JSON endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ainews.models import Item
from ainews.sources.base import Source, register

log = logging.getLogger(__name__)

_DEFAULT_SUBS = ["MachineLearning", "LocalLLaMA", "artificial", "singularity"]


@register
class RedditSource(Source):
    """Options:
        subreddits: list of subreddit names (no /r/ prefix)
        timeframe:  hour|day|week|month (default week)
        min_score:  minimum upvotes, default 50
    """

    type_name = "reddit"

    def fetch(self, since: datetime) -> list[Item]:
        subs = self.options.get("subreddits") or _DEFAULT_SUBS
        timeframe = str(self.options.get("timeframe", "week"))
        min_score = int(self.options.get("min_score", 50))

        items: list[Item] = []
        for sub in subs:
            if len(items) >= self.limit:
                break
            url = f"https://www.reddit.com/r/{sub}/top.json"
            resp = self.ctx.fetcher.get(url, params={"t": timeframe, "limit": "100"})
            if resp is None:
                # Reddit blocks generic clients aggressively; don't fail the run.
                log.info("%s: r/%s unavailable (rate limited or blocked)", self.name, sub)
                continue
            try:
                children = resp.json().get("data", {}).get("children", [])
            except ValueError:
                log.warning("%s: non-JSON response from r/%s", self.name, sub)
                continue

            for child in children:
                post = child.get("data", {})
                score = int(post.get("score") or 0)
                if score < min_score:
                    continue
                created = post.get("created_utc")
                published = (
                    datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None
                )
                if published is not None and published < since:
                    continue
                permalink = f"https://www.reddit.com{post.get('permalink', '')}"
                external = post.get("url_overridden_by_dest") or ""
                items.append(
                    Item(
                        source=f"{self.name}: r/{sub}",
                        source_type=self.type_name,
                        title=(post.get("title") or "").strip(),
                        url=external or permalink,
                        published=published,
                        author=post.get("author"),
                        summary=(post.get("selftext") or "")[:1500]
                        or f"{score} upvotes, {post.get('num_comments', 0)} comments in r/{sub}.",
                        extra={
                            "score": score,
                            "comments": post.get("num_comments", 0),
                            "discussion": permalink,
                            "subreddit": sub,
                        },
                    )
                )
                if len(items) >= self.limit:
                    break

        items.sort(key=lambda i: i.extra.get("score", 0), reverse=True)
        return self.log_result(items)
