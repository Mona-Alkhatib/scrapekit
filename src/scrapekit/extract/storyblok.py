"""Extractor for Next.js sites backed by the Storyblok CMS (createto.ca is one).

These pages embed the full CMS record as JSON in <script id="__NEXT_DATA__">, so fields
like phase, ward and dates can be read exactly, with no guessing and no AI. Named after
the technology, not the site, so any other Storyblok site can reuse it.
"""

import json
import re

from . import ExtractionError, Fact, generic_html

NEXT_DATA_RE = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

PROJECT_FIELDS = {
    # Storyblok field -> our field name
    "title": "name",
    "summary": "summary",
    "address": "address",
    "ward": "ward",
    "councillorName": "councillor",
    "majorIntersection": "major_intersection",
    "siteArea": "site_area",
    "currentUse": "current_use",
    "proposedUse": "proposed_use",
    "surroundingUse": "surrounding_use",
    "delivery": "delivery",
}

NEWS_FIELDS = {
    "title": "title",
    "eventStartsAt": "event_starts_at",
    "eventEndsAt": "event_ends_at",
    "externalUrl": "external_url",
    "externalSource": "external_source",
}


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


def extract(url: str, html: bytes) -> list[Fact]:
    data = load_next_data(html)
    if data is None:
        return generic_html.extract(url, html)
    props = data.get("props", {}).get("pageProps", {})
    page = props.get("page") or {}
    content = page.get("content") or {}
    component = content.get("component")

    if component == "project":
        return project_facts(page, content, props)
    if component == "article":
        return news_facts(page, content)
    return generic_html.extract(url, html)  # static pages: about, policies, listings


def common_facts(kind: str, key: str, page: dict, content: dict) -> list[Fact]:
    facts = []
    for src, dst in (("first_published_at", "first_published_at"), ("updated_at", "cms_updated_at")):
        if not _blank(page.get(src)):
            facts.append(Fact(kind, key, dst, page[src], _loc(f"page.{src}")))
    body = richtext(content.get("body")).strip()
    if body:
        facts.append(Fact(kind, key, "body_text", body, _loc("page.content.body")))
    return facts


def project_facts(page: dict, content: dict, props: dict) -> list[Fact]:
    key = page.get("slug") or page.get("full_slug", "")
    facts = [Fact("project", key, "url_slug", page.get("full_slug"), _loc("page.full_slug"))]
    for src, dst in PROJECT_FIELDS.items():
        if not _blank(content.get(src)):
            facts.append(Fact("project", key, dst, _clean(content[src]), _loc(f"page.content.{src}")))

    if _blank(content.get("title")) and not _blank(page.get("name")):
        facts.append(Fact("project", key, "name", _clean(page["name"]), _loc("page.name")))

    phases = props.get("projectPhases") or {}
    if content.get("phase"):
        facts.append(Fact("project", key, "phase", phases.get(content["phase"], content["phase"]), _loc("page.content.phase")))

    types = props.get("projectTypes") or {}
    if content.get("type"):
        facts.append(Fact("project", key, "types", [types.get(t, t) for t in content["type"]], _loc("page.content.type")))

    try:
        lat, lon = float(content.get("latitude")), float(content.get("longitude"))
        facts.append(Fact("project", key, "location", {"lat": lat, "lon": lon}, _loc("page.content.latitude/longitude")))
    except (TypeError, ValueError):
        pass

    timeline = [
        {"phase": phases.get(s.get("phase"), s.get("phase")), "schedule": s.get("schedule"), "active": s.get("active")}
        for s in content.get("timeline") or []
    ]
    if timeline:
        facts.append(Fact("project", key, "timeline", timeline, _loc("page.content.timeline")))

    initiatives = [i.get("name") for i in content.get("initiatives") or [] if isinstance(i, dict) and i.get("name")]
    if initiatives:
        facts.append(Fact("project", key, "initiatives", initiatives, _loc("page.content.initiatives")))

    return facts + common_facts("project", key, page, content)


def news_facts(page: dict, content: dict) -> list[Fact]:
    key = page.get("slug") or page.get("full_slug", "")
    facts = [Fact("news", key, "url_slug", page.get("full_slug"), _loc("page.full_slug"))]
    for src, dst in NEWS_FIELDS.items():
        if not _blank(content.get(src)):
            facts.append(Fact("news", key, dst, _clean(content[src]), _loc(f"page.content.{src}")))

    if _blank(content.get("title")) and not _blank(page.get("name")):
        # Older posts keep the headline only in the CMS story name.
        facts.append(Fact("news", key, "title", _clean(page["name"]), _loc("page.name")))

    topics = [t.get("name") for t in content.get("topics") or [] if isinstance(t, dict) and t.get("name")]
    if topics:
        facts.append(Fact("news", key, "topics", topics, _loc("page.content.topics")))

    related = []
    for p in content.get("relatedProjects") or []:
        related.append(p.get("slug") or p.get("name") if isinstance(p, dict) else p)
    if related:
        facts.append(Fact("news", key, "related_projects", related, _loc("page.content.relatedProjects")))

    return facts + common_facts("news", key, page, content)
