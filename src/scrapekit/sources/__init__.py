"""Registry of known sources. To add a site: create a module with a Source and list it here."""

from .base import Source
from .createto import CREATETO

__all__ = ["CREATETO", "SOURCES", "Source", "get_source"]

SOURCES: dict[str, Source] = {s.name: s for s in (CREATETO,)}


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError:
        raise SystemExit(f"Unknown source '{name}'. Known: {', '.join(sorted(SOURCES))}") from None
