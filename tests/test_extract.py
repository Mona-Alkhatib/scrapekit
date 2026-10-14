"""Extraction and fact history, using small handmade pages and PDFs (no network)."""

import json
from dataclasses import replace

import pymupdf
import pytest
import respx

from scrapekit.cli import run_pipeline
from scrapekit.config import Settings
from scrapekit.db import connect
from scrapekit.extract import storyblok
from scrapekit.extract.pdf import pdf_date, read_pdf
from scrapekit.sources import CREATETO


def project_page(phase: str = "leasing") -> bytes:
    data = {
        "props": {
            "pageProps": {
                "projectPhases": {"leasing": "Leasing", "completed": "Completed"},
                "projectTypes": {"commercial": "Commercial"},
                "page": {
                    "slug": "south-river",
                    "full_slug": "projects/south-river",
                    "first_published_at": "2025-11-21T14:59:48.043Z",
                    "content": {
                        "component": "project",
                        "title": "South River",
                        "ward": "Toronto-Danforth",
                        "phase": phase,
                        "type": ["commercial"],
                        "latitude": "43.64",
                        "longitude": "-79.35",
                        "siteArea": "18.5 acres",
                        "address": "",
                        "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "A prime site."}]}]},
                    },
                },
            }
        }
    }
    script = json.dumps(data)
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{script}</script></body></html>'.encode()


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_storyblok_project_fields():
    facts = {f.field: f for f in storyblok.extract("https://createto.ca/projects/south-river", project_page())}
    assert facts["phase"].value == "Leasing"  # code mapped to its label
    assert facts["ward"].value == "Toronto-Danforth"
    assert facts["types"].value == ["Commercial"]
    assert facts["location"].value == {"lat": 43.64, "lon": -79.35}
    assert facts["body_text"].value == "A prime site."
    assert "address" not in facts  # blank fields are not facts
    assert facts["phase"].locator == "__NEXT_DATA__ page.content.phase"


def test_pdf_pages_are_one_based_and_dates_parse():
    text = read_pdf(make_pdf(["Board meeting agenda", "Item 2: 300 Commissioners Street"]))
    assert len(text.pages) == 2 and "Commissioners" in text.pages[1]
    assert pdf_date("D:20240917103000-04'00'") == "2024-09-17"


def mock_small_site(router, phase="leasing", pdf_body=None):
    router.get("https://createto.ca/robots.txt").respond(404)
    router.get("https://a-us.storyblok.com/robots.txt").respond(404)
    router.get("https://createto.ca/sitemap.xml").respond(404)
    home = b'<a href="/projects/south-river">p</a><a href="https://a-us.storyblok.com/f/1/agenda.pdf">a</a>'
    router.get("https://createto.ca/").respond(200, content=home, headers={"content-type": "text/html"})
    router.get("https://createto.ca/projects/south-river").respond(
        200, content=project_page(phase), headers={"content-type": "text/html"}
    )
    router.get("https://a-us.storyblok.com/f/1/agenda.pdf").respond(
        200, content=pdf_body or make_pdf(["Agenda", "Page two"]), headers={"content-type": "application/pdf"}
    )


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), data_dir=tmp_path / "data", runs_dir=tmp_path / "runs", seconds_between_requests=0)


@respx.mock
async def test_unchanged_repeat_skips_extraction(settings):
    mock_small_site(respx.mock)
    first = await run_pipeline(settings, CREATETO)
    assert first.docs_extracted == 3 and first.records_new > 0

    second = await run_pipeline(settings, CREATETO)
    assert second.docs_extracted == 0
    assert second.docs_extract_cached == 3
    assert second.records_new == second.records_updated == 0


@respx.mock
async def test_changed_value_supersedes_old_fact(settings):
    mock_small_site(respx.mock, phase="leasing")
    await run_pipeline(settings, CREATETO)
    respx.mock.reset()
    mock_small_site(respx.mock, phase="completed")
    stats = await run_pipeline(settings, CREATETO)
    assert stats.records_updated == 1

    db = connect(settings.db_path)
    rows = db.execute(
        "SELECT value, valid_to, superseded_by FROM facts WHERE field = 'phase' ORDER BY id"
    ).fetchall()
    assert [json.loads(r["value"]) for r in rows] == ["Leasing", "Completed"]
    assert rows[0]["valid_to"] is not None and rows[0]["superseded_by"] is not None
    assert rows[1]["valid_to"] is None


@respx.mock
async def test_corrupt_pdf_is_logged_not_fatal(settings):
    mock_small_site(respx.mock, pdf_body=b"%PDF-1.4 this is not really a pdf")
    stats = await run_pipeline(settings, CREATETO)
    assert stats.docs_extract_failed == 1
    assert stats.skips["parse_error"] == 1
    assert stats.docs_extracted == 2  # the two pages still worked
