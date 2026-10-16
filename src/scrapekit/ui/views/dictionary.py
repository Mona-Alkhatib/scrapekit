"""Data dictionary: what every field means, how it was extracted, how often it is filled, and an example."""

import pandas as pd
import streamlit as st

from scrapekit.extract import describe, field_docs
from scrapekit.sources import SOURCES
from scrapekit.ui import common, queries as q, tables_doc

st.title("Data dictionary")
conn, source = common.require_data()
docs = field_docs(SOURCES.get(source))
st.caption("Each fact is one field of one thing (a project, news item, document or page). "
           "Coverage = share of things of that type that have the field.")
st.caption(common.METHOD_EXPLAINED)

stats = q.field_stats(conn, source)
for kind in sorted({s["entity_type"] for s in stats}):
    rows = [s for s in stats if s["entity_type"] == kind]
    st.subheader(f"{kind} ({len(rows)} fields)")
    frame = pd.DataFrame([{
        "field": s["field"], "meaning": describe(docs, kind, s["field"]) or "(no description yet)",
        "how it was found": common.METHOD_LABELS.get(s["method"], s["method"]),
        "coverage": s["coverage"], "entities": s["entities"], "example": s["example"], "locator example": s["locator"],
    } for s in rows])
    st.dataframe(frame, hide_index=True, column_config={
        "meaning": st.column_config.TextColumn(width="large"),
        "coverage": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
    })

st.header("Tables")
st.caption("For querying the SQLite file directly (or through Ask AI). Path: the Database chosen in the sidebar.")
st.dataframe(pd.DataFrame(tables_doc.TABLES, columns=["table", "what it holds", "main columns"]), hide_index=True,
             column_config={"what it holds": st.column_config.TextColumn(width="medium"),
                            "main columns": st.column_config.TextColumn(width="large")})
st.markdown("**Tips**\n" + "\n".join(f"- {t}" for t in tables_doc.TIPS))
