"""Conservative normalisation and per-source quality gate (Phase 1.4).

Only sources that pass are written as normalised documents for chunking. A source
that fails is marked ``quarantined`` in the registry with machine-readable reasons.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.extractor import ExtractedDocument, PageRecord
from app.registry import Scheme, SourceRecord, SourceRegistry, load_registry, write_registry

QualityStatus = Literal["passed", "quarantined"]

# The cover pages are where each document states which scheme it is for.
COVER_PAGES = 3
EARLIEST_PLAUSIBLE_DATE = date(2015, 1, 1)
USABLE_PAGE_STATUSES = frozenset({"ok", "review_required"})

_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_DATE = rf"({_MONTH}\s+\d{{1,2}}\s*,\s*\d{{4}})"
# Each document type states its own date in a fixed phrase; other dates in the text
# (portfolio dates, circular dates, w.e.f. dates) must not be mistaken for it.
DATE_PATTERNS: dict[str, re.Pattern[str]] = {
    "KIM": re.compile(rf"(?:KIM|Key Information Memorandum) is dated (?:on )?{_DATE}", re.IGNORECASE),
    "SID": re.compile(rf"Scheme Information Document is dated (?:on )?{_DATE}", re.IGNORECASE),
    "FACTSHEET": re.compile(rf"Growth Option as on {_DATE}", re.IGNORECASE),
    "SAI": re.compile(rf"SAI is dated (?:on )?{_DATE}", re.IGNORECASE),
    "INVESTOR_GUIDE": re.compile(rf"updated as on {_DATE}", re.IGNORECASE),
}
# General documents are checked for their publisher instead of a scheme name.
EXPECTED_PUBLISHER: dict[str, re.Pattern[str]] = {
    "SAI": re.compile(r"ICICI Prudential Mutual Fund", re.IGNORECASE),
    "INVESTOR_GUIDE": re.compile(r"\bSEBI\b|Securities and Exchange Board of India|\bAMFI\b|Association of Mutual Funds", re.IGNORECASE),
}


class NormalisedPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_number: int
    extraction_method: str
    status: str
    text: str
    ocr_check_text: str | None = None


class NormalisedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: str
    scheme_id: str | None
    document_type: str
    official_url: str
    content_hash: str
    content_version: int
    document_date: date | None
    warnings: tuple[str, ...]
    pages: tuple[NormalisedPage, ...]


@dataclass
class QualityResult:
    source_id: str
    status: QualityStatus
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    document_date: date | None = None
    usable_pages: int = 0
    total_pages: int = 0
    output_file_name: str | None = None


def normalise_text(text: str) -> str:
    """Whitespace/character clean-up only; wording, numbers, and line order are preserved."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("­", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t  -​]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _name_key(text: str) -> str:
    """Comparison key tolerant of '&'/'and', spacing ('Flexi Cap'/'Flexicap'), case and punctuation."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", text)


def _scheme_keys(scheme: Scheme) -> set[str]:
    names = {scheme.scheme_name, *(f"ICICI Prudential {alias}" for alias in scheme.aliases)}
    return {_name_key(name) for name in names}


def scheme_mentions(text: str, registry: SourceRegistry) -> dict[str, int]:
    key = _name_key(text)
    return {scheme.scheme_id: sum(key.count(name) for name in _scheme_keys(scheme)) for scheme in registry.schemes}


def find_document_date(document_type: str, pages: tuple[PageRecord, ...]) -> str | None:
    pattern = DATE_PATTERNS.get(document_type)
    if pattern is None:
        return None
    for page in pages:
        match = pattern.search(re.sub(r"\s+", " ", page.text))
        if match:
            return match.group(1)
    return None


def parse_document_date(raw: str) -> date | None:
    cleaned = re.sub(r"\s*,\s*", ", ", raw.strip())
    try:
        return datetime.strptime(cleaned, "%B %d, %Y").date()
    except ValueError:
        return None


class QualityChecker:
    def __init__(
        self,
        *,
        registry_file: Path | None = None,
        extracted_text_dir: Path | None = None,
        processed_dir: Path | None = None,
        today: date | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.registry_file = registry_file or settings.resolved_path(settings.source_registry_path)
        self.extracted_text_dir = extracted_text_dir or settings.resolved_path(settings.extracted_text_dir)
        self.processed_dir = processed_dir or settings.resolved_path(settings.processed_dir)
        self.today = today or datetime.now(UTC).date()

    def check_all(self) -> list[QualityResult]:
        registry = load_registry(self.registry_file)
        results: list[QualityResult] = []
        seen_hashes: dict[str, str] = {}
        updated_sources: dict[str, SourceRecord] = {}
        for source in registry.sources:
            if source.status not in {"approved", "quarantined"}:
                results.append(QualityResult(source.source_id, "quarantined", reasons=[f"source_status_{source.status}"]))
                continue
            result, document = self._check(source, registry, seen_hashes)
            if source.content_hash:
                seen_hashes.setdefault(source.content_hash, source.source_id)
            self._persist(source, result, document)
            updated_sources[source.source_id] = self._registry_update(source, result)
            results.append(result)

        new_registry = registry.model_copy(
            update={"sources": tuple(updated_sources.get(source.source_id, source) for source in registry.sources)}
        )
        write_registry(SourceRegistry.model_validate(new_registry.model_dump(mode="json")), self.registry_file)
        self._write_report(results)
        return results

    def _check(
        self, source: SourceRecord, registry: SourceRegistry, seen_hashes: dict[str, str]
    ) -> tuple[QualityResult, NormalisedDocument | None]:
        result = QualityResult(source.source_id, "passed")

        if not source.content_hash or not source.raw_file_name or not source.retrieved_at:
            result.reasons.append("missing_registry_metadata")
        elif source.content_hash in seen_hashes:
            result.reasons.append(f"duplicate_content_hash_of_{seen_hashes[source.content_hash]}")

        extracted = self._load_extraction(source)
        if extracted is None:
            result.reasons.append("missing_extraction")
            return self._finish(result), None
        if extracted.content_hash != source.content_hash or extracted.scheme_id != source.scheme_id:
            result.reasons.append("stale_or_mismatched_extraction")

        result.total_pages = extracted.page_count
        result.usable_pages = sum(page.status in USABLE_PAGE_STATUSES for page in extracted.pages)
        if result.usable_pages == 0:
            result.reasons.append("no_usable_pages")
        if any(page.status in {"ocr_unavailable", "failed"} for page in extracted.pages):
            result.reasons.append("incomplete_extraction")
        if any(page.status == "review_required" for page in extracted.pages):
            result.warnings.append("ocr_pages_require_review")
        blank = sum(page.status == "blank" for page in extracted.pages)
        if blank:
            result.warnings.append(f"blank_pages_{blank}")

        cover = "\n".join(page.text for page in extracted.pages[:COVER_PAGES])
        if source.scheme_id is None:
            publisher = EXPECTED_PUBLISHER.get(source.document_type)
            if publisher is None or not publisher.search(cover):
                result.reasons.append("expected_publisher_not_found")
        else:
            mentions = scheme_mentions(cover, registry)
            expected = mentions.get(source.scheme_id, 0)
            if expected == 0:
                result.reasons.append("expected_scheme_name_not_found")
            elif any(count > expected for scheme_id, count in mentions.items() if scheme_id != source.scheme_id):
                result.reasons.append("other_scheme_dominates_cover")

        raw_date = find_document_date(source.document_type, extracted.pages)
        if raw_date is None:
            result.warnings.append("no_document_date_using_fetch_date")
        else:
            parsed = parse_document_date(raw_date)
            if parsed is None:
                result.reasons.append("invalid_document_date")
            elif parsed > self.today or parsed < EARLIEST_PLAUSIBLE_DATE:
                result.reasons.append("implausible_document_date")
            else:
                result.document_date = parsed

        result = self._finish(result)
        if result.status != "passed":
            return result, None
        document = NormalisedDocument(
            source_id=source.source_id,
            scheme_id=source.scheme_id,
            document_type=source.document_type,
            official_url=str(source.official_url),
            content_hash=extracted.content_hash,
            content_version=extracted.content_version,
            document_date=result.document_date,
            warnings=tuple(result.warnings),
            pages=tuple(
                NormalisedPage(
                    page_number=page.page_number,
                    extraction_method=page.extraction_method,
                    status=page.status,
                    text=normalise_text(page.text),
                    ocr_check_text=normalise_text(page.ocr_check_text) if page.ocr_check_text is not None else None,
                )
                for page in extracted.pages
                if page.status in USABLE_PAGE_STATUSES
            ),
        )
        return result, document

    @staticmethod
    def _finish(result: QualityResult) -> QualityResult:
        result.status = "quarantined" if result.reasons else "passed"
        return result

    def _load_extraction(self, source: SourceRecord) -> ExtractedDocument | None:
        if not source.content_hash:
            return None
        path = self.extracted_text_dir / f"{source.source_id}.{source.content_hash[:16]}.json"
        if not path.is_file():
            return None
        return ExtractedDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def _persist(self, source: SourceRecord, result: QualityResult, document: NormalisedDocument | None) -> None:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.processed_dir.glob(f"{source.source_id}.*.json"):
            stale.unlink()
        if document is None:
            return
        name = f"{source.source_id}.{document.content_hash[:16]}.json"
        self._atomic_write(self.processed_dir / name, document.model_dump(mode="json"))
        result.output_file_name = name

    @staticmethod
    def _registry_update(source: SourceRecord, result: QualityResult) -> SourceRecord:
        data = source.model_dump(mode="json")
        if result.status == "passed":
            data.update(status="approved", failure_reason=None, published_date=result.document_date.isoformat() if result.document_date else None)
        else:
            data.update(status="quarantined", failure_reason="Quality check failed: " + ", ".join(result.reasons))
        return SourceRecord.model_validate(data)

    def _write_report(self, results: list[QualityResult]) -> None:
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "sources": [
                {
                    "source_id": r.source_id,
                    "status": r.status,
                    "document_date": r.document_date.isoformat() if r.document_date else None,
                    "usable_pages": r.usable_pages,
                    "total_pages": r.total_pages,
                    "reasons": r.reasons,
                    "warnings": r.warnings,
                }
                for r in results
            ],
        }
        self._atomic_write(self.processed_dir / "quality_report.json", report)

    @staticmethod
    def _atomic_write(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)


def main() -> int:
    results = QualityChecker().check_all()
    for r in results:
        detail = ", ".join(r.reasons) if r.reasons else f"date={r.document_date or 'none'} pages={r.usable_pages}/{r.total_pages}"
        if r.warnings:
            detail += f"  [{', '.join(r.warnings)}]"
        print(f"{r.status:<11} {r.source_id:<34} {detail}")
    passed = sum(r.status == "passed" for r in results)
    print(f"\n{passed}/{len(results)} sources passed quality checks")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
