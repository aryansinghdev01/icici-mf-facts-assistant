import json
import shutil
from pathlib import Path

import pymupdf
import pytest

from app.downloader import SourceDownloader
from app.extractor import OcrUnavailableError, SourceExtractor
from tests.test_downloader import FakeClient, FakeResponse, copy_registry

SOURCE_ID = "icici_large_cap_kim"
NATIVE_TEXT = "ICICI Prudential Large Cap Fund exit load is 1% of the applicable NAV within one month."


def fixture_pdf() -> bytes:
    """Page 1: native text. Page 2: intentionally blank. Page 3: image-only (no native text)."""
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), NATIVE_TEXT, fontsize=9)
    document.new_page().insert_text((72, 72), "THIS PAGE HAS BEEN LEFT INTENTIONALLY BLANK")
    scratch = pymupdf.open()
    scratch.new_page().insert_text((72, 72), "Riskometer: the risk of the scheme is very high. Benchmark: Nifty 100 TRI.", fontsize=14)
    image = scratch[0].get_pixmap(dpi=200).tobytes("png")
    page = document.new_page()
    page.insert_image(page.rect, stream=image)
    return document.tobytes()


def approved_source(tmp_path: Path, content: bytes) -> tuple[Path, Path]:
    registry_file = copy_registry(tmp_path)
    raw_dir = tmp_path / "raw_sources"
    client = FakeClient(FakeResponse(url="https://www.icicipruamc.com/current.pdf", content=content))
    assert SourceDownloader(registry_file=registry_file, raw_sources_dir=raw_dir, client=client).download(SOURCE_ID).status == "approved"
    return registry_file, raw_dir


def extractor(tmp_path: Path, registry_file: Path, raw_dir: Path, ocr) -> SourceExtractor:
    return SourceExtractor(registry_file=registry_file, raw_sources_dir=raw_dir, extracted_text_dir=tmp_path / "extracted", ocr=ocr)


def fake_ocr(_: bytes) -> str:
    return "Riskometer: the risk of the scheme is very high. Benchmark: Nifty 100 TRI."


def test_pages_are_extracted_independently_with_methods_and_statuses(tmp_path: Path) -> None:
    registry_file, raw_dir = approved_source(tmp_path, fixture_pdf())
    result = extractor(tmp_path, registry_file, raw_dir, fake_ocr).extract(SOURCE_ID)

    assert result.status == "extracted"
    record = json.loads((tmp_path / "extracted" / result.output_file_name).read_text())
    assert record["source_id"] == SOURCE_ID and record["scheme_id"] == "icici_large_cap" and record["page_count"] == 3
    native, blank, ocr = record["pages"]
    assert (native["page_number"], native["extraction_method"], native["status"]) == (1, "native", "ok")
    assert "exit load is 1%" in native["text"]
    assert (blank["page_number"], blank["status"]) == (2, "blank")
    assert (ocr["page_number"], ocr["extraction_method"], ocr["status"]) == (3, "ocr", "review_required")


def test_missing_ocr_engine_marks_page_unavailable_instead_of_guessing(tmp_path: Path) -> None:
    def unavailable(_: bytes) -> str:
        raise OcrUnavailableError("OCR engine not found")

    registry_file, raw_dir = approved_source(tmp_path, fixture_pdf())
    result = extractor(tmp_path, registry_file, raw_dir, unavailable).extract(SOURCE_ID)

    assert result.page_statuses == {"native:ok": 1, "native:blank": 1, "none:ocr_unavailable": 1}


def test_rerun_for_unchanged_source_is_reused(tmp_path: Path) -> None:
    registry_file, raw_dir = approved_source(tmp_path, fixture_pdf())
    first = extractor(tmp_path, registry_file, raw_dir, fake_ocr).extract(SOURCE_ID)
    second = extractor(tmp_path, registry_file, raw_dir, fake_ocr).extract(SOURCE_ID)

    assert second.reused and second.output_file_name == first.output_file_name
    assert len(list((tmp_path / "extracted").glob("*.json"))) == 1


def test_corrupt_pdf_fails_without_output(tmp_path: Path) -> None:
    registry_file, raw_dir = approved_source(tmp_path, b"%PDF-1.7 not really a pdf")
    result = extractor(tmp_path, registry_file, raw_dir, fake_ocr).extract(SOURCE_ID)

    assert result.status == "failed"
    assert not (tmp_path / "extracted").exists()


def test_unapproved_source_is_skipped(tmp_path: Path) -> None:
    registry_file = copy_registry(tmp_path)
    result = extractor(tmp_path, registry_file, tmp_path / "raw", fake_ocr).extract(SOURCE_ID)
    assert result.status == "skipped"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_real_tesseract_reads_image_only_page(tmp_path: Path) -> None:
    registry_file, raw_dir = approved_source(tmp_path, fixture_pdf())
    result = SourceExtractor(registry_file=registry_file, raw_sources_dir=raw_dir, extracted_text_dir=tmp_path / "extracted").extract(SOURCE_ID)

    ocr_page = json.loads((tmp_path / "extracted" / result.output_file_name).read_text())["pages"][2]
    assert ocr_page["extraction_method"] == "ocr"
    assert "risk of the scheme is very high" in ocr_page["text"].lower()
