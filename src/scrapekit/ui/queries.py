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
    """Opens the database read-only: any write raises an error."""
    # "mode=ro" in a file: URI makes SQLite itself refuse writes. check_same_thread=False lets Streamlit's
    # worker threads share the one cached connection (we only read, so that is safe).
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["url"]
    return conn


def _rows(conn, sql: str, params=()) -> list[dict]:
    """Runs a query with parameters (never string-formatted user input) and returns plain dicts."""
    return [dict(r) for r in conn.execute(sql, params)]


def _like(text: str) -> str:
    """User text as a literal LIKE pattern (used with ESCAPE '\\')."""
    # In LIKE, % and _ are wildcards. Escape them (and the escape character) so "100%" searches for
    # the text "100%", then wrap in % so it matches anywhere in the value.
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _current(as_of: str | None, alias: str = "") -> tuple[str, tuple]:
    """The SQL condition for "the value at that time", with its parameters.

    as_of=None: the latest values (valid_to IS NULL). as_of=a run's finished_at: facts that existed by then
    and had not yet been replaced or removed, i.e. the data exactly as that run left it. Runs only write
    what changed, so this time window, not run_id, is what rebuilds an older state."""
    c = f"{alias}." if alias else ""
    if as_of is None:
        return f"{c}valid_to IS NULL", ()
    return f"{c}valid_from <= ? AND ({c}valid_to IS NULL OR {c}valid_to > ?)", (as_of, as_of)


def as_of_runs(conn, source: str) -> list[dict]:
    """Finished runs, newest first: the points in time the data can be viewed at (a crashed run has no end)."""
    return _rows(conn, """SELECT run_id, finished_at FROM runs WHERE source = ? AND finished_at IS NOT NULL
                          ORDER BY started_at DESC""", (source,))


def sources(conn) -> list[str]:
    """Source names that have runs in this database."""
    return [r["source"] for r in _rows(conn, "SELECT DISTINCT source FROM runs ORDER BY source")]


# ---------- runs ----------

def run_status(finished_at: str | None, stats: dict) -> str:
    """ok, ok with failures (an http_* skip or a network, parse or LLM error), or incomplete (never finished)."""
    if not finished_at:  # the run stopped (crash, Ctrl+C) before writing its end time
        return "incomplete"
    skips = stats.get("skips") or {}  # {reason: count} for this run
    failed = any(r in FAILURE_REASONS or r.startswith("http_") for r in skips) or stats.get("llm_failed")
    return "ok, with failures" if failed else "ok"


def _run_row(r: dict) -> dict:
    """One runs row turned into flat columns for the table. Metrics live in stats_json, written at run end;
    a run that never finished has none, so every value falls back to 0."""
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
    """Every run of a source, newest first, with flat metric columns and the full stats dict."""
    return [_run_row(r) for r in _rows(conn, "SELECT * FROM runs WHERE source = ? ORDER BY run_id DESC", (source,))]


def run(conn, run_id: str) -> dict | None:
    """One run by id, in the same shape as runs()."""
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
    # 1. Rows this run inserted. A row is "updated" if an older row points to it (superseded_by), else "new".
    #    The IN list is computed once, not per row, which keeps this fast on large tables.
    added = _rows(conn, """
        SELECT CASE WHEN f.id IN (SELECT superseded_by FROM facts WHERE superseded_by IS NOT NULL)
                    THEN 'updated' ELSE 'new' END AS change, f.*
        FROM facts f WHERE f.run_id = ? ORDER BY f.id LIMIT ?""", (run_id, limit))
    # 2. Rows this run closed without a replacement ("removed"). They only carry a close time, so match
    #    them to the run's time window.
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
    """Number of skips per reason, for a source and optionally one run."""
    # skips has no source column, so join runs to keep only this source's skips.
    sql = "SELECT s.reason, COUNT(*) AS n FROM skips s JOIN runs r ON r.run_id = s.run_id WHERE r.source = ?"
    params: list = [source]
    if run_id:
        sql += " AND s.run_id = ?"
        params.append(run_id)
    return _rows(conn, sql + " GROUP BY s.reason ORDER BY n DESC", params)


