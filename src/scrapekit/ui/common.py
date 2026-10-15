"""Shared pieces for every page: the database connection, the source picker, tables, provenance."""

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from . import queries as q

DB_ENV = "SCRAPEKIT_UI_DB"


def db_path() -> Path:
    return Path(st.session_state.get("db_path") or os.environ.get(DB_ENV, "data/scrapekit.db"))


@st.cache_resource
def open_conn(path: str):
    return q.open_db(Path(path))


def require_data():
    """Returns (connection, source). Shows a friendly message and stops the page if there is no data."""
    path = db_path()
    if not path.exists():
        st.info(f"No runs yet: {path} does not exist. Run `scrapekit run <source>` first.")
        st.stop()
    conn = open_conn(str(path))
    names = q.sources(conn)
    if not names:
        st.info("No runs in this database yet. Run `scrapekit run <source>` first.")
        st.stop()
    current = st.session_state.get("source")
    return conn, current if current in names else names[0]


def table(rows: list[dict], key: str, columns: list[str] | None = None, column_config: dict | None = None) -> dict | None:
    """A sortable table where clicking a row selects it. Returns the selected row or None."""
    if not rows:
        st.caption("Nothing matches.")
        return None
    frame = pd.DataFrame(rows)
    event = st.dataframe(frame, key=key, hide_index=True, on_select="rerun", selection_mode="single-row",
                         column_order=columns, column_config=column_config)
    picked = event.selection.rows
    return rows[picked[0]] if picked else None
