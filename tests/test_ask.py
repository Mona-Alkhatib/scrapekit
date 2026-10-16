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


def test_prompt_lists_run_stats_keys_skip_reasons_and_methods(db):  # noqa: F811
    prompt = ask.Asker(FakeChat(), db, docs={}, source="demo").system
    assert "runs.stats_json keys: requests, status_counts" in prompt  # real keys from the latest run
    assert "skips.reason values: http_404, off_domain" in prompt  # real reasons, not guessed ones like '404'
    assert "'parse' = copied" in prompt and "'llm' = read from a PDF" in prompt
    assert "compare with = 0, not = '0'" in prompt  # json_extract(false) is the integer 0
    assert "'parse' means copied" in ask.ANSWER_PROMPT  # the answer step names the methods correctly too


def test_huge_values_are_refused_and_cells_trimmed(db):  # noqa: F811
    with pytest.raises(sqlite3.Error):
        ask.run_sql(db, "SELECT zeroblob(50000000)")
    _, rows, _ = ask.run_sql(db, "SELECT printf('%.*c', 5000, 'x')")
    assert len(rows[0][0]) <= ask.MAX_CELL_CHARS + 3


def test_dangerous_functions_are_blocked(db):  # noqa: F811
    for sql in ("SELECT fts3_tokenizer('simple')", "SELECT load_extension('x')"):
        with pytest.raises(sqlite3.Error):
            ask.run_sql(db, sql)


def test_answer_markdown_is_neutralized():
    from scrapekit.ui.ask import plain_markdown

    shown = plain_markdown("See ![x](https://evil.example/?q=1) and [click](https://phish.example) for $5")
    assert "![" not in shown and "](" not in shown and "\\$5" in shown


class FailingChat(FakeChat):
    def complete(self, messages, schema):
        if self.sent:
            raise TimeoutError("network down")
        return super().complete(messages, schema)


def test_failure_mid_question_keeps_the_cost_already_spent(db):  # noqa: F811
    result = ask.Asker(FailingChat(GOOD), db, docs={}, source="demo").ask("q")
    assert result.cost_usd == pytest.approx(0.001)
    assert result.rows == [["alpha"]]  # the query worked, so its rows are still shown
    assert "network down" in result.answer


def test_prompt_examples_are_generic(db):  # noqa: F811
    prompt = ask.Asker(FakeChat(), db, docs={}, source="demo").system
    examples = prompt.split("Examples:")[1].split("Main view")[0]
    assert "project" not in examples and "phase" not in examples


def test_ui_code_names_no_site_vocabulary():
    """Example questions and charts that only make sense for one site belong in its Source config."""
    import re

    words = re.compile(r"Leasing|Delivery and Construction|board minutes|Etobicoke|'phase'|\"phase\"")
    root = Path(__file__).parent.parent / "src" / "scrapekit" / "ui"
    offenders = [f"{p.name}:{n}" for p in root.rglob("*.py")
                 for n, line in enumerate(p.read_text().splitlines(), 1) if words.search(line)]
    assert offenders == []


def test_each_question_is_logged_and_totals_add_up(db, tmp_path):  # noqa: F811
    log = ask.log_path(db)
    assert log == db.parent / "ask_log.jsonl" and ask.log_totals(log) == {"questions": 0, "cost_usd": 0.0}
    for _ in range(2):
        result = ask.Asker(FakeChat(GOOD, {"answer": "alpha"}), db, docs={}, source="demo").ask("Which phase?")
        ask.append_log(log, result, source="demo")
    entries = ask.read_log(log)
    assert len(entries) == 2 and entries[0]["question"] == "Which phase?" and entries[0]["sql"].startswith("SELECT")
    assert entries[0]["rows"] == 1 and entries[0]["source"] == "demo" and "at" in entries[0]
    assert ask.log_totals(log) == {"questions": 2, "cost_usd": pytest.approx(0.004)}


def test_a_broken_log_line_is_skipped(db):  # noqa: F811
    log = ask.log_path(db)
    log.write_text('{"question": "ok", "cost_usd": 0.001}\nnot json\n')
    assert ask.log_totals(log)["questions"] == 1


def test_overview_shows_ask_ai_cost_separately(db):  # noqa: F811
    ask.log_path(db).write_text('{"question": "q", "cost_usd": 0.0015, "source": "demo"}\n')
    os.environ["SCRAPEKIT_UI_DB"] = str(db)
    at = AppTest.from_file(str(Path(__file__).parent.parent / "src/scrapekit/ui/views/overview.py"), default_timeout=30).run()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["LLM cost (runs)"] == "$0.01" and metrics["Ask AI cost"] == "$0.0015"
