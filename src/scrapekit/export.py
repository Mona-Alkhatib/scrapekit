"""Writes a source's data as JSON Lines files that another service can load without SQLite.

    export/<source>/entities.jsonl   one line per project / news / document / page, current values only
    export/<source>/facts.jsonl      every fact ever recorded, including superseded ones (the history)
    export/<source>/documents.jsonl  every URL with status, hash, and where it was found
    export/<source>/skips.jsonl      every skipped or failed link, with the reason
    export/<source>/runs.jsonl       metrics for every run
    export/<source>/SCHEMA.md        what each file and field means

entities.jsonl is built by grouping the current facts (valid_to IS NULL) by entity, then adding
linked files, best_date and the within_last_5_years flag. Each field keeps its own source
(locator, excerpt, method, content_hash), so a downstream service can trace every value.
JSON Lines because any language can stream it line by line, with no database driver.
"""

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .db import Store

SCHEMA_DOC = """# Export format

All files are JSON Lines (one JSON object per line), UTF-8.

## entities.jsonl
One line per entity with its current values.

| Field | Meaning |
|---|---|
| `source` | Source name, e.g. `createto` |
| `entity_type` | `project`, `news`, `document` or `page` |
| `entity_key` | Stable id inside the source: a slug, or the file URL for documents |
| `source_url` | Page or file the values were read from |
| `fields` | Object: field name -> `{value, locator, excerpt, method, valid_from, content_hash}` |
| `linked_documents` | File URLs linked from this entity's page |
| `linked_from` | For documents: pages that link to this file |
| `best_date` | Most reliable date available (YYYY-MM-DD): publish date, else the date stated in the document, else PDF metadata |
| `best_date_field` | Which field `best_date` came from |
| `within_last_5_years` | `true` / `false` relative to the export day, `null` if no date is known |

`locator` says where the value was found: a JSON path in the page, `page N` in a PDF, or `pages N, M` for LLM facts.
`method` is `parse` (rules, exact) or `llm` (model output whose excerpt was verified on the cited page).

## facts.jsonl
Every value ever recorded. A correction or change does not overwrite: the old row gets `valid_to` and
`superseded_by` (the id of the new row). Rows with `valid_to = null` are current.

## documents.jsonl
Every URL seen: `kind` (page or file), `host`, `last_status`, `current_hash`, `discovered_from`, first and last run seen.

## skips.jsonl
Links not fetched or not usable: `reason` (off_domain, robots_disallowed, http_404, too_large, parse_error,
no_text_layer, llm_error, ...), `detail`, `run_id`.

## runs.jsonl
One line per run with all metrics: duration, requests, status codes, files, records, LLM calls and cost.
"""


# Most reliable date first: what the publisher states, then what the document says, then file metadata.
DATE_FIELDS = ("first_published_at", "llm_document_date", "pdf_created", "cms_updated_at", "pdf_modified")
WINDOW_YEARS = 5


def best_date(fields: dict) -> tuple[str | None, str | None]:
    for name in DATE_FIELDS:
        value = (fields.get(name) or {}).get("value")
        if isinstance(value, str) and len(value) >= 10 and value[:4].isdigit():
            return value[:10], name
    return None, None


def within_window(day: str | None, today: date) -> bool | None:
    if day is None:
        return None
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return None
    return d >= today.replace(year=today.year - WINDOW_YEARS)


def _write(path: Path, rows) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def export_source(store: Store, source: str, out_dir: Path) -> dict[str, int]:
    out = out_dir / source
    out.mkdir(parents=True, exist_ok=True)
    conn = store.conn

    links_out, links_in = defaultdict(list), defaultdict(list)
    for r in conn.execute(
        "SELECT l.src, l.dst FROM links l JOIN documents d ON d.url = l.dst WHERE d.kind = 'file' AND d.source = ?",
        (source,),
    ):
        links_out[r["src"]].append(r["dst"])
        links_in[r["dst"]].append(r["src"])

    # Pivot current facts into one object per entity: {"fields": {field: {value, locator, ...}}}.
    entities: dict[tuple, dict] = {}
    for r in conn.execute(
        "SELECT * FROM facts WHERE source = ? AND valid_to IS NULL ORDER BY entity_type, entity_key, field", (source,)
    ):
        key = (r["entity_type"], r["entity_key"])
        ent = entities.setdefault(key, {
            "source": source, "entity_type": r["entity_type"], "entity_key": r["entity_key"],
            "source_url": r["source_url"], "fields": {},
        })
        ent["fields"][r["field"]] = {
            "value": json.loads(r["value"]), "locator": r["locator"], "excerpt": r["excerpt"],
            "method": r["method"], "valid_from": r["valid_from"], "content_hash": r["content_hash"],
        }
    today = datetime.now(timezone.utc).date()
    for ent in entities.values():
        ent["best_date"], ent["best_date_field"] = best_date(ent["fields"])
        ent[f"within_last_{WINDOW_YEARS}_years"] = within_window(ent["best_date"], today)
        if ent["entity_type"] == "document":
            ent["linked_from"] = sorted(links_in.get(ent["source_url"], []))
        else:
            ent["linked_documents"] = sorted(links_out.get(ent["source_url"], []))

    def facts():
        for r in conn.execute("SELECT * FROM facts WHERE source = ? ORDER BY id", (source,)):
            row = dict(r)
            row["value"] = json.loads(row["value"])
            yield row

    counts = {
        "entities": _write(out / "entities.jsonl", entities.values()),
        "facts": _write(out / "facts.jsonl", facts()),
        "documents": _write(out / "documents.jsonl",
                            (dict(r) for r in conn.execute("SELECT * FROM documents WHERE source = ? ORDER BY url", (source,)))),
        "skips": _write(out / "skips.jsonl", (dict(r) for r in conn.execute(
            "SELECT s.* FROM skips s JOIN runs r ON r.run_id = s.run_id WHERE r.source = ? ORDER BY s.id", (source,)))),
        "runs": _write(out / "runs.jsonl", (
            {**{k: r[k] for k in ("run_id", "source", "mode", "started_at", "finished_at")},
             "stats": json.loads(r["stats_json"] or "{}")}
            for r in conn.execute("SELECT * FROM runs WHERE source = ? ORDER BY started_at", (source,)))),
    }
    (out / "SCHEMA.md").write_text(SCHEMA_DOC)
    return counts
