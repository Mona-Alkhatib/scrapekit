"""Works on any HTML page: title, description, headings, and main text. The fallback for every source."""

from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser

from . import ExtractionError, Fact

MAX_TEXT_CHARS = 20_000

# What each field means, shown in the UI's data dictionary.
FIELD_DOCS = {
    "page.title": "The page's <title> tag.",
    "page.description": "The page's meta description, a one-line summary written by the site.",
    "page.headings": "The page's h1 and h2 headings, in order (up to 30).",
    "page.text": "Main readable text of the page (inside <main>, without menus and footers), up to 20,000 characters.",
}


def page_key(url: str) -> str:
    return urlparse(url).path.strip("/") or "home"


def main_text(tree: LexborHTMLParser) -> str:
    root = tree.css_first("main") or tree.body
    if root is None:
        return ""
    for node in root.css("script, style, nav, footer, header, noscript"):
        node.decompose()
    return " ".join(root.text(separator=" ").split())[:MAX_TEXT_CHARS]


def extract(url: str, html: bytes, options=None) -> list[Fact]:  # needs no options
    try:
        tree = LexborHTMLParser(html.decode("utf-8", errors="replace"))
    except Exception as exc:  # lexbor raises plain errors on broken input
        raise ExtractionError(f"html parse failed: {exc}") from exc

    key = page_key(url)
    facts = []
    title = tree.css_first("title")
    if title and title.text(strip=True):
        facts.append(Fact("page", key, "title", title.text(strip=True), "<title>"))
    desc = tree.css_first('meta[name="description"]')
    if desc and desc.attributes.get("content"):
        facts.append(Fact("page", key, "description", desc.attributes["content"].strip(), 'meta[name="description"]'))
    headings = [h.text(strip=True) for h in tree.css("h1, h2") if h.text(strip=True)]
    if headings:
        facts.append(Fact("page", key, "headings", headings[:30], "h1, h2"))
    text = main_text(tree)
    if text:
        facts.append(Fact("page", key, "text", text, "<main> text"))
    return facts
