"""SQLite storage: what we fetched, every distinct version of it, and what we skipped.

The tables, in the order a run fills them:
- runs         one row per run, with its metrics (stats_json)
- documents    one row per URL: kind, host, last status, ETag, current content hash
- snapshots    one row per distinct version of a URL; the raw bytes are in data/raw (blobs.py)
- skips        every skipped or failed URL with a reason
- links        which page links to which file
- extractions  which (url, content hash, extractor version) is done, so re-runs skip it
- facts        one row per field of one entity, with source, locator, excerpt and history
- pdf_pages    PDF text per page, for the LLM and for checking its quotes
- llm_cache    paid LLM answers, so the same content is never paid for twice

History rule: facts are never overwritten. See apply_facts().
"""

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    mode         TEXT NOT NULL,
    stats_json   TEXT
);

-- One row per URL we know about (a page on a crawl host, or a linked file on any host).
CREATE TABLE IF NOT EXISTS documents (
    url             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- 'page' or 'file'
    host            TEXT NOT NULL,
    discovered_from TEXT,
    first_seen_run  TEXT NOT NULL,
    last_seen_run   TEXT NOT NULL,
    last_status     INTEGER,
    etag            TEXT,
    last_modified   TEXT,
    content_type    TEXT,
    current_hash    TEXT
);

-- One row per distinct content version. A new row means the source changed.
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    path         TEXT NOT NULL,
    UNIQUE (url, content_hash)
);

