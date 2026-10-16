"""Shared pieces for every page: the database connection, the source picker, tables, provenance."""

import hashlib
import html
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from . import queries as q

DB_ENV = "SCRAPEKIT_UI_DB"
# How a fact was found, in plain words (the database stores "parse" or "llm").
METHOD_LABELS = {"parse": "Rules (exact, no AI)", "llm": "AI from PDF text (quote checked)"}
MAX_INLINE_PDF_BYTES = 15_000_000  # bigger PDFs are slow to embed in the browser; offer download only


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV, "data/scrapekit.db"))


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


def _readable(row: dict) -> dict:
    """Fact values are stored as JSON; show text as plain text and lists or objects as compact JSON."""
    if row.get("method") in METHOD_LABELS:
        row = {**row, "method": METHOD_LABELS[row["method"]]}
    if "value" not in row or not isinstance(row["value"], str):
        return row
    try:
        value = json.loads(row["value"])
    except ValueError:
        return row
    shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {**row, "value": shown}


def table(rows: list[dict], key: str, columns: list[str] | None = None, column_config: dict | None = None) -> dict | None:
    """A sortable table where clicking a row selects it. Returns the selected row or None."""
    if not rows:
        st.caption("Nothing matches.")
        return None
    # Streamlit remembers a selection as a row number. When the rows change (a filter, another parent row),
    # that number would point at a different row, so drop the old selection instead.
    fingerprint = hashlib.md5(repr([tuple(r.values())[:3] for r in rows]).encode()).hexdigest()
    previous = st.session_state.get(f"_rows_{key}")
    if previous is not None and previous != fingerprint and key in st.session_state:
        del st.session_state[key]
    st.session_state[f"_rows_{key}"] = fingerprint
    frame = pd.DataFrame([_readable(r) for r in rows]).convert_dtypes()  # keeps ids as integers next to blanks
    event = st.dataframe(frame, key=key, hide_index=True, on_select="rerun", selection_mode="single-row",
                         column_order=columns, column_config=column_config)
    picked = event.selection.rows
    return rows[picked[0]] if picked and picked[0] < len(rows) else None


def provenance(conn, fact: dict) -> None:
    """Where one fact came from: source, method, locator, run, validity, and for PDF quotes the page itself."""
    value = json.loads(fact["value"])
    st.markdown(f"**{fact['field']}** of `{fact['entity_key']}`")
    meta = {
        "source_url": fact["source_url"], "method": METHOD_LABELS.get(fact["method"], fact["method"]), "locator": fact["locator"],
        "run_id": fact["run_id"], "valid_from": fact["valid_from"], "valid_to": fact["valid_to"] or "current",
        "content_hash": fact["content_hash"],
    }
    st.table(meta)
    quotes = value if fact["field"] == "llm_key_facts" and isinstance(value, list) else []
    if fact["excerpt"] and not quotes:
        quotes = [{"value": value, "page": 1, "excerpt": fact["excerpt"]}]
    for i, item in enumerate(quotes):
        with st.expander(f"page {item.get('page')}: {item.get('value')}", expanded=i == 0):
            text = q.pdf_page_text(conn, fact["content_hash"], int(item.get("page") or 0))
            if text is None:
                st.caption("Page text not saved.")
                continue
            span = q.find_excerpt(text, item.get("excerpt", ""))
            if span:
                a, b = span
                body = html.escape(text[:a]) + "<mark>" + html.escape(text[a:b]) + "</mark>" + html.escape(text[b:])
            else:
                body = html.escape(text)
                st.caption("Quote not found verbatim in this page's text (spacing differs); showing the page.")
            # st.html shows the escaped text as-is: no Markdown or math parsing of PDF text.
            st.html(f"<div style='white-space:pre-wrap;font-size:0.85em'>{body}</div>")
    if not quotes and fact["method"] == "parse":
        st.caption(f"Read exactly from the saved copy at `{fact['locator']}`.")


def raw_viewer(conn, content_hash: str, content_type: str | None) -> None:
    """Shows the exact bytes we saved: PDFs in a viewer, HTML as source. Always offers a download."""
    path = q.raw_path(conn, content_hash, raw_dir=db_path().parent / "raw")
    if path is None:
        st.caption("Raw copy is not on this machine (only the database was copied).")
        return
    data = path.read_bytes()
    st.download_button("Download raw copy", data, file_name=path.name, key=f"dl_{content_hash}")
    if path.suffix == ".pdf" or "pdf" in (content_type or ""):
        if len(data) > MAX_INLINE_PDF_BYTES:
            st.caption(f"{len(data) / 1e6:.0f} MB: too big to show inline, use the download button.")
        else:
            st.pdf(data, height=600)
    else:
        st.code(data[:200_000].decode("utf-8", "replace"), language="html")
