"""SQLite persistence for resumable crawls.

The crawler writes every processed page, its links and the pending frontier
to a small SQLite database.  If the process is interrupted the next run with
``--resume`` picks up exactly where the previous one stopped.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import Link, Page, SiteData

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_source ON links (source);
CREATE TABLE IF NOT EXISTS frontier (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    depth INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (
    url TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS sitemap (
    url TEXT PRIMARY KEY
);
"""


class CrawlStore:
    """Thin wrapper around the crawl state database.

    All methods are meant to be called from a single thread (the crawler's
    coordinating thread); worker threads never touch the store.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._conn.commit()

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> CrawlStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def commit(self) -> None:
        self._conn.commit()

    def clear(self) -> None:
        """Drop all crawl data but keep the schema."""
        for table in ("pages", "links", "frontier", "seen", "sitemap", "meta"):
            self._conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._conn.commit()

    # -- meta ----------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    # -- pages and links -----------------------------------------------------

    def save_page(self, page: Page) -> None:
        self._conn.execute(
            "INSERT INTO pages (url, data) VALUES (?, ?) "
            "ON CONFLICT(url) DO UPDATE SET data = excluded.data",
            (page.url, json.dumps(page.to_dict(), ensure_ascii=False)),
        )

    def save_links(self, source: str, links: Iterable[Link]) -> None:
        """Replace the stored outlinks of ``source``."""
        self._conn.execute("DELETE FROM links WHERE source = ?", (source,))
        self._conn.executemany(
            "INSERT INTO links (source, data) VALUES (?, ?)",
            [(source, json.dumps(link.to_dict(), ensure_ascii=False)) for link in links],
        )

    def page_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    def link_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]

    # -- frontier ------------------------------------------------------------

    def add_frontier(self, items: Iterable[tuple[str, int]], priority: int = 0) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO frontier (url, depth, priority) VALUES (?, ?, ?)",
            [(url, depth, priority) for url, depth in items],
        )

    def remove_frontier(self, url: str) -> None:
        self._conn.execute("DELETE FROM frontier WHERE url = ?", (url,))

    def load_frontier(self, priority: int = 0) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT url, depth FROM frontier WHERE priority = ? ORDER BY position", (priority,)
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def frontier_size(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]

    # -- seen / sitemap sets ---------------------------------------------------

    def mark_seen(self, urls: Iterable[str]) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen (url) VALUES (?)", [(url,) for url in urls]
        )

    def load_seen(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT url FROM seen")}

    def add_sitemap_urls(self, urls: Iterable[str]) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO sitemap (url) VALUES (?)", [(url,) for url in urls]
        )

    def load_sitemap_urls(self) -> set[str]:
        return {row[0] for row in self._conn.execute("SELECT url FROM sitemap")}

    # -- whole-site load -------------------------------------------------------

    def load_site(self) -> SiteData:
        """Rebuild a :class:`SiteData` from everything stored so far."""
        site = SiteData(start_url=self.get_meta("start_url", "") or "")
        for (data,) in self._conn.execute("SELECT data FROM pages ORDER BY url"):
            page = Page.from_dict(json.loads(data))
            site.pages[page.url] = page
        for (data,) in self._conn.execute("SELECT data FROM links ORDER BY id"):
            site.links.append(Link.from_dict(json.loads(data)))
        site.sitemap_urls = self.load_sitemap_urls()
        return site

    def has_state(self) -> bool:
        return self.get_meta("start_url") is not None
