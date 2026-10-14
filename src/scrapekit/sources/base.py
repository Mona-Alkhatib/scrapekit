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
    page_extractor: str = "generic"  # "generic" works anywhere; "storyblok" reads Next.js + Storyblok JSON
    notes: str = ""  # what we learned about the site, shown by `scrapekit sources`
    tags: tuple[str, ...] = ()
