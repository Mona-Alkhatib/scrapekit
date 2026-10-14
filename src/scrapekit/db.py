"""SQLite storage: what we fetched, every distinct version of it, and what we skipped."""

import sqlite3
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

    def commit(self) -> None:
        self.conn.commit()
