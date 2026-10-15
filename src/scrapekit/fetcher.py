"""Polite HTTP client: one request per second per host, retries with backoff, conditional GETs.

Every request in a run goes through Fetcher.fetch(), so the politeness rules live in one place:
- HostThrottle: at least 1 second between two requests to the same host (settings).
- Conditional GET: send the saved ETag / Last-Modified; the server answers 304 if nothing changed.
- Retries: 429 and 5xx are retried with backoff (1s, 2s, 4s), honoring Retry-After.
- Size cap: downloads are streamed and stopped at 50 MB (settings.max_file_bytes).
- Every request is counted in the run report (requests, status codes, bytes).

robots.txt is not handled here: the crawler fetches it once per host (crawler.py, allowed()).
"""

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .config import Settings
from .metrics import RunStats

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status: int  # 0 means a network error, see `error`
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class HostThrottle:
    """Makes sure two requests to the same host are at least `interval` seconds apart."""

    def __init__(self, interval: float):
        self.interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        # One lock per host: workers on the same host wait in turn, other hosts are not slowed.
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self.interval - (time.monotonic() - self._last.get(host, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[host] = time.monotonic()


class Fetcher:
    def __init__(self, settings: Settings, stats: RunStats, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.stats = stats
        self.throttle = HostThrottle(settings.seconds_between_requests)
        self.client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        # Conditional GET headers, from the previous run's response.
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        host = urlparse(url).netloc
        last_error = None
        for attempt in range(self.settings.max_retries + 1):
            await self.throttle.wait(host)
            self.stats.requests += 1
            try:
                result = await self._get(url, headers)
            except httpx.HTTPError as exc:
                self.stats.status_counts["error"] += 1
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                self.stats.status_counts[result.status] += 1
                # Success, 304, 404 etc. return right away; only 429 / 5xx are retried.
                if result.status not in RETRY_STATUSES or attempt == self.settings.max_retries:
                    return result
                last_error = f"HTTP {result.status}"
                retry_after = result.headers.get("retry-after", "")
                if retry_after.isdigit():
                    await asyncio.sleep(min(int(retry_after), 60))
                    continue
            await asyncio.sleep(2**attempt)  # exponential backoff: 1s, 2s, 4s
        return FetchResult(url=url, status=0, error=last_error)

    async def _get(self, url: str, headers: dict) -> FetchResult:
        async with self.client.stream("GET", url, headers=headers) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code != 200:
                return FetchResult(url=url, status=resp.status_code, headers=resp_headers)

            # Size cap: check the declared size first, then count bytes while streaming.
            declared = int(resp_headers.get("content-length") or 0)
            if declared > self.settings.max_file_bytes:
                return FetchResult(url=url, status=200, headers=resp_headers, error=f"too_large:{declared}")

            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > self.settings.max_file_bytes:
                    return FetchResult(url=url, status=200, headers=resp_headers, error=f"too_large:>{size}")
                chunks.append(chunk)
            self.stats.bytes_downloaded += size
            return FetchResult(url=url, status=200, headers=resp_headers, body=b"".join(chunks))
