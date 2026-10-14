"""Stage 2 of a run: turn every saved page and file of a source into facts.

Reads only local snapshots, never the network. A document whose content hash was already
processed by the same extractor version is skipped, which is what keeps repeat runs cheap.
"""

import logging
from pathlib import Path

from .db import Store
from .extract import ExtractionError, get_page_extractor, pdf
from .metrics import RunStats
from .sources import Source

log = logging.getLogger(__name__)

# Bump a version when its extractor's output changes, so old content is re-extracted once.
EXTRACTOR_VERSIONS = {"generic": 1, "storyblok": 2, "pdf": 1}


def extractor_id(name: str) -> str:
    return f"{name}@{EXTRACTOR_VERSIONS[name]}"


def run_extract(source: Source, store: Store, stats: RunStats) -> None:
    page_extract = get_page_extractor(source.page_extractor)

    for doc in store.documents_to_extract(source.name):
        url, content_hash = doc["url"], doc["current_hash"]
        is_pdf = doc["kind"] == "file" and ("pdf" in (doc["content_type"] or "") or url.lower().endswith(".pdf"))
        if doc["kind"] == "page":
            name = source.page_extractor
        elif is_pdf:
            name = "pdf"
        else:
            continue  # Word / Excel files are saved but not parsed yet

        ext_id = extractor_id(name)
        if store.already_extracted(url, content_hash, ext_id):
            stats.docs_extract_cached += 1
            continue

        data = Path(doc["path"]).read_bytes()
        try:
            if is_pdf:
                text = pdf.read_pdf(data)
                store.save_pdf_pages(content_hash, text.pages)
                facts = pdf.extract(url, text)
                if not text.has_text_layer:
                    store.add_skip(url, "no_text_layer", "scanned PDF, OCR not supported")
                    stats.skips["no_text_layer"] += 1
            else:
                facts = page_extract(url, data)
        except ExtractionError as exc:
            log.warning("extract failed for %s: %s", url, exc)
            store.record_extraction(url, content_hash, ext_id, "failed", str(exc))
            store.add_skip(url, "parse_error", str(exc))
            stats.skips["parse_error"] += 1
            stats.docs_extract_failed += 1
            store.commit()
            continue

        counts = store.apply_facts(source.name, url, content_hash, facts)
        stats.records_new += counts["new"]
        stats.records_updated += counts["updated"]
        stats.records_unchanged += counts["unchanged"]
        stats.records_removed += counts["removed"]
        store.record_extraction(url, content_hash, ext_id, "ok", f"{len(facts)} facts")
        stats.docs_extracted += 1
        store.commit()

    stats.entities = store.conn.execute(
        "SELECT COUNT(DISTINCT entity_type || ':' || entity_key) FROM facts WHERE source = ? AND valid_to IS NULL",
        (source.name,),
    ).fetchone()[0]
