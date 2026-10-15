"""The UI's query layer, against a small database built with the real Store (no Streamlit, no network)."""

import json
import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from scrapekit.db import Store, connect
from scrapekit.extract import Fact
from scrapekit.ui import queries as q

PAGE = "https://example.org/projects/alpha"
PDF = "https://files.example.net/report.pdf"


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "data" / "scrapekit.db"
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    conn = connect(path)

    s1 = Store(conn, "20260101T000000Z-aaaa")
    s1.start_run("demo", "crawl+extract")
    for url, kind, host in ((PAGE, "page", "example.org"), (PDF, "file", "files.example.net")):
        s1.upsert_document("demo", url, kind, host, None)
    (raw / "page.html").write_text("<html>alpha</html>")
    (raw / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    s1.add_snapshot(PAGE, "h_page1", 18, str(raw / "page.html"))
    s1.add_snapshot(PDF, "h_pdf", 13, str(raw / "report.pdf"))
    s1.record_fetch(PAGE, 200, '"e1"', None, "text/html", "h_page1")
    s1.record_fetch(PDF, 200, None, None, "application/pdf", "h_pdf")
    s1.add_link(PAGE, PDF)
    s1.add_skip("https://social.example.com/x", "off_domain", PAGE)
    s1.add_skip("https://example.org/gone.pdf", "http_404", PAGE)
    s1.apply_facts("demo", PAGE, "h_page1", [
        Fact("project", "alpha", "name", "Alpha Site", "json name"),
        Fact("project", "alpha", "location", {"lat": 43.65, "lon": -79.38}, "json lat/lon"),
        Fact("project", "alpha", "first_published_at", "2024-05-01T10:00:00Z", "page.first_published_at"),
        Fact("project", "alpha", "phase", "Planning", "json phase"),
    ])
    s1.save_pdf_pages("h_pdf", ["Cover page", "The board approved   $2.5 million for Alpha."])
    s1.apply_facts("demo", PDF, "h_pdf", [
        Fact("document", PDF, "llm_key_facts",
             [{"kind": "amount", "value": "$2.5M", "page": 2, "excerpt": "the board approved $2.5 million"}],
             "pages 2", method="llm"),
    ], method="llm")
    s1.finish_run(json.dumps({"requests": 5, "status_counts": {"200": 4, "404": 1},
                              "skips": {"off_domain": 1, "http_404": 1}, "llm_cost_usd": 0.01}))

    s2 = Store(conn, "20260102T000000Z-bbbb")
    s2.start_run("demo", "crawl+extract")
    s2.apply_facts("demo", PAGE, "h_page2", [
        Fact("project", "alpha", "name", "Alpha Site", "json name"),
        Fact("project", "alpha", "location", {"lat": 43.65, "lon": -79.38}, "json lat/lon"),
        Fact("project", "alpha", "first_published_at", "2024-05-01T10:00:00Z", "page.first_published_at"),
        Fact("project", "alpha", "phase", "Construction", "json phase"),
    ])
    s2.finish_run(json.dumps({"requests": 3, "status_counts": {"304": 3}, "skips": {}}))

    s3 = Store(conn, "20260103T000000Z-cccc")
    s3.start_run("demo", "crawl")  # crashed: never finished
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def conn(db):
    return q.open_db(db)


def test_open_db_is_read_only(conn):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM runs")


def test_runs_newest_first_with_status(conn):
    rows = q.runs(conn, "demo")
    assert [r["run_id"][-4:] for r in rows] == ["cccc", "bbbb", "aaaa"]
    assert [r["status"] for r in rows] == ["incomplete", "ok", "ok, with failures"]
    assert rows[1]["not_modified"] == 3 and rows[2]["requests"] == 5


def test_run_status_incomplete():
    assert q.run_status(None, {}) == "incomplete"
    assert q.run_status("2026-01-01", {"skips": {"off_domain": 9}}) == "ok"
    assert q.run_status("2026-01-01", {"skips": {"parse_error": 1}}) == "ok, with failures"


def test_skips_filter_and_counts(conn):
    assert {r["reason"]: r["n"] for r in q.skip_counts(conn, "demo")} == {"off_domain": 1, "http_404": 1}
    assert [r["reason"] for r in q.skips(conn, "demo", reason="http_404")] == ["http_404"]


def test_search_is_literal(conn):
    for text in ("%", "_", "'", "100%_'x"):
        assert q.skips(conn, "demo", search=text) == []
        assert q.entities(conn, "demo", search=text) == []


def test_run_fact_changes(conn):
    changes = {(c["field"], c["change"]) for c in q.run_fact_changes(conn, "20260102T000000Z-bbbb")}
    assert changes == {("phase", "updated")}


def test_entities_and_history(conn):
    ents = q.entities(conn, "demo", entity_type="project")
    assert ents[0]["entity_key"] == "alpha" and ents[0]["name"] == "Alpha Site"
    assert q.entities(conn, "demo", search="constr")[0]["entity_key"] == "alpha"
    current = {f["field"]: json.loads(f["value"]) for f in q.entity_facts(conn, "demo", "project", "alpha")}
    assert current == {"name": "Alpha Site", "phase": "Construction", "location": {"lat": 43.65, "lon": -79.38},
                       "first_published_at": "2024-05-01T10:00:00Z"}
    phase_now = [f for f in q.entity_facts(conn, "demo", "project", "alpha") if f["field"] == "phase"][0]
    chain = q.fact_history(conn, phase_now["id"])
    assert [json.loads(f["value"]) for f in chain] == ["Planning", "Construction"]
    assert chain[0]["superseded_by"] == chain[1]["id"]


def test_documents_and_links(conn):
    docs = {d["url"]: d for d in q.documents(conn, "demo")}
    assert docs[PDF]["saved"] == 1 and docs[PDF]["kind"] == "file"
    assert q.links_to(conn, PDF) == [PAGE] and q.links_from(conn, PAGE) == [PDF]
    assert q.snapshots(conn, PAGE)[0]["content_hash"] == "h_page1"


def test_pdf_page_and_raw_path(conn):
    assert "approved" in q.pdf_page_text(conn, "h_pdf", 2)
    assert q.raw_path(conn, "h_pdf").name == "report.pdf"
    assert {f["field"] for f in q.facts(conn, "demo", source_url=PDF)} == {"llm_key_facts"}


def test_raw_path_falls_back_to_db_raw_dir(conn, db, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)  # stored absolute paths still work; a relative one would use raw_dir
    assert q.raw_path(conn, "h_pdf", raw_dir=db.parent / "raw") is not None


def test_raw_path_missing(conn, db):
    (db.parent / "raw" / "report.pdf").unlink()
    assert q.raw_path(conn, "h_pdf") is None
    assert q.raw_path(conn, "nope") is None


def test_find_excerpt_loose():
    text = "Cover\nThe Board approved   $2.5 million for Alpha."
    start, end = q.find_excerpt(text, "the board approved $2.5 million")
    assert text[start:end] == "Board approved   $2.5 million" or text[start:end].lower().startswith("the board")
    assert q.find_excerpt(text, "nothing like this") is None


PAGES = ["overview", "runs", "skips", "entities", "documents", "facts"]
UI = Path(__file__).parent.parent / "src" / "scrapekit" / "ui"


def render(page: str, db_path: Path) -> AppTest:
    os.environ["SCRAPEKIT_UI_DB"] = str(db_path)
    at = AppTest.from_file(str(UI / "views" / f"{page}.py"), default_timeout=30)
    return at.run()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page, db):
    at = render(page, db)
    assert not at.exception


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_on_empty_db(page, tmp_path):
    empty = tmp_path / "empty.db"
    connect(empty).close()
    at = render(page, empty)
    assert not at.exception
    assert any("No runs" in m.value for m in at.info)


