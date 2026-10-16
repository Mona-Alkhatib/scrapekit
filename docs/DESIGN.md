# scrapekit design

This document explains how scrapekit works, in plain terms. It is updated after every build round.

scrapekit is a general tool for turning public websites and their linked files into sourced, versioned data. The first source is CreateTO (createto.ca). Adding another website means adding one small config file, not changing the crawler (see section 5).

## 1. The goal in one paragraph

Visit every public page on createto.ca, download every file those pages link to (even when the file lives on another website), keep a copy of everything, and turn it into clean records such as "Project X is at 300 Commissioners Street, phase: leasing". Every record says where it came from. When we run it again later, it should only do work for things that changed, so a repeat run is fast and costs almost nothing.

## 2. What we learned about the site before building

| Finding | What it means for the design |
|---|---|
| `robots.txt` allows everything and lists a sitemap | We start from the sitemap (232 URLs) plus the homepage, then follow links to catch anything the sitemap misses. |
| Built with Next.js on Vercel, pages are rendered on the server | Plain HTTP is enough. No browser automation (Playwright) needed. |
| The server sends `ETag` headers | On a repeat run we ask "has this changed since ETag X?" and the server can answer `304 Not Modified` with no body. |
| Every sitemap `lastmod` is the same build timestamp | We cannot trust `lastmod` to detect changes. We use content hashes instead. |
| Project pages embed structured JSON (`address`, `phase`, `ward`) | Many useful fields can be read directly, for free, with no AI. |
| Most PDFs live on `a-us.storyblok.com`, others on `toronto.ca` | Files on other hosts are allowed. We fetch the file, but we do not crawl those other sites. |
| `/system/debug` is listed in the sitemap | Some public URLs are not useful. We skip them on purpose and log why. |

## 3. The stack, and why

| Tool | What it does here | Why we chose it | Alternatives we considered |
|---|---|---|---|
| **Python 3.11** | Everything | Best ecosystem for scraping, PDFs and LLM clients. Easy for any data engineer to read and extend. | Go or Rust would be faster, but speed is not the limit here (politeness is: 1 request per second per host). |
| **uv** | Installs dependencies, runs the CLI | One command setup (`uv sync`), a lock file so every machine gets the same versions, and very fast. | pip + venv (slower, no lock file by default), Poetry (heavier). |
| **httpx** (async) | All HTTP requests | Async, so files on other hosts download in parallel while each host still gets only 1 request per second. Supports streaming (to stop huge files early), redirects, timeouts, and has a built-in way to fake responses in tests (respx). | requests (no async), aiohttp (lower level, no easy test mocking). |
| **No Scrapy** | | For ~250 pages, a small asyncio crawler is easier to read, explain and test line by line. | Scrapy is the right call for many large sites; Part 2 could move to it or to a queue-based worker design. |
| **No headless browser** | | The site is rendered on the server (Next.js), so the HTML already contains the content and data. A browser would be about 10x slower and heavier. | Playwright, if a future source only renders in the browser. |
| **selectolax** (lexbor) | Parses HTML to find links and text | Very fast, small, and follows the modern HTML5 standard. | BeautifulSoup (simpler API but much slower), lxml. |
| **defusedxml** | Parses sitemaps | Safe against malicious XML (entity expansion attacks), since sitemaps come from outside. | The standard library XML parser, which is not hardened. |
| **PyMuPDF** | Reads PDF text page by page, and metadata | Fast, reliable text extraction with exact page numbers, which we need to cite facts. | pdfplumber (great for tables, slower), pypdf (weaker on messy PDFs). |
| **SQLite** | Stores documents, versions, facts, skips, runs | Zero setup, one file to hand to another service, real SQL for queries and checks. | Postgres is the next step when many workers write at once (Part 2). |
| **Files named by SHA-256** | Raw copies of pages and files | Identical content is stored once, and the hash doubles as the change detector. | Storing blobs in the database (makes it huge), naming by URL (breaks on changes). |
| **OpenRouter** (via the OpenAI SDK) | LLM calls for PDFs | One API key for many model providers, so we can switch to a cheaper or better model by changing one setting. It reports the real cost of each call, which we sum per run. | Calling one provider directly (locks us in, cost must be computed by hand). |
| **google/gemini-3.1-flash-lite** (default model) | Reads PDF text, returns structured facts | Cheap ($0.25 per million input tokens, $1.50 per million output), very long context, supports strict JSON schema output. Good enough for "find addresses, dates and decisions with a quote". | Larger models cost 3x to 10x more; worth testing on a small labeled sample before switching. Configurable with `--llm-model`. |
| **Streamlit** (UI only, optional extra) | The read-only data browser (section 12) | Python only, so the UI reuses the project's language and tests; clickable tables, a PDF viewer and multi-page navigation are built in; starts with one command. A consumer tool needs to be clear and fast to build, not pixel perfect. | FastAPI + React (two codebases, much more code), Gradio (made for model demos, weaker tables). |
| **pytest + respx** | Tests | respx fakes the website, so tests run offline in under a second and never touch CreateTO. | Recording real responses (brittle, and repeatedly hits the real site). |

