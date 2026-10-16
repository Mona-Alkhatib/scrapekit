"""Ask AI: ask a question in plain English; the model writes the SQL, we run it read-only, and show both."""

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from scrapekit.extract import field_docs
from scrapekit.sources import SOURCES
from scrapekit.ui import ask, common

st.title("Ask AI")
conn, source = common.require_data()
st.caption("The model writes one SQL query, the query runs on a separate connection that is only allowed to read, "
           "and the model summarizes the rows. You always see the SQL and the rows, so you can check the answer. "
           f"Each question costs about \\$0.001; this session stops at \\${ask.DEFAULT_SESSION_BUDGET_USD:.2f}.")

load_dotenv()
if not os.environ.get("OPENROUTER_API_KEY"):
    st.info("Ask AI needs an OpenRouter key: put OPENROUTER_API_KEY=... in the .env file at the repo root "
            "and restart `scrapekit ui`. Every other page works without it.")
    st.stop()

spent = st.session_state.setdefault("ai_spent", 0.0)
history = st.session_state.setdefault("ai_history", [])
budget_left = ask.DEFAULT_SESSION_BUDGET_USD - spent
st.progress(min(spent / ask.DEFAULT_SESSION_BUDGET_USD, 1.0),
            text=f"Spent this session: \\${spent:.4f} of \\${ask.DEFAULT_SESSION_BUDGET_USD:.2f}")

MAX_HISTORY = 20
md = ask.plain_markdown  # model and user text: no links, images or math, shown exactly as written


GENERIC_EXAMPLES = (
    "How many entities of each type do we have?",
    "Which hosts did we download files from, and how many from each?",
    "Which facts changed between runs?",
    "Why were links skipped in the last run, by reason?",
)
config = SOURCES.get(source)
examples = [*(config.ask_examples if config else ()), *GENERIC_EXAMPLES]
picked = st.pills("Examples", examples, selection_mode="single")
question = st.text_input("Your question", value=picked or "", placeholder="Ask anything about the data")

if st.button("Ask", type="primary", disabled=not question or budget_left <= 0):
    with st.spinner("Writing and running the query ..."):
        try:
            asker = ask.Asker(ask.OpenRouterChat(), common.db_path(), field_docs(SOURCES.get(source)), source)
            result = asker.ask(question, budget_left=budget_left)
        except Exception as exc:  # network or API errors: show them, never crash the page
            result = ask.AskResult(question, error=f"{type(exc).__name__}: {exc}")
    st.session_state["ai_spent"] = spent + result.cost_usd
    try:
        ask.append_log(ask.log_path(common.db_path()), result, source)
    except OSError as exc:  # a read-only folder must not break the page
        st.warning(f"Could not save this question to the log: {exc}")
    history.insert(0, result)
    del history[MAX_HISTORY:]  # keep memory bounded
    st.rerun()

for i, r in enumerate(history):
    with st.container(border=True):
        st.markdown(f"**Q: {md(r.question)}**")
        if r.error:
            st.error(md(r.error))
        elif r.answer:
            st.success(md(r.answer))
        if r.sql:
            st.code(r.sql, language="sql", wrap_lines=True)
            if r.explanation:
                st.caption(md(r.explanation))
        if r.columns:
            st.dataframe(pd.DataFrame(r.rows, columns=r.columns), hide_index=True, key=f"ai_rows_{len(history) - i}")
            st.caption(f"{len(r.rows)} rows{' (capped at 500)' if r.truncated else ''}.")
        st.caption(f"Attempts: {r.attempts}. Cost: \\${r.cost_usd:.4f} (actual, from OpenRouter).")

past = ask.read_log(ask.log_path(common.db_path()))
if past:
    total = ask.log_totals(ask.log_path(common.db_path()), source)
    with st.expander(f"Question log: {total['questions']} questions, \\${total['cost_usd']:.4f} in total (all sessions)"):
        st.caption("Saved next to the database in ask_log.jsonl: an audit trail of what was asked, the SQL that ran, "
                   "and what it cost.")
        st.dataframe(pd.DataFrame(past[::-1]).reindex(columns=["at", "question", "rows", "attempts", "cost_usd", "error", "sql"]),
                     hide_index=True, key="ask_log")

