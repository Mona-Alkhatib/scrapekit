"""Overview: the whole dataset at a glance, with charts. Every chart comes from one function in queries.py."""

from datetime import date

import pandas as pd
import streamlit as st

from scrapekit.sources import SOURCES
from scrapekit.ui import common, queries as q

st.title("Overview")
conn, source = common.require_data()
o = q.overview(conn, source)

cols = st.columns(6)
cols[0].metric("Entities", f"{o['entities']:,}", help="Projects, news items, documents and pages with current facts")
cols[1].metric("Facts", f"{o['facts']:,}", help="One field of one entity, each with its own source")
cols[2].metric("Pages saved", f"{o['pages']:,}")
cols[3].metric("Files saved", f"{o['files']:,}")
cols[4].metric("Skipped links (last run)", f"{o['skips_last_run']:,}",
               help="Links found in the last run but not followed. Most point to other websites, skipped on purpose. "
                    "Every run finds them again, so the count is per run. See the Skip log page.")
cols[5].metric("LLM cost", f"${o['llm_cost_usd']:.2f}", help=f"Actual, summed over {o['runs']} runs")


def bars(rows: list[dict], x: str, y: str = "n", color: str | None = None, horizontal: bool = False) -> None:
    if not rows:
        st.caption("No data.")
        return
    frame = pd.DataFrame(rows)
    if color:
        frame = frame.pivot_table(index=x, columns=color, values=y, fill_value=0)
        st.bar_chart(frame, horizontal=horizontal)
    else:
        st.bar_chart(frame.set_index(x)[y], horizontal=horizontal)


left, right = st.columns(2)
with left:
    st.subheader("Facts: copied or read by AI")
    st.caption(common.METHOD_EXPLAINED)
    bars([{**r, "method": common.METHOD_LABELS.get(r["method"], r["method"])}
          for r in q.facts_by_type_method(conn, source)], "entity_type", color="method")
with right:
    st.subheader("Entities per year")
    st.caption(f"By best date: publish date, else the date in the document, else PDF metadata. "
               f"The last 5 years start in {date.today().year - 5}.")
    bars(q.entities_per_year(conn, source), "year", color="entity_type")

places = q.locations(conn, source)
if places:
    st.subheader(f"Map ({len(places)} entities with a location)")
    st.map(pd.DataFrame(places), latitude="lat", longitude="lon", size=40)

left, right = st.columns(2)
with left:
    config = SOURCES.get(source)
    for kind, field in (config.overview_labels if config else ()):  # the site's own label fields, from its config
        st.subheader(f"{kind} by {field}")
        bars(q.label_counts(conn, source, kind, field), "value", horizontal=True)
    st.subheader("Files per host")
    st.caption("Files are downloaded from any host; pages are only crawled on the source's own site.")
    bars(q.files_per_host(conn, source), "host", horizontal=True)
with right:
    st.subheader("PDF document types (LLM)")
    bars(q.label_counts(conn, source, "document", "llm_doc_type"), "value", horizontal=True)
    st.subheader("Skipped links in the last run, by reason")
    st.caption("off_domain = a link to another website (we only crawl this site's pages, but download files from anywhere).")
    bars(q.skip_counts(conn, source, o["last_run"]) if o["last_run"] else [], "reason", horizontal=True)

runs = q.runs(conn, source)
if runs:
    st.subheader("Runs compared")
    st.caption("A repeat run should be mostly 304 (unchanged, no body downloaded) and cost $0.")
    frame = pd.DataFrame([{"run": r["run_id"], "other (200, 404 ...)": r["requests"] - r["not_modified"],
                           "304 (unchanged)": r["not_modified"]} for r in reversed(runs)]).set_index("run")
    a, b, c = st.columns(3)
    a.caption("Requests")
    a.bar_chart(frame, stack=True)
    b.caption("Minutes")
    b.bar_chart(pd.DataFrame({"run": [r["run_id"] for r in reversed(runs)],
                              "minutes": [r["minutes"] for r in reversed(runs)]}).set_index("run"))
    c.caption("LLM cost (USD)")
    c.bar_chart(pd.DataFrame({"run": [r["run_id"] for r in reversed(runs)],
                              "LLM $": [r["llm_cost_usd"] for r in reversed(runs)]}).set_index("run"))
