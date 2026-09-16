"""Turn an HTML page into readable article text."""

from __future__ import annotations

import logging
import re
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

log = logging.getLogger(__name__)

_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "svg",
    "button",
)

# Chrome that survives tag stripping. Word-bounded so "Subscriber-only
# reporting..." - a real sentence - is not mistaken for a subscribe prompt.
_BOILERPLATE = re.compile(
    r"\b(subscribe|newsletter|advertisement|sign in|sign up|cookies?|privacy policy|"
    r"share this article|related stories|most popular)\b",
    re.IGNORECASE,
)
# Boilerplate lines are short; a long paragraph that happens to mention
# "newsletter" is usually the article talking about one.
_BOILERPLATE_MAX_CHARS = 120

# Paywall interstitials tend to leave a short stub of real text behind.
_PAYWALL_MARKERS = re.compile(
    r"(subscribe to continue|continue reading your article|already a subscriber|"
    r"to continue reading|this article is for subscribers|create a free account)",
    re.IGNORECASE,
)


def _soup(html: str) -> BeautifulSoup:
    # Feeds hand us all sorts of things in a summary field - a bare URL, a
    # filename, a single word. BeautifulSoup warns that those "look more like a
    # filename than HTML", which is true and harmless here: flattening a
    # non-HTML string is exactly what html_to_text is for. Silence just that
    # warning so it can't bury a real one.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:  # lxml missing or malformed markup
            return BeautifulSoup(html, "html.parser")


def html_to_text(html: str) -> str:
    """Flatten arbitrary HTML (e.g. a newsletter body) to text."""
    if not html:
        return ""
    soup = _soup(html)
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    text = soup.get_text("\n")
    return _collapse(text)


def extract_links(html: str, limit: int = 60) -> list[str]:
    """Outbound http(s) links from an HTML body, in document order, deduped."""
    if not html:
        return []
    soup = _soup(html)
    out, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out


def extract_article(html: str) -> tuple[str, str]:
    """Return (title, body_text) for an article page.

    Heuristic, not a full readability port: prefer <article>, then the container
    with the most paragraph text.
    """
    if not html:
        return "", ""
    soup = _soup(html)
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    candidates = soup.find_all(["article", "main"]) or []
    candidates += soup.find_all("div", attrs={"class": re.compile(r"(article|story|post|content)-?(body|text)?", re.I)})
    if not candidates and soup.body:
        candidates = [soup.body]

    best_text = ""
    for node in candidates:
        paragraphs = [p.get_text(" ", strip=True) for p in node.find_all("p")]
        paragraphs = [
            p
            for p in paragraphs
            if len(p) > 40
            and not (len(p) < _BOILERPLATE_MAX_CHARS and _BOILERPLATE.search(p))
        ]
        text = _collapse("\n\n".join(paragraphs))
        if len(text) > len(best_text):
            best_text = text

    return title, best_text


def looks_paywalled(text: str, min_chars: int = 900) -> bool:
    """True when the extracted text looks like a paywall stub rather than a story."""
    if not text:
        return True
    if _PAYWALL_MARKERS.search(text[-1500:]) and len(text) < min_chars * 3:
        return True
    return len(text) < min_chars


def _collapse(text: str) -> str:
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
