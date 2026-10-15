"""PDF fact extraction with an LLM through OpenRouter.

Design rules:
- Only text the PDF really contains is sent, tagged by page, so the model can cite pages.
- The model must answer in a strict JSON schema.
- Every fact must quote a short excerpt; we check that the excerpt really appears on the
  cited page and drop the fact if it does not (a cheap hallucination guard).
- Results are cached by (content hash, model, prompt version), so unchanged files are never paid for twice.

Where each part lives:
- Prompt and JSON schema: PROMPT_TEMPLATE, response_schema() below. Site details come from the Source.
- What is sent: build_document_text(), page-tagged text, start + end of long documents.
- Hallucination guard: excerpt_on_page() and verify_facts().
- The API call and real cost: OpenRouterExtractor.extract().
- Cache, budget cap, dry run, concurrency: extract_stage.py, run_llm().
Only PDFs go to the LLM. Pages are read exactly from their embedded JSON, for free.
"""

import json
import os
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

PROMPT_VERSION = 2  # 2: long documents send start + end instead of start only
DEFAULT_MODEL = "google/gemini-3.1-flash-lite"
MAX_INPUT_CHARS = 40_000  # about 10k tokens; long reports are cut and marked truncated
DEFAULT_RUN_BUDGET_USD = 0.50  # hard stop per run; protects the shared API credit

# USD per million tokens (input, output), from openrouter.ai/api/v1/models on 2026-10-14.
# Only used for the dry-run estimate; real cost always comes from OpenRouter's response.
KNOWN_PRICES = {
    "google/gemini-3.1-flash-lite": (0.25, 1.50),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "openai/gpt-5-mini": (0.25, 2.00),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
}
EST_OUTPUT_TOKENS = 800
PROMPT_OVERHEAD_TOKENS = 600  # system prompt + JSON schema


