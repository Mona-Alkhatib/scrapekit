"""Command line entry point. <source> is a name from `scrapekit sources`.

    uv run scrapekit run <source>       # crawl + extract, the normal command
    uv run scrapekit crawl <source>     # only download
    uv run scrapekit extract <source>   # only re-extract from saved copies (no network)
    uv run scrapekit ui                 # read-only data browser (needs `uv sync --extra ui`)
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .crawler import Crawler
from .db import Store, connect
from .export import export_source
from .extract_stage import run_extract
from .fetcher import Fetcher
from .llm import DEFAULT_MODEL, DEFAULT_RUN_BUDGET_USD, OpenRouterExtractor
from .metrics import RunStats
from .replay import ReplayTransport
from .sources import SOURCES, Source, get_source


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


async def run_pipeline(
    settings: Settings, source: Source, crawl: bool = True, extract: bool = True,
    llm: OpenRouterExtractor | None = None, llm_limit: int | None = None,
    llm_budget: float = DEFAULT_RUN_BUDGET_USD, llm_dry_run: bool = False,
    transport=None,
) -> RunStats:
    mode = "+".join(name for name, on in (("crawl", crawl), ("extract", extract)) if on)
    run_id = new_run_id()
    stats = RunStats(run_id=run_id, mode=f"{mode}:{source.name}")
    store = Store(connect(settings.db_path), run_id)
    store.start_run(source.name, mode)
    try:
        if crawl:
            fetcher = Fetcher(settings, stats, transport=transport)
            try:
                await Crawler(settings, source, store, fetcher, stats).run()
            finally:
                await fetcher.close()
        if extract:
            await run_extract(source, store, stats, llm=llm, llm_limit=llm_limit,
                              llm_budget=llm_budget, llm_dry_run=llm_dry_run)
    finally:
        stats.finish()
        store.finish_run(json.dumps(stats.to_dict()))
        stats.write(settings.runs_dir)
    return stats


async def crawl(settings: Settings, source: Source) -> RunStats:
    return await run_pipeline(settings, source, crawl=True, extract=False)


async def _closing(coro, llm: OpenRouterExtractor | None):
    """Runs a coroutine, then closes the LLM client. Whoever creates the client closes it."""
    try:
        return await coro
    finally:
        if llm is not None:
            await llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="scrapekit", description="Turn public websites and their files into data.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="List the sources scrapekit knows how to crawl.")

    p_var = sub.add_parser("variants", help="Run first / unchanged / changed / failed scenarios from saved copies.")
    p_var.add_argument("source")
    p_var.add_argument("--data-dir", type=Path, default=Settings.data_dir, help="Real crawl to take the fixtures from.")
    p_var.add_argument("--out", type=Path, default=Path("examples/variants"))
    p_var.add_argument("--no-llm", action="store_true")
    p_var.add_argument("--llm-model", default=os.environ.get("SCRAPEKIT_LLM_MODEL", DEFAULT_MODEL))
    p_var.add_argument("--llm-budget", type=float, default=0.05)
    p_var.add_argument("--reuse-fixtures", action="store_true",
                       help="Use the fixtures already in --out (for example the committed ones) instead of rebuilding from a crawl.")

    p_export = sub.add_parser("export", help="Write JSON Lines files for other services.")
    p_export.add_argument("source")
    p_export.add_argument("--data-dir", type=Path, default=Settings.data_dir)
    p_export.add_argument("--out", type=Path, default=Path("export"))

    p_ui = sub.add_parser("ui", help="Open the read-only data browser (needs `uv sync --extra ui`).")
    p_ui.add_argument("--data-dir", type=Path, default=Settings.data_dir)
    p_ui.add_argument("--port", type=int, default=8501)

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
        p.add_argument("--replay", type=Path, help="Serve pages and files from a fixture folder instead of the web.")
        p.add_argument("--revalidate-all", action="store_true",
                       help="Re-check every saved file, even ones the source marks as immutable.")
        p.add_argument("--no-llm", action="store_true", help="Skip the paid LLM pass on PDFs.")
        p.add_argument("--llm-model", default=os.environ.get("SCRAPEKIT_LLM_MODEL", DEFAULT_MODEL))
        p.add_argument("--llm-limit", type=int, help="Send at most N new PDFs to the LLM (for trying it out).")
        p.add_argument("--llm-budget", type=float, default=DEFAULT_RUN_BUDGET_USD,
                       help=f"Stop starting new LLM calls once this run has spent this many USD (default {DEFAULT_RUN_BUDGET_USD}).")
        p.add_argument("--llm-dry-run", action="store_true", help="Show how many PDFs would go to the LLM and the estimated cost. No calls.")
        p.add_argument("-v", "--verbose", action="store_true")

    load_dotenv()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "variants":
        from .variants import build_fixtures, report, run_variants

        source = get_source(args.source)
        fixtures = args.out / "fixtures"
        if args.reuse_fixtures:
            plan = json.loads((fixtures / "plan.json").read_text())
        else:
            plan = build_fixtures(replace(Settings(), data_dir=args.data_dir).db_path, fixtures, source)
        llm = None if args.no_llm or not os.environ.get("OPENROUTER_API_KEY") else OpenRouterExtractor(model=args.llm_model)
        results = asyncio.run(_closing(run_variants(source, fixtures, args.out / ".work", llm, args.llm_budget, plan), llm))
        text = report(results, plan, args.out / ".work" / "data" / "scrapekit.db", source)
        (args.out / "REPORT.md").write_text(text)
        print(text)
    elif args.command == "ui":
        import subprocess
        import sys

        app = Path(__file__).parent / "ui" / "app.py"
        env = dict(os.environ, SCRAPEKIT_UI_DB=str(replace(Settings(), data_dir=args.data_dir).db_path))
        raise SystemExit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.port",
                                          str(args.port), "--browser.gatherUsageStats", "false",
                                          "--client.toolbarMode", "minimal"], env=env))
    elif args.command == "export":
        get_source(args.source)
        store = Store(connect(replace(Settings(), data_dir=args.data_dir).db_path), run_id="export")
        counts = export_source(store, args.source, args.out)
        print(f"Wrote {args.out / args.source}: " + ", ".join(f"{v:,} {k}" for k, v in counts.items()))
    elif args.command == "sources":
        for s in SOURCES.values():
            print(f"{s.name:12} {', '.join(s.start_urls)}\n{'':12} {s.notes}")
    else:
        source = get_source(args.source)
        if args.max_pages is not None:
            source = replace(source, max_pages=args.max_pages)
        settings = replace(Settings(), data_dir=args.data_dir, runs_dir=args.runs_dir,
                           seconds_between_requests=args.delay, revalidate_all=args.revalidate_all)
        stages = {"run": (True, True), "crawl": (True, False), "extract": (False, True)}[args.command]
        transport = None
        if args.replay:
            transport = ReplayTransport(args.replay)
            source = replace(source, url_allowlist=frozenset(transport.urls()))
            settings = replace(settings, seconds_between_requests=0)
        llm = None
        if stages[1] and not args.no_llm:
            if os.environ.get("OPENROUTER_API_KEY") or args.llm_dry_run:
                llm = OpenRouterExtractor(model=args.llm_model, api_key=None if not args.llm_dry_run else "dry-run")
            else:
                print("OPENROUTER_API_KEY not set: skipping the LLM pass (use --no-llm to hide this).")
        stats = asyncio.run(_closing(run_pipeline(settings, source, *stages, llm=llm, llm_limit=args.llm_limit,
                                                  llm_budget=args.llm_budget, llm_dry_run=args.llm_dry_run,
                                                  transport=transport), llm))
        print(stats.to_markdown())
