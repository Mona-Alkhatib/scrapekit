"""Skip log: every link we did not fetch, or fetched and could not use, with the reason."""

import streamlit as st

from scrapekit.ui import common, queries as q

st.title("Skip log")
conn, source = common.require_data()
counts = q.skip_counts(conn, source)
st.caption("`detail` is the page the link was found on, or why a file failed (size, scanned PDF, error).")
if counts:
    st.bar_chart({c["reason"]: c["n"] for c in counts}, horizontal=True)
c1, c2, c3 = st.columns(3)
reason = c1.selectbox("Reason", [""] + [c["reason"] for c in counts], format_func=lambda r: r or "all")
run_id = c2.selectbox("Run", [""] + [r["run_id"] for r in q.runs(conn, source)], format_func=lambda r: r or "all")
search = c3.text_input("URL contains")
rows = q.skips(conn, source, reason=reason or None, run_id=run_id or None, search=search or None)
st.caption(f"{len(rows)} rows (max 1,000).")
common.table(rows, "skips", column_config={"url": st.column_config.LinkColumn("url")})