-- Every link we did not fetch, or fetched and could not use, with a reason.
CREATE TABLE IF NOT EXISTS skips (
    id      INTEGER PRIMARY KEY,
    run_id  TEXT NOT NULL,
    url     TEXT NOT NULL,
    reason  TEXT NOT NULL,
    detail  TEXT,
    at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
-- One row per field of one entity, as observed. Never updated in place except to close it:
-- a changed value inserts a new row and sets valid_to + superseded_by on the old one.
CREATE TABLE IF NOT EXISTS facts (
    id                 INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,
    entity_type        TEXT NOT NULL,
    entity_key         TEXT NOT NULL,
    field              TEXT NOT NULL,
    value              TEXT NOT NULL,          -- JSON
    source_url         TEXT NOT NULL,
    content_hash       TEXT NOT NULL,          -- the snapshot this was read from
    locator            TEXT NOT NULL,          -- JSON path, "page 3", "<title>"
    excerpt            TEXT,
    method             TEXT NOT NULL,          -- parse or llm
    run_id             TEXT NOT NULL,
    valid_from         TEXT NOT NULL,
    valid_to           TEXT,                   -- NULL means this is the current value
    superseded_by      INTEGER,
    last_confirmed_run TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_by_url ON facts (source_url, valid_to);
CREATE INDEX IF NOT EXISTS facts_by_entity ON facts (source, entity_type, entity_key);

-- Which (url, content version, extractor) we already processed. This is what makes re-runs cheap.
CREATE TABLE IF NOT EXISTS extractions (
    url          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extractor    TEXT NOT NULL,
    status       TEXT NOT NULL,                -- ok, failed, unsupported
    detail       TEXT,
    run_id       TEXT NOT NULL,
    at           TEXT NOT NULL,
    PRIMARY KEY (url, content_hash, extractor)
);

-- Paid LLM answers, keyed by content and model+prompt version. A hit costs nothing.
CREATE TABLE IF NOT EXISTS llm_cache (
    content_hash  TEXT NOT NULL,
    model_key     TEXT NOT NULL,               -- model|prompt version
    response      TEXT NOT NULL,               -- JSON
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (content_hash, model_key)
);

CREATE TABLE IF NOT EXISTS pdf_pages (
    content_hash TEXT NOT NULL,
    page_number  INTEGER NOT NULL,             -- 1-based, as a person would cite it
    text         TEXT NOT NULL,
    PRIMARY KEY (content_hash, page_number)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


class Store:
    """Thin wrapper so the crawler never writes SQL directly."""

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn = conn
        self.run_id = run_id

    def start_run(self, source: str, mode: str) -> None:
        self.conn.execute(
            "INSERT INTO runs (run_id, source, started_at, mode) VALUES (?, ?, ?, ?)",
            (self.run_id, source, utcnow(), mode),
        )
        self.conn.commit()

    def finish_run(self, stats_json: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE run_id = ?",
            (utcnow(), stats_json, self.run_id),
        )
        self.conn.commit()

    def get_document(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()

    def upsert_document(self, source: str, url: str, kind: str, host: str, discovered_from: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO documents (url, source, kind, host, discovered_from, first_seen_run, last_seen_run)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET last_seen_run = excluded.last_seen_run
            """,
            (url, source, kind, host, discovered_from, self.run_id, self.run_id),
        )

    def record_fetch(
        self,
        url: str,
        status: int,
        etag: str | None,
        last_modified: str | None,
        content_type: str | None,
        content_hash: str | None,
    ) -> None:
        # On a 304 we keep the previous validators and hash.
        self.conn.execute(
            """
            UPDATE documents SET
                last_status   = ?,
                etag          = COALESCE(?, etag),
                last_modified = COALESCE(?, last_modified),
                content_type  = COALESCE(?, content_type),
                current_hash  = COALESCE(?, current_hash)
            WHERE url = ?
            """,
            (status, etag, last_modified, content_type, content_hash, url),
        )

    def add_snapshot(self, url: str, content_hash: str, size: int, path: str) -> bool:
        """Returns True when this content is new for this URL."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO snapshots (url, content_hash, fetched_at, run_id, bytes, path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (url, content_hash, utcnow(), self.run_id, size, path),
        )
        return cur.rowcount == 1

    def latest_snapshot_path(self, url: str) -> str | None:
        row = self.conn.execute(
            "SELECT path FROM snapshots WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
        ).fetchone()
        return row["path"] if row else None

    def add_skip(self, url: str, reason: str, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO skips (run_id, url, reason, detail, at) VALUES (?, ?, ?, ?, ?)",
            (self.run_id, url, reason, detail, utcnow()),
        )

    def add_link(self, src: str, dst: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO links (src, dst) VALUES (?, ?)", (src, dst))

    # ---------- extraction ----------

    def documents_to_extract(self, source: str) -> list[sqlite3.Row]:
        """Documents seen in the latest crawl of this source that have a saved copy."""
        return self.conn.execute(
            """
            SELECT d.url, d.kind, d.content_type, d.current_hash, s.path
            FROM documents d JOIN snapshots s ON s.url = d.url AND s.content_hash = d.current_hash
            WHERE d.source = ? AND d.last_status IN (200, 304)
              AND d.last_seen_run = (SELECT MAX(last_seen_run) FROM documents WHERE source = ?)
            ORDER BY d.url
            """,
            (source, source),
        ).fetchall()

    def already_extracted(self, url: str, content_hash: str, extractor: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM extractions WHERE url = ? AND content_hash = ? AND extractor = ?",
            (url, content_hash, extractor),
        ).fetchone()
        return row is not None

    def record_extraction(self, url: str, content_hash: str, extractor: str, status: str, detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO extractions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, content_hash, extractor, status, detail, self.run_id, utcnow()),
        )

    def save_pdf_pages(self, content_hash: str, pages: list[str]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO pdf_pages VALUES (?, ?, ?)",
            [(content_hash, i + 1, text) for i, text in enumerate(pages)],
        )

    def pdf_pages(self, content_hash: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT text FROM pdf_pages WHERE content_hash = ? ORDER BY page_number", (content_hash,)
        ).fetchall()
        return [r["text"] for r in rows]

    def llm_cache_get(self, content_hash: str, model_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM llm_cache WHERE content_hash = ? AND model_key = ?", (content_hash, model_key)
        ).fetchone()

    def llm_cache_put(self, content_hash: str, model_key: str, response: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content_hash, model_key, response, tokens_in, tokens_out, cost, utcnow()),
        )

    # The history logic. For a changed value it inserts a new row, then closes the old one
    # (valid_to = now, superseded_by = new id), so every correction keeps its previous value.
    def apply_facts(self, source: str, url: str, content_hash: str, facts: list, method: str = "parse") -> Counter:
        """Compares new facts from one document with the current ones and records the difference.

        new: field seen for the first time. updated: value changed, old row closed and linked.
        unchanged: same value, only last_confirmed_run moves. removed: field no longer present.
        Only facts made by the same method are compared, so rule facts and LLM facts never cancel out.
        """
        now = utcnow()
        counts: Counter = Counter()
        current = {
            (r["entity_type"], r["entity_key"], r["field"]): r
            for r in self.conn.execute(
                "SELECT id, entity_type, entity_key, field, value FROM facts"
                " WHERE source_url = ? AND method = ? AND valid_to IS NULL",
                (url, method),
            )
        }
        seen = set()
        for f in facts:
            key = (f.entity_type, f.entity_key, f.field)
            if key in seen:
                continue
            seen.add(key)
            value = f.value_json()
            old = current.get(key)
            if old is not None and old["value"] == value:
                self.conn.execute("UPDATE facts SET last_confirmed_run = ? WHERE id = ?", (self.run_id, old["id"]))
                counts["unchanged"] += 1
                continue
            cur = self.conn.execute(
                """
                INSERT INTO facts (source, entity_type, entity_key, field, value, source_url, content_hash,
                                   locator, excerpt, method, run_id, valid_from, last_confirmed_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source, *key, value, url, content_hash, f.locator, f.excerpt, f.method, self.run_id, now, self.run_id),
            )
            if old is None:
                counts["new"] += 1
            else:
                self.conn.execute(
                    "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?", (now, cur.lastrowid, old["id"])
                )
                counts["updated"] += 1
        for key, old in current.items():
            if key not in seen:
                self.conn.execute("UPDATE facts SET valid_to = ? WHERE id = ?", (now, old["id"]))
                counts["removed"] += 1
        return counts

    def commit(self) -> None:
        self.conn.commit()