def test_selecting_a_run_shows_its_details(db):
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "runs.py"), default_timeout=30)
    at.session_state["runs"] = {"selection": {"rows": [2], "columns": [], "cells": []}}  # oldest run
    at.run()
    assert not at.exception
    assert at.subheader[0].value == "20260101T000000Z-aaaa"
    assert {m.label: m.value for m in at.metric}["Status"] == "ok, with failures"


def test_pdf_fact_shows_its_page_with_the_quote_highlighted(db):
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "entities.py"), default_timeout=30)
    at.session_state["entities"] = {"selection": {"rows": [0], "columns": [], "cells": []}}  # the PDF document
    at.session_state["entity_facts"] = {"selection": {"rows": [0], "columns": [], "cells": []}}  # llm_key_facts
    at.run()
    assert not at.exception
    shown = " ".join(h.proto.body for h in at.get("html"))
    assert "<mark>The board approved   $2.5 million</mark>" in shown


def test_tables_show_plain_values_and_integer_ids():
    from scrapekit.ui.common import _readable

    assert _readable({"value": '"Completed"'})["value"] == "Completed"
    assert _readable({"value": '{"lat": 1.5}'})["value"] == '{"lat": 1.5}'
    assert _readable({"value": "not json"})["value"] == "not json"


def select(at: AppTest, key: str, row: int) -> None:
    at.session_state[key] = {"selection": {"rows": [row], "columns": [], "cells": []}}


