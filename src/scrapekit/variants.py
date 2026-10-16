"""Builds replay fixtures from a real crawl of any source and runs the four required scenarios:

    1. first run          (base copies, empty database)
    2. unchanged repeat   (same copies again)
    3. changed source     (one extracted value edited in a page, a PDF amended with an extra page)
    4. failed file        (one PDF corrupted, one PDF gone with 404)

Everything runs from local copies, so it is repeatable and does not touch the website.
Nothing here is specific to one site: URLs come from the Source, and pages are picked by
what was extracted from them.
"""

import json
import re
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

from .config import Settings
from .links import normalize
from .metrics import RunStats
from .replay import ReplayTransport
from .sources import Source

ROBOTS = "User-agent: *\nAllow: /\n"
MAX_PDF_BYTES = 400_000  # small files keep the demo fast and cheap


def _name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("://", 1)[1])[-120:]


def _origin(source: Source) -> str:
    parts = urlparse(source.start_urls[0])
    return f"{parts.scheme}://{parts.netloc}"


def pick_slice(db: sqlite3.Connection, source: Source, per_type: int = 3) -> tuple[list[str], list[str]]:
    """Pages that produced structured entities (not plain pages) and link to small PDFs with text."""
    rows = db.execute(
        """
        SELECT ent.entity_type, l.src, MIN(l.dst) FROM links l
        JOIN documents p ON p.url = l.src AND p.source = ? AND p.kind = 'page'
        JOIN (SELECT DISTINCT source_url, entity_type FROM facts
              WHERE valid_to IS NULL AND method = 'parse' AND entity_type NOT IN ('page', 'document')) ent
          ON ent.source_url = l.src
        JOIN documents f ON f.url = l.dst AND f.kind = 'file' AND f.last_status = 200
        JOIN snapshots s ON s.url = f.url AND s.content_hash = f.current_hash
        WHERE lower(l.dst) LIKE '%.pdf' AND s.bytes < ?
          AND EXISTS (SELECT 1 FROM pdf_pages t WHERE t.content_hash = f.current_hash AND length(t.text) > 200)
        GROUP BY ent.entity_type, l.src ORDER BY ent.entity_type, l.src
        """,
        (source.name, MAX_PDF_BYTES),
    ).fetchall()
    taken: dict[str, int] = {}
    chosen = []
    for entity_type, page, pdf in rows:
        if taken.get(entity_type, 0) < per_type:
            taken[entity_type] = taken.get(entity_type, 0) + 1
            chosen.append((page, pdf))
    files = sorted({pdf for _, pdf in chosen})
    if len(files) < 3:
        raise SystemExit(f"Need at least 3 small PDFs linked from structured pages of '{source.name}'; found {len(files)}.")
    start = normalize(source.start_urls[0], source.start_urls[0])
    pages = [start] + sorted({page for page, _ in chosen} - {start})
    return pages, files


def _latest_copy(db: sqlite3.Connection, url: str) -> tuple[Path, str]:
    row = db.execute(
        "SELECT s.path, d.content_type FROM documents d JOIN snapshots s ON s.url = d.url AND s.content_hash = d.current_hash "
        "WHERE d.url = ?", (url,)).fetchone()
    return Path(row[0]), row[1] or "application/octet-stream"


def pick_value_to_change(db: sqlite3.Connection, pages: list[str], html_of) -> tuple[str, str, str]:
    """A short text value extracted by rules from one of the pages, that appears verbatim in its HTML."""
    for page in pages[1:]:
        html = html_of(page)
        for field, value in db.execute(
            "SELECT field, value FROM facts WHERE source_url = ? AND valid_to IS NULL AND method = 'parse' "
            "AND field NOT IN ('url_slug', 'first_published_at', 'cms_updated_at', 'body_text', 'text') ORDER BY field",
            (page,),
        ):
            text = json.loads(value)
            if isinstance(text, str) and 3 <= len(text) <= 60 and json.dumps(text).encode() in html:
                return page, field, text
    raise SystemExit("Could not find a short extracted value to edit in the chosen pages.")


def change_value(html: bytes, old: str) -> tuple[bytes, str]:
    """Edits one value inside a saved page's embedded JSON, adding ' (amended)'."""
    new = f"{old} (amended)"
    return html.replace(json.dumps(old).encode(), json.dumps(new).encode()), new


