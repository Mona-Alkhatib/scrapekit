"""Raw copies of every page and file, stored by content hash so identical bytes are saved once.

A file is saved as data/raw/<first 2 hash chars>/<sha256>.<ext>. The hash is also the version id:
the same bytes always get the same name (stored once, even if linked from 3 URLs), and a new hash
for a URL means its content changed. The snapshots table in db.py maps URL + hash to this path.
"""

import hashlib
from pathlib import Path
from urllib.parse import urlparse


def sha256(data: bytes) -> str:
    """Hex SHA-256 of the bytes: the content's id and its version number."""
    return hashlib.sha256(data).hexdigest()


def extension_for(url: str, content_type: str | None) -> str:
    """File extension for the saved copy: from the URL if it has one, else .html for pages, else .bin."""
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    if content_type and "html" in content_type:
        return ".html"
    return ".bin"


def save_blob(raw_dir: Path, data: bytes, url: str, content_type: str | None) -> tuple[str, Path]:
    """Saves the bytes under data/raw/<xx>/<hash>.<ext> (once; identical content is not written twice). Returns (hash, path)."""
    digest = sha256(data)
    path = raw_dir / digest[:2] / f"{digest}{extension_for(url, content_type)}"
    if not path.exists():  # already saved: identical content, nothing to write
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return digest, path
