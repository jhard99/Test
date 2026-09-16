"""Core data types shared by sources, pipeline and renderer."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that identify a campaign/referrer rather than the content.
_TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|smid|smtyp|"
    r"partner|mod|cmp|cmpid|ncid|sh|share|__twitter_impression|guccounter|"
    r"campaign_id|emc|nl|te|user_email|impression_id|_hsenc|_hsmi|igshid)$",
    re.IGNORECASE,
)


def normalize_url(url: str | None) -> str:
    """Strip tracking params and trivial variations so the same story from two
    sources collapses to one key."""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme and not parts.netloc:
        return url

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":80"):
        netloc = netloc[:-3]
    if netloc.endswith(":443"):
        netloc = netloc[:-4]

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"

    return urlunsplit(("https" if parts.scheme in ("http", "https") else parts.scheme, netloc, path, urlencode(query), ""))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class Item:
    """One candidate story from any source."""

    source: str
    source_type: str
    title: str
    url: str | None = None
    published: datetime | None = None
    author: str | None = None
    summary: str = ""  # abstract / teaser as published
    text: str = ""  # full body when we were able to read it
    links: list[str] = field(default_factory=list)  # outbound links (newsletters)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.summary = (self.summary or "").strip()
        self.text = (self.text or "").strip()
        if self.published is not None and self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=timezone.utc)

    @property
    def key(self) -> str:
        """Stable identity: normalized URL if we have one, else source+title."""
        base = normalize_url(self.url) or f"{self.source_type}:{_slug(self.title)}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    @property
    def domain(self) -> str:
        if not self.url:
            return ""
        netloc = urlsplit(self.url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc

    @property
    def body(self) -> str:
        """Best available prose for this item."""
        return self.text or self.summary

    def excerpt(self, limit: int) -> str:
        """Body trimmed to `limit` characters on a word boundary."""
        body = self.body
        if limit <= 0 or len(body) <= limit:
            return body
        cut = body[:limit].rsplit(" ", 1)[0]
        return f"{cut} […truncated from {len(body)} chars]"

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.published is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.published).total_seconds() / 86400


@dataclass
class ScoredItem:
    """An item after LLM triage."""

    item: Item
    ai_related: bool
    importance: int  # 1 (minor) .. 5 (field-defining)
    topic: str
    one_liner: str

    @property
    def key(self) -> str:
        return self.item.key


@dataclass
class Cluster:
    """A group of items covering the same story."""

    headline: str
    topic: str
    importance: int
    items: list[ScoredItem] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        seen, out = set(), []
        for scored in self.items:
            name = scored.item.source
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out
