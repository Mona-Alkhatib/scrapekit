"""Everything specific to createto.ca lives in this file, and only here.

The crawler, extractors and LLM code are generic. They read these settings:
- where to start, which hosts to crawl, which paths to skip   (crawler.py, links.py)
- how the site's CMS data maps to our fields (CREATETO_MODEL)  (extract/storyblok.py)
- who publishes the PDFs and what facts matter                 (llm.py prompt)
- which file URLs never change (so repeat runs skip them)      (crawler.py)

A new website is a new file like this one, plus one line in sources/__init__.py.
"""

from ..extract.storyblok import Component, ObjectList, StoryblokModel
from .base import Source

# CreateTO's CMS content model: which Storyblok components we read, and how.
# Each page embeds its CMS record as JSON; a "project" or "article" component becomes one entity,
# and each mapped CMS field becomes one fact, e.g. content.councillorName -> fact "councillor".
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
        # These CMS fields hold codes; the page ships lookup tables that turn them into labels.
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
    # Where the crawl starts and what it may walk. Files linked from these pages are downloaded
    # from any host; pages are only crawled on crawl_hosts.
    start_urls=("https://createto.ca/",),
    crawl_hosts=("createto.ca", "www.createto.ca"),
    sitemap_urls=("https://createto.ca/sitemap.xml",),
    excluded_path_prefixes=("/system/", "/search"),
    # How to read pages: the Storyblok extractor driven by the model above.
    page_extractor="storyblok",
    extractor_options=CREATETO_MODEL,
    # Filled into the LLM prompt and schema (llm.py), so the prompt itself names no site.
    publisher="CreateTO, the City of Toronto's real estate agency",
    publisher_body_example="CreateTO Board of Directors",
    llm_focus=(
        "Prefer facts a researcher tracking city land and development would want: property addresses, project names,\n"
        "board or committee decisions, dollar amounts, key dates, and project phases."
    ),
    overview_labels=(("project", "phase"), ("project", "ward")),
    ask_examples=(
        "How many projects are in each phase?",
        "Which projects are in Delivery and Construction, with their address and ward?",
        "Which PDFs are board minutes from 2024 or later?",
        "How many projects are in each ward?",
        "Which projects include affordable housing, and what phase is each in?",
        "Which news items mention a project, and which projects?",
        "What are the 10 most recent news items, with their topics?",
        "Which projects have no address on their page?",
    ),
    field_docs={
        "project.name": "Project name as shown on the site.",
        "project.summary": "Short description of the project from the CMS.",
        "project.address": "Street address of the site.",
        "project.ward": "City of Toronto ward the site is in.",
        "project.councillor": "Ward councillor named on the project page.",
        "project.major_intersection": "Nearest major intersection.",
        "project.site_area": "Size of the site, as written on the page (units vary).",
        "project.current_use": "What the site is used for today.",
        "project.proposed_use": "What the site is planned to become.",
        "project.surrounding_use": "What is around the site.",
        "project.delivery": "How the project is delivered (e.g. partner or program), as written on the page.",
        "project.phase": "Current project phase (Pre-development, Development Concept, Market Offering, Leasing, "
                         "Delivery and Construction, Completed ...), translated from the CMS code.",
        "project.types": "Project types (e.g. housing, office), translated from CMS codes.",
        "project.timeline": "The project's phases in order, each {phase, schedule, active}.",
        "project.initiatives": "City programs the project belongs to, e.g. Housing Now.",
        "news.title": "Headline of the news item or meeting notice.",
        "news.event_starts_at": "Start of the event, for meetings and consultations.",
        "news.event_ends_at": "End of the event.",
        "news.external_url": "Link to the original article when the item points to another site.",
        "news.external_source": "Name of that other site, e.g. a newspaper.",
        "news.topics": "Topic tags from the CMS.",
        "news.related_projects": "Projects this item is about (their slugs), to join news to projects.",
    },
    # Storyblok asset URLs contain a unique id per upload (/f/<space>/<size>/<id>/<name>).
    immutable_url_patterns=(r"^https://a-us\.storyblok\.com/f/\d+/[^/]+/[0-9a-f]{10}/",),
    notes=(
        "Next.js + Storyblok CMS, server rendered. Pages embed __NEXT_DATA__ JSON with structured fields. "
        "Sends ETags. Sitemap lastmod is the build time, so not useful for change detection. "
        "Files mostly on a-us.storyblok.com, some on toronto.ca."
    ),
    tags=("toronto", "real-estate", "municipal-agency"),
)
