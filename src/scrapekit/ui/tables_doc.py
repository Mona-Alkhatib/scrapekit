"""Plain-English guide to the database tables, for engineers who want to query them directly.

Shown on the Data dictionary page and given to the Ask AI model as part of its schema.
"""

TABLES = [
    ("runs", "One row per run.", "run_id, source, mode, started_at, finished_at (empty = did not finish), "
     "stats_json (all metrics: requests, status codes, files, facts, LLM calls and cost)"),
    ("documents", "One row per URL we know about: a page on the site or a linked file on any host.",
     "url, kind (page / file), host, last_status (200, 304, empty = never saved), etag, current_hash, "
     "discovered_from, first_seen_run, last_seen_run"),
    ("snapshots", "One row per distinct saved version of a URL. A second row for a URL means its content changed.",
     "url, content_hash (SHA-256), fetched_at, run_id, bytes, path (the raw file on disk)"),
    ("facts", "One row per field of one entity, as observed. Never overwritten: a change adds a row and closes the old one.",
     "entity_type, entity_key, field, value (JSON), source_url, content_hash, locator, excerpt, method (parse / llm), "
     "run_id, valid_from, valid_to (empty = current), superseded_by (id of the newer row)"),
    ("skips", "Every link not fetched, or fetched and not usable, with the reason.",
     "run_id, url, reason (off_domain, robots_disallowed, http_404, too_large, no_text_layer ...), detail, at"),
    ("links", "Which page links to which URL.", "src (the page), dst (the linked URL)"),
    ("extractions", "Which (url, content version, extractor version) was already processed, so re-runs skip it.",
     "url, content_hash, extractor, status (ok / failed / unsupported), detail, run_id, at"),
    ("pdf_pages", "PDF text, one row per page. Used to cite and check LLM quotes.", "content_hash, page_number (1-based), text"),
    ("llm_cache", "Paid LLM answers, so the same content is never paid for twice.",
     "content_hash, model_key (model and prompt version), response (JSON), input_tokens, output_tokens, cost_usd"),
]

TIPS = [
    "Current values: `WHERE valid_to IS NULL`.",
    "Text out of a JSON value: `json_extract(value, '$')`; a key of an object: `json_extract(value, '$.lat')`.",
    "One row per entity: group facts by entity_type, entity_key and pivot with `MAX(CASE WHEN field = ... )`.",
    "Facts from one page or file: `WHERE source_url = ?`; its raw bytes: join snapshots on content_hash and open path.",
]