def skips(conn, source: str, reason=None, run_id=None, search=None, limit: int = 1000) -> list[dict]:
    """Skipped URLs with reason and detail, filtered by reason, run and URL text; newest first."""
    sql = ("SELECT s.run_id, s.reason, s.url, s.detail, s.at FROM skips s JOIN runs r ON r.run_id = s.run_id"
           " WHERE r.source = ?")
    params: list = [source]
    # Each filter adds one "AND ... = ?" clause and one parameter, only when the user set it.
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
    """The entity types present (project, news, document, page ...)."""
    return [r["entity_type"] for r in _rows(
        conn, "SELECT DISTINCT entity_type FROM facts WHERE source = ? ORDER BY 1", (source,))]


def entities(conn, source: str, entity_type=None, search=None, limit: int = 500, as_of=None) -> list[dict]:
    """One row per entity with a display name and fact count; search matches the key or any value."""
    # Facts are one row per field, so an entity is a GROUP BY of its rows. Its display name is the first of
    # name, title, llm_title ... that it has; MAX(CASE ...) picks that field's value out of the group.
    name = "COALESCE(" + ", ".join(
        f"MAX(CASE WHEN field = '{f}' THEN json_extract(value, '$') END)" for f in NAME_FIELDS) + ")"
    sql = (f"SELECT entity_type, entity_key, {name} AS name, MIN(source_url) AS source_url, COUNT(*) AS facts"
           " FROM facts WHERE source = ? AND " + _current(as_of)[0])
    params: list = [source, *_current(as_of)[1]]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    sql += " GROUP BY entity_type, entity_key"
    if search:
        # HAVING filters whole entities: keep it if its key, or any of its values (except long body text), matches.
        sql += (" HAVING entity_key LIKE ? ESCAPE '\\' OR SUM(CASE WHEN field != 'body_text'"
                " AND value LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) > 0")
        params += [_like(search), _like(search)]
    return _rows(conn, sql + " ORDER BY entity_type, entity_key LIMIT ?", params + [limit])


def entity_facts(conn, source: str, entity_type: str, entity_key: str, history: bool = False,
                 as_of=None) -> list[dict]:
    """All facts of one entity: current only, or with history (replaced values) too. With as_of, as of that time."""
    sql = "SELECT * FROM facts WHERE source = ? AND entity_type = ? AND entity_key = ?"
    params: list = [source, entity_type, entity_key]
    if not history:  # current values only; with history, closed rows (valid_to set) are included too
        cond, p = _current(as_of)
        sql += " AND " + cond
        params += p
    elif as_of:  # history up to that time: nothing written later
        sql += " AND valid_from <= ?"
        params.append(as_of)
    return _rows(conn, sql + " ORDER BY field, id", params)


def fact_fields(conn, source: str) -> list[str]:
    """Every field name present for the source."""
    return [r["field"] for r in _rows(conn, "SELECT DISTINCT field FROM facts WHERE source = ? ORDER BY 1", (source,))]