def test_document_shows_versions_raw_copy_and_facts(db):
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "documents.py"), default_timeout=30)
    select(at, "documents", 0)  # sorted by kind then url: the PDF file comes first
    at.run()
    assert not at.exception
    assert at.subheader[0].value == PDF
    assert at.get("download_button")  # raw copy found on disk
    assert any(s["content_hash"] == "h_pdf" for s in at.dataframe[1].value.to_dict("records"))


def test_document_raw_copy_missing_is_explained(db):
    (db.parent / "raw" / "report.pdf").unlink()
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "documents.py"), default_timeout=30)
    select(at, "documents", 0)
    at.run()
    assert not at.exception
    assert any("not on this machine" in c.value for c in at.caption)


def test_facts_filter_by_method(db):
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "facts.py"), default_timeout=30).run()
    at.selectbox[2].set_value("llm").run()
    assert not at.exception
    assert set(at.dataframe[0].value["method"]) == {"llm"}


def test_no_streamlit_pages_folder():
    """A folder named pages/ next to app.py turns on Streamlit's automatic pages, which bypass app.py's sidebar."""
    assert not (UI / "pages").exists()


def test_changing_a_filter_clears_the_selected_row(db):
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "entities.py"), default_timeout=30)
    select(at, "entities", 0)  # the PDF document
    select(at, "entity_facts", 0)
    at.run()
    assert at.subheader[0].value != "Alpha Site"
    at.selectbox[0].set_value("project").run()  # row 0 is now a different entity
    assert not at.exception
    assert not at.subheader  # selection cleared, not silently moved to another entity
    assert not any("** of `" in m.value for m in at.markdown)


def test_run_changes_query_has_no_correlated_subquery(conn):
    seen = []
    conn.set_trace_callback(seen.append)
    q.run_fact_changes(conn, "20260102T000000Z-bbbb")
    conn.set_trace_callback(None)
    plans = [" ".join(str(tuple(r)) for r in conn.execute("EXPLAIN QUERY PLAN " + sql)) for sql in seen if sql.lstrip().upper().startswith("SELECT")]
    assert plans and not any("CORRELATED" in p for p in plans)


def test_removed_facts_are_not_blamed_on_a_crashed_run(db):
    rw = connect(db)
    later = Store(rw, "20260104T000000Z-dddd")
    later.start_run("demo", "extract")
    later.apply_facts("demo", PAGE, "h_page3", [  # name removed
        Fact("project", "alpha", "phase", "Construction", "json phase"),
        Fact("project", "alpha", "location", {"lat": 43.65, "lon": -79.38}, "json lat/lon"),
        Fact("project", "alpha", "first_published_at", "2024-05-01T10:00:00Z", "page.first_published_at"),
    ])
    later.finish_run("{}")
    rw.commit()
    rw.close()
    conn = q.open_db(db)
    assert [c["field"] for c in q.run_fact_changes(conn, "20260103T000000Z-cccc") if c["change"] == "removed"] == []
    assert [c["field"] for c in q.run_fact_changes(conn, "20260104T000000Z-dddd") if c["change"] == "removed"] == ["name"]


def test_pdf_page_text_is_rendered_as_html_not_markdown(db):
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(UI / "views" / "entities.py"), default_timeout=30)
    select(at, "entities", 0)
    select(at, "entity_facts", 0)
    at.run()
    assert not at.exception
    assert any("<mark>" in h.proto.body for h in at.get("html"))


def test_overview_numbers(conn):
    o = q.overview(conn, "demo")
    assert (o["entities"], o["pages"], o["files"], o["skips"]) == (2, 1, 1, 2)
    assert o["llm_cost_usd"] == 0.01


def test_overview_charts_data(conn):
    assert {(r["entity_type"], r["method"]): r["n"] for r in q.facts_by_type_method(conn, "demo")} == {
        ("project", "parse"): 4, ("document", "llm"): 1}
    assert q.label_counts(conn, "demo", "project", "phase") == [{"value": "Construction", "n": 1}]
    assert q.files_per_host(conn, "demo") == [{"host": "files.example.net", "n": 1}]
    assert q.entities_per_year(conn, "demo") == [{"year": "2024", "entity_type": "project", "n": 1}]
    assert q.locations(conn, "demo") == [{"entity_type": "project", "entity_key": "alpha", "name": "Alpha Site",
                                          "lat": 43.65, "lon": -79.38}]

