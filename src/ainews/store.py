"""SQLite state: which stories we already covered, plus a cache for expensive
work (article extraction and LLM triage) so re-runs are cheap."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    key        TEXT PRIMARY KEY,
    url        TEXT,
    title      TEXT,
    source     TEXT,
    first_seen TEXT NOT NULL,
    week       TEXT
);
CREATE TABLE IF NOT EXISTS cache (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_items_first_seen ON seen_items (first_seen);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(_SCHEMA)
        self.conn.commit()

    # --- dedupe across weeks -------------------------------------------------

    def seen_keys(self, keys: Iterable[str]) -> set[str]:
        keys = list(keys)
        if not keys:
            return set()
        found: set[str] = set()
        for chunk_start in range(0, len(keys), 500):
            chunk = keys[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT key FROM seen_items WHERE key IN ({placeholders})", chunk
            ).fetchall()
            found.update(row["key"] for row in rows)
        return found

    def mark_seen(self, items: Iterable[Any], week: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = [(it.key, it.url or "", it.title, it.source, now, week) for it in items]
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items (key, url, title, source, first_seen, week)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    # --- generic cache -------------------------------------------------------

    def get_cache(self, key: str, max_age_days: int = 30) -> Any | None:
        row = self.conn.execute("SELECT value, created FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            created = datetime.fromisoformat(row["created"])
        except ValueError:
            return None
        if datetime.now(timezone.utc) - created > timedelta(days=max_age_days):
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return None

    def set_cache(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, created) VALUES (?, ?, ?)",
            (key, json.dumps(value), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    # --- housekeeping --------------------------------------------------------

    def purge(self, older_than_days: int = 180) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        cur = self.conn.execute("DELETE FROM seen_items WHERE first_seen < ?", (cutoff,))
        self.conn.execute("DELETE FROM cache WHERE created < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