## 4. Big picture

```mermaid
flowchart LR
    A[Sitemap + homepage] --> B[Queue]
    B --> C{Allowed?<br/>robots.txt, scope}
    C -- no --> S[(skips table)]
    C -- yes --> D[Polite fetcher<br/>1 req/s per host<br/>retries, ETag]
    D -- 304 or error --> S
    D -- new bytes --> E[(raw files on disk<br/>by SHA-256)]
    E --> F[(snapshots table<br/>one row per version)]
    D -- HTML page --> G[Link finder]
    G --> B
    F --> H[Extractors<br/>rules, then LLM for PDFs]
    H --> I[(facts table<br/>with source + history)]
    I --> J[export/*.jsonl]
```

## 5. The pieces (one job each)

### Sources: one file per website

Everything that is specific to a website lives in a `Source` object in `src/scrapekit/sources/`. The crawler only reads it.

```python
CREATETO = Source(                                   # abridged; see sources/createto.py
    name="createto",
    start_urls=("https://createto.ca/",),
    crawl_hosts=("createto.ca", "www.createto.ca"),
    sitemap_urls=("https://createto.ca/sitemap.xml",),
    excluded_path_prefixes=("/system/", "/search"),
    page_extractor="storyblok",
    extractor_options=CREATETO_MODEL,                # the site's CMS content model
    publisher="CreateTO, the City of Toronto's real estate agency",   # used in the LLM prompt
    ...
)
```

To add a new website:
1. Create `sources/<name>.py` with a `Source`.
2. Add it to the list in `sources/__init__.py`.
3. Run `uv run scrapekit run <name>`.

What a Source can say about its website:

| Field | What it controls | CreateTO value |
|---|---|---|
| `start_urls`, `crawl_hosts`, `sitemap_urls` | Where crawling starts and which hosts' pages are crawled | `https://createto.ca/`, `createto.ca` + `www.createto.ca`, the sitemap |
| `excluded_path_prefixes` | Public but useless pages to skip | `/system/`, `/search` |
| `page_extractor` + `extractor_options` | Which extractor reads pages, and the site's content model for it | `storyblok` + `CREATETO_MODEL` (which CMS components become projects and news, and which CMS fields map to which of our fields) |
| `publisher`, `publisher_body_example`, `llm_focus` | The site-specific parts of the LLM prompt: who publishes the documents and which facts matter | "CreateTO, the City of Toronto's real estate agency", "CreateTO Board of Directors", city land and development |
| `immutable_url_patterns` | File URLs that never change, so saved copies are not re-checked | Storyblok asset URLs |
| `url_allowlist` | Restrict a run to a fixed list of URLs (replay, backfills) | not set |
| `field_docs`, `overview_labels`, `ask_examples` | The data browser: what the site's own fields mean, which label fields get a chart, example questions for Ask AI | ward, phase, councillor ...; phase and ward charts; 3 questions |

