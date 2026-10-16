"""Entry point: `scrapekit ui` runs `streamlit run` on this file.

How Streamlit works, in one paragraph: every click or filter change re-runs the page's script from top to
bottom. Widgets (filters, tables) remember their state between runs, so each page reads like a plain script:
get data, draw it, and if a row is selected, draw its details. This file runs first on every click: it draws
the sidebar, then hands over to the page the user picked.

Sidebar: the database in use (set by --data-dir) and the source. Pages: runs, entities, facts, pages & files, skips.
"""

from pathlib import Path

import streamlit as st

from scrapekit.ui import common, queries as q

st.set_page_config(page_title="scrapekit data browser", layout="wide")

# 1. Sidebar: which data we are showing.
with st.sidebar:
    st.header("scrapekit")
    path = common.db_path()  # one database, chosen when the app starts: scrapekit ui --data-dir <folder>
    st.caption(f"Data: `{path}`")
    if path.exists():
        names = q.sources(common.open_conn(str(path)))
        if len(names) > 1:
            st.session_state["source"] = st.selectbox("Source", names)
        elif names:
            st.session_state["source"] = names[0]
            st.caption(f"Source: {names[0]}")

# 2. Pages, in sidebar order. Each is a file in views/ (not pages/, which Streamlit would auto-load).
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
