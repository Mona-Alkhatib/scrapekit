"""The crawl loop for any Source: seed from sitemaps, walk the crawl hosts, download linked files, log every skip."""

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from defusedxml import ElementTree as ET

from .blobs import save_blob
from .config import Settings
from .db import Store
from .fetcher import FetchResult, Fetcher
from .links import canonicalize, classify, extract_links, normalize
from .metrics import RunStats
from .sources import Source

log = logging.getLogger(__name__)
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@dataclass
class Job:
    url: str
    kind: str  # 'page' or 'file'
    found_on: str | None


class Crawler:
    def __init__(self, settings: Settings, source: Source, store: Store, fetcher: Fetcher, stats: RunStats):
        self.settings = settings
        self.source = source
        self.store = store
        self.fetcher = fetcher
        self.stats = stats
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.seen: set[str] = set()
        self.skipped: set[str] = set()
        self.pages_queued = 0
        self.robots: dict[str, RobotFileParser] = {}
        self.robots_locks: dict[str, asyncio.Lock] = {}

    # ---------- public ----------

    async def run(self) -> None:
        for start in self.source.start_urls:
            self.enqueue(normalize(start, start), "page", None)
        for url in await self.sitemap_urls():
            self.enqueue(url, "page", "sitemap")

        workers = [asyncio.create_task(self.worker()) for _ in range(self.settings.workers)]
        await self.queue.join()
        for w in workers:
            w.cancel()
        self.store.commit()

    # ---------- queue ----------

    def enqueue(self, url: str, kind: str, found_on: str | None) -> None:
        if url in self.seen:
            return
        self.seen.add(url)
        if kind == "page":
            if self.pages_queued >= self.source.max_pages:
                self.skip(url, "max_pages")
                return
            self.pages_queued += 1
        self.queue.put_nowait(Job(url, kind, found_on))

    async def worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self.process(job)
            except Exception as exc:  # one bad URL must never stop the crawl
                log.exception("failed on %s", job.url)
                self.skip(job.url, "internal_error", repr(exc))
            finally:
                self.store.commit()
                self.queue.task_done()

    def skip(self, url: str, reason: str, detail: str | None = None) -> None:
        if url in self.skipped:
            return
        self.skipped.add(url)
        self.stats.skips[reason] += 1
        self.store.add_skip(url, reason, detail)

    # ---------- robots + sitemap ----------

    async def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        # One lock per host, so parallel workers fetch robots.txt once, not once each.
        async with self.robots_locks.setdefault(host, asyncio.Lock()):
            if host not in self.robots:
                parser = RobotFileParser()
                result = await self.fetcher.fetch(f"{urlparse(url).scheme}://{host}/robots.txt")
                # No robots.txt (or an error) means no rules, per the standard.
                parser.parse(result.body.decode("utf-8", "replace").splitlines() if result.ok else [])
                self.robots[host] = parser
        return self.robots[host].can_fetch(self.settings.user_agent, url)

    async def sitemap_urls(self) -> list[str]:
        todo, urls = list(self.source.sitemap_urls), []
        while todo:
            result = await self.fetcher.fetch(todo.pop())
            if not result.ok:
                self.skip(result.url, f"http_{result.status}", result.error)
                continue
            try:
                tree = ET.fromstring(result.body)
            except ET.ParseError as exc:
                self.skip(result.url, "bad_sitemap", str(exc))
                continue
            for loc in tree.iter(f"{SITEMAP_NS}loc"):
                url = normalize(loc.text or "", result.url)
                url = url and canonicalize(url, self.source)
                if not url:
                    continue
                (todo if tree.tag.endswith("sitemapindex") else urls).append(url)
        return urls

    # ---------- one URL ----------

    async def process(self, job: Job) -> None:
        if not await self.allowed(job.url):
            self.skip(job.url, "robots_disallowed")
            return

        host = urlparse(job.url).netloc
        self.store.upsert_document(self.source.name, job.url, job.kind, host, job.found_on)
        if job.found_on and job.found_on != "sitemap":
            self.store.add_link(job.found_on, job.url)

        previous = self.store.get_document(job.url)
        prev_hash = previous["current_hash"] if previous else None
        prev_path = self.store.latest_snapshot_path(job.url) if prev_hash else None
        have_copy = prev_path is not None and Path(prev_path).exists()

        if have_copy and job.kind == "file" and self.is_immutable(job.url) and not self.settings.revalidate_all:
            # A new version of this file would get a new URL, so the saved copy is still current.
            self.count(job.kind, "unchanged")
            self.stats.files_not_rechecked += 1
            return

        result = await self.fetcher.fetch(
            job.url,
            etag=previous["etag"] if have_copy else None,
            last_modified=previous["last_modified"] if have_copy else None,
        )

        if result.not_modified:
            self.store.record_fetch(job.url, 304, None, None, None, None)
            self.count(job.kind, "unchanged")
            body, content_type = Path(prev_path).read_bytes(), previous["content_type"]
        else:
            body = self.handle_failure(job, result)
            if body is None:
                return
            content_type = result.headers.get("content-type", "")
            digest, path = save_blob(self.settings.raw_dir, body, job.url, content_type)
            self.store.add_snapshot(job.url, digest, len(body), str(path))
            self.store.record_fetch(
                job.url, 200, result.headers.get("etag"), result.headers.get("last-modified"), content_type, digest
            )
            self.count(job.kind, "fetched")
            if prev_hash:
                self.count(job.kind, "unchanged" if prev_hash == digest else "changed")

        if job.kind == "page" and "html" in (content_type or ""):
            self.follow_links(job.url, body)

    def is_immutable(self, url: str) -> bool:
        return any(re.search(p, url) for p in self.source.immutable_url_patterns)

    def handle_failure(self, job: Job, result: FetchResult) -> bytes | None:
        """Logs the reason and returns None if the response is unusable, else the body."""
        if result.status == 0:
            self.skip(job.url, "network_error", result.error)
        elif result.status != 200:
            self.skip(job.url, f"http_{result.status}", job.found_on)
        elif result.error:
            self.skip(job.url, "too_large", result.error)
        else:
            return result.body
        return None

    def count(self, kind: str, what: str) -> None:
        name = f"{'pages' if kind == 'page' else 'files'}_{what}"
        setattr(self.stats, name, getattr(self.stats, name) + 1)

    def follow_links(self, page_url: str, html: bytes) -> None:
        links, rejected = extract_links(html, page_url, self.source)
        for href in rejected:
            self.skip(href, "non_http_link", page_url)  # mailto:, tel:, etc.
        for url in sorted(links):
            url = canonicalize(url, self.source)
            kind, reason = classify(url, self.source)
            if kind:
                self.enqueue(url, kind, page_url)
            else:
                self.skip(url, reason, page_url)