**Rule: nothing outside `sources/` names a website.** A test (`test_no_site_names_outside_sources`) fails if a site name or host appears in any other code file. `test_ui_code_names_no_site_vocabulary` does the same for the UI (no site-specific example questions, field values or charts). Another test (`test_a_new_source_needs_only_a_config`) crawls a made-up second website with no crawler changes, and `test_prompt_and_schema_come_from_the_source` checks the LLM prompt is built from the Source.

### Code modules

| File | Job |
|---|---|
| `sources/` | One `Source` per website (above). |
| `config.py` | Runtime settings shared by all sources (rate limit, size cap, user agent, folders). |
| `fetcher.py` | Downloads one URL politely. Waits 1 second between requests to the same host, retries on 429 and 5xx with backoff, honors `Retry-After`, sends `If-None-Match` / `If-Modified-Since`, refuses files over 50 MB. |
| `links.py` | Finds links in a page (including file links hidden inside Next.js JSON) and decides: crawl it, download it, or skip it. |
| `crawler.py` | The loop. Reads robots.txt per host, reads the sitemap, runs a small pool of workers over a queue, saves results, logs every skip. |
| `blobs.py` | Saves raw bytes to `data/raw/ab/abcdef....pdf`, named by SHA-256, so identical content is stored once. |
| `db.py` | SQLite tables (below). The crawler never writes SQL directly. |
| `metrics.py` | Counts everything in a run and writes `runs/<run_id>.md` and `.json`. |
| `extract/` | Rule-based extractors: `storyblok`, `generic_html`, `pdf`. |
| `extract_stage.py` | Runs pass 1 (rules) and pass 2 (LLM) over saved copies. |
| `llm.py` | OpenRouter client, prompt, JSON schema, excerpt check. |
| `export.py` | JSON Lines export for other services. |
| `replay.py` | Serves saved copies as if they were the website. |
| `variants.py` | Builds fixtures and runs the first / unchanged / changed / failed scenarios. |
| `ui/` | Read-only data browser (section 12): `queries.py` holds all SQL, `views/` one file per page, `ask.py` the text-to-SQL agent. |
| `cli.py` | `scrapekit run / crawl / extract / export / variants / ui` and `scrapekit sources` |

## 6. Extraction: from saved copies to facts

Extraction is a separate stage that only reads saved copies, never the website. So it can be re-run any time (for example after improving an extractor) with zero requests to the site.

### What a "fact" is

One field of one thing, plus where it came from:

| entity_type | entity_key | field | value | source_url | locator |
|---|---|---|---|---|---|
| project | south-river | phase | "Leasing" | https://createto.ca/projects/south-river | `__NEXT_DATA__ page.content.phase` |
| project | south-river | site_area | "18.5 acres" | same | `__NEXT_DATA__ page.content.siteArea` |
| document | https://a-us.storyblok.com/.../agenda.pdf | page_count | 12 | same | pdf structure |

Storing one row per field (instead of one wide row per project) makes history simple: each field can change on its own, and each one keeps its own source.

### Extractors

| Extractor | Used for | How |
|---|---|---|
| `storyblok` | Sites built with Next.js + the Storyblok CMS, like createto.ca | Reads the CMS record embedded in the page (`__NEXT_DATA__` JSON). Exact values, free, no AI. The extractor knows how Storyblok pages are shaped, but nothing about one site: each site passes its own content model (`StoryblokModel`: components, field mappings, label lookups) from its Source. CreateTO's model is in `sources/createto.py`. |
| `generic` | Any HTML page, and static pages on createto.ca | Title, meta description, headings, main text. Works on any site. |
| `pdf` (pass 1) | Every linked PDF | Text of each page (saved with 1-based page numbers for citing), page count, PDF metadata dates, first page excerpt. Scanned PDFs with no text are logged as `no_text_layer`. |

