"""Registry of known sources. To add a site: create a module with a Source and list it here.

`scrapekit run <name>` looks the name up with get_source().
"""

from .base import Source
from .createto import CREATETO

__all__ = ["CREATETO", "SOURCES", "Source", "get_source"]

SOURCES: dict[str, Source] = {s.name: s for s in (CREATETO,)}


def get_source(name: str) -> Source:
    """The Source with this name, or a clear error listing the known ones."""
    try:
        return SOURCES[name]
    except KeyError:
        raise SystemExit(f"Unknown source '{name}'. Known: {', '.join(sorted(SOURCES))}") from None
