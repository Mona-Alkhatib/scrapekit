"""Entry point: `scrapekit ui` runs `streamlit run` on this file.

Sidebar: which database and which source. Pages: runs, entities, facts, pages & files, skips.
"""

from pathlib import Path

import streamlit as st

from scrapekit.ui import common, queries as q

st.set_page_config(page_title="scrapekit data browser", layout="wide")

with st.sidebar:
    st.header("scrapekit")
    found = sorted(str(p) for root in ("data", "examples") for p in Path(root).glob("**/scrapekit.db"))
    default = str(common.db_path())
    options = sorted(set(found) | {default})
    st.session_state["db_path"] = st.selectbox("Database", options, index=options.index(default),
                                               help="Each data folder holds one SQLite database plus raw/ copies.")
    if Path(st.session_state["db_path"]).exists():
        names = q.sources(common.open_conn(st.session_state["db_path"]))
        if names:
            st.session_state["source"] = st.selectbox("Source", names)
    st.caption("Read-only. Nothing here changes the data.")

views = Path(__file__).parent / "views"
nav = st.navigation([
    st.Page(views / "overview.py", title="Overview", icon=":material/dashboard:", default=True),
    st.Page(views / "runs.py", title="Runs", icon=":material/history:"),
    st.Page(views / "entities.py", title="Entities", icon=":material/category:"),
    st.Page(views / "facts.py", title="Facts", icon=":material/fact_check:"),
    st.Page(views / "documents.py", title="Pages & files", icon=":material/description:"),
    st.Page(views / "skips.py", title="Skip log", icon=":material/block:"),
    st.Page(views / "dictionary.py", title="Data dictionary", icon=":material/menu_book:"),
    st.Page(views / "ask.py", title="Ask AI", icon=":material/chat:"),
])
nav.run()
