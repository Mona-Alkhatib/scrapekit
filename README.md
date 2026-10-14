# scrapekit

A small, polite crawler that turns public websites and the files they link to into versioned, sourced data.

The first source is [CreateTO](https://createto.ca). Each website is described by one small `Source` config, so adding another site does not change the crawler.

- Crawls every public page on a site (sitemap + link walk).
- Downloads every linked file (PDF, Word, Excel), including files hosted on other sites.
- Saves a raw copy of every page and file, with one snapshot per version, so changes over time are kept.
- Logs every skipped or failed link with a reason.
- Writes a metrics report for every run (time, requests, files, records, paid calls, cost).
- Repeat runs use conditional requests, so unchanged pages are not downloaded or processed again.

How it works: see [docs/DESIGN.md](docs/DESIGN.md).

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # add OPENROUTER_API_KEY (only needed for PDF extraction)
```

## Run

```bash
uv run scrapekit sources                     # list known sources
uv run scrapekit run createto                # crawl + extract (the normal command)
uv run scrapekit run createto --max-pages 20 # quick smoke test
uv run scrapekit crawl createto              # only download
uv run scrapekit extract createto            # only re-extract from saved copies, no network
```

LLM pass on PDFs (needs `OPENROUTER_API_KEY` in `.env`, never committed):

```bash
uv run scrapekit extract createto --llm-dry-run      # how many PDFs and estimated cost, no calls
uv run scrapekit extract createto --llm-limit 10     # try on 10 PDFs (about $0.02)
uv run scrapekit run createto --llm-budget 1.50      # full run, hard spend cap per run (default $0.50)
uv run scrapekit run createto --no-llm               # skip the LLM entirely
```

Each run prints a summary and writes:

| Output | What it is |
|---|---|
| `data/scrapekit.db` | SQLite: documents, snapshots, facts (with history), pdf_pages, skips, links, runs |
| `data/raw/` | Raw copies of pages and files, named by SHA-256 |
| `runs/<run_id>.md` / `.json` | Metrics for that run |

## Adding a website

1. Create `src/scrapekit/sources/<name>.py` with a `Source` (start URLs, hosts to crawl, sitemap, paths to skip).
2. Add it to `src/scrapekit/sources/__init__.py`.
3. `uv run scrapekit crawl <name>`

## Tests

```bash
uv run pytest
```

Tests run against a fake site, with no network access.

## Status

Built in rounds. See the table at the end of [docs/DESIGN.md](docs/DESIGN.md).
