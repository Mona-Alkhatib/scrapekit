"""Stage 2 of a run: turn every saved page and file of a source into facts.

Pass 1 (free): rule-based extractors on every page and PDF.
Pass 2 (paid, optional): an LLM reads PDFs that have text and returns cited facts.

Reads only local snapshots, never the website. A document whose content hash was already
processed by the same extractor version is skipped, which is what keeps repeat runs cheap.
"""

import asyncio
import json
import logging
from pathlib import Path

from .db import Store
from .extract import ExtractionError, Fact, get_page_extractor, pdf
from .llm import (DEFAULT_RUN_BUDGET_USD, VERIFY_VERSION, OpenRouterExtractor, estimate_cost, response_schema,
                  system_prompt, verify_facts)
from .metrics import RunStats
from .sources import Source

log = logging.getLogger(__name__)

# What the LLM fields mean, shown in the UI's data dictionary. All come from PDFs only.
FIELD_DOCS = {
    "document.llm_doc_type": "Kind of document chosen by the model from a fixed list (board_minutes, staff_report, notice ...).",
    "document.llm_title": "Title as the model read it from the text.",
    "document.llm_document_date": "Date stated in the document text (YYYY-MM-DD), usually the meeting or report date.",
    "document.llm_organization": "The body that issued the document, e.g. a board or committee.",
    "document.llm_summary": "One or two plain sentences on what the document is about.",
    "document.llm_key_facts": "Up to 15 facts, each {kind, value, page, excerpt}. Kept only if the excerpt is really on that page.",
}

# Bump a version when its extractor's output changes, so old content is re-extracted once.
EXTRACTOR_VERSIONS = {"generic": 2, "storyblok": 3, "pdf": 1}  # 2 / 3: query string kept in page ids


def extractor_id(name: str) -> str:
    return f"{name}@{EXTRACTOR_VERSIONS[name]}"


def run_rules(source: Source, store: Store, stats: RunStats) -> None:
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
                facts = page_extract(url, data, source.extractor_options)
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


class BudgetReached(Exception):
    pass


def count_entities(source: Source, store: Store, stats: RunStats) -> None:
    stats.entities = store.conn.execute(
        "SELECT COUNT(DISTINCT entity_type || ':' || entity_key) FROM facts WHERE source = ? AND valid_to IS NULL",
        (source.name,),
    ).fetchone()[0]


async def run_extract(
    source: Source, store: Store, stats: RunStats, llm: OpenRouterExtractor | None = None,
    llm_limit: int | None = None, llm_concurrency: int = 8,
    llm_budget: float = DEFAULT_RUN_BUDGET_USD, llm_dry_run: bool = False,
) -> None:
    run_rules(source, store, stats)
    if llm is not None:
        await run_llm(source, store, stats, llm, llm_limit, llm_concurrency, llm_budget, llm_dry_run)
    count_entities(source, store, stats)


def _page_ranges(pages: list[int]) -> str:
    ranges, start = [], None
    for i, p in enumerate(pages):
        if start is None:
            start = p
        if i + 1 == len(pages) or pages[i + 1] != p + 1:
            ranges.append(f"{start}-{p}" if start != p else str(p))
            start = None
    return ", ".join(ranges)


def llm_facts(url: str, data: dict, kept: list[dict], page_count: int, pages_sent: list[int] | None) -> list[Fact]:
    sent = pages_sent or list(range(1, page_count + 1))
    where = f"llm over pages {_page_ranges(sent)} of {page_count}"
    facts = [
        Fact("document", url, f"llm_{name}", data.get(name), where, method="llm")
        for name in ("doc_type", "title", "document_date", "organization", "summary")
        if data.get(name) not in (None, "")
    ]
    if kept:
        pages = sorted({f["page"] for f in kept})
        facts.append(Fact("document", url, "llm_key_facts", kept, "pages " + ", ".join(map(str, pages)), method="llm"))
    return facts


