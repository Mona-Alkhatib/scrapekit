"""Every query the UI runs. Plain functions over a read-only SQLite connection.

This is the boundary between the scraper's data and the UI: pages call these functions and
never write SQL. They can be tested without Streamlit, and wrapped in an HTTP API later.
"""

import json
import re
import sqlite3
from pathlib import Path

# Skip reasons that mean something went wrong (others, like off_domain, are expected policy).
# Any http_<code> skip (404, 403, 500 ...) also counts.
FAILURE_REASONS = ("network_error", "parse_error", "llm_error", "internal_error", "bad_sitemap")


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _like(text: str) -> str:
    """User text as a literal LIKE pattern (used with ESCAPE '\\')."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def sources(conn) -> list[str]:
    return [r["source"] for r in _rows(conn, "SELECT DISTINCT source FROM runs ORDER BY source")]


# ---------- runs ----------

def run_status(finished_at: str | None, stats: dict) -> str:
    if not finished_at:
        return "incomplete"
    skips = stats.get("skips") or {}
    failed = any(r in FAILURE_REASONS or r.startswith("http_") for r in skips) or stats.get("llm_failed")
    return "ok, with failures" if failed else "ok"


def _run_row(r: dict) -> dict:
    stats = json.loads(r["stats_json"] or "{}")
    codes = stats.get("status_counts") or {}
    return {
        "run_id": r["run_id"], "source": r["source"], "mode": r["mode"],
        "status": run_status(r["finished_at"], stats),
        "started_at": r["started_at"], "finished_at": r["finished_at"],
        "minutes": round((stats.get("duration_seconds") or 0) / 60, 1),
        "requests": stats.get("requests", 0), "not_modified": codes.get("304", 0),
        "pages_downloaded": stats.get("pages_fetched", 0), "files_downloaded": stats.get("files_fetched", 0),
        "files_not_rechecked": stats.get("files_not_rechecked", 0),
        "facts_new": stats.get("records_new", 0), "facts_updated": stats.get("records_updated", 0),
        "skips": sum((stats.get("skips") or {}).values()),
        "llm_calls": stats.get("llm_calls", 0), "llm_cost_usd": round(stats.get("llm_cost_usd", 0.0), 4),
        "stats": stats,
    }


def runs(conn, source: str) -> list[dict]:
    return [_run_row(r) for r in _rows(conn, "SELECT * FROM runs WHERE source = ? ORDER BY run_id DESC", (source,))]


def run(conn, run_id: str) -> dict | None:
    rows = _rows(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    return _run_row(rows[0]) if rows else None


def run_fact_changes(conn, run_id: str, limit: int = 1000) -> list[dict]:
    """Facts this run added or replaced, plus facts it closed without a replacement (removed).

    Removals carry no run id, only a time, so they are matched to the run's time window: from its start to
    its end, or for a run that never finished, to the start of the next run of the same source.
    """
    r = _rows(conn, "SELECT source, started_at, finished_at FROM runs WHERE run_id = ?", (run_id,))
    if not r:
        return []
    source, start, end = r[0]["source"], r[0]["started_at"], r[0]["finished_at"]
    added = _rows(conn, """
        SELECT CASE WHEN f.id IN (SELECT superseded_by FROM facts WHERE superseded_by IS NOT NULL)
                    THEN 'updated' ELSE 'new' END AS change, f.*
        FROM facts f WHERE f.run_id = ? ORDER BY f.id LIMIT ?""", (run_id, limit))
    if end:
        window, params = "f.valid_to BETWEEN ? AND ?", [start, end]
    else:
        nxt = _rows(conn, "SELECT MIN(started_at) AS t FROM runs WHERE source = ? AND run_id > ?", (source, run_id))
        window, params = "f.valid_to >= ? AND f.valid_to < ?", [start, nxt[0]["t"] or "9999"]
    removed = _rows(conn, f"""
        SELECT 'removed' AS change, f.* FROM facts f
        WHERE f.source = ? AND f.superseded_by IS NULL AND {window} ORDER BY f.id LIMIT ?""",
        [source, *params, limit])
    return added + removed


# ---------- skips ----------

def skip_counts(conn, source: str, run_id: str | None = None) -> list[dict]:
    sql = "SELECT s.reason, COUNT(*) AS n FROM skips s JOIN runs r ON r.run_id = s.run_id WHERE r.source = ?"
    params: list = [source]
    if run_id:
        sql += " AND s.run_id = ?"
        params.append(run_id)
    return _rows(conn, sql + " GROUP BY s.reason ORDER BY n DESC", params)


def skips(conn, source: str, reason=None, run_id=None, search=None, limit: int = 1000) -> list[dict]:
    sql = ("SELECT s.run_id, s.reason, s.url, s.detail, s.at FROM skips s JOIN runs r ON r.run_id = s.run_id"
           " WHERE r.source = ?")
    params: list = [source]
    if reason:
        sql += " AND s.reason = ?"
        params.append(reason)
    if run_id:
        sql += " AND s.run_id = ?"
        params.append(run_id)
    if search:
        sql += " AND (s.url LIKE ? ESCAPE '\\' OR s.detail LIKE ? ESCAPE '\\')"
        params += [_like(search), _like(search)]
    return _rows(conn, sql + " ORDER BY s.id DESC LIMIT ?", params + [limit])


# ---------- entities and facts ----------

NAME_FIELDS = ("name", "title", "llm_title", "pdf_title", "file_name_title")


def entity_types(conn, source: str) -> list[str]:
    return [r["entity_type"] for r in _rows(
        conn, "SELECT DISTINCT entity_type FROM facts WHERE source = ? ORDER BY 1", (source,))]


def entities(conn, source: str, entity_type=None, search=None, limit: int = 500) -> list[dict]:
    name = "COALESCE(" + ", ".join(
        f"MAX(CASE WHEN field = '{f}' THEN json_extract(value, '$') END)" for f in NAME_FIELDS) + ")"
    sql = (f"SELECT entity_type, entity_key, {name} AS name, MIN(source_url) AS source_url, COUNT(*) AS facts"
           " FROM facts WHERE source = ? AND valid_to IS NULL")
    params: list = [source]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    sql += " GROUP BY entity_type, entity_key"
    if search:
        sql += (" HAVING entity_key LIKE ? ESCAPE '\\' OR SUM(CASE WHEN field != 'body_text'"
                " AND value LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) > 0")
        params += [_like(search), _like(search)]
    return _rows(conn, sql + " ORDER BY entity_type, entity_key LIMIT ?", params + [limit])


def entity_facts(conn, source: str, entity_type: str, entity_key: str, history: bool = False) -> list[dict]:
    sql = "SELECT * FROM facts WHERE source = ? AND entity_type = ? AND entity_key = ?"
    if not history:
        sql += " AND valid_to IS NULL"
    return _rows(conn, sql + " ORDER BY field, id", (source, entity_type, entity_key))


def fact_fields(conn, source: str) -> list[str]:
    return [r["field"] for r in _rows(conn, "SELECT DISTINCT field FROM facts WHERE source = ? ORDER BY 1", (source,))]


def facts(conn, source: str, entity_type=None, field=None, method=None, run_id=None, source_url=None,
          current_only=True, search=None, limit: int = 1000) -> list[dict]:
    sql = "SELECT * FROM facts WHERE source = ?"
    params: list = [source]
    for col, val in (("entity_type", entity_type), ("field", field), ("method", method), ("run_id", run_id),
                     ("source_url", source_url)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    if current_only:
        sql += " AND valid_to IS NULL"
    if search:
        sql += " AND (value LIKE ? ESCAPE '\\' OR entity_key LIKE ? ESCAPE '\\')"
        params += [_like(search), _like(search)]
    return _rows(conn, sql + " ORDER BY id DESC LIMIT ?", params + [limit])


def fact(conn, fact_id: int) -> dict | None:
    rows = _rows(conn, "SELECT * FROM facts WHERE id = ?", (fact_id,))
    return rows[0] if rows else None


def fact_history(conn, fact_id: int) -> list[dict]:
    """Every version of this fact (same entity, field, source and method), oldest first."""
    f = fact(conn, fact_id)
    if f is None:
        return []
    return _rows(conn, """SELECT * FROM facts WHERE source = ? AND entity_type = ? AND entity_key = ?
                          AND field = ? AND source_url = ? AND method = ? ORDER BY id""",
                 (f["source"], f["entity_type"], f["entity_key"], f["field"], f["source_url"], f["method"]))


# ---------- pages and files ----------

def hosts(conn, source: str) -> list[str]:
    return [r["host"] for r in _rows(conn, "SELECT DISTINCT host FROM documents WHERE source = ? ORDER BY 1", (source,))]


def documents(conn, source: str, kind=None, host=None, saved=None, search=None, limit: int = 1000) -> list[dict]:
    sql = """SELECT d.url, d.kind, d.host, d.last_status, d.current_hash IS NOT NULL AS saved,
                    (SELECT COUNT(*) FROM snapshots s WHERE s.url = d.url) AS versions,
                    (SELECT bytes FROM snapshots s WHERE s.url = d.url AND s.content_hash = d.current_hash) AS bytes,
                    d.content_type, d.first_seen_run, d.last_seen_run
             FROM documents d WHERE d.source = ?"""
    params: list = [source]
    for col, val in (("d.kind", kind), ("d.host", host)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    if saved is not None:
        sql += " AND d.current_hash IS " + ("NOT NULL" if saved else "NULL")
    if search:
        sql += " AND d.url LIKE ? ESCAPE '\\'"
        params.append(_like(search))
    return _rows(conn, sql + " ORDER BY d.kind, d.url LIMIT ?", params + [limit])


def document(conn, url: str) -> dict | None:
    rows = _rows(conn, "SELECT * FROM documents WHERE url = ?", (url,))
    return rows[0] if rows else None


def snapshots(conn, url: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM snapshots WHERE url = ? ORDER BY id DESC", (url,))


def links_to(conn, url: str) -> list[str]:
    return [r["src"] for r in _rows(conn, "SELECT src FROM links WHERE dst = ? ORDER BY src", (url,))]


def links_from(conn, url: str) -> list[str]:
    return [r["dst"] for r in _rows(conn, "SELECT dst FROM links WHERE src = ? ORDER BY dst", (url,))]


def url_skips(conn, url: str) -> list[dict]:
    return _rows(conn, "SELECT run_id, reason, detail, at FROM skips WHERE url = ? ORDER BY id DESC", (url,))


# ---------- provenance ----------

def pdf_page_text(conn, content_hash: str, page: int) -> str | None:
    rows = _rows(conn, "SELECT text FROM pdf_pages WHERE content_hash = ? AND page_number = ?", (content_hash, page))
    return rows[0]["text"] if rows else None


def raw_path(conn, content_hash: str, raw_dir: Path | None = None) -> Path | None:
    """The saved file. Paths are stored as the crawl saw them (relative to where it ran), so if that does not
    resolve, look for the same file name under this database's own raw/ folder."""
    rows = _rows(conn, "SELECT path FROM snapshots WHERE content_hash = ? LIMIT 1", (content_hash,))
    if not rows:
        return None
    path = Path(rows[0]["path"])
    for candidate in (path, raw_dir / path.parent.name / path.name if raw_dir else None):
        if candidate is not None and candidate.exists():
            return candidate
    return None