def facts(conn, source: str, entity_type=None, field=None, method=None, run_id=None, source_url=None,
          current_only=True, search=None, limit: int = 1000, as_of=None) -> list[dict]:
    """Fact rows filtered by type, field, method, run, source URL, current-only and text search.
    With as_of, "current" means current at that time, and nothing written later is shown."""
    sql = "SELECT * FROM facts WHERE source = ?"
    params: list = [source]
    # Column names come from this fixed list, never from the user; the values go in as parameters.
    for col, val in (("entity_type", entity_type), ("field", field), ("method", method), ("run_id", run_id),
                     ("source_url", source_url)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    if current_only:
        cond, p = _current(as_of)
        sql += " AND " + cond
        params += p
    elif as_of:
        sql += " AND valid_from <= ?"
        params.append(as_of)
    if search:
        sql += " AND (value LIKE ? ESCAPE '\\' OR entity_key LIKE ? ESCAPE '\\')"
        params += [_like(search), _like(search)]
    return _rows(conn, sql + " ORDER BY id DESC LIMIT ?", params + [limit])


def fact(conn, fact_id: int) -> dict | None:
    """One fact by id, or None."""
    rows = _rows(conn, "SELECT * FROM facts WHERE id = ?", (fact_id,))
    return rows[0] if rows else None


def fact_history(conn, fact_id: int) -> list[dict]:
    """Every version of this fact (same entity, field, source and method), oldest first."""
    f = fact(conn, fact_id)
    if f is None:
        return []
    # Every row ever recorded for the same field of the same entity from the same page and method.
    # Ordered by id, each row's superseded_by points to the next one.
    return _rows(conn, """SELECT * FROM facts WHERE source = ? AND entity_type = ? AND entity_key = ?
                          AND field = ? AND source_url = ? AND method = ? ORDER BY id""",
                 (f["source"], f["entity_type"], f["entity_key"], f["field"], f["source_url"], f["method"]))


# ---------- pages and files ----------

def hosts(conn, source: str) -> list[str]:
    """Every host we saw URLs on."""
    return [r["host"] for r in _rows(conn, "SELECT DISTINCT host FROM documents WHERE source = ? ORDER BY 1", (source,))]


def documents(conn, source: str, kind=None, host=None, saved=None, search=None, limit: int = 1000,
              as_of=None) -> list[dict]:
    """One row per URL with kind, host, status, whether a copy is saved, versions and size.
    With as_of, only URLs first seen by a run that had started by then (status and copy stay the latest)."""
    # saved = we have a copy (current_hash set); versions = how many distinct contents we saved for the URL;
    # bytes = size of the current version.
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
    if saved is not None:  # only a fixed "NOT NULL" or "NULL" is added here, never user text
        sql += " AND d.current_hash IS " + ("NOT NULL" if saved else "NULL")
    if search:
        sql += " AND d.url LIKE ? ESCAPE '\\'"
        params.append(_like(search))
    if as_of:
        sql += " AND d.first_seen_run IN (SELECT run_id FROM runs WHERE started_at <= ?)"
        params.append(as_of)
    return _rows(conn, sql + " ORDER BY d.kind, d.url LIMIT ?", params + [limit])


def document(conn, url: str) -> dict | None:
    """The documents row for one URL, or None."""
    rows = _rows(conn, "SELECT * FROM documents WHERE url = ?", (url,))
    return rows[0] if rows else None


def snapshots(conn, url: str) -> list[dict]:
    """Every saved version of a URL, newest first."""
    return _rows(conn, "SELECT * FROM snapshots WHERE url = ? ORDER BY id DESC", (url,))


def links_to(conn, url: str) -> list[str]:
    """Pages that link to this URL."""
    return [r["src"] for r in _rows(conn, "SELECT src FROM links WHERE dst = ? ORDER BY src", (url,))]


def links_from(conn, url: str) -> list[str]:
    """URLs that this page links to."""
    return [r["dst"] for r in _rows(conn, "SELECT dst FROM links WHERE src = ? ORDER BY dst", (url,))]


def url_skips(conn, url: str) -> list[dict]:
    """Every skip recorded for this exact URL."""
    return _rows(conn, "SELECT run_id, reason, detail, at FROM skips WHERE url = ? ORDER BY id DESC", (url,))


# ---------- provenance ----------

def pdf_page_text(conn, content_hash: str, page: int) -> str | None:
    """The saved text of one PDF page, or None."""
    rows = _rows(conn, "SELECT text FROM pdf_pages WHERE content_hash = ? AND page_number = ?", (content_hash, page))
    return rows[0]["text"] if rows else None


def raw_path(conn, content_hash: str, raw_dir: Path | None = None) -> Path | None:
    """The saved file. Paths are stored as the crawl saw them (relative to where it ran), so if that does not
    resolve, look for the same file name under this database's own raw/ folder."""
    rows = _rows(conn, "SELECT path FROM snapshots WHERE content_hash = ? LIMIT 1", (content_hash,))
    if not rows:
        return None
    path = Path(rows[0]["path"])
    # Try the stored path, then the same xx/<hash>.<ext> under this database's raw/ folder (data copied elsewhere).
    for candidate in (path, raw_dir / path.parent.name / path.name if raw_dir else None):
        if candidate is not None and candidate.exists():
            return candidate
    return None


def find_excerpt(text: str, excerpt: str) -> tuple[int, int] | None:
    """Where the quote is in the page text, ignoring case, spacing and punctuation between words."""
    # Split the quote into words, then allow any spacing or punctuation between them: PDF text often has
    # extra line breaks and double spaces that the model's quote does not.
    words = re.findall(r"[A-Za-z0-9$%.,]+", excerpt)
    if not words:
        return None
    pattern = r"\W*".join(re.escape(w.strip(".,")) for w in words if w.strip(".,"))
    match = re.search(pattern, text, re.IGNORECASE)
    return match.span() if match else None


# ---------- overview ----------

# Most reliable date first, same order as the export's best_date.
DATE_FIELDS = ("first_published_at", "llm_document_date", "pdf_created")


def overview(conn, source: str, as_of=None) -> dict:
    """Headline numbers: entities, facts, saved pages and files, last run's skips, total LLM cost of runs.
    With as_of, everything is counted as of that time: later runs and facts are left out."""
    def one(sql, *p):  # runs a query that returns one value (None when there is no row)
        row = conn.execute(sql, p).fetchone()
        return row[0] if row else None

    until = as_of or "9999"  # "9999" sorts after every real timestamp, so None means "up to now"
    # Each run logs the same skipped links again, so skips are counted for the latest finished run (by then).
    last = one("SELECT run_id FROM runs WHERE source = ? AND finished_at IS NOT NULL AND finished_at <= ?"
               " ORDER BY started_at DESC LIMIT 1", source, until)
    # The scraper's LLM cost is summed from every run's metrics. Ask AI cost is separate (ask_log.jsonl).
    cost = sum(json.loads(r["stats_json"] or "{}").get("llm_cost_usd", 0.0)
               for r in _rows(conn, "SELECT stats_json FROM runs WHERE source = ? AND started_at <= ?", (source, until)))
    cond, p = _current(as_of)
    # A page or file counts once we had saved a copy of it (by then).
    saved = ("SELECT COUNT(*) FROM documents d WHERE d.source = ? AND d.kind = ? AND EXISTS"
             " (SELECT 1 FROM snapshots s WHERE s.url = d.url AND s.fetched_at <= ?)")
    return {
        # An entity is a distinct (type, key) pair among current facts.
        "entities": one("SELECT COUNT(DISTINCT entity_type || ':' || entity_key) FROM facts"
                        f" WHERE source = ? AND {cond}", source, *p),
        "facts": one(f"SELECT COUNT(*) FROM facts WHERE source = ? AND {cond}", source, *p),
        "pages": one(saved, source, "page", until),
        "files": one(saved, source, "file", until),
        "last_run": last,
        "skips_last_run": one("SELECT COUNT(*) FROM skips WHERE run_id = ?", last) if last else 0,
        "runs": one("SELECT COUNT(*) FROM runs WHERE source = ? AND started_at <= ?", source, until),
        "llm_cost_usd": round(cost, 4),
    }


def facts_by_type_method(conn, source: str, as_of=None) -> list[dict]:
    """Current facts counted by entity type and how they were found."""
    cond, p = _current(as_of)
    return _rows(conn, f"""SELECT entity_type, method, COUNT(*) AS n FROM facts
                           WHERE source = ? AND {cond} GROUP BY 1, 2 ORDER BY 1, 2""", (source, *p))


def label_counts(conn, source: str, entity_type: str, field: str, as_of=None) -> list[dict]:
    """How many entities have each value of a text field, e.g. projects per phase."""
    # Values are stored as JSON; json_extract gives the plain text. Lists and objects are left out.
    cond, p = _current(as_of)
    return _rows(conn, f"""SELECT json_extract(value, '$') AS value, COUNT(*) AS n FROM facts
                           WHERE source = ? AND entity_type = ? AND field = ? AND {cond}
                             AND json_type(value) = 'text'
                           GROUP BY 1 ORDER BY n DESC, value""", (source, entity_type, field, *p))


def files_per_host(conn, source: str, as_of=None) -> list[dict]:
    """Saved files counted by host (with as_of: files we had a copy of by then)."""
    return _rows(conn, """SELECT host, COUNT(*) AS n FROM documents d
                          WHERE source = ? AND kind = 'file'
                            AND EXISTS (SELECT 1 FROM snapshots s WHERE s.url = d.url AND s.fetched_at <= ?)
                          GROUP BY host ORDER BY n DESC""", (source, as_of or "9999"))


def entities_per_year(conn, source: str, as_of=None) -> list[dict]:
    """Entities per year of their best date (publish date, else the date in the document, else PDF metadata)."""
    # Inner query: one row per entity with the year (first 4 characters) of its best date field.
    # Outer query: count entities per year and type, keeping only real years.
    best = "COALESCE(" + ", ".join(
        f"MAX(CASE WHEN field = '{f}' THEN substr(json_extract(value, '$'), 1, 4) END)" for f in DATE_FIELDS) + ")"
    cond, p = _current(as_of)
    return _rows(conn, f"""SELECT year, entity_type, COUNT(*) AS n FROM (
                               SELECT entity_type, {best} AS year FROM facts
                               WHERE source = ? AND {cond} GROUP BY entity_type, entity_key)
                           WHERE year GLOB '[12][0-9][0-9][0-9]' GROUP BY year, entity_type ORDER BY year, entity_type""",
                 (source, *p))


def locations(conn, source: str, as_of=None) -> list[dict]:
    """Every entity with a location fact, with its name, for a map."""
    # The location fact is {"lat": .., "lon": ..}; json_extract pulls each number out. The name is looked up
    # from the same entity's name or title fact.
    name = "COALESCE(" + ", ".join(
        f"(SELECT json_extract(n.value, '$') FROM facts n WHERE n.source = f.source AND n.entity_type = f.entity_type"
        f" AND n.entity_key = f.entity_key AND n.field = '{x}' AND {_current(as_of, 'n')[0]})" for x in NAME_FIELDS[:2]) + ")"
    return _rows(conn, f"""SELECT f.entity_type, f.entity_key, {name} AS name,
                                  json_extract(f.value, '$.lat') AS lat, json_extract(f.value, '$.lon') AS lon
                           FROM facts f WHERE f.source = ? AND f.field = 'location' AND {_current(as_of, 'f')[0]}
                             AND json_extract(f.value, '$.lat') IS NOT NULL ORDER BY f.entity_key""",
                 # parameters in the order they appear: the two name lookups, then source, then f's time window
                 (*_current(as_of, "n")[1] * 2, source, *_current(as_of, "f")[1]))


# ---------- data dictionary ----------

def field_stats(conn, source: str, as_of=None) -> list[dict]:
    """Every field per entity type and method: how many entities have it, share of all entities of that type,
    and one example value with its locator."""
    # cur = current facts; agg = per field: how many entities have it, and the first row as an example;
    # tot = how many entities of each type exist, so coverage = entities with the field / all of that type.
    cond, p = _current(as_of)
    rows = _rows(conn, f"""
        WITH cur AS (SELECT * FROM facts WHERE source = ? AND {cond}),
             agg AS (SELECT entity_type, field, method, COUNT(DISTINCT entity_key) AS entities, MIN(id) AS first_id
                     FROM cur GROUP BY entity_type, field, method),
             tot AS (SELECT entity_type, COUNT(DISTINCT entity_key) AS n FROM cur GROUP BY entity_type)
        SELECT a.entity_type, a.field, a.method, a.entities, ROUND(a.entities * 1.0 / t.n, 3) AS coverage,
               f.value AS example, f.locator
        FROM agg a JOIN tot t ON t.entity_type = a.entity_type JOIN facts f ON f.id = a.first_id
        ORDER BY a.entity_type, a.method DESC, a.field""", (source, *p))
    for r in rows:  # make the example readable and short
        value = json.loads(r["example"])
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        r["example"] = text if len(text) <= 120 else text[:117] + "..."
    return rows
