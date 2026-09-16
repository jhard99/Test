"""Keyword triage and clustering for runs with no model access at all.

Not as good as asking Claude - it cannot tell you *why* something matters - but
it filters the noise, ranks what is left and groups obvious duplicates, which is
enough for a useful weekly digest when no API is available.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ainews.config import Config
from ainews.models import Cluster, Item, ScoredItem

# Terms that make an item unambiguously about AI.
_STRONG = [
    "artificial intelligence", "machine learning", "deep learning", "neural network",
    "large language model", "language model", "foundation model", "frontier model",
    "generative ai", "genai", "llm", "llms", "gpt", "chatgpt", "claude", "gemini",
    "llama", "mistral", "deepseek", "qwen", "transformer", "diffusion model",
    "stable diffusion", "midjourney", "openai", "anthropic", "deepmind",
    "hugging face", "mixture of experts", "fine-tune", "fine-tuning", "fine-tuned",
    "rlhf", "reinforcement learning", "embeddings", "inference", "ai agent",
    "agentic", "copilot", "agi", "superintelligence", "alignment", "multimodal",
    "prompt injection", "prompt engineering", "model weights", "open-weight",
    "open weights", "training data", "hallucination", "chain of thought",
    "computer vision", "speech recognition", "recommendation algorithm",
]
# Terms that only count as AI in company with others.
_WEAK = [
    "ai", "a.i.", "algorithm", "algorithmic", "chatbot", "automation", "automate",
    "data center", "datacenter", "gpu", "gpus", "nvidia", "chip", "chips",
    "semiconductor", "compute", "robot", "robotics", "autonomous", "deepfake",
    "synthetic media", "facial recognition", "self-driving",
    # Vocabulary of model releases, which often name no known product at all
    # ("Northwind Labs releases Orion 3"). Weak on their own, telling together.
    "model", "models", "benchmark", "benchmarks", "token", "tokens", "parameters",
    "training", "trained", "dataset", "reasoning", "assistant", "agent", "agents",
    "context window", "open-source model", "lab", "labs",
]
# Words suggesting something actually happened, rather than commentary.
_EVENT = [
    "launch", "launches", "launched", "release", "releases", "released", "unveil",
    "unveils", "announce", "announces", "announced", "ship", "ships", "shipped",
    "raise", "raises", "raised", "funding", "acquire", "acquires", "acquisition",
    "merger", "ipo", "ban", "bans", "banned", "rule", "rules", "ruling", "regulate",
    "regulation", "lawsuit", "sues", "sued", "settle", "settlement", "breach",
    "leak", "resign", "resigns", "fired", "layoff", "layoffs", "shut down",
    "open-source", "open sources", "partnership", "deal",
]
# Headline shapes that are usually filler.
_LOW_VALUE = [
    "best", "top 10", "top 5", "how to", "why you should", "everything you need",
    "review:", "hands-on", "deals", "roundup", "explainer", "opinion:", "guide to",
]

_TOPIC_RULES: list[tuple[str, list[str]]] = [
    ("policy-and-regulation", ["regulat", "law", "bill", "congress", "senate", "parliament",
                               "commission", "court", "lawsuit", "sued", "ban", "policy",
                               "antitrust", "copyright", "compliance", "executive order"]),
    ("business-and-funding", ["funding", "raise", "raised", "valuation", "ipo", "acquire",
                              "acquisition", "merger", "revenue", "profit", "layoff",
                              "hire", "resign", "ceo", "startup", "investor", "deal"]),
    ("compute-and-infrastructure", ["data center", "datacenter", "gpu", "chip", "nvidia",
                                    "tpu", "semiconductor", "cluster", "capacity", "energy",
                                    "power", "supply"]),
    ("safety-and-alignment", ["safety", "alignment", "misuse", "jailbreak", "red team",
                              "guardrail", "risk", "bioweapon", "prompt injection", "abuse"]),
    ("research", ["paper", "preprint", "arxiv", "benchmark", "study", "researchers",
                  "we propose", "state-of-the-art", "evaluation"]),
    ("models-and-releases", ["model", "release", "launch", "version", "preview",
                             "open-source", "open weights", "api", "context window"]),
    ("products-and-tools", ["app", "feature", "product", "tool", "integration", "plugin",
                            "assistant", "rollout", "users"]),
    ("society-and-labor", ["jobs", "workers", "employment", "school", "student",
                           "artist", "writer", "union", "public"]),
]

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "with", "from", "into", "that", "this",
    "its", "it", "of", "to", "in", "on", "at", "by", "as", "is", "are", "was", "were",
    "be", "been", "has", "have", "had", "will", "would", "can", "could", "new", "says",
    "said", "after", "over", "how", "why", "what", "more", "than", "not", "you", "your",
}


def _matcher(terms: list[str]) -> re.Pattern[str]:
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"(?<![\w-])(" + "|".join(escaped) + r")(?![\w-])", re.IGNORECASE)


_STRONG_RE = _matcher(_STRONG)
_WEAK_RE = _matcher(_WEAK)
_EVENT_RE = _matcher(_EVENT)
_LOW_VALUE_RE = re.compile("|".join(re.escape(t) for t in _LOW_VALUE), re.IGNORECASE)


def _count(pattern: re.Pattern[str], text: str) -> int:
    """Number of distinct terms from `pattern` present in `text`."""
    return len({m.group(0).lower() for m in pattern.finditer(text)})


def assume_ai(item: Item) -> bool:
    """Some sources are topical by construction - an arXiv cs.AI feed, a source
    named 'TechCrunch AI' - so a weak signal is enough there."""
    if item.source_type == "arxiv":
        return True
    return bool(re.search(r"(?<![\w-])(ai|ml|llm)(?![\w-])", item.source, re.IGNORECASE))


def classify_topic(text: str, item: Item) -> str:
    if item.source_type == "arxiv":
        return "research"
    lowered = text.lower()
    for topic, terms in _TOPIC_RULES:
        if any(term in lowered for term in terms):
            return topic
    return "other"


def engagement(item: Item) -> int:
    return max(int(item.extra.get("points", 0) or 0), int(item.extra.get("score", 0) or 0))


def score_item(item: Item, now: datetime | None = None) -> ScoredItem:
    """Approximate the triage call with keywords and engagement."""
    now = now or datetime.now(timezone.utc)
    haystack = f"{item.title}\n{item.summary[:1500]}"
    title_strong = _count(_STRONG_RE, item.title)
    title_weak = _count(_WEAK_RE, item.title)
    strong = _count(_STRONG_RE, haystack)
    weak = _count(_WEAK_RE, haystack)

    # Title matches count for more than body matches: a story whose headline is
    # about AI is AI news, while one passing mention in a blurb ("nothing to do
    # with machine learning") is not.
    ai_related = (
        item.source_type == "arxiv"  # feed is already restricted to AI categories
        or title_strong >= 1
        or title_weak >= 2
        or strong >= 2
        or (strong >= 1 and weak >= 1)
        or (weak >= 2 and title_weak >= 1)
        or (assume_ai(item) and (title_weak >= 1 or strong >= 1))
    )

    importance = 2
    if strong >= 2:
        importance += 1
    if _EVENT_RE.search(item.title):
        importance += 1
    points = engagement(item)
    if points >= 500:
        importance += 1
    elif points >= 200:
        importance += 0
    if len(item.extra.get("also_seen_in", [])) >= 1:
        importance += 1  # several outlets thought it was worth covering
    if _LOW_VALUE_RE.search(item.title):
        importance -= 1
    age = item.age_days(now)
    if age is not None and age > 10:
        importance -= 1
    importance = max(1, min(5, importance))

    return ScoredItem(
        item=item,
        ai_related=ai_related,
        importance=importance,
        topic=classify_topic(haystack, item),
        one_liner=first_sentence(item.summary or item.text),
    )


def first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    match = re.search(r"(?<=[.!?])\s", text[: limit + 80])
    if match and match.start() <= limit:
        return text[: match.start()]
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def triage(items: list[Item], now: datetime | None = None) -> list[ScoredItem]:
    scored = [score_item(item, now) for item in items]
    scored.sort(key=lambda s: (s.importance, engagement(s.item)), reverse=True)
    return scored


# --- clustering ---------------------------------------------------------------


def _tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    overlap = len(a & b)
    jaccard = overlap / len(a | b)
    containment = overlap / min(len(a), len(b))
    return jaccard >= 0.5 or (containment >= 0.8 and overlap >= 3)


def cluster(scored: list[ScoredItem]) -> list[Cluster]:
    """Group by headline-token overlap. Conservative: it would rather leave two
    copies of a story apart than merge two different ones."""
    clusters: list[Cluster] = []
    signatures: list[set[str]] = []

    for entry in scored:
        tokens = _tokens(entry.item.title)
        for index, existing in enumerate(signatures):
            if _similar(tokens, existing):
                clusters[index].items.append(entry)
                clusters[index].importance = max(clusters[index].importance, entry.importance)
                signatures[index] = existing | tokens
                break
        else:
            clusters.append(
                Cluster(
                    headline=entry.item.title,
                    topic=entry.topic,
                    importance=entry.importance,
                    items=[entry],
                )
            )
            signatures.append(tokens)

    clusters.sort(key=lambda c: (c.importance, len(c.items)), reverse=True)
    return clusters


def run(items: list[Item], cfg: Config) -> list[Cluster]:
    """Full offline pipeline: score, filter, cluster."""
    scored = [s for s in triage(items) if s.ai_related and s.importance >= cfg.min_importance]
    return cluster(scored)
