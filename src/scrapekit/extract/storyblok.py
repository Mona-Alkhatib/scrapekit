"""Extractor for Next.js sites backed by the Storyblok CMS.

These pages embed the full CMS record as JSON in <script id="__NEXT_DATA__">, so fields
can be read exactly, with no guessing and no AI.

This module knows how Storyblok pages are shaped, but nothing about any one site. Each site
describes its own content model (which CMS components become which entities, and which CMS
fields map to which of our fields) in a `StoryblokModel`, set on its Source in `sources/`.

Flow: extract() finds the __NEXT_DATA__ JSON, looks up the page's component in the model,
and component_facts() turns each mapped field into a Fact whose locator is the JSON path
it came from (e.g. "__NEXT_DATA__ page.content.ward"). No match: fall back to generic_html.
"""

import json
import re
from dataclasses import dataclass, field

from . import ExtractionError, Fact, generic_html

NEXT_DATA_RE = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


@dataclass(frozen=True)
class ObjectList:
    """A CMS list of objects kept as a list of small dicts, e.g. a project timeline."""

    field: str  # our field name
    keep: tuple[str, ...]  # keys kept from each object, in order
    labels: dict[str, str] = field(default_factory=dict)  # key -> page lookup table that turns codes into labels


@dataclass(frozen=True)
class Component:
    """How one Storyblok component (content type) becomes one of our entities."""

    entity_type: str  # e.g. "project"
    fields: dict[str, str] = field(default_factory=dict)  # CMS field -> our field, plain text values
    name_fallback: tuple[str, str] | None = None  # (CMS field, our field): if blank, use the story name
    labels: dict[str, tuple[str, str]] = field(default_factory=dict)  # CMS field -> (lookup table, our field)
    location: tuple[str, str] | None = None  # (latitude field, longitude field) -> our "location"
    object_lists: dict[str, ObjectList] = field(default_factory=dict)  # CMS field -> ObjectList
    name_lists: dict[str, str] = field(default_factory=dict)  # CMS list of linked stories -> our field (their names)
    ref_lists: dict[str, str] = field(default_factory=dict)  # CMS list of linked stories -> our field (slug, else name)
    body_field: str = "body"  # rich text kept as body_text


@dataclass(frozen=True)
class StoryblokModel:
    components: dict[str, Component]  # Storyblok component name -> how to read it


def richtext(node) -> str:
    """Flattens Storyblok rich text (a ProseMirror tree) to plain text."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        sep = "\n" if node.get("type") in ("doc", "bullet_list", "ordered_list") else ""
        return sep.join(richtext(c) for c in node.get("content", []) or [])
    if isinstance(node, list):
        return "\n".join(richtext(c) for c in node)
    return ""


def load_next_data(html: bytes) -> dict | None:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc


def _loc(path: str) -> str:
    return f"__NEXT_DATA__ {path}"


def _clean(value) -> str:
    """Collapses line breaks and repeated spaces that CMS editors leave in short fields."""
    return " ".join(str(value).split())


def _blank(value) -> bool:
    return value in (None, "", [], {})


def extract(url: str, html: bytes, model: StoryblokModel | None = None) -> list[Fact]:
    data = load_next_data(html)
    if data is None or model is None:
        return generic_html.extract(url, html)
    props = data.get("props", {}).get("pageProps", {})
    page = props.get("page") or {}
    content = page.get("content") or {}
    component = model.components.get(content.get("component"))  # e.g. "project", "article"
    if component is None:
        return generic_html.extract(url, html)  # pages the model does not describe: about, policies, listings
    return component_facts(component, page, content, props)


def component_facts(c: Component, page: dict, content: dict, props: dict) -> list[Fact]:
    kind = c.entity_type
    key = page.get("slug") or page.get("full_slug", "")
    facts = [Fact(kind, key, "url_slug", page.get("full_slug"), _loc("page.full_slug"))]

    # Plain fields: one CMS field -> one fact, with the JSON path as the locator.
    for src, dst in c.fields.items():
        if not _blank(content.get(src)):
            facts.append(Fact(kind, key, dst, _clean(content[src]), _loc(f"page.content.{src}")))

    if c.name_fallback:
        src, dst = c.name_fallback
        if _blank(content.get(src)) and not _blank(page.get("name")):
            # Older stories may keep the headline only in the CMS story name.
            facts.append(Fact(kind, key, dst, _clean(page["name"]), _loc("page.name")))

    # Coded fields: translate codes to labels using lookup tables shipped in the same page.
    for src, (table, dst) in c.labels.items():
        value = content.get(src)
        if value:
            lookup = props.get(table) or {}
            label = [lookup.get(v, v) for v in value] if isinstance(value, list) else lookup.get(value, value)
            facts.append(Fact(kind, key, dst, label, _loc(f"page.content.{src}")))

    if c.location:
        lat_field, lon_field = c.location
        try:
            lat, lon = float(content.get(lat_field)), float(content.get(lon_field))
            facts.append(Fact(kind, key, "location", {"lat": lat, "lon": lon},
                              _loc(f"page.content.{lat_field}/{lon_field}")))
        except (TypeError, ValueError):
            pass

    for src, spec in c.object_lists.items():
        items = []
        for obj in content.get(src) or []:
            item = {}
            for k in spec.keep:
                value = obj.get(k)
                if k in spec.labels:
                    value = (props.get(spec.labels[k]) or {}).get(value, value)
                item[k] = value
            items.append(item)
        if items:
            facts.append(Fact(kind, key, spec.field, items, _loc(f"page.content.{src}")))

    for src, dst in c.name_lists.items():
        names = [x.get("name") for x in content.get(src) or [] if isinstance(x, dict) and x.get("name")]
        if names:
            facts.append(Fact(kind, key, dst, names, _loc(f"page.content.{src}")))

    for src, dst in c.ref_lists.items():
        refs = [x.get("slug") or x.get("name") if isinstance(x, dict) else x for x in content.get(src) or []]
        if refs:
            facts.append(Fact(kind, key, dst, refs, _loc(f"page.content.{src}")))

    for src, dst in (("first_published_at", "first_published_at"), ("updated_at", "cms_updated_at")):
        if not _blank(page.get(src)):
            facts.append(Fact(kind, key, dst, page[src], _loc(f"page.{src}")))
    body = richtext(content.get(c.body_field)).strip()
    if body:
        facts.append(Fact(kind, key, "body_text", body, _loc(f"page.content.{c.body_field}")))
    return facts