async def run_llm(
    source: Source, store: Store, stats: RunStats, llm: OpenRouterExtractor, limit: int | None,
    concurrency: int, budget: float, dry_run: bool,
) -> None:
    ext_id = f"llm:{llm.cache_key_suffix}|check{VERIFY_VERSION}"  # re-checking reuses the cache, no cost
    stats.llm_model = llm.model
    todo = []
    for doc in store.documents_to_extract(source.name):
        if doc["kind"] != "file" or not doc["url"].lower().split("?")[0].endswith(".pdf"):
            continue
        if store.already_extracted(doc["url"], doc["current_hash"], ext_id):
            stats.llm_cached += 1
            continue
        pages = store.pdf_pages(doc["current_hash"])
        if not pdf.PdfText(pages, {}).has_text_layer:
            continue  # scanned or unreadable, already logged by pass 1
        todo.append((doc["url"], doc["current_hash"], pages))
    # Cache: answers already stored for this content + model + prompt are re-used for free.
    # The limit only counts paid calls.
    cached = [t for t in todo if store.llm_cache_get(t[1], llm.cache_key_suffix)]
    uncached = [t for t in todo if not store.llm_cache_get(t[1], llm.cache_key_suffix)]
    if limit is not None:
        uncached = uncached[:limit]
    todo = cached + uncached
    tokens, cost = estimate_cost(llm.model, [t[2] for t in uncached])
    cost_text = f"${cost:.2f}" if cost is not None else "unknown (model price not listed)"
    print(f"LLM plan: {len(uncached)} PDFs to send (paid), {len(cached)} from cache (free), "
          f"about {tokens:,} input tokens, estimated {cost_text}; "
          f"run budget ${budget:.2f}")
    if dry_run:
        return  # --llm-dry-run: print the estimate above, make no calls

    prompt = system_prompt(source.publisher, source.llm_focus)
    schema = response_schema(source.publisher_body_example)
    sem = asyncio.Semaphore(concurrency)

    async def one(url: str, content_hash: str, pages: list[str]):
        cached = store.llm_cache_get(content_hash, llm.cache_key_suffix)
        if cached:
            return url, content_hash, pages, json.loads(cached["response"]), None, None
        async with sem:
            # Budget cap: stop paying once this run's real spend reaches --llm-budget (default $0.50).
            if stats.llm_cost_usd >= budget:
                return url, content_hash, pages, None, None, BudgetReached(f"run budget ${budget:.2f} reached")
            try:
                return url, content_hash, pages, None, await llm.extract(url, pages, prompt, schema), None
            except Exception as exc:  # API errors, bad JSON: logged per document, never fatal
                return url, content_hash, pages, None, None, exc

    for task in asyncio.as_completed([one(*t) for t in todo]):
        url, content_hash, pages, cached_data, result, error = await task
        if isinstance(error, BudgetReached):
            # Not a failure: nothing is recorded, so the next run picks this document up.
            stats.skips["llm_budget_reached"] += 1
            continue
        if error is not None:
            log.warning("llm failed for %s: %s", url, error)
            stats.llm_failed += 1
            stats.skips["llm_error"] += 1
            store.add_skip(url, "llm_error", f"{type(error).__name__}: {error}"[:500])
            store.record_extraction(url, content_hash, ext_id, "failed", str(error)[:500])
            store.commit()
            continue
        if result is not None:
            stats.llm_calls += 1
            stats.llm_input_tokens += result.input_tokens
            stats.llm_output_tokens += result.output_tokens
            stats.llm_cost_usd += result.cost_usd
            data = dict(result.data, _truncated=result.truncated, _pages_sent=result.pages_sent)
            store.llm_cache_put(content_hash, llm.cache_key_suffix, json.dumps(data),
                                result.input_tokens, result.output_tokens, result.cost_usd)
        else:
            stats.llm_cached += 1
            data = cached_data

        # Hallucination guard: keep a fact only if its quote is really on the cited page.
        kept, dropped = verify_facts(data.get("facts") or [], pages)
        stats.llm_facts_kept += len(kept)
        stats.llm_facts_dropped += len(dropped)
        facts = llm_facts(url, data, kept, len(pages), data.get("_pages_sent"))
        counts = store.apply_facts(source.name, url, content_hash, facts, method="llm")
        stats.records_new += counts["new"]
        stats.records_updated += counts["updated"]
        stats.records_unchanged += counts["unchanged"]
        stats.records_removed += counts["removed"]
        store.record_extraction(url, content_hash, ext_id, "ok", f"kept {len(kept)}, dropped {len(dropped)}")
        store.commit()
