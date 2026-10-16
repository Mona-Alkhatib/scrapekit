"""Extractors turn one saved page or file into facts.

A fact is one field of one thing, with where it came from:
    (entity_type="project", entity_key="south-river", field="phase", value="Leasing",
     source_url=..., locator="__NEXT_DATA__ page.content.phase")

Extractors are pure functions over saved bytes, so extraction can be re-run any time
without touching the website.
"""

import json
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Fact:
    """One field of one entity, with where it came from (locator, optional excerpt) and how (rules or LLM)."""
    entity_type: str  # project, news, page, document
    entity_key: str  # stable id inside the source: a slug or the file URL
    field: str
    value: str | int | float | list | dict | None
    locator: str  # where in the source: a JSON path, "page 3", "<title>"
    excerpt: str | None = None  # short supporting text, mainly for PDFs
    method: str = "parse"  # parse (rules, free) or llm (paid)

    def value_json(self) -> str:
        """The value as stable JSON text, so equal values compare equal in the database."""
        return json.dumps(self.value, ensure_ascii=False, sort_keys=True)


class ExtractionError(Exception):
    """Raised when a page or file cannot be parsed. Logged, never fatal."""


PageExtractor = Callable[[str, bytes, object], list[Fact]]  # (url, html, the source's extractor_options)


def field_docs(source) -> dict[str, str]:
    """What each field means: generic descriptions from the extractors, then the source's own (which win).
    Keys are "entity_type.field" or just "field" when the meaning is the same for every entity type."""
    from .. import extract_stage
    from . import generic_html, pdf, storyblok

    docs: dict[str, str] = {}
    for part in (storyblok.FIELD_DOCS, generic_html.FIELD_DOCS, pdf.FIELD_DOCS, extract_stage.FIELD_DOCS,
                 getattr(source, "field_docs", None) or {}):
        docs.update(part)
    return docs


def describe(docs: dict[str, str], entity_type: str, field: str) -> str | None:
    """The description of one field: the entity-specific one if there is one, else the generic one."""
    return docs.get(f"{entity_type}.{field}") or docs.get(field)


def get_page_extractor(name: str) -> PageExtractor:
    """The page extractor function for a name in a Source config (generic or storyblok)."""
    from . import generic_html, storyblok

    extractors = {"generic": generic_html.extract, "storyblok": storyblok.extract}
    return extractors[name]