def amend_pdf(data: bytes) -> bytes:
    """Adds an amendment page to a PDF, so its content (and hash) changes."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc.new_page()
    page.insert_text((72, 72), "AMENDMENT: this document was revised by the publisher.", fontsize=12)
    page.insert_text((72, 96), "The meeting date has been moved to December 15, 2026.", fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


def build_fixtures(db_path: Path, out: Path, source: Source) -> dict:
    """Builds three fixture folders (base, changed, failed) from a real crawl and returns the plan."""
    db = sqlite3.connect(db_path)
    pages, files = pick_slice(db, source)
    if out.exists():
        shutil.rmtree(out)
    (out / "files").mkdir(parents=True)

    def put(name: str, data: bytes) -> str:
        """Saves one fixture file and returns its path relative to a manifest."""
        (out / "files" / name).write_bytes(data)
        return f"../files/{name}"

    origin = _origin(source)
    sitemap_url = source.sitemap_urls[0] if source.sitemap_urls else f"{origin}/sitemap.xml"
    base = {f"{origin}/robots.txt": {"file": put("robots.txt", ROBOTS.encode()), "content_type": "text/plain"}}
    sitemap = "".join(f"<url><loc>{u}</loc></url>" for u in pages)
    base[sitemap_url] = {"file": put("sitemap.xml", (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + sitemap + "</urlset>"
    ).encode()), "content_type": "application/xml"}
    for url in pages + files:
        path, content_type = _latest_copy(db, url)
        base[url] = {"file": put(_name(url), path.read_bytes()), "content_type": content_type}

    page, field, old_value = pick_value_to_change(db, pages, lambda u: (out / "files" / _name(u)).read_bytes())
    changed_pdf, corrupt_pdf, gone_pdf = files[0], files[1], files[2]

    changed = dict(base)
    html, new_value = change_value((out / "files" / _name(page)).read_bytes(), old_value)
    changed[page] = {"file": put("changed_" + _name(page), html), "content_type": base[page]["content_type"]}
    changed[changed_pdf] = {"file": put("changed_" + _name(changed_pdf), amend_pdf((out / "files" / _name(changed_pdf)).read_bytes())),
                            "content_type": "application/pdf"}

    failed = dict(changed)
    failed[corrupt_pdf] = {"file": put("corrupt_" + _name(corrupt_pdf), b"%PDF-1.7\n%broken upload, truncated\n"),
                           "content_type": "application/pdf"}
    failed[gone_pdf] = {"status": 404}

    for name, manifest in (("base", base), ("changed", changed), ("failed", failed)):
        (out / name).mkdir()
        (out / name / "manifest.json").write_text(json.dumps(manifest, indent=2))

    plan = {
        "source": source.name, "pages": pages, "files": files, "sitemap_url": sitemap_url,
        "changed_page": page, "changed_field": field, "value_change": [old_value, new_value],
        "amended_pdf": changed_pdf, "corrupt_pdf": corrupt_pdf, "gone_pdf": gone_pdf,
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=2))
    return plan


async def run_variants(source: Source, fixtures: Path, work: Path, llm, llm_budget: float,
                       plan: dict) -> list[tuple[str, RunStats]]:
    """Runs the four scenarios in order into a fresh database, replaying the fixtures."""
    from .cli import run_pipeline  # avoid an import cycle

    if work.exists():
        shutil.rmtree(work)
    # Re-check every file, including ones the source marks immutable, so that file changes
    # and failures are visible. Normal repeat runs skip immutable files for speed.
    settings = replace(Settings(), data_dir=work / "data", runs_dir=work / "runs", seconds_between_requests=0,
                       revalidate_all=True)
    results = []
    for label, folder in (("1. first run", "base"), ("2. unchanged repeat", "base"),
                          ("3. changed source", "changed"), ("4. failed file", "failed")):
        transport = ReplayTransport(fixtures / folder)
        sitemap_url = plan.get("sitemap_url") or f"{_origin(source)}/sitemap.xml"
        scoped = replace(source, url_allowlist=frozenset(transport.urls()), sitemap_urls=(sitemap_url,))
        stats = await run_pipeline(settings, scoped, llm=llm, llm_budget=llm_budget, transport=transport)
        results.append((label, stats))
    return results


def describe_change(plan: dict) -> str:
    """One line saying which value was edited for run 3."""
    if "changed_page" in plan:
        old, new = plan["value_change"]
        return f"- Page `{plan['changed_page']}`: field `{plan['changed_field']}` edited from `{old}` to `{new}` in the page data."
    # fixtures built by an earlier version of this tool
    return (f"- Page `{plan['changed_project']}`: phase edited from `{plan['phase_change'][0]}` "
            f"to `{plan['phase_change'][1]}` in the page data.")


def report(results: list[tuple[str, RunStats]], plan: dict, db_path: Path, source: Source) -> str:
    """Writes REPORT.md: metrics per scenario, the fact history from run 3, the skips from run 4."""
    rows = [
        ("Duration (s)", lambda s: s.duration_seconds),
        ("HTTP requests", lambda s: s.requests),
        ("Responses 200 / 304 / 404", lambda s: f"{s.status_counts.get(200, 0)} / {s.status_counts.get(304, 0)} / {s.status_counts.get(404, 0)}"),
        ("Pages fetched / unchanged / changed", lambda s: f"{s.pages_fetched} / {s.pages_unchanged} / {s.pages_changed}"),
        ("Files fetched / unchanged / changed", lambda s: f"{s.files_fetched} / {s.files_unchanged} / {s.files_changed}"),
        ("Documents extracted / skipped (unchanged) / failed", lambda s: f"{s.docs_extracted} / {s.docs_extract_cached} / {s.docs_extract_failed}"),
        ("Useful records: facts new / updated / unchanged / removed", lambda s: f"{s.records_new} / {s.records_updated} / {s.records_unchanged} / {s.records_removed}"),
        ("Entities with current facts", lambda s: s.entities),
        ("Paid LLM calls / cache hits / failed", lambda s: f"{s.llm_calls} / {s.llm_cached} / {s.llm_failed}"),
        ("LLM facts kept / dropped by excerpt check", lambda s: f"{s.llm_facts_kept} / {s.llm_facts_dropped}"),
        ("LLM cost (USD, reported by OpenRouter)", lambda s: f"${s.llm_cost_usd:.4f}"),
        ("Skips (excluding not_in_allowlist)", lambda s: {k: v for k, v in s.skips.items() if k != "not_in_allowlist"} or "none"),
    ]
    head = "| Metric | " + " | ".join(label for label, _ in results) + " |"
    lines = [head, "|---" * (len(results) + 1) + "|"]
    for name, get in rows:
        lines.append(f"| {name} | " + " | ".join(str(get(s)) for _, s in results) + " |")

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    changed_run, failed_run = results[2][1].run_id, results[3][1].run_id
    history = db.execute(
        """
        SELECT old.entity_key, old.field, old.value AS old_value, new.value AS new_value, old.locator
        FROM facts old JOIN facts new ON new.id = old.superseded_by
        WHERE new.run_id = ? AND old.field NOT IN ('body_text', 'text', 'first_page_excerpt', 'llm_key_facts', 'llm_summary')
        ORDER BY old.entity_key, old.field
        """, (changed_run,)).fetchall()
    skips = db.execute(
        "SELECT url, reason, detail FROM skips WHERE run_id = ? AND reason != 'not_in_allowlist' ORDER BY reason", (failed_run,)
    ).fetchall()
    amended = db.execute(
        "SELECT value FROM facts WHERE source_url = ? AND field = 'llm_key_facts' AND valid_to IS NULL", (plan["amended_pdf"],)
    ).fetchone()

    out = ["# Variant runs: unchanged, changed source, failed file", "",
           f"Generated by `uv run scrapekit variants {source.name}`. All runs replay saved local copies of real "
           f"`{source.name}` pages and PDFs "
           f"({len(plan['pages'])} pages, {len(plan['files'])} PDFs), so the results are repeatable and the website is not touched. "
           "Run 1 starts from an empty database. These runs use `--revalidate-all` so every file is re-checked; "
           "normal repeat runs skip files whose URL marks them as immutable (see docs/DESIGN.md).", "", "## Results", ""] + lines
    out += ["", "## What changed in run 3", "",
            describe_change(plan),
            f"- PDF `{plan['amended_pdf'].rsplit('/', 1)[-1]}`: an amendment page was added (new content hash).", "",
            "Fact history recorded in run 3 (old value kept, linked to the new one):", "",
            "| Entity | Field | Old value | New value |", "|---|---|---|---|"]
    out += [f"| {r['entity_key'][-60:]} | {r['field']} | {r['old_value'][:60]} | {r['new_value'][:60]} |" for r in history]
    if amended:
        found = [f for f in json.loads(amended["value"])
                 if "december 15" in f["excerpt"].lower() or "revised by the publisher" in f["excerpt"].lower()]
        if found:
            out += ["", "The LLM picked up the amendment, with its page cited:", ""]
            out += [f"- `{f['kind']}`: {f['value']} (page {f['page']}: \"{f['excerpt']}\")" for f in found]
    out += ["", "## What failed in run 4", "",
            f"- `{plan['corrupt_pdf'].rsplit('/', 1)[-1]}` replaced by a truncated, corrupt file.",
            f"- `{plan['gone_pdf'].rsplit('/', 1)[-1]}` now returns 404.", "",
            "Logged in the skips table (the run continued):", "", "| URL | Reason | Detail |", "|---|---|---|"]
    out += [f"| {r['url'].rsplit('/', 1)[-1]} | {r['reason']} | {(r['detail'] or '')[:80]} |" for r in skips]
    out += ["", "Facts from the last good version of these files are kept (not deleted), because one failed fetch may be "
            "temporary. The failure is visible in `skips` and in `documents.last_status`."]
    return "\n".join(out) + "\n"
