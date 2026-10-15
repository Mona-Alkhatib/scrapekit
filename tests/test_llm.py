"""LLM pass with a fake model: no network, no cost."""

from dataclasses import replace

import pytest
import respx

from scrapekit.cli import run_pipeline
from scrapekit.config import Settings
from scrapekit.db import connect
from scrapekit.llm import LlmResult, build_document_text, excerpt_on_page
from scrapekit.sources import CREATETO

from test_extract import make_pdf, mock_small_site


class FakeLLM:
    model = "fake/model"
    cache_key_suffix = "fake/model|v1"

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    async def extract(self, url, pages, prompt, schema):
        self.last_prompt = prompt
        self.calls += 1
        if self.fail:
            raise RuntimeError("503 from provider")
        return LlmResult(
            data={
                "doc_type": "board_agenda", "title": "Agenda", "document_date": "2026-11-10",
                "organization": "CreateTO Board of Directors", "summary": "Agenda for the board.",
                "facts": [
                    {"kind": "address", "value": "300 Commissioners Street", "page": 2,
                     "excerpt": "Item 2: 300 Commissioners Street"},
                    {"kind": "amount", "value": "$5M", "page": 1, "excerpt": "approved $5 million"},  # not in the PDF
                ],
            },
            model=self.model, input_tokens=1000, output_tokens=100, cost_usd=0.0004, truncated=False,
            pages_sent=list(range(1, len(pages) + 1)),
        )

    async def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), data_dir=tmp_path / "data", runs_dir=tmp_path / "runs", seconds_between_requests=0)


def test_excerpt_check_ignores_case_and_spacing():
    assert excerpt_on_page("ITEM 2:  300 commissioners", "Agenda\nItem 2: 300 Commissioners Street")
    assert not excerpt_on_page("approved $5 million", "Agenda\nItem 2")


def test_excerpt_check_survives_broken_numbers_and_page_breaks():
    from scrapekit.llm import verify_facts

    pages = ["... a 50 0,000-square foot film hub", "Units rented at an average of", "80 per cent of market rent."]
    facts = [
        {"page": 1, "excerpt": "a 500,000-square foot film hub"},
        {"page": 3, "excerpt": "rented at an average of 80 per cent of market rent"},
        {"page": 2, "excerpt": "a million dollar subsidy"},
    ]
    good, bad = verify_facts(facts, pages)
    assert len(good) == 2 and bad == [facts[2]]


def test_excerpt_may_skip_a_page_footer_but_not_invent_text():
    page = "The units will be rented at an average of Meeting Summary 3 80 per cent of market rent."
    assert excerpt_on_page("The units will be rented at an average of 80 per cent of market rent.", page)
    assert not excerpt_on_page("The units will be rented at an average of 50 per cent of market rent.", page)


def test_page_tags_and_truncation():
    text, truncated, sent = build_document_text(["one", "two"])
    assert text.startswith("[page 1]\none") and "[page 2]" in text and not truncated and sent == [1, 2]
    _, truncated, sent = build_document_text(["x" * 50_000])
    assert truncated and sent == [1]


def test_long_documents_keep_start_and_end():
    pages = [f"page {i} " + "x" * 3000 for i in range(1, 31)] + ["AMENDMENT: meeting moved to December 15"]
    text, truncated, sent = build_document_text(pages)
    assert truncated and sent[0] == 1 and sent[-1] == 31
    assert "AMENDMENT" in text and "omitted]" in text


@respx.mock
async def test_llm_facts_are_verified_cached_and_cost_counted(settings):
    pdf = make_pdf(["Agenda", "Item 2: 300 Commissioners Street"])
    mock_small_site(respx.mock, pdf_body=pdf)
    llm = FakeLLM()
    first = await run_pipeline(settings, CREATETO, llm=llm)
    assert (first.llm_calls, first.llm_facts_kept, first.llm_facts_dropped) == (1, 1, 1)
    assert first.llm_cost_usd == pytest.approx(0.0004)

    db = connect(settings.db_path)
    row = db.execute("SELECT value, locator FROM facts WHERE field = 'llm_key_facts'").fetchone()
    assert "300 Commissioners Street" in row["value"] and "$5M" not in row["value"]
    assert row["locator"] == "pages 2"

    second = await run_pipeline(settings, CREATETO, llm=llm)  # unchanged repeat
    assert second.llm_calls == 0 and second.llm_cost_usd == 0
    assert llm.calls == 1


@respx.mock
async def test_llm_error_is_logged_not_fatal(settings):
    mock_small_site(respx.mock, pdf_body=make_pdf(["Board meeting agenda for November 2026"]))
    stats = await run_pipeline(settings, CREATETO, llm=FakeLLM(fail=True))
    assert stats.llm_failed == 1 and stats.skips["llm_error"] == 1
    assert stats.docs_extracted == 3  # rule-based facts still saved


@respx.mock
async def test_budget_stops_new_calls(settings):
    mock_small_site(respx.mock, pdf_body=make_pdf(["Board meeting agenda for November 2026"]))
    llm = FakeLLM()
    stats = await run_pipeline(settings, CREATETO, llm=llm, llm_budget=0.0)
    assert llm.calls == 0 and stats.llm_cost_usd == 0
    assert stats.skips["llm_budget_reached"] == 1

    stats = await run_pipeline(settings, CREATETO, llm=llm)  # picked up next run
    assert llm.calls == 1


@respx.mock
async def test_limit_counts_only_paid_calls(settings):
    mock_small_site(respx.mock, pdf_body=make_pdf(["Board meeting agenda for November 2026"]))
    llm = FakeLLM()
    await run_pipeline(settings, CREATETO, llm=llm)
    stats = await run_pipeline(settings, CREATETO, llm=llm, llm_limit=0)  # unchanged: nothing paid
    assert llm.calls == 1 and stats.llm_calls == 0


def test_prompt_and_schema_come_from_the_source():
    from scrapekit.llm import response_schema, system_prompt
    from scrapekit.sources import Source

    other = Source(name="example", start_urls=("https://example.gov/",), crawl_hosts=("example.gov",),
                   publisher="the Example County Council", publisher_body_example="Planning Committee")
    prompt = system_prompt(other.publisher, other.llm_focus)
    assert "published by the Example County Council." in prompt and "CreateTO" not in prompt
    org = response_schema(other.publisher_body_example)["schema"]["properties"]["organization"]["description"]
    assert org.endswith("Planning Committee")


def test_no_site_names_outside_sources():
    """Everything site-specific lives in sources/. This keeps it that way."""
    import re
    from pathlib import Path

    pattern = re.compile(r"createto\.ca|CreateTO|storyblok\.com|Toronto")
    root = Path(__file__).parent.parent / "src" / "scrapekit"
    offenders = [
        f"{p.relative_to(root)}:{n}"
        for p in root.rglob("*.py") if "sources" not in p.parts
        for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)
    ]
    assert offenders == []