def find_excerpt(text: str, excerpt: str) -> tuple[int, int] | None:
    """Where the quote is in the page text, ignoring case, spacing and punctuation between words."""
    words = re.findall(r"[A-Za-z0-9$%.,]+", excerpt)
    if not words:
        return None
    pattern = r"\W*".join(re.escape(w.strip(".,")) for w in words if w.strip(".,"))
    match = re.search(pattern, text, re.IGNORECASE)
    return match.span() if match else None


# ---------- overview ----------

# Most reliable date first, same order as the export's best_date.
DATE_FIELDS = ("first_published_at", "llm_document_date", "pdf_created")


def overview(conn, source: str) -> dict:
    one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    cost = sum(json.loads(r["stats_json"] or "{}").get("llm_cost_usd", 0.0)
               for r in _rows(conn, "SELECT stats_json FROM runs WHERE source = ?", (source,)))
    return {
        "entities": one("SELECT COUNT(DISTINCT entity_type || ':' || entity_key) FROM facts"
                        " WHERE source = ? AND valid_to IS NULL", source),
        "facts": one("SELECT COUNT(*) FROM facts WHERE source = ? AND valid_to IS NULL", source),
        "pages": one("SELECT COUNT(*) FROM documents WHERE source = ? AND kind = 'page' AND current_hash IS NOT NULL", source),
        "files": one("SELECT COUNT(*) FROM documents WHERE source = ? AND kind = 'file' AND current_hash IS NOT NULL", source),
        "skips": one("SELECT COUNT(*) FROM skips s JOIN runs r ON r.run_id = s.run_id WHERE r.source = ?", source),
        "runs": one("SELECT COUNT(*) FROM runs WHERE source = ?", source),
        "llm_cost_usd": round(cost, 4),
    }


