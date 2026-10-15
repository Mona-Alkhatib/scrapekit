"""Facts: search every value we extracted, filter by field, method and run, and see where each came from."""

import streamlit as st

from scrapekit.ui import common, queries as q

st.title("Facts")
conn, source = common.require_data()
c1, c2, c3, c4 = st.columns(4)
kind = c1.selectbox("Entity type", [""] + q.entity_types(conn, source), format_func=lambda t: t or "all")
field = c2.selectbox("Field", [""] + q.fact_fields(conn, source), format_func=lambda f: f or "all")
method = c3.selectbox("Method", ["", "parse", "llm"], format_func=lambda m: {"": "all", "parse": "rules (exact)", "llm": "LLM (quote checked)"}[m])
run_id = c4.selectbox("Created in run", [""] + [r["run_id"] for r in q.runs(conn, source)], format_func=lambda r: r or "any")
c5, c6 = st.columns([3, 1])
search = c5.text_input("Value or entity contains")
current_only = c6.toggle("Current values only", value=True)
rows = q.facts(conn, source, entity_type=kind or None, field=field or None, method=method or None,
               run_id=run_id or None, current_only=current_only, search=search or None)
st.caption(f"{len(rows)} facts (max 1,000). Select one for its source and history.")
picked = common.table(rows, "facts", columns=["id", "entity_type", "entity_key", "field", "value", "method", "locator", "valid_from", "valid_to"])
if picked:
    common.provenance(conn, picked)
    chain = q.fact_history(conn, picked["id"])
    if len(chain) > 1:
        st.markdown("**History**")
        common.table(chain, "fact_chain", columns=["id", "value", "valid_from", "valid_to", "superseded_by", "run_id"])
