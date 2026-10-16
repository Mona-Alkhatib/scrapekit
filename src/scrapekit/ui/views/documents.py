"""Pages & files: every URL we know about, its saved versions, the raw copy, and the facts read from it.

Tabs for the selected URL: versions (snapshots), raw copy (common.raw_viewer), facts (queries.facts by
source_url), links in and out, and skips for that URL.
"""

import streamlit as st

from scrapekit.ui import common, queries as q

st.title("Pages & files")
conn, source = common.require_data()
# 1. Filters, then one row per URL (from the documents table).
c1, c2, c3, c4 = st.columns(4)
kind = c1.selectbox("Kind", ["", "page", "file"], format_func=lambda k: k or "all")
host = c2.selectbox("Host", [""] + q.hosts(conn, source), format_func=lambda h: h or "all")
saved = c3.selectbox("Saved copy", ["all", "saved", "not saved"])
search = c4.text_input("URL contains")
rows = q.documents(conn, source, kind=kind or None, host=host or None,
                   saved={"all": None, "saved": True, "not saved": False}[saved], search=search or None)
st.caption(f"{len(rows)} URLs (max 1,000). last_status 304 = checked and unchanged; blank = never saved (see skips).")
picked = common.table(rows, "documents", columns=["kind", "url", "host", "last_status", "saved", "versions", "bytes", "last_seen_run"],
                      column_config={"url": st.column_config.LinkColumn("url")})
# 2. A URL is selected: everything we know about it, one tab per question.
if picked:
    doc = q.document(conn, picked["url"])
    st.subheader(picked["url"])
    tabs = st.tabs(["Versions", "Raw copy", "Facts from it", "Links", "Skips"])
    with tabs[0]:  # what versions did we save?
        st.caption("One row per distinct content. A new row means the source changed.")
        common.table(q.snapshots(conn, picked["url"]), "snapshots", columns=["content_hash", "bytes", "fetched_at", "run_id"])
    with tabs[1]:  # the exact bytes
        if doc and doc["current_hash"]:
            common.raw_viewer(conn, doc["current_hash"], doc["content_type"])
        else:
            st.caption("Never saved.")
    with tabs[2]:  # what did we extract from it?
        rows_f = q.facts(conn, source, source_url=picked["url"])
        chosen = common.table(rows_f, "doc_facts", columns=["entity_type", "entity_key", "field", "value", "method", "locator"])
        if chosen:
            common.provenance(conn, chosen)
    with tabs[3]:  # how is it connected?
        st.markdown("**Linked from**")
        for u in q.links_to(conn, picked["url"]) or ["(none, or found via the sitemap)"]:
            st.markdown(f"- {u}")
        out = q.links_from(conn, picked["url"])
        if out:
            st.markdown("**Files linked from this page**")
            for u in out:
                st.markdown(f"- {u}")
    with tabs[4]:  # did anything go wrong with it?
        common.table(q.url_skips(conn, picked["url"]), "doc_skips")