def facts_by_type_method(conn, source: str) -> list[dict]:
    return _rows(conn, """SELECT entity_type, method, COUNT(*) AS n FROM facts
                          WHERE source = ? AND valid_to IS NULL GROUP BY 1, 2 ORDER BY 1, 2""", (source,))


def label_counts(conn, source: str, entity_type: str, field: str) -> list[dict]:
    """How many entities have each value of a text field, e.g. projects per phase."""
    return _rows(conn, """SELECT json_extract(value, '$') AS value, COUNT(*) AS n FROM facts
                          WHERE source = ? AND entity_type = ? AND field = ? AND valid_to IS NULL
                            AND json_type(value) = 'text'
                          GROUP BY 1 ORDER BY n DESC, value""", (source, entity_type, field))


def files_per_host(conn, source: str) -> list[dict]:
    return _rows(conn, """SELECT host, COUNT(*) AS n FROM documents
                          WHERE source = ? AND kind = 'file' AND current_hash IS NOT NULL
                          GROUP BY host ORDER BY n DESC""", (source,))


def entities_per_year(conn, source: str) -> list[dict]:
    """Entities per year of their best date (publish date, else the date in the document, else PDF metadata)."""
    best = "COALESCE(" + ", ".join(
        f"MAX(CASE WHEN field = '{f}' THEN substr(json_extract(value, '$'), 1, 4) END)" for f in DATE_FIELDS) + ")"
    return _rows(conn, f"""SELECT year, entity_type, COUNT(*) AS n FROM (
                               SELECT entity_type, {best} AS year FROM facts
                               WHERE source = ? AND valid_to IS NULL GROUP BY entity_type, entity_key)
                           WHERE year GLOB '[12][0-9][0-9][0-9]' GROUP BY year, entity_type ORDER BY year, entity_type""",
                 (source,))


