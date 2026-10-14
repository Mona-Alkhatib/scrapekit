from .base import Source

CREATETO = Source(
    name="createto",
    start_urls=("https://createto.ca/",),
    crawl_hosts=("createto.ca", "www.createto.ca"),
    sitemap_urls=("https://createto.ca/sitemap.xml",),
    excluded_path_prefixes=("/system/", "/search"),
    page_extractor="storyblok",
    # Storyblok asset URLs contain a unique id per upload (/f/<space>/<size>/<id>/<name>).
    immutable_url_patterns=(r"^https://a-us\.storyblok\.com/f/\d+/[^/]+/[0-9a-f]{10}/",),
    notes=(
        "Next.js + Storyblok CMS, server rendered. Pages embed __NEXT_DATA__ JSON with structured fields. "
        "Sends ETags. Sitemap lastmod is the build time, so not useful for change detection. "
        "Files mostly on a-us.storyblok.com, some on toronto.ca."
    ),
    tags=("toronto", "real-estate", "municipal-agency"),
)