def estimate_cost(model: str, pages_per_doc: list[list[str]]) -> tuple[int, float | None]:
    """Rough cost before calling: about 4 characters per token."""
    tokens_in = sum(len(build_document_text(p)[0]) // 4 + PROMPT_OVERHEAD_TOKENS for p in pages_per_doc)
    price = KNOWN_PRICES.get(model)
    if price is None:
        return tokens_in, None
    cost = tokens_in / 1e6 * price[0] + len(pages_per_doc) * EST_OUTPUT_TOKENS / 1e6 * price[1]
    return tokens_in, cost

DOC_TYPES = [
    "board_agenda", "board_minutes", "committee_agenda", "committee_minutes", "staff_report",
    "annual_report", "financial_statement", "plan_or_study", "presentation", "brochure_or_listing",
    "notice", "policy", "procurement", "other",
]
FACT_KINDS = ["address", "project", "decision", "amount", "date", "phase", "organization", "other"]

PROMPT_TEMPLATE = """You extract facts from public documents published by {publisher}.
The document text is split into pages marked [page N]. Use only what the text says. Do not guess.
For every fact give the page number where it appears and a short excerpt copied exactly from that page (5 to 25 words).
{focus}
Return at most 15 facts. If a field is unknown, use null."""

DEFAULT_FOCUS = """Prefer facts a researcher would want: places and addresses, named projects or programs,
decisions, dollar amounts, key dates, and status changes."""


def system_prompt(publisher: str, focus: str | None = None) -> str:
    """The site-specific parts (who publishes, what matters) come from the Source."""
    return PROMPT_TEMPLATE.format(publisher=publisher, focus=focus or DEFAULT_FOCUS)


def response_schema(body_example: str) -> dict:
    return {
        "name": "document_facts",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["doc_type", "title", "document_date", "organization", "summary", "facts"],
            "properties": {
                "doc_type": {"type": "string", "enum": DOC_TYPES},
                "title": {"type": ["string", "null"]},
                "document_date": {"type": ["string", "null"], "description": "YYYY-MM-DD if stated in the text"},
                "organization": {"type": ["string", "null"], "description": f"Body that issued it, e.g. {body_example}"},
                "summary": {"type": "string", "description": "One or two plain sentences"},
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "value", "page", "excerpt"],
                        "properties": {
                            "kind": {"type": "string", "enum": FACT_KINDS},
                            "value": {"type": "string"},
                            "page": {"type": "integer"},
                            "excerpt": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


@dataclass
class LlmResult:
    data: dict
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    truncated: bool
    pages_sent: list[int]


def _page_chunk(number: int, text: str) -> str:
    return f"[page {number}]\n{' '.join(text.split())}\n"


def build_document_text(pages: list[str]) -> tuple[str, bool, list[int]]:
    """Page-tagged text for the model. Returns (text, truncated, page numbers sent).

    Long documents keep their start (about 70% of the budget) and their end (about 30%),
    because amendments, signatures and final decisions often sit at the end.
    """
    chunks = [_page_chunk(n, t) for n, t in enumerate(pages, start=1)]
    if sum(map(len, chunks)) <= MAX_INPUT_CHARS:
        return "".join(chunks), False, list(range(1, len(pages) + 1))

    head, size = [], 0
    for i, chunk in enumerate(chunks):
        if size + len(chunk) > MAX_INPUT_CHARS * 0.7:
            break
        head.append(i)
        size += len(chunk)
    tail = []
    for i in range(len(chunks) - 1, (head[-1] if head else -1), -1):
        if size + len(chunks[i]) > MAX_INPUT_CHARS:
            break
        tail.insert(0, i)
        size += len(chunks[i])
    if not head and not tail:  # a single huge page: send its beginning
        return chunks[0][:MAX_INPUT_CHARS], True, [1]

    parts = [chunks[i] for i in head]
    first_omitted = (head[-1] + 2) if head else 1
    last_omitted = (tail[0]) if tail else len(chunks)
    if first_omitted <= last_omitted:
        parts.append(f"[pages {first_omitted}-{last_omitted} omitted]\n")
    parts += [chunks[i] for i in tail]
    return "".join(parts), True, [i + 1 for i in head + tail]


VERIFY_VERSION = 4  # bump when the excerpt check changes; re-checks reuse cached answers for free


def _norm(text: str) -> str:
    """Lowercase letters, digits and $ % only. Removing all spacing also survives PDF text
    layers that split words or numbers oddly (for example "50 0,000" for "500,000")."""
    return re.sub(r"[^a-z0-9$%]+", "", text.lower())


MAX_GAP_CHARS = 300  # room for a page footer or header between the two halves of a quote
MIN_SIDE_WORDS = 4  # each side of the gap must still be a real, verbatim phrase


def excerpt_on_page(excerpt: str, page_text: str) -> bool:
    """True if the excerpt (ignoring case, spacing and punctuation) appears in the text.

    If not found whole, it may be split once into two parts (4+ words each) that both appear
    word for word, in order, close together.
    This allows a running page header or footer inside the quote, but nothing invented.
    """
    needle, hay = _norm(excerpt), _norm(page_text)
    if not needle:
        return False
    if needle in hay:
        return True
    words = excerpt.split()
    for cut in range(MIN_SIDE_WORDS, len(words) - MIN_SIDE_WORDS + 1):
        first, second = _norm(" ".join(words[:cut])), _norm(" ".join(words[cut:]))
        start = hay.find(first)
        while start != -1:
            after = start + len(first)
            if second in hay[after: after + MAX_GAP_CHARS + len(second)]:
                return True
            start = hay.find(first, start + 1)
    return False


def verify_facts(facts: list[dict], pages: list[str]) -> tuple[list[dict], list[dict]]:
    """Keeps a fact only if its excerpt is on the cited page. A quote may run over the
    page break into the previous or next page, since sentences often do."""
    good, bad = [], []
    for fact in facts:
        page, excerpt = fact.get("page"), fact.get("excerpt", "")
        ok = False
        if isinstance(page, int) and 1 <= page <= len(pages):
            i = page - 1
            windows = [pages[i], "\n".join(pages[max(0, i - 1): i + 1]), "\n".join(pages[i: i + 2])]
            ok = any(excerpt_on_page(excerpt, w) for w in windows)
        (good if ok else bad).append(fact)
    return good, bad


class OpenRouterExtractor:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self.model = model
        self.client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, max_retries=3, timeout=120)

    @property
    def cache_key_suffix(self) -> str:
        # Cache key = content hash (in the db) + this. Same file, model and prompt -> no new call.
        """Identifies a paid answer. Changing the model or prompt means a new paid call."""
        return f"{self.model}|v{PROMPT_VERSION}"

    async def extract(self, url: str, pages: list[str], prompt: str, schema: dict) -> LlmResult:
        text, truncated, pages_sent = build_document_text(pages)
        resp = await self.client.chat.completions.create(
            model=self.model,
            temperature=0,  # same input, same answer as far as possible
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Source URL: {url}\n\n{text}"},
            ],
            # Strict JSON schema: the reply must match the schema, so no free-text parsing.
            response_format={"type": "json_schema", "json_schema": schema},
            extra_body={"usage": {"include": True}},  # OpenRouter then returns the real cost
        )
        content = resp.choices[0].message.content or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"model returned invalid JSON: {content[:200]!r}") from exc
        usage = resp.usage
        cost = float((usage.model_extra or {}).get("cost") or 0.0) if usage else 0.0
        return LlmResult(
            data=data,
            model=resp.model or self.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cost_usd=cost,
            truncated=truncated,
            pages_sent=pages_sent,
        )

    async def close(self) -> None:
        await self.client.close()
