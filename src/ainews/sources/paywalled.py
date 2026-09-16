"""Subscription news sites (NYT, WSJ, The Atlantic, ...).

Every one of these publishes a public RSS feed of headlines and abstracts, and
that is what this source reads by default - enough for the digest to tell you
what happened and link you to it.

If you point `NEWS_COOKIES_FILE` at a cookies.txt exported from a browser
profile that is already signed in to your own subscription, the source will
additionally read the full article text for the domains you listed in
`http.cookie_domains`. That is your own logged-in session being reused for your
own reading; it is not a paywall bypass, it stays inside robots.txt and the
configured rate limit, and article text is never republished - only the digest
you send yourself. Check each publisher's terms if you plan to use it for
anything beyond personal use.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ainews.models import Item
from ainews.sources.base import register
from ainews.sources.rss import RSSSource

log = logging.getLogger(__name__)


@register
class PaywalledSource(RSSSource):
    """Options: url (feed), domain (for the credential check), full_text."""

    type_name = "paywalled"
    required_options = ("url",)

    def fetch(self, since: datetime) -> list[Item]:
        domain = str(self.options.get("domain", "")).lower().lstrip(".")
        wants_full_text = bool(self.options.get("full_text", True))
        has_creds = bool(domain) and self.ctx.fetcher.has_credentials_for(domain)

        if wants_full_text and not has_creds:
            log.info(
                "%s: no subscription cookies for %s - using headlines and abstracts only",
                self.name,
                domain or "(no domain set)",
            )
        # RSSSource only fetches article bodies when full_text is truthy.
        self.options["full_text"] = wants_full_text and has_creds

        items = super().fetch(since)
        for item in items:
            item.extra.setdefault("subscription", domain or self.name)
            item.extra.setdefault("authenticated", has_creds)
        return items
