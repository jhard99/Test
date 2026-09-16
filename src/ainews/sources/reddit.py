"""Subreddit top posts via Reddit's OAuth API.

Reddit's robots.txt is a blanket `Disallow: /` that covers both its HTML pages
and the `.json` views of them, backed by a stated Public Content Policy. So the
anonymous `reddit.com/r/<sub>/top.json` route this source used to take is not
available to a well-behaved client, and it returned nothing at all.

The supported route for programmatic reads is the OAuth API, governed by
Reddit's API terms rather than robots.txt. It needs a (free) registered app:

    https://www.reddit.com/prefs/apps  ->  "create another app..."  ->  script

Put the client id and secret in REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET. With
them unset this source reports itself as missing credentials in `ainews check`
and contributes nothing, rather than silently looking healthy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ainews.models import Item
from ainews.sources.base import Source, register

log = logging.getLogger(__name__)

_DEFAULT_SUBS = ["MachineLearning", "LocalLLaMA", "artificial", "singularity"]
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_ROOT = "https://oauth.reddit.com"


@register
class RedditSource(Source):
    """Options:
        client_id:     Reddit app client id (required)
        client_secret: Reddit app secret (required)
        subreddits:    list of subreddit names (no /r/ prefix)
        timeframe:     hour|day|week|month (default week)
        min_score:     minimum upvotes, default 50
    """

    type_name = "reddit"
    required_options = ("client_id", "client_secret")

    def _token(self) -> str | None:
        """Exchange the app credentials for an app-only bearer token."""
        resp = self.ctx.fetcher.post(
            _TOKEN_URL,
            auth=(str(self.options["client_id"]), str(self.options["client_secret"])),
            data={"grant_type": "client_credentials"},
        )
        if resp is None:
            log.warning(
                "%s: could not get a Reddit access token - check REDDIT_CLIENT_ID / "
                "REDDIT_CLIENT_SECRET (an app of type 'script' works)",
                self.name,
            )
            return None
        try:
            token = resp.json().get("access_token")
        except ValueError:
            log.warning("%s: Reddit returned a non-JSON token response", self.name)
            return None
        if not token:
            log.warning("%s: Reddit token response carried no access_token", self.name)
        return token

    def fetch(self, since: datetime) -> list[Item]:
        missing = self.missing_options()
        if missing:
            log.info(
                "%s: skipping, missing options: %s. Reddit's robots.txt disallows "
                "anonymous access, so this source needs a registered app "
                "(https://www.reddit.com/prefs/apps).",
                self.name,
                ", ".join(missing),
            )
            return []

        token = self._token()
        if not token:
            return []

        subs = self.options.get("subreddits") or _DEFAULT_SUBS
        timeframe = str(self.options.get("timeframe", "week"))
        min_score = int(self.options.get("min_score", 50))
        headers = {"Authorization": f"bearer {token}"}

        items: list[Item] = []
        for sub in subs:
            if len(items) >= self.limit:
                break
            resp = self.ctx.fetcher.get(
                f"{_API_ROOT}/r/{sub}/top",
                params={"t": timeframe, "limit": "100"},
                headers=headers,
            )
            if resp is None:
                # A private, banned or renamed subreddit shouldn't fail the run.
                log.info("%s: r/%s unavailable", self.name, sub)
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