def locations(conn, source: str) -> list[dict]:
    """Every entity with a location fact, with its name, for a map."""
    name = "COALESCE(" + ", ".join(
        f"(SELECT json_extract(n.value, '$') FROM facts n WHERE n.source = f.source AND n.entity_type = f.entity_type"
        f" AND n.entity_key = f.entity_key AND n.field = '{x}' AND n.valid_to IS NULL)" for x in NAME_FIELDS[:2]) + ")"
    return _rows(conn, f"""SELECT f.entity_type, f.entity_key, {name} AS name,
                                  json_extract(f.value, '$.lat') AS lat, json_extract(f.value, '$.lon') AS lon
                           FROM facts f WHERE f.source = ? AND f.field = 'location' AND f.valid_to IS NULL
                             AND json_extract(f.value, '$.lat') IS NOT NULL ORDER BY f.entity_key""", (source,))


# ---------- data dictionary ----------

def field_stats(conn, source: str) -> list[dict]:
    """Every field per entity type and method: how many entities have it, share of all entities of that type,
    and one example value with its locator."""
    rows = _rows(conn, """
        WITH cur AS (SELECT * FROM facts WHERE source = ? AND valid_to IS NULL),
             agg AS (SELECT entity_type, field, method, COUNT(DISTINCT entity_key) AS entities, MIN(id) AS first_id
                     FROM cur GROUP BY entity_type, field, method),
             tot AS (SELECT entity_type, COUNT(DISTINCT entity_key) AS n FROM cur GROUP BY entity_type)
        SELECT a.entity_type, a.field, a.method, a.entities, ROUND(a.entities * 1.0 / t.n, 3) AS coverage,
               f.value AS example, f.locator
        FROM agg a JOIN tot t ON t.entity_type = a.entity_type JOIN facts f ON f.id = a.first_id
        ORDER BY a.entity_type, a.method DESC, a.field""", (source,))
    for r in rows:
        value = json.loads(r["example"])
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        r["example"] = text if len(text) <= 120 else text[:117] + "..."
    return rows
