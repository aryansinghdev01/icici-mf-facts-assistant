"""Page-bounded chunking with full provenance (Phase 1.5).

Chunks are cut directly out of each normalised page (never rewritten), never span two
pages or two documents, and carry everything needed to cite them: source, scheme,
document type, official URL, page number, section heading, fact tags, and source date.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config import Settings, get_settings
from app.quality import NormalisedDocument, NormalisedPage
from app.registry import SourceRecord, load_registry
from app.source_policy import validate_source_url

# Token budget, measured with the BGE tokenizer. The hard cap leaves room under BGE's
# 512-token limit for special tokens and a short retrieval context header.
TARGET_TOKENS = 400
MAX_TOKENS = 480
OVERLAP_TOKENS = 60
# A cut is only moved back to a line/sentence break if the chunk keeps at least this many tokens.
MIN_TOKENS_BEFORE_BREAK = 250

# (start_char, end_char) offsets of each token in a text.
Encoder = Callable[[str], list[tuple[int, int]]]

FACT_TAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "expense_ratio": re.compile(r"expense ratio|\bter\b|recurring expenses", re.IGNORECASE),
    "exit_load": re.compile(r"exit load|load for redemption", re.IGNORECASE),
    "minimum_investment": re.compile(r"minimum (?:application|additional|investment|amount)|min\.? ?addl|application amount", re.IGNORECASE),
    "sip": re.compile(r"\bsip\b|systematic investment", re.IGNORECASE),
    "benchmark": re.compile(r"benchmark", re.IGNORECASE),
    "riskometer": re.compile(r"riskometer|risk-o-meter|risk of the scheme", re.IGNORECASE),
    "lock_in": re.compile(r"lock[- ]?in", re.IGNORECASE),
    "nav": re.compile(r"\bnav\b|net asset value", re.IGNORECASE),
    "fund_manager": re.compile(r"fund manager", re.IGNORECASE),
    "investment_objective": re.compile(r"investment objective", re.IGNORECASE),
    "statement": re.compile(r"account statement|capital gains? statement|\bcas\b", re.IGNORECASE),
    "plans_options": re.compile(r"direct plan|regular plan|\bidcw\b|growth option", re.IGNORECASE),
}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    chunk_id: str
    chunk_index: int = Field(ge=0)
    source_id: str
    # None for general documents (SAI, investor guides); scheme_name then names the publisher.
    scheme_id: str | None
    scheme_name: str
    document_type: str
    official_url: str
    page_number: int = Field(ge=1)
    section_title: str | None
    text: str = Field(min_length=1)
    token_count: int = Field(ge=1, le=MAX_TOKENS)
    fact_tags: tuple[str, ...]
    source_updated_date: date
    source_date_basis: Literal["document", "fetch"]
    extraction_method: Literal["native", "ocr"]
    # OCR chunks only: numbers in this chunk that the second OCR pass did not read identically.
    ocr_unconfirmed_numbers: tuple[str, ...] | None
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_version: int = Field(ge=1)

    @model_validator(mode="after")
    def provenance_must_be_complete(self) -> "Chunk":
        if not validate_source_url(self.official_url).approved:
            raise ValueError("chunk official_url is not an approved source")
        if not self.chunk_id.startswith(f"{self.source_id}-"):
            raise ValueError("chunk_id must be derived from its source_id")
        if (self.extraction_method == "ocr") != (self.ocr_unconfirmed_numbers is not None):
            raise ValueError("only OCR chunks carry OCR number confirmation")
        return self


@dataclass
class ChunkingResult:
    source_id: str
    status: Literal["chunked", "skipped", "failed"]
    chunks: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)


@lru_cache
def bge_encoder(model_name: str) -> Encoder:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_pretrained(model_name)

    def encode(text: str) -> list[tuple[int, int]]:
        return list(tokenizer.encode(text, add_special_tokens=False).offsets)

    return encode


def _is_heading(line: str) -> bool:
    """Short all-caps titles ('EXPENSES OF THE SCHEME:') or label lines ending in ':' ('Load Structure:')."""
    stripped = line.strip().rstrip(":").strip()
    words = re.findall(r"[A-Za-z]{2,}", stripped)
    # Table cells ('BSE 500 TRI'), figures and OCR fragments are not headings.
    if not 2 <= len(words) <= 10 or len(stripped) > 80 or sum(ch.isdigit() for ch in stripped) > 2:
        return False
    if stripped.isupper():
        return True
    return line.strip().endswith(":") and stripped[0].isupper() and len(words) <= 6


def _last_heading(text: str) -> str | None:
    heading = None
    for line in text.split("\n"):
        line = line.strip()
        if _is_heading(line):
            heading = line.rstrip(":").strip()
    return heading


def fact_tags(text: str) -> tuple[str, ...]:
    return tuple(tag for tag, pattern in FACT_TAG_PATTERNS.items() if pattern.search(text))


def numbers_in(text: str) -> set[str]:
    return {match.replace(",", "") for match in _NUMBER.findall(text)}


def split_page(text: str, encode: Encoder) -> list[tuple[int, int, int]]:
    """Return (start_char, end_char, token_count) windows covering the page text."""
    offsets = encode(text)
    if not offsets:
        return []
    windows: list[tuple[int, int, int]] = []
    start = 0
    total = len(offsets)
    while start < total:
        if total - start <= MAX_TOKENS:
            end = total
        else:
            end = start + TARGET_TOKENS
            # Prefer to cut just after a line break, then a sentence end, within the window.
            region_start = offsets[start + MIN_TOKENS_BEFORE_BREAK][0]
            region = text[region_start : offsets[end - 1][1]]
            cut = max(region.rfind("\n"), -1)
            if cut < 0:
                sentence = max(region.rfind(". "), region.rfind("; "))
                cut = sentence + 1 if sentence >= 0 else -1
            if cut >= 0:
                cut_char = region_start + cut
                end = next((i for i in range(start + MIN_TOKENS_BEFORE_BREAK, end) if offsets[i][0] >= cut_char), end)
        windows.append((offsets[start][0], offsets[end - 1][1], end - start))
        if end >= total:
            break
        # Overlap the next window, starting it on a word boundary rather than mid-word.
        next_start = max(end - OVERLAP_TOKENS, start + 1)
        while next_start < end and next_start > 0 and offsets[next_start][0] == offsets[next_start - 1][1]:
            next_start += 1
        start = next_start
    return windows


class SourceChunker:
    def __init__(
        self,
        *,
        registry_file: Path | None = None,
        processed_dir: Path | None = None,
        encoder: Encoder | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.registry_file = registry_file or settings.resolved_path(settings.source_registry_path)
        self.processed_dir = processed_dir or settings.resolved_path(settings.processed_dir)
        self._encoder = encoder
        self._model_name = settings.bge_model

    @property
    def encode(self) -> Encoder:
        if self._encoder is None:
            self._encoder = bge_encoder(self._model_name)
        return self._encoder

    def chunk_all(self) -> tuple[list[ChunkingResult], list[Chunk]]:
        registry = load_registry(self.registry_file)
        results: list[ChunkingResult] = []
        chunks: list[Chunk] = []
        for source in registry.sources:
            result, source_chunks = self.chunk_source(source)
            results.append(result)
            chunks.extend(source_chunks)
        self._write_jsonl(self.processed_dir / "chunks.jsonl", chunks)
        self._write_report(results, chunks)
        return results, chunks

    def chunk_source(self, source: SourceRecord) -> tuple[ChunkingResult, list[Chunk]]:
        if source.status != "approved" or not source.content_hash:
            return ChunkingResult(source.source_id, "skipped", reasons=[f"source_status_{source.status}"]), []
        path = self.processed_dir / f"{source.source_id}.{source.content_hash[:16]}.json"
        if not path.is_file():
            return ChunkingResult(source.source_id, "failed", reasons=["missing_quality_passed_document"]), []
        document = NormalisedDocument.model_validate_json(path.read_text(encoding="utf-8"))
        if document.content_hash != source.content_hash or document.scheme_id != source.scheme_id:
            return ChunkingResult(source.source_id, "failed", reasons=["stale_or_mismatched_document"]), []

        if source.published_date:
            source_date, basis = source.published_date, "document"
        else:
            assert source.retrieved_at is not None
            source_date, basis = source.retrieved_at.date(), "fetch"

        result = ChunkingResult(source.source_id, "chunked")
        chunks: list[Chunk] = []
        carried_heading: str | None = None
        for page in document.pages:
            for chunk in self._chunk_page(source, document, page, carried_heading, source_date, basis, len(chunks), result):
                chunks.append(chunk)
            carried_heading = _last_heading(page.text) or carried_heading
        result.chunks = len(chunks)
        if not chunks:
            result.status = "failed"
            result.reasons.append("no_valid_chunks")
        return result, chunks

    def _chunk_page(
        self,
        source: SourceRecord,
        document: NormalisedDocument,
        page: NormalisedPage,
        carried_heading: str | None,
        source_date: date,
        basis: str,
        first_index: int,
        result: ChunkingResult,
    ) -> list[Chunk]:
        check_numbers = numbers_in(page.ocr_check_text or "") if page.extraction_method == "ocr" else None
        chunks: list[Chunk] = []
        for start, end, token_count in split_page(page.text, self.encode):
            text = page.text[start:end].strip()
            if not text:
                continue
            index = first_index + len(chunks)
            unconfirmed = None
            if check_numbers is not None:
                unconfirmed = tuple(sorted(numbers_in(text) - check_numbers))
            try:
                chunks.append(
                    Chunk(
                        chunk_id=f"{source.source_id}-v{source.content_version}-p{page.page_number:03d}-c{index:03d}",
                        chunk_index=index,
                        source_id=source.source_id,
                        scheme_id=source.scheme_id,
                        scheme_name=source.scheme_name,
                        document_type=source.document_type,
                        official_url=str(source.official_url),
                        page_number=page.page_number,
                        # The heading in effect where the chunk starts, including one on its first line.
                        section_title=_last_heading(page.text[: start + len(text.split("\n", 1)[0])]) or carried_heading,
                        text=text,
                        token_count=token_count,
                        fact_tags=fact_tags(text),
                        source_updated_date=source_date,
                        source_date_basis=basis,
                        extraction_method=page.extraction_method,
                        ocr_unconfirmed_numbers=unconfirmed,
                        content_hash=document.content_hash,
                        content_version=source.content_version,
                    )
                )
            except ValidationError as error:
                result.rejected += 1
                result.reasons.append(f"rejected_chunk_p{page.page_number}: {error.errors()[0]['msg']}")
        return chunks

    def _write_jsonl(self, path: Path, chunks: list[Chunk]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            for chunk in chunks:
                handle.write(json.dumps(chunk.model_dump(mode="json"), ensure_ascii=False) + "\n")
        temporary.replace(path)

    def _write_report(self, results: list[ChunkingResult], chunks: list[Chunk]) -> None:
        by_source: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            by_source.setdefault(chunk.source_id, []).append(chunk)
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "settings": {"target_tokens": TARGET_TOKENS, "max_tokens": MAX_TOKENS, "overlap_tokens": OVERLAP_TOKENS},
            "total_chunks": len(chunks),
            "sources": [
                {
                    "source_id": r.source_id,
                    "status": r.status,
                    "chunks": r.chunks,
                    "rejected": r.rejected,
                    "ocr_chunks_with_unconfirmed_numbers": sum(bool(c.ocr_unconfirmed_numbers) for c in by_source.get(r.source_id, [])),
                    "reasons": r.reasons,
                }
                for r in results
            ],
        }
        path = self.processed_dir / "chunk_report.json"
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def main() -> int:
    results, chunks = SourceChunker().chunk_all()
    for r in results:
        detail = f"chunks={r.chunks}" + (f" rejected={r.rejected}" if r.rejected else "") + (f"  {'; '.join(r.reasons[:2])}" if r.reasons else "")
        print(f"{r.status:<8} {r.source_id:<34} {detail}")
    chunked = sum(r.status == "chunked" for r in results)
    print(f"\n{chunked}/{len(results)} sources chunked, {len(chunks)} chunks total")
    return 0 if chunked == len(results) and not any(r.rejected for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
