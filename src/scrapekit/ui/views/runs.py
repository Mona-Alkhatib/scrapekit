"""Runs: status and metrics of every run, newest first. Select one for details."""

import streamlit as st

from scrapekit.ui import common, queries as q

st.title("Runs")
conn, source = common.require_data()
rows = q.runs(conn, source)
st.caption("Status: ok, ok with failures (an HTTP error such as 404, or a network, parse or LLM error was logged), "
           "or incomplete (the run stopped before finishing). Select a row for details.")
picked = common.table(rows, "runs", columns=[
    "run_id", "status", "mode", "started_at", "minutes", "requests", "not_modified", "pages_downloaded",
    "files_downloaded", "files_not_rechecked", "facts_new", "facts_updated", "skips", "llm_calls", "llm_cost_usd"],
    column_config={"not_modified": st.column_config.NumberColumn("304s"),
                   "llm_cost_usd": st.column_config.NumberColumn("LLM $", format="$%.4f")})

if picked:
    st.subheader(picked["run_id"])
    cols = st.columns(4)
    cols[0].metric("Status", picked["status"])
    cols[1].metric("Requests", picked["requests"], help="All HTTP requests, including robots.txt and sitemap")
    cols[2].metric("Answered 304", picked["not_modified"], help="Unchanged since the last run, no body downloaded")
    cols[3].metric("LLM cost", f"${picked['llm_cost_usd']:.4f}", help="Actual cost reported by OpenRouter")
    tab_skips, tab_changes, tab_raw = st.tabs(["Skips in this run", "Facts this run changed", "All metrics"])
    with tab_skips:
        common.table(q.skip_counts(conn, source, picked["run_id"]), "run_skip_counts")
    with tab_changes:
        changes = q.run_fact_changes(conn, picked["run_id"])
        st.caption(f"This run: {picked['facts_new']:,} new and {picked['facts_updated']:,} updated facts "
                   f"(removed: {picked['stats'].get('records_removed', 0):,}). Showing the first {len(changes):,}.")
        common.table(changes, "run_changes", columns=["change", "entity_type", "entity_key", "field", "value", "method", "source_url"])
    with tab_raw:
        st.json(picked["stats"])
