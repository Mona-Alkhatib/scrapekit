"""Skip log: every link we did not fetch, or fetched and could not use, with the reason.

Opens on the latest run, because every run finds the same links again. Data: queries.skips() and skip_counts().
"""

import streamlit as st

from scrapekit.ui import common, queries as q

REASONS = {
    "off_domain": "a link to another website: we crawl only this site's pages (files are downloaded from anywhere)",
    "non_http_link": "an email or phone link (mailto:, tel:), not a web page",
    "too_large": "a file over the 50 MB limit",
    "robots_disallowed": "that site's robots.txt asks crawlers not to fetch it",
    "excluded_path": "a page this source's config skips on purpose (e.g. search)",
    "http_404": "dead link: the server says it does not exist",
    "http_403": "the server refused access",
    "no_text_layer": "a scanned PDF (image only): saved, but no text to read",
    "parse_error": "the page or file could not be parsed",
    "llm_error": "the AI call for this PDF failed",
}

st.title("Skip log")
conn, source = common.require_data()
st.caption("Links we found but did not follow, or fetched but could not use, each with a reason. Every run finds "
           "the same links again, so look at one run at a time. `detail` is the page the link was found on, "
           "or why a file failed.")
# 1. Filters. The run list is newest first, so the page opens on the latest run.
runs = [r["run_id"] for r in q.runs(conn, source)]  # newest first
c1, c2, c3 = st.columns(3)
run_id = c1.selectbox("Run", runs + [""], format_func=lambda r: r or "all runs")
counts = q.skip_counts(conn, source, run_id or None)
reason = c2.selectbox("Reason", [""] + [c["reason"] for c in counts], format_func=lambda r: r or "all")
search = c3.text_input("URL contains")
# 2. Counts per reason for the chosen run, with what each reason means.
if counts:
    st.bar_chart({c["reason"]: c["n"] for c in counts}, horizontal=True)
    with st.expander("What the reasons mean"):
        for c in counts:
            st.markdown(f"- **{c['reason']}** ({c['n']}): {REASONS.get(c['reason'], 'see detail')}")
# 3. The skipped URLs themselves.
rows = q.skips(conn, source, reason=reason or None, run_id=run_id or None, search=search or None)
st.caption(f"{len(rows)} rows (max 1,000).")
common.table(rows, "skips", column_config={"url": st.column_config.LinkColumn("url")})
