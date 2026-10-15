"""End-to-end crawl against a fake site served by respx (no network)."""

from dataclasses import replace

import httpx
import pytest
import respx

from scrapekit.cli import crawl
from scrapekit.config import Settings
from scrapekit.db import connect
from scrapekit.links import classify, extract_links, normalize
from scrapekit.sources import CREATETO

HOME = b"""<html><body>
<a href="/projects/a">A</a>
<a href="/projects/a#top">A again</a>
<a href="mailto:info@createto.ca">mail</a>
<a href="https://twitter.com/createto">tw</a>
<a href="/system/debug">debug</a>
<script>{"file":"https://a-us.storyblok.com/f/1/report.pdf"}</script>
</body></html>"""
PROJECT_A = b'<html><body><a href="/missing">gone</a><a href="https://www.toronto.ca/x.pdf">doc</a></body></html>'
SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://createto.ca/projects/a</loc></url></urlset>"""


def mock_site(router: respx.Router, project_body: bytes = PROJECT_A) -> None:
    router.get("https://createto.ca/robots.txt").respond(200, text="User-agent: *\nAllow: /\n")
    router.get("https://a-us.storyblok.com/robots.txt").respond(404)
    router.get("https://www.toronto.ca/robots.txt").respond(200, text="User-agent: *\nDisallow: /x.pdf\n")
    router.get("https://createto.ca/sitemap.xml").respond(200, content=SITEMAP)
    router.get("https://createto.ca/").respond(200, content=HOME, headers={"content-type": "text/html", "etag": '"h1"'})

    def project(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"a1"' and project_body == PROJECT_A:
            return httpx.Response(304)
        return httpx.Response(200, content=project_body, headers={"content-type": "text/html", "etag": '"a1"'})

    router.get("https://createto.ca/projects/a").mock(side_effect=project)
    router.get("https://createto.ca/missing").respond(404)
    router.get("https://a-us.storyblok.com/f/1/report.pdf").respond(
        200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"}
    )


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), data_dir=tmp_path / "data", runs_dir=tmp_path / "runs", seconds_between_requests=0)


def test_normalize_and_classify():
    s = CREATETO
    assert normalize("/projects/a/#x", "https://createto.ca/") == "https://createto.ca/projects/a"
    assert normalize("mailto:a@b.c", "https://createto.ca/") is None
    assert classify("https://other.org/file.PDF", s) == ("file", None)
    assert classify("https://other.org/page", s) == (None, "off_domain")
    assert classify("https://createto.ca/system/debug", s) == (None, "excluded_path")


def test_extract_links_finds_files_inside_json():
    links, rejected = extract_links(HOME, "https://createto.ca/", CREATETO)
    assert "https://a-us.storyblok.com/f/1/report.pdf" in links
    assert "mailto:info@createto.ca" in rejected


@respx.mock
async def test_first_run_fetches_and_logs_skips(settings):
    mock_site(respx.mock)
    stats = await crawl(settings, CREATETO)

    assert stats.pages_fetched == 2
    assert stats.files_fetched == 1
    assert stats.skips["http_404"] == 1
    assert stats.skips["robots_disallowed"] == 1
    assert stats.skips["off_domain"] == 1
    assert stats.skips["excluded_path"] == 1
    assert stats.skips["non_http_link"] == 1

    db = connect(settings.db_path)
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3
    assert (settings.runs_dir / f"{stats.run_id}.md").exists()


@respx.mock
async def test_repeat_run_uses_conditional_get_and_detects_change(settings):
    mock_site(respx.mock)
    await crawl(settings, CREATETO)

    stats = await crawl(settings, CREATETO)  # unchanged repeat
    assert stats.pages_unchanged == 2  # one via 304, one via same hash
    assert stats.pages_changed == 0

    respx.mock.reset()
    mock_site(respx.mock, project_body=PROJECT_A + b"<p>amended</p>")
    stats = await crawl(settings, CREATETO)
    assert stats.pages_changed == 1

    db = connect(settings.db_path)
    versions = db.execute("SELECT COUNT(*) FROM snapshots WHERE url = 'https://createto.ca/projects/a'").fetchone()[0]
    assert versions == 2


@respx.mock
async def test_a_new_source_needs_only_a_config(settings):
    """Adding a site is a Source object. No crawler code changes."""
    from scrapekit.sources import Source

    other = Source(name="example", start_urls=("https://example.gov/",), crawl_hosts=("example.gov",))
    respx.get("https://example.gov/robots.txt").respond(404)
    respx.get("https://example.gov/").respond(
        200, content=b'<a href="/minutes.pdf">m</a><a href="https://createto.ca/">x</a>',
        headers={"content-type": "text/html"},
    )
    respx.get("https://example.gov/minutes.pdf").respond(200, content=b"%PDF-1.4")

    stats = await crawl(settings, other)
    assert (stats.pages_fetched, stats.files_fetched, stats.skips["off_domain"]) == (1, 1, 1)


def test_alias_hosts_collapse_to_one_address():
    from scrapekit.links import canonicalize

    assert canonicalize("http://www.createto.ca/projects/a", CREATETO) == "https://createto.ca/projects/a"
    assert canonicalize("https://www.toronto.ca/x.pdf", CREATETO) == "https://www.toronto.ca/x.pdf"


@respx.mock
async def test_immutable_files_are_not_rechecked(settings):
    pdf_url = "https://a-us.storyblok.com/f/1017201/x/e26c0e2362/notice.pdf"
    respx.get("https://createto.ca/robots.txt").respond(404)
    respx.get("https://a-us.storyblok.com/robots.txt").respond(404)
    respx.get("https://createto.ca/sitemap.xml").respond(404)
    respx.get("https://createto.ca/").respond(200, content=f'<a href="{pdf_url}">n</a>'.encode(),
                                               headers={"content-type": "text/html"})
    pdf_route = respx.get(pdf_url).respond(200, content=b"%PDF-1.4 x")

    await crawl(settings, CREATETO)
    stats = await crawl(settings, CREATETO)
    assert pdf_route.call_count == 1  # second run made no request for it
    assert stats.files_not_rechecked == 1

    stats = await crawl(replace(settings, revalidate_all=True), CREATETO)
    assert pdf_route.call_count == 2


@respx.mock
async def test_robots_txt_fetched_once_per_host(settings):
    mock_site(respx.mock)
    await crawl(settings, CREATETO)
    robots = [c for c in respx.mock.calls if c.request.url.path == "/robots.txt" and c.request.url.host == "createto.ca"]
    assert len(robots) == 1
