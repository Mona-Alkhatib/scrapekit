"""Command line entry point.

    uv run scrapekit run createto       # crawl + extract, the normal command
    uv run scrapekit crawl createto     # only download
    uv run scrapekit extract createto   # only re-extract from saved copies (no network)
"""

import argparse
import asyncio
import json
import logging
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .crawler import Crawler
from .db import Store, connect
from .extract_stage import run_extract
from .fetcher import Fetcher
from .metrics import RunStats
from .sources import SOURCES, Source, get_source


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


async def run_pipeline(settings: Settings, source: Source, crawl: bool = True, extract: bool = True) -> RunStats:
    mode = "+".join(name for name, on in (("crawl", crawl), ("extract", extract)) if on)
    run_id = new_run_id()
    stats = RunStats(run_id=run_id, mode=f"{mode}:{source.name}")
    store = Store(connect(settings.db_path), run_id)
    store.start_run(source.name, mode)
    try:
        if crawl:
            fetcher = Fetcher(settings, stats)
            try:
                await Crawler(settings, source, store, fetcher, stats).run()
            finally:
                await fetcher.close()
        if extract:
            run_extract(source, store, stats)
    finally:
        stats.finish()
        store.finish_run(json.dumps(stats.to_dict()))
        stats.write(settings.runs_dir)
    return stats


async def crawl(settings: Settings, source: Source) -> RunStats:
    return await run_pipeline(settings, source, crawl=True, extract=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="scrapekit", description="Turn public websites and their files into data.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="List the sources scrapekit knows how to crawl.")

    stage_help = {
        "run": "Crawl, then extract. The normal command.",
        "crawl": "Only download pages and files.",
        "extract": "Only extract facts from saved copies. No network.",
    }
    for name, help_text in stage_help.items():
        p = sub.add_parser(name, help=help_text)
        p.add_argument("source", help="Source name, see `scrapekit sources`.")
        p.add_argument("--data-dir", type=Path, default=Settings.data_dir)
        p.add_argument("--runs-dir", type=Path, default=Settings.runs_dir)
        p.add_argument("--max-pages", type=int, help="Override the source's page limit (handy for smoke tests).")
        p.add_argument("--delay", type=float, default=Settings.seconds_between_requests,
                       help="Seconds between requests to the same host (default 1.0).")
        p.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "sources":
        for s in SOURCES.values():
            print(f"{s.name:12} {', '.join(s.start_urls)}\n{'':12} {s.notes}")
    else:
        source = get_source(args.source)
        if args.max_pages is not None:
            source = replace(source, max_pages=args.max_pages)
        settings = replace(Settings(), data_dir=args.data_dir, runs_dir=args.runs_dir,
                           seconds_between_requests=args.delay)
        stages = {"run": (True, True), "crawl": (True, False), "extract": (False, True)}[args.command]
        stats = asyncio.run(run_pipeline(settings, source, *stages))
        print(stats.to_markdown())
