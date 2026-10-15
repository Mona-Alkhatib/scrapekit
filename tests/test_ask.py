"""Ask AI: question -> one read-only SQL query -> answer. Uses a fake LLM, so no network and no cost."""

import os
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from scrapekit.ui import ask
from test_ui import db  # noqa: F401  (the same small database fixture)


class FakeChat:
    """Returns prepared answers in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def complete(self, messages, schema):
        self.sent.append(messages)
        return self.replies.pop(0), 0.001


GOOD = {"sql": "SELECT entity_key FROM facts WHERE field = 'phase' AND valid_to IS NULL", "explanation": "phase facts"}


def test_question_becomes_query_rows_and_answer(db):  # noqa: F811
    llm = FakeChat(GOOD, {"answer": "One project: alpha."})
    result = ask.Asker(llm, db, docs={}, source="demo").ask("Which projects have a phase?")
    assert result.error is None
    assert result.columns == ["entity_key"] and result.rows == [["alpha"]]
    assert result.answer == "One project: alpha." and result.attempts == 1
    assert result.cost_usd == pytest.approx(0.002)
    assert "phase" in llm.sent[0][0]["content"] or "facts" in llm.sent[0][0]["content"]  # schema is in the prompt


def test_sql_error_is_sent_back_once_and_fixed(db):  # noqa: F811
    llm = FakeChat({"sql": "SELECT nope FROM facts", "explanation": ""}, GOOD, {"answer": "alpha"})
    result = ask.Asker(llm, db, docs={}, source="demo").ask("q")
    assert result.error is None and result.attempts == 2
    assert "no such column: nope" in llm.sent[1][-1]["content"]


def test_gives_up_after_two_bad_queries(db):  # noqa: F811
    bad = {"sql": "SELECT nope FROM facts", "explanation": ""}
    result = ask.Asker(FakeChat(bad, bad), db, docs={}, source="demo").ask("q")
    assert result.attempts == 2 and "no such column" in result.error and result.answer == ""


@pytest.mark.parametrize("sql", ["DELETE FROM runs", "PRAGMA table_info(runs)", "SELECT 1; DROP TABLE runs",
                                 "ATTACH DATABASE 'x.db' AS x", "UPDATE facts SET value = 1"])
def test_only_single_select_statements(sql):
    with pytest.raises(ask.UnsafeSql):
        ask.check_sql(sql)


def test_reader_connection_refuses_writes_even_inside_with(db):  # noqa: F811
    with pytest.raises(sqlite3.Error):
        ask.run_sql(db, "WITH x AS (SELECT 1) DELETE FROM runs")
    assert ask.run_sql(db, "SELECT COUNT(*) FROM runs")[1] == [[3]]


def test_slow_query_is_stopped(db):  # noqa: F811
    endless = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n"
    with pytest.raises(sqlite3.OperationalError, match="longer than"):
        ask.run_sql(db, endless, timeout_s=0.2)


def test_rows_are_capped(db):  # noqa: F811
    cols, rows, truncated = ask.run_sql(db, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 30) "
                                            "SELECT i FROM n", max_rows=10)
    assert len(rows) == 10 and truncated


def test_budget_stops_calls(db):  # noqa: F811
    llm = FakeChat(GOOD, {"answer": "x"})
    result = ask.Asker(llm, db, docs={}, source="demo").ask("q", budget_left=0.0)
    assert llm.sent == [] and "budget" in result.error


def test_page_explains_missing_api_key(db, monkeypatch):  # noqa: F811
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SCRAPEKIT_UI_DB", str(db))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    at = AppTest.from_file(str(Path(__file__).parent.parent / "src/scrapekit/ui/views/ask.py"), default_timeout=30).run()
    assert not at.exception
    assert any("OPENROUTER_API_KEY" in m.value for m in at.info)


def test_current_facts_view_has_plain_values(db):  # noqa: F811
    cols, rows, _ = ask.run_sql(db, "SELECT value, COUNT(*) FROM current_facts WHERE field = 'phase' GROUP BY value")
    assert rows == [["Construction", 1]]


def test_prompt_teaches_the_view_with_examples(db):  # noqa: F811
    prompt = ask.Asker(FakeChat(), db, docs={}, source="demo").system
    assert "current_facts(" in prompt and "GROUP BY" in prompt and "MAX(CASE WHEN field" in prompt
