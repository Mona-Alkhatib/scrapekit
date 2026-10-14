"""Runtime settings shared by every source. Site-specific settings live in `sources/`."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    user_agent: str = "scrapekit/0.1 (research crawler; polite, 1 req/s per host)"
    data_dir: Path = Path("data")
    runs_dir: Path = Path("runs")
    seconds_between_requests: float = 1.0  # per host
    workers: int = 4
    max_file_bytes: int = 50 * 1024 * 1024
    timeout_seconds: float = 30.0
    max_retries: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scrapekit.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"
