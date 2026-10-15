"""PDF text per page, plus basic facts that need no AI (title, page count, metadata dates).

read_pdf() keeps the text page by page. extract_stage.py saves those pages in the `pdf_pages`
table, so the LLM can be told "[page N]" and every LLM fact can be checked against its page.
A PDF with almost no text on any page is a scan: it is saved and logged (no_text_layer), not parsed.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import pymupdf

from . import ExtractionError, Fact

MIN_TEXT_CHARS_PER_PAGE = 20  # below this on every page, the PDF is probably a scan

# What each field means, shown in the UI's data dictionary.
FIELD_DOCS = {
    "document.file_name_title": "A readable title made from the file name (dashes and underscores become spaces).",
    "document.page_count": "Number of pages in the PDF.",
    "document.has_text_layer": "true if the PDF has selectable text. false means a scanned image: saved, but not parsed.",
    "document.pdf_title": "The title stored in the PDF's metadata, if the author set one.",
    "document.pdf_created": "Creation date from the PDF's metadata (YYYY-MM-DD). Often the export date, not the meeting date.",
    "document.pdf_modified": "Last-modified date from the PDF's metadata (YYYY-MM-DD).",
    "document.first_page_excerpt": "The first 300 characters of page 1, a quick preview of what the document is.",
}


@dataclass
class PdfText:
    pages: list[str]  # index 0 is page 1
    metadata: dict

    @property
    def has_text_layer(self) -> bool:
        return any(len(p.strip()) >= MIN_TEXT_CHARS_PER_PAGE for p in self.pages)


def read_pdf(data: bytes) -> PdfText:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            # One string per page; page numbers are 1-based from here on, as a person would cite them.
            return PdfText(pages=[page.get_text() for page in doc], metadata=dict(doc.metadata or {}))
    except Exception as exc:  # pymupdf raises several types for broken files
        raise ExtractionError(f"pdf open failed: {type(exc).__name__}: {exc}") from exc


def pdf_date(raw: str | None) -> str | None:
    """PDF dates look like D:20240917103000-04'00'. Returns 2024-09-17."""
    if not raw:
        return None
    digits = raw.removeprefix("D:")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


def file_title(url: str) -> str:
    stem = PurePosixPath(unquote(urlparse(url).path)).stem
    return stem.replace("-", " ").replace("_", " ").strip()


def extract(url: str, pdf: PdfText) -> list[Fact]:
    facts = [
        Fact("document", url, "file_name_title", file_title(url), "file name"),
        Fact("document", url, "page_count", len(pdf.pages), "pdf structure"),
        Fact("document", url, "has_text_layer", pdf.has_text_layer, "pdf text layer"),
    ]
    title = (pdf.metadata.get("title") or "").strip()
    if title:
        facts.append(Fact("document", url, "pdf_title", title, "pdf metadata: title"))
    for meta_key, field in (("creationDate", "pdf_created"), ("modDate", "pdf_modified")):
        date = pdf_date(pdf.metadata.get(meta_key))
        if date:
            facts.append(Fact("document", url, field, date, f"pdf metadata: {meta_key}"))
    if pdf.pages and pdf.pages[0].strip():
        first = " ".join(pdf.pages[0].split())[:300]
        facts.append(Fact("document", url, "first_page_excerpt", first, "page 1", excerpt=first))
    return facts
