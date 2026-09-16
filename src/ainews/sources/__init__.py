"""Source plugins. Importing this package registers every built-in type."""

from ainews.sources.base import Context, Source, available_types, build_source, register
from ainews.sources import arxiv, hackernews, imap, paywalled, reddit, rss, websearch  # noqa: F401

__all__ = [
    "Context",
    "Source",
    "available_types",
    "build_source",
    "register",
]
