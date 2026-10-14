"""Counters for one run, plus the JSON and Markdown summaries we hand in."""

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunStats:
    run_id: str
    mode: str
    started: float = field(default_factory=time.monotonic)
    duration_seconds: float = 0.0
    requests: int = 0
    status_counts: Counter = field(default_factory=Counter)
    pages_fetched: int = 0
    pages_unchanged: int = 0
    pages_changed: int = 0
    files_fetched: int = 0
    files_unchanged: int = 0
    files_changed: int = 0
    files_not_rechecked: int = 0  # immutable URLs with a saved copy: no request at all
    bytes_downloaded: int = 0
    skips: Counter = field(default_factory=Counter)
    llm_calls: int = 0
    llm_cached: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0
    llm_failed: int = 0
    llm_facts_kept: int = 0
    llm_facts_dropped: int = 0  # excerpt not found on the cited page
    llm_model: str = ""
    docs_extracted: int = 0
    docs_extract_cached: int = 0  # same content as last time, skipped
    docs_extract_failed: int = 0
    entities: int = 0  # distinct projects, news items, pages and documents with current facts
    records_new: int = 0  # a record is one fact: one field of one entity
    records_updated: int = 0
    records_unchanged: int = 0
    records_removed: int = 0

    def finish(self) -> None:
        self.duration_seconds = round(time.monotonic() - self.started, 1)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "started"}
        d["status_counts"] = {str(k): v for k, v in sorted(self.status_counts.items(), key=str)}
        d["skips"] = dict(self.skips.most_common())
        d["llm_cost_usd"] = round(self.llm_cost_usd, 6)
        return d

    def write(self, runs_dir: Path) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"{self.run_id}.json").write_text(json.dumps(self.to_dict(), indent=2))
        md = runs_dir / f"{self.run_id}.md"
        md.write_text(self.to_markdown())
        return md

    def to_markdown(self) -> str:
        d = self.to_dict()
        lines = [
            f"# Run {self.run_id} ({self.mode})",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Duration (s) | {d['duration_seconds']} |",
            f"| HTTP requests | {d['requests']} |",
            f"| Status codes | {d['status_counts']} |",
            f"| Pages fetched / unchanged / changed | {self.pages_fetched} / {self.pages_unchanged} / {self.pages_changed} |",
            f"| Files fetched / unchanged / changed | {self.files_fetched} / {self.files_unchanged} / {self.files_changed} |",
            f"| Files not re-checked (immutable URL, saved copy) | {self.files_not_rechecked} |",
            f"| Bytes downloaded | {self.bytes_downloaded:,} |",
            f"| Documents extracted / skipped as unchanged / failed | {self.docs_extracted} / {self.docs_extract_cached} / {self.docs_extract_failed} |",
            f"| Entities with current facts | {self.entities} |",
            f"| Records (facts) new / updated / unchanged / removed | {self.records_new} / {self.records_updated} / {self.records_unchanged} / {self.records_removed} |",
            f"| LLM model | {self.llm_model or 'not used'} |",
            f"| Paid LLM calls / cache hits / failed | {self.llm_calls} / {self.llm_cached} / {self.llm_failed} |",
            f"| LLM facts kept / dropped by excerpt check | {self.llm_facts_kept} / {self.llm_facts_dropped} |",
            f"| LLM tokens in / out | {self.llm_input_tokens:,} / {self.llm_output_tokens:,} |",
            f"| LLM cost (USD, as reported by OpenRouter) | ${d['llm_cost_usd']:.4f} |",
            f"| Skipped links by reason | {d['skips']} |",
            "",
        ]
        return "\n".join(lines)
