"""Raw copies of every page and file, stored by content hash so identical bytes are saved once.

A file is saved as data/raw/<first 2 hash chars>/<sha256>.<ext>. The hash is also the version id:
the same bytes always get the same name (stored once, even if linked from 3 URLs), and a new hash
for a URL means its content changed. The snapshots table in db.py maps URL + hash to this path.
"""

import hashlib
from pathlib import Path
from urllib.parse import urlparse


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extension_for(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    if content_type and "html" in content_type:
        return ".html"
    return ".bin"


def save_blob(raw_dir: Path, data: bytes, url: str, content_type: str | None) -> tuple[str, Path]:
    digest = sha256(data)
    path = raw_dir / digest[:2] / f"{digest}{extension_for(url, content_type)}"
    if not path.exists():  # already saved: identical content, nothing to write
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return digest, path