A source picks its page extractor in its config (`page_extractor="storyblok"`) and passes the site's content model in `extractor_options`. Pages the model does not describe, and sites with no settings, use `generic`.

### Fields we keep and why

| Entity | Fields | Why they are useful later |
|---|---|---|
| project | name, phase, timeline (phase, schedule, active step), ward, councillor, address, major intersection, location (lat/lon), site area, current / proposed / surrounding use, types, initiatives, summary, body text, published and CMS updated dates | Answers "what is CreateTO doing, where, and at what stage", and lets you track phase changes over time |
| news | title, published date, topics (e.g. board meetings), related projects, event dates, external link, body text | Board and committee meetings and announcements are where decisions show up first; dates let consumers filter to the last 5 years |
| document | title (from file name and PDF metadata), page count, created / modified dates, first page excerpt, page text | Agendas, minutes and reports are the deepest source; page text is what the LLM pass (below) reads and cites |
| page | title, description, headings, text | Context pages (about, policies, strategic plan) |

### History: corrections and amendments

```mermaid
flowchart LR
    A[New facts from a document] --> B{Same field already current?}
    B -- no --> N[Insert: new]
    B -- yes, same value --> U[Mark confirmed in this run: unchanged]
    B -- yes, different value --> C[Insert new row<br/>close old row: valid_to + superseded_by]
    D[Current field missing now] --> R[Close it: removed]
```

Nothing is overwritten. For any field you can see every value it ever had, when we first saw it, when it stopped being true, and which snapshot it came from. Example: a project moving from "Leasing" to "Completed" leaves two rows, linked.

### Re-runs stay cheap

The `extractions` table remembers (url, content hash, extractor version). If a document's content did not change, its extraction is skipped. If an extractor is improved, its version number is bumped and everything is re-extracted once from the saved copies.

### Pass 2: the LLM reads PDFs (paid, optional)

Rules cannot read meaning from a 40 page board report. For that, an LLM reads each PDF that has text and returns facts with citations.

```mermaid
flowchart LR
    P[PDF page text<br/>from pdf_pages] --> T["Page-tagged text<br/>[page 1] ... [page 2] ...<br/>capped at 40k chars"]
    T --> C{In llm_cache?<br/>same hash + model + prompt}
    C -- yes --> R[Reuse answer, cost 0]
    C -- no --> L[OpenRouter call<br/>strict JSON schema]
    L --> R2[Answer + real cost]
    R --> V{Excerpt really on<br/>the cited page?}
    R2 --> V
    V -- yes --> K[Keep as fact, method=llm]
    V -- no --> D[Drop, count as dropped]
```

**What we ask for** (strict JSON schema, so the answer is always machine readable):
- `doc_type` from a fixed list (board agenda, minutes, staff report, plan or study, brochure, ...)
- `title`, `document_date`, `organization`, a one or two sentence `summary`
- up to 15 `facts`, each with `kind` (address, project, decision, amount, date, phase, organization), `value`, `page`, and an exact `excerpt` of 5 to 25 words

**How we keep it honest and cheap:**

