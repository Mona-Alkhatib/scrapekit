# Export format

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
