"""Page-level text extraction for approved raw sources, with an OCR fallback."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

import pymupdf
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.registry import SourceRecord, load_registry

ExtractionMethod = Literal["native", "ocr", "none"]
PageStatus = Literal["ok", "review_required", "blank", "ocr_unavailable", "failed"]

# Bump when the page-record format or extraction logic changes, so old outputs are rebuilt.
EXTRACTOR_SCHEMA = 2
# Pages with fewer word characters than this are treated as having no usable native text.
MIN_USABLE_CHARS = 40
_BLANK_PAGE_PATTERN = re.compile(r"^\W*this page (has been )?(left )?intentionally (left )?blank\W*$", re.IGNORECASE)

OcrFunction = Callable[[bytes], str]


class OcrUnavailableError(Exception):
    """The OCR engine is not installed or could not run."""


class PageRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_number: int = Field(ge=1)
    extraction_method: ExtractionMethod
    status: PageStatus
    char_count: int = Field(ge=0)
    text: str
    # A second, independent OCR pass at a different resolution. A number is only treated as
    # verified OCR evidence when both passes read it identically (see app.chunker).
    ocr_check_text: str | None = None
    reason: str | None = None


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: str
    scheme_id: str | None
    document_type: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_version: int
    raw_file_name: str
    extracted_at: datetime
    extractor: dict[str, object]
    page_count: int = Field(ge=1)
    pages: tuple[PageRecord, ...]


@dataclass(frozen=True)
class ExtractionResult:
    source_id: str
    status: Literal["extracted", "failed", "skipped"]
    reason: str | None = None
    output_file_name: str | None = None
    reused: bool = False
    page_statuses: dict[str, int] | None = None


def _word_chars(text: str) -> int:
    return len(re.sub(r"\W", "", text))


def tesseract_ocr(tesseract_cmd: str) -> OcrFunction:
    """Return an OCR function using Tesseract's own layout analysis (keeps columns apart)."""

    def run(png: bytes) -> str:
        if shutil.which(tesseract_cmd) is None:
            raise OcrUnavailableError(f"OCR engine not found: {tesseract_cmd}")
        try:
            completed = subprocess.run(
                [tesseract_cmd, "stdin", "stdout", "--psm", "3", "-l", "eng"],
                input=png,
                capture_output=True,
                check=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            raise OcrUnavailableError(f"OCR failed: {type(error).__name__}") from error
        return completed.stdout.decode("utf-8", errors="replace")

    return run


def _tesseract_version(tesseract_cmd: str) -> str | None:
    if shutil.which(tesseract_cmd) is None:
        return None
    try:
        output = subprocess.run([tesseract_cmd, "--version"], capture_output=True, check=True, timeout=10).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return output.decode("utf-8", errors="replace").splitlines()[0].strip() or None


class SourceExtractor:
    """Extracts every page of each approved source independently, preserving page numbers."""

    def __init__(
        self,
        *,
        registry_file: Path | None = None,
        raw_sources_dir: Path | None = None,
        extracted_text_dir: Path | None = None,
        ocr: OcrFunction | None = None,
        ocr_dpi: int | None = None,
        ocr_check_dpi: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.registry_file = registry_file or settings.resolved_path(settings.source_registry_path)
        self.raw_sources_dir = raw_sources_dir or settings.resolved_path(settings.raw_sources_dir)
        self.extracted_text_dir = extracted_text_dir or settings.resolved_path(settings.extracted_text_dir)
        self.ocr = ocr or tesseract_ocr(settings.tesseract_cmd)
        self.ocr_dpi = ocr_dpi or settings.ocr_dpi
        self.ocr_check_dpi = ocr_check_dpi or settings.ocr_check_dpi
        self.extractor_info: dict[str, object] = {
            "pymupdf": pymupdf.__version__,
            "ocr_engine": _tesseract_version(settings.tesseract_cmd) if ocr is None else "custom",
            "schema": EXTRACTOR_SCHEMA,
            "ocr_dpi": self.ocr_dpi,
            "ocr_check_dpi": self.ocr_check_dpi,
            "min_usable_chars": MIN_USABLE_CHARS,
        }

    def extract_all(self) -> list[ExtractionResult]:
        return [self.extract(source.source_id) for source in load_registry(self.registry_file).sources]

    def extract(self, source_id: str) -> ExtractionResult:
        registry = load_registry(self.registry_file)
        source = next((item for item in registry.sources if item.source_id == source_id), None)
        if source is None:
            raise ValueError(f"Unknown registered source_id: {source_id}")
        if source.status != "approved" or not source.raw_file_name or not source.content_hash:
            return ExtractionResult(source_id, "skipped", reason=f"Source status is {source.status}, not approved")

        raw_path = self.raw_sources_dir / source.raw_file_name
        if not raw_path.is_file():
            return ExtractionResult(source_id, "failed", reason="Approved raw file is missing")

        output_file_name = f"{source.source_id}.{source.content_hash[:16]}.json"
        output_path = self.extracted_text_dir / output_file_name
        if output_path.is_file():
            existing = ExtractedDocument.model_validate_json(output_path.read_text(encoding="utf-8"))
            if existing.content_hash == source.content_hash and existing.extractor.get("schema") == EXTRACTOR_SCHEMA:
                return ExtractionResult(source_id, "extracted", output_file_name=output_file_name, reused=True, page_statuses=_count(existing.pages))

        try:
            document = pymupdf.open(raw_path, filetype="pdf")
        except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
            return ExtractionResult(source_id, "failed", reason=f"PDF could not be opened: {type(error).__name__}")
        with document:
            if document.page_count == 0:
                return ExtractionResult(source_id, "failed", reason="PDF has no pages")
            pages = tuple(self._extract_page(page) for page in document)

        extracted = ExtractedDocument(
            source_id=source.source_id,
            scheme_id=source.scheme_id,
            document_type=source.document_type,
            content_hash=source.content_hash,
            content_version=source.content_version,
            raw_file_name=source.raw_file_name,
            extracted_at=datetime.now(UTC),
            extractor=self.extractor_info,
            page_count=len(pages),
            pages=pages,
        )
        self._write(output_path, extracted)
        self._remove_stale_outputs(source, keep=output_file_name)
        return ExtractionResult(source_id, "extracted", output_file_name=output_file_name, page_statuses=_count(pages))

    def _extract_page(self, page: pymupdf.Page) -> PageRecord:
        number = page.number + 1
        try:
            native = page.get_text("text").strip()
        except RuntimeError:
            return PageRecord(page_number=number, extraction_method="none", status="failed", char_count=0, text="", reason="Native extraction error")

        if _BLANK_PAGE_PATTERN.match(native):
            return PageRecord(page_number=number, extraction_method="native", status="blank", char_count=len(native), text=native, reason="Intentionally blank page")
        if _word_chars(native) >= MIN_USABLE_CHARS:
            return PageRecord(page_number=number, extraction_method="native", status="ok", char_count=len(native), text=native)

        # No usable native text: only OCR pages that visibly contain content (vector or image).
        if not page.get_drawings() and not page.get_images():
            return PageRecord(page_number=number, extraction_method="none", status="blank", char_count=len(native), text=native, reason="No text or visual content")
        try:
            text = self.ocr(page.get_pixmap(dpi=self.ocr_dpi).tobytes("png")).strip()
            check_text = self.ocr(page.get_pixmap(dpi=self.ocr_check_dpi).tobytes("png")).strip()
        except OcrUnavailableError as error:
            return PageRecord(page_number=number, extraction_method="none", status="ocr_unavailable", char_count=0, text="", reason=str(error))
        if _word_chars(text) < MIN_USABLE_CHARS:
            return PageRecord(page_number=number, extraction_method="ocr", status="blank", char_count=len(text), text=text, reason="OCR found no usable text")
        # OCR output (especially numbers and tables) is never auto-approved.
        return PageRecord(
            page_number=number,
            extraction_method="ocr",
            status="review_required",
            char_count=len(text),
            text=text,
            ocr_check_text=check_text,
            reason="OCR-derived text: numbers must be confirmed by both OCR passes",
        )

    def _write(self, path: Path, document: ExtractedDocument) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        temporary.write_text(json.dumps(document.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _remove_stale_outputs(self, source: SourceRecord, *, keep: str) -> None:
        for stale in self.extracted_text_dir.glob(f"{source.source_id}.*.json"):
            if stale.name != keep:
                stale.unlink()


def _count(pages: tuple[PageRecord, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in pages:
        key = f"{page.extraction_method}:{page.status}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> int:
    results = SourceExtractor().extract_all()
    for result in results:
        if result.status == "extracted":
            detail = ("unchanged " if result.reused else "") + ", ".join(f"{key}={value}" for key, value in sorted((result.page_statuses or {}).items()))
        else:
            detail = result.reason or ""
        print(f"{result.status:<9} {result.source_id:<34} {detail}")
    extracted = sum(result.status == "extracted" for result in results)
    print(f"\n{extracted}/{len(results)} sources extracted")
    return 0 if extracted == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