| Concern | What we do |
|---|---|
| Made-up facts | Every fact must quote the page. We check the quote is really there, ignoring case, spacing and punctuation. If not, the fact is dropped and counted in the run report. The check allows three real-world PDF quirks we found in testing: a sentence that runs over a page break, numbers split by the PDF text layer (`50 0,000`), and a page footer sitting inside the quoted sentence (the quote may be split once into two verbatim parts of 4+ words, at most 300 characters apart). Side effect, by design: a quote the model shortens with "..." is accepted only when both parts are verbatim on the page and at most 300 characters apart; if they are further apart or not verbatim, it is rejected. |
| Wrong page numbers | Same check: the quote must be on the cited page, and the page must exist. |
| Paying twice | Answers are cached by content hash + model + prompt version. An unchanged PDF is never sent again, even if it appears under a second URL. Changing the model or prompt re-runs only what is needed. Improving the excerpt check has its own version number and re-checks cached answers for free. `--llm-limit` counts only paid calls. |
| Huge documents | Text is capped at 40,000 characters (about 10,000 tokens). Long documents send their start (about 70%) and their end (about 30%) with `[pages X-Y omitted]` in between, because amendments, signatures and final decisions often sit at the end. We found this in the variant test: an amendment added as the last page was invisible when only the start was sent. The locator records exactly which pages the model saw, for example `llm over pages 1-15, 20-22 of 22`. |
| Slow runs | Up to 8 calls at once. |
| API errors or bad JSON | Retries (3), then the document is logged as `llm_error` and the run continues. Rule-based facts for it are still saved. |
| Real cost | OpenRouter returns the actual cost of every call. We add it up per run. |
| Runaway spend | Hard budget per run (default $0.50, `--llm-budget`). Once reached, no new calls start; the remaining PDFs are logged as `llm_budget_reached` and picked up next run. `--llm-dry-run` shows the number of PDFs and the estimated cost without calling anything. |
| Rule facts vs LLM facts | Stored with `method = parse` or `llm`, and compared separately, so one never overwrites the other. |

Run without the LLM: `uv run scrapekit run createto --no-llm`. Try it on a few files first: `--llm-limit 10`.

Dry-run estimate on the real crawl (466 PDFs with text): about 1.49M input tokens, estimated $0.93 for the whole first run with the default model.

Trial on 10 real PDFs: $0.0152 actual (estimate $0.02), 4.9 seconds, 0 failures. Doc types, dates, organizations, addresses and project names were correct on manual review.

## 6b. Export for other services

`uv run scrapekit export createto` writes JSON Lines files to `export/createto/`, so another service can load the data without SQLite:

| File | One line per | Use it for |
|---|---|---|
| `entities.jsonl` | project, news item, document or page, with current values only | Loading the latest state. Every field carries its value, locator, excerpt, method and the snapshot hash it came from. Pages list their linked documents; documents list the pages that link to them. |
| `facts.jsonl` | every value ever recorded, including superseded ones | History, audits, "what did the site say on date X" |
| `documents.jsonl` | URL | Crawl status, hashes, where each URL was found |
| `skips.jsonl` | skipped or failed link | Monitoring and coverage checks |
| `runs.jsonl` | run | Metrics over time |
| `SCHEMA.md` | | Field-by-field description |

## 6c. Replay mode and the three variants

The brief asks to show an unchanged run, a changed source and a failed file. Waiting for the live site to change is not repeatable, so scrapekit can **replay saved local copies** through the normal crawler.

```mermaid
flowchart LR
    F[fixtures/base, changed, failed<br/>manifest.json + files] --> T[ReplayTransport<br/>same ETag and 304 rules as a server]
    T --> FE[Normal Fetcher] --> C[Normal crawler + extractors]
```

- `ReplayTransport` plugs into the same HTTP client, so the code under test is exactly the code that runs live. It serves ETags and answers `304 Not Modified`, like a real server.
- A replay run limits the source to the fixture URLs with `url_allowlist` (other links are logged as `not_in_allowlist`). The same allowlist is the natural hook for backfilling a chosen list of pages.
- `uv run scrapekit variants <source>` takes a small real slice of the crawl (the start page, plus pages that produced structured entities and link to small PDFs, chosen by what was extracted rather than by URL patterns), builds three fixture sets, and runs four scenarios into a fresh database. The "changed source" fixture edits one real extracted value in a saved page and amends one PDF; the "failed" fixture corrupts one PDF and removes another (404):

