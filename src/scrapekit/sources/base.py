"""A Source describes one website. Adding a new site means writing one of these, not changing the crawler."""

from dataclasses import dataclass

DEFAULT_FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx")


@dataclass(frozen=True)
class Source:
    name: str  # short id used on the CLI and in the database, e.g. "createto"
    start_urls: tuple[str, ...]
    crawl_hosts: tuple[str, ...]  # pages are only crawled on these hosts
    sitemap_urls: tuple[str, ...] = ()
    excluded_path_prefixes: tuple[str, ...] = ()  # public but useless pages on the crawl hosts
    file_extensions: tuple[str, ...] = DEFAULT_FILE_EXTENSIONS  # downloaded from any host
    max_pages: int = 2000
    # Regexes for file URLs whose content never changes (the host gives each version a new URL).
    # Saved copies of these are not re-checked on repeat runs, which saves one request per file.
    immutable_url_patterns: tuple[str, ...] = ()
    # If set, only these URLs are fetched (others are logged as not_in_allowlist). Used by replay
    # fixtures, and the natural hook for backfilling a chosen list of pages.
    url_allowlist: frozenset[str] | None = None
    page_extractor: str = "generic"  # "generic" works anywhere; "storyblok" reads Next.js + Storyblok JSON
    notes: str = ""  # what we learned about the site, shown by `scrapekit sources`
    tags: tuple[str, ...] = ()
