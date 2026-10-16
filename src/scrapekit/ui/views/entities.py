"""Entities: projects, news items, documents and pages, with every fact and where it came from.

Flow: pick an entity (queries.entities) -> its facts (entity_facts) -> pick a fact -> provenance panel
and, if the value changed over time, its history chain (fact_history).
"""

import streamlit as st

from scrapekit.ui import common, queries as q

st.title("Entities")
conn, source = common.require_data()

# 1. Filters, then one row per entity.
c1, c2 = st.columns([1, 3])
kind = c1.selectbox("Type", [""] + q.entity_types(conn, source), format_func=lambda t: t or "all")
search = c2.text_input("Search", placeholder="name, address, phase, ward ...")
rows = q.entities(conn, source, entity_type=kind or None, search=search or None)
st.caption(f"{len(rows)} entities (max 500). Select one to see its facts.")
picked = common.table(rows, "entities", columns=["entity_type", "entity_key", "name", "facts", "source_url"],
                      column_config={"source_url": st.column_config.LinkColumn("source_url")})
# 2. An entity is selected: show its facts (current only, or with replaced values when history is on).
if picked:
    st.subheader(picked["name"] or picked["entity_key"])
    history = st.toggle("Show history (replaced values too)")
    facts = q.entity_facts(conn, source, picked["entity_type"], picked["entity_key"], history=history)
    for f in facts:
        f["status"] = "current" if f["valid_to"] is None else f"replaced {f['valid_to'][:10]}"
    chosen = common.table(facts, "entity_facts", columns=["field", "value", "method", "locator", "status", "valid_from"])
    # 3. A fact is selected: where it came from, and every version of it.
    if chosen:
        common.provenance(conn, chosen)
        chain = q.fact_history(conn, chosen["id"])
        if len(chain) > 1:
            st.markdown("**History of this field**")
            common.table(chain, "chain", columns=["id", "value", "valid_from", "valid_to", "superseded_by", "run_id"])
    # 4. Files linked from this entity's page (or, for a document, the pages that link to it).
    links = q.links_from(conn, picked["source_url"])
    linked_from = q.links_to(conn, picked["source_url"])
    if links or linked_from:
        st.markdown("**Linked files**" if links else "**Linked from**")
        for u in links or linked_from:
            st.markdown(f"- {u}")