| Run | Fixture | What it shows |
|---|---|---|
| 1. first run | base | Everything new, LLM pays for each PDF |
| 2. unchanged repeat | base | All 304, nothing re-extracted, 0 paid calls |
| 3. changed source | changed | One extracted value edited in a page (the committed example: a project's phase), a PDF amended with an extra page: only those two are re-processed, old values kept as history, 1 paid call |
| 4. failed file | failed | One PDF corrupt, one returns 404: both logged, the run continues |

The result is written to `examples/variants/REPORT.md`. Reviewers can re-run it from the committed fixtures with no crawl: `uv run scrapekit variants createto --reuse-fixtures` (add `--no-llm` to skip the few cents of LLM cost).

## 7. Data model

```mermaid
erDiagram
    runs ||--o{ snapshots : "created in"
    runs ||--o{ skips : "logged in"
    snapshots ||--o{ facts : "read from"
    snapshots ||--o| pdf_pages : "text of"
    documents ||--o{ snapshots : "has versions"
    documents ||--o{ links : "links to"

    runs { text run_id PK; text source; text started_at; text finished_at; text stats_json }
    documents { text url PK; text source; text kind; text host; text etag; text current_hash; int last_status }
    snapshots { int id PK; text url; text content_hash; text fetched_at; text run_id; text path }
    skips { int id PK; text run_id; text url; text reason; text detail }
    links { text src; text dst }
    facts { int id PK; text entity_type; text entity_key; text field; text value; text source_url; text content_hash; text locator; text valid_from; text valid_to; int superseded_by }
    pdf_pages { text content_hash; int page_number; text text }
    llm_cache { text content_hash; text model_key; text response; real cost_usd }
```

- **documents**: one row per URL we know (page or file), tagged with its source, with the latest status, ETag and hash.
- **snapshots**: one row per *distinct version* of a URL. Same content again does not add a row. A new row means the source changed, and the old version is still on disk. This is our history of the raw source.
- **skips**: every link we did not use, or used and could not process, with a reason. Crawl: `off_domain`, `excluded_path`, `robots_disallowed`, `http_<code>` (404, 403, ...), `network_error`, `too_large`, `non_http_link`, `max_pages`, `not_in_allowlist`. Extraction: `parse_error`, `no_text_layer` (scanned PDF), `llm_error`, `llm_budget_reached`.
- **links**: which page linked to which URL, so every file can be traced back to the page that mentioned it.
- **facts**: one row per field of one entity, with source, locator and validity dates (section 6).
- **extractions**: which document versions were already extracted, and any failures.
- **pdf_pages**: text of each PDF page, keyed by content hash and page number.
- **llm_cache**: every paid LLM answer, keyed by content hash and model + prompt version, with tokens and cost.
- **runs**: one row per run with all the metrics.

## 8. What happens on a repeat run

```mermaid
flowchart TD
    A[URL] --> B{We have a saved copy?}
    B -- no --> F[Normal GET]
    B -- yes --> I{File URL the source marks immutable?}
    I -- yes --> U0[Unchanged: no request at all]
    I -- no --> C[GET with If-None-Match]
    C -- 304 --> U[Unchanged: reuse saved copy, no download]
    C -- 200 --> H{Same SHA-256 as before?}
    F --> N[New: save snapshot]
    H -- yes --> U2[Unchanged: no new snapshot]
    H -- no --> CH[Changed: save new snapshot, keep old one]
```

"Unchanged" means no new snapshot, so the extraction step (and any paid AI call) is skipped for it.

Measured on the live site: the unchanged repeat made 299 requests instead of 738 (440 files not re-checked, 274 answers were `304`), downloaded 49 MB instead of 990 MB, re-extracted nothing, and made 0 paid calls. See the README results table.

## 9. Being polite (not DDoS-ing the site)

- At most 1 request per second **per host**, no matter how many workers run.
- A clear `User-Agent` that says what we are.
- robots.txt is read once per host per run, before fetching anything from that host, and its rules are followed.
- Retries back off (1s, 2s, 4s) and respect `Retry-After`.
- Conditional GETs mean a repeat run downloads almost nothing.

## 10. Decisions and trade-offs

| Decision | Why | What we gave up |
|---|---|---|
| Website details in a `Source` config, not in code | Adding a site is cheap and safe, the crawler is tested once; a test keeps site names out of the code | A site on a new CMS needs a new extractor module (once), then only config for each site using that CMS |
| Plain async Python (`httpx`) instead of Scrapy | Small, easy to read and explain, enough for ~250 pages | Scrapy's built-in middleware ecosystem |
| No headless browser | Pages are server-rendered | Would need Playwright if the site moved to client-side rendering |
| SQLite + files on disk | Zero setup, one file to hand over, easy to query | Not built for many writers at once (Postgres would be the next step) |
| One row per fact, not one wide row per project | Each field keeps its own source and history; works for any entity type and any site | Consumers need a pivot to get one row per project (the export's `entities.jsonl` does this) |
| Rules first, AI only for PDFs | Exact, free and testable where the data is structured | Free text inside pages is not mined for facts yet |
| Hash raw bytes for change detection | Exact and simple | Next.js pages may change bytes on every deploy without real content changes. Fact-level comparison handles this: a cosmetic change re-runs the cheap parser, but no fact changes and no AI call is made. |
| Do not re-check files with immutable URLs (Storyblok gives every upload a new URL) | The live repeat run skipped 440 requests and took 4.6 minutes instead of 14 | A file deleted or corrupted in place would not be noticed until a full re-check. Mitigation: schedule `--revalidate-all` periodically (for example weekly). The variant demo uses it so file failures are visible. Checked a day later: a full re-check of all 440 answered 304 (unchanged). |

## 11. Build rounds

| Round | Scope | Status |
|---|---|---|
| 1 | Polite crawler, sitemap + link walk, raw copies, skip log, run metrics, pluggable sources | Done |
| 2 | Extract fields from HTML (Next.js JSON) and text + page numbers from PDFs, fact history | Done |
| 3 | OpenRouter extraction for PDFs only, cached by hash, real cost tracking, budget cap | Done |
| 4 | Replay mode and the 3 variants, JSONL export, faster repeat runs (immutable file URLs) | Done |
| 5 | Two real runs, results table, labeled costs, example output, 5 year flag in export | Done |
| UI-1 | Data browser: query module, runs, skip log, `scrapekit ui` | Done |
| UI-2 | Entities and the provenance panel (PDF page with the quote highlighted) | Done |
| UI-3 | Pages & files with raw viewer, facts | Done |
| UI-4 | Review fixes, docs, screenshots | Done |
| UI-5 | Overview charts and map, data dictionary, Ask AI | Done |

## 12. Data browser (Part 2)

A local app for a downstream engineer: see what was scraped and parsed, navigate it with full provenance, and check the status of past runs. It never changes the scraper's data: the database is opened read-only. The one file it writes is `ask_log.jsonl`, the Ask AI question log (below).

```
scrapekit run   ──writes──▶  data/scrapekit.db  +  data/raw/<hash>
                                     │
                         read-only connection (mode=ro)
                                     │
                          ui/queries.py   ◀── all SQL, plain functions, tested without Streamlit
                                     │
                          ui/views/*.py   ◀── Streamlit pages, no SQL
                                     │
                          scrapekit ui  →  http://localhost:8501
```

| Choice | Why |
|---|---|
| Read SQLite directly, read-only | The database is already the source of truth; no copy to keep in sync. `mode=ro` means the UI cannot change data (a test checks it). |
| One query module as the boundary | Pages never write SQL, so queries are tested on their own. This module is the API: putting FastAPI in front of it later is a thin wrapper, not a rewrite. |
| No HTTP API yet | One local consumer does not need one. Add it when a second consumer (another service, a hosted UI) appears. |
| Streamlit | See section 3. |
| Click-through instead of many screens | Every table row opens its details: run → its skips and changed facts; entity → facts → provenance → history; URL → versions → raw copy → facts. |
| One database per session | The app opens the database chosen at start (`--data-dir`) and shows its path; no database picker, so a reviewer always knows which data they are looking at. |
| Works for any source | The sidebar lists the sources in that database; nothing in `ui/` names a website (the same guard test covers it). |

**Provenance panel.** For any fact: source URL, method (rules or LLM), locator (JSON path, PDF metadata key, or page numbers), run, valid from / to, and the content hash of the snapshot it was read from. For an LLM fact the panel shows the saved text of the cited PDF page with the quote highlighted, so the reader checks the claim in one click. If the quote is split by a page footer, the page is shown with a note instead.

**Details that matter for correctness.**
- A table selection is a row number in Streamlit. When filters change the rows, the old selection is cleared, so the panel never shows a different fact than the one clicked.
- "Facts this run changed" matches removals by time window; a run that never finished is bounded by the next run of the same source.
- PDF text is shown as escaped HTML, never as Markdown, so `$` amounts and `#` or `1.` at line starts are shown exactly as in the source.
- PDFs over 15 MB are offered as a download instead of an inline viewer.

**Overview.** Headline numbers and charts, each from one function in `queries.py`. The map uses every entity that has a `location` fact, so a new source with locations gets a map for free.

**Data dictionary.** Field descriptions live next to the code that produces the field: generic ones (`pdf_*`, `llm_*`, `title`, `body_text`, `location`) in the extractor modules as `FIELD_DOCS`, site-specific ones (`ward`, `phase`, `councillor`) in the Source config as `field_docs`. A new website brings its own descriptions in its config. Coverage and examples are computed from the data, so the page is never out of date. A test fails if a field the CreateTO config can produce has no description.

**Ask AI (text to SQL).**

```
question ─▶ model writes one SQL query (sees: tables, columns, data dictionary, 2 worked examples)
         ─▶ check: one SELECT / WITH statement only
         ─▶ run on its own connection: read-only file, authorizer allows only reading, 500 rows, 5 seconds
         ─▶ SQL error? send the error back, one retry
         ─▶ model answers from up to 50 rows ─▶ page shows answer + SQL + rows + actual cost
```

| Choice | Why |
|---|---|
| The model writes SQL, it does not answer from memory | The answer is computed from our data, and the engineer sees the exact query and rows, so it can be checked. |
| A temporary `current_facts` view with plain-text values | Fact values are stored as JSON (`"Leasing"` with quotes). With the raw table the model compared against plain text and returned 0 rows or wrong counts; with the view and two generic examples, all three test questions were correct (re-checked after every prompt change). The view lives in memory on the AI's connection only. |
| Three layers of protection | The file is opened read-only; a statement check allows one SELECT; SQLite's authorizer denies everything except reading, and also blocks functions that reach native code or files (`fts3_tokenizer`, `load_extension`). A write hidden in `WITH ... DELETE` is still refused. A reviewer tried 29 attack queries; the database file was unchanged. |
| Limits | 500 rows, 5 seconds, values up to 1 MB (stops `zeroblob(900000000)` from using gigabytes), cells trimmed to 1,000 characters, 2 attempts, last 20 questions kept, $0.25 per browser session. |
| Scraped text is untrusted | It reaches the answer step, so a poisoned page could ask the model for a link or an image. The answer is shown with every Markdown character escaped: no links, no images, no math. The model has no tools, so the worst case is a misleading sentence next to the real SQL and rows. |
| Cost is always counted | A failed call keeps the cost already spent; if the provider reports no cost, a conservative $0.002 per call is counted so the cap still works. |
| Same model as the PDF pass | `google/gemini-3.1-flash-lite`, about $0.001 per question, configurable with `SCRAPEKIT_LLM_MODEL`. |
| A question log, kept apart from the data | Every question is appended to `ask_log.jsonl` next to the database: time, question, SQL, rows, attempts, cost, error. It is the audit trail of AI use, and the Overview shows its total as "Ask AI cost", separate from "LLM cost (runs)", because querying the data is not the cost of building it. The database stays read-only. |

**Out of scope for now:** starting crawls from the UI, login, editing data, a hosted deployment.

**Scaling it.** With Postgres and many sources, the same `queries.py` functions move behind a FastAPI service with pagination, and the UI (this one or a React app) calls that API. Run status would come from a `status` column written by the scheduler instead of being derived.
