from ..extract.storyblok import Component, ObjectList, StoryblokModel
from .base import Source

# CreateTO's CMS content model: which Storyblok components we read, and how.
CREATETO_MODEL = StoryblokModel(components={
    "project": Component(
        entity_type="project",
        fields={
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
        },
        name_fallback=("title", "name"),
        labels={"phase": ("projectPhases", "phase"), "type": ("projectTypes", "types")},
        location=("latitude", "longitude"),
        object_lists={"timeline": ObjectList("timeline", keep=("phase", "schedule", "active"),
                                             labels={"phase": "projectPhases"})},
        name_lists={"initiatives": "initiatives"},
    ),
    "article": Component(
        entity_type="news",
        fields={
            "title": "title",
            "eventStartsAt": "event_starts_at",
            "eventEndsAt": "event_ends_at",
            "externalUrl": "external_url",
            "externalSource": "external_source",
        },
        name_fallback=("title", "title"),
        name_lists={"topics": "topics"},
        ref_lists={"relatedProjects": "related_projects"},
    ),
})

CREATETO = Source(
    name="createto",
    start_urls=("https://createto.ca/",),
    crawl_hosts=("createto.ca", "www.createto.ca"),
    sitemap_urls=("https://createto.ca/sitemap.xml",),
    excluded_path_prefixes=("/system/", "/search"),
    page_extractor="storyblok",
    extractor_options=CREATETO_MODEL,
    publisher="CreateTO, the City of Toronto's real estate agency",
    publisher_body_example="CreateTO Board of Directors",
    llm_focus=(
        "Prefer facts a researcher tracking city land and development would want: property addresses, project names,\n"
        "board or committee decisions, dollar amounts, key dates, and project phases."
    ),
    # Storyblok asset URLs contain a unique id per upload (/f/<space>/<size>/<id>/<name>).
    immutable_url_patterns=(r"^https://a-us\.storyblok\.com/f/\d+/[^/]+/[0-9a-f]{10}/",),
    notes=(
        "Next.js + Storyblok CMS, server rendered. Pages embed __NEXT_DATA__ JSON with structured fields. "
        "Sends ETags. Sitemap lastmod is the build time, so not useful for change detection. "
        "Files mostly on a-us.storyblok.com, some on toronto.ca."
    ),
    tags=("toronto", "real-estate", "municipal-agency"),
)
