"""Finding links in a page and deciding what to do with each one."""

import re
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

from selectolax.lexbor import LexborHTMLParser

from .sources import Source


@lru_cache
def file_url_pattern(extensions: tuple[str, ...]) -> re.Pattern:
    """Matches absolute file URLs anywhere in raw text. Next.js and other CMS pages
    embed file links inside JSON, not only in <a> tags."""
    exts = "|".join(re.escape(e.lstrip(".")) for e in extensions)
    return re.compile(rf"https?://[^\s\"'<>\\]+?\.(?:{exts})(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE)


def normalize(href: str, base: str) -> str | None:
    """Absolute URL without fragment, lowercase host, no trailing slash. None if not http(s)."""
    href = href.strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return None
    url, _ = urldefrag(urljoin(base, href))
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urlunparse((parts.scheme, parts.netloc.lower(), path, "", parts.query, ""))


def canonicalize(url: str, source: Source) -> str:
    """Rewrites http:// and alias hosts (www.) of a crawl host to the source's canonical origin,
    so the same page is never fetched twice under two addresses."""
    parts = urlparse(url)
    if parts.netloc in source.crawl_hosts:
        canon = urlparse(source.start_urls[0])
        return urlunparse((canon.scheme, canon.netloc, parts.path, "", parts.query, ""))
    return url


def is_file_url(url: str, source: Source) -> bool:
    return PurePosixPath(urlparse(url).path).suffix.lower() in source.file_extensions


def classify(url: str, source: Source) -> tuple[str | None, str | None]:
    """Returns (kind, None) to fetch it, or (None, reason) to skip it."""
    if is_file_url(url, source):
        return "file", None  # files may live on any host
    parts = urlparse(url)
    if parts.netloc not in source.crawl_hosts:
        return None, "off_domain"
    if parts.path.startswith(source.excluded_path_prefixes):
        return None, "excluded_path"
    return "page", None


def extract_links(html: bytes, base: str, source: Source) -> tuple[set[str], set[str]]:
    """Returns (normalized links, raw hrefs we could not use)."""
    text = html.decode("utf-8", errors="replace")
    found, rejected = set(), set()
    for node in LexborHTMLParser(text).css("a[href]"):
        href = node.attributes.get("href") or ""
        url = normalize(href, base)
        (found.add(url) if url else rejected.add(href))
    for match in file_url_pattern(source.file_extensions).findall(text):
        url = normalize(match, base)
        if url:
            found.add(url)
    return found, rejected
