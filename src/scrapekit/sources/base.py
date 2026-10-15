"""A Source describes one website. Adding a new site means writing one of these, not changing the crawler."""

from dataclasses import dataclass, field
from typing import Any

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
    extractor_options: Any = None  # site-specific settings for the page extractor, e.g. a StoryblokModel
    # Who publishes this site's documents, used in the LLM prompt so the model has the right context.
    publisher: str = "a public organization"
    publisher_body_example: str = "Board of Directors"  # example of an issuing body, for the LLM schema
    llm_focus: str | None = None  # which facts matter for this site, in the LLM prompt; None = a generic default
    # Charts of "how many entities per value" for this site's own label fields, as (entity_type, field).
    overview_labels: tuple[tuple[str, str], ...] = ()
    # Example questions offered on the UI's Ask AI page; generic ones are used when empty.
    ask_examples: tuple[str, ...] = ()
    # What this site's own fields mean ("entity_type.field" -> text), shown in the UI's data dictionary.
    field_docs: dict[str, str] = field(default_factory=dict)
    notes: str = ""  # what we learned about the site, shown by `scrapekit sources`
    tags: tuple[str, ...] = ()
