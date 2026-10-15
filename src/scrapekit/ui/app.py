"""Entry point: `scrapekit ui` runs `streamlit run` on this file.

Sidebar: which database and which source. Pages: runs, entities, facts, pages & files, skips.
"""

from pathlib import Path

import streamlit as st

from scrapekit.ui import common, queries as q

st.set_page_config(page_title="scrapekit data browser", layout="wide")

with st.sidebar:
    st.header("scrapekit")
    found = sorted(str(p) for p in Path("data").glob("**/scrapekit.db"))
    default = str(common.db_path())
    options = sorted(set(found) | {default})
    st.session_state["db_path"] = st.selectbox("Database", options, index=options.index(default),
                                               help="Each data folder holds one SQLite database plus raw/ copies.")
    if Path(st.session_state["db_path"]).exists():
        names = q.sources(common.open_conn(st.session_state["db_path"]))
        if names:
            st.session_state["source"] = st.selectbox("Source", names)
    st.caption("Read-only. Nothing here changes the data.")

pages = Path(__file__).parent / "pages"
nav = st.navigation([
    st.Page(pages / "runs.py", title="Runs", icon=":material/history:", default=True),
    st.Page(pages / "entities.py", title="Entities", icon=":material/category:"),
    st.Page(pages / "skips.py", title="Skip log", icon=":material/block:"),
])
nav.run()
