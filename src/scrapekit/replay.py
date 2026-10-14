"""Replay mode: serve saved local copies instead of the live website.

A fixture folder holds a manifest.json and the files it points to:

    {"https://createto.ca/projects/x": {"file": "projects-x.html", "content_type": "text/html"},
     "https://a-us.storyblok.com/f/.../a.pdf": {"file": "a.pdf", "content_type": "application/pdf"},
     "https://a-us.storyblok.com/f/.../gone.pdf": {"status": 404}}

The transport plugs into the normal Fetcher, so the crawler runs exactly as it does live,
including ETags and 304 Not Modified. Unknown URLs get a 404.
"""

import hashlib
import json
from pathlib import Path

import httpx

from .links import normalize


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, fixture_dir: Path):
        self.dir = fixture_dir
        self.manifest = {normalize(u, u): v for u, v in json.loads((fixture_dir / "manifest.json").read_text()).items()}

    def urls(self) -> set[str]:
        return set(self.manifest)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        entry = self.manifest.get(normalize(str(request.url), str(request.url)))
        if entry is None:
            return httpx.Response(404, request=request)
        status = entry.get("status", 200)
        if status != 200:
            return httpx.Response(status, request=request)
        body = (self.dir / entry["file"]).read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag}, request=request)
        headers = {"etag": etag, "content-type": entry.get("content_type", "application/octet-stream")}
        return httpx.Response(200, content=body, headers=headers, request=request)
