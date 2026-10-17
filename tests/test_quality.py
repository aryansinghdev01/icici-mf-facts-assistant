import json
from datetime import date
from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError

from app.downloader import SourceDownloader
from app.extractor import SourceExtractor
from app.quality import QualityChecker, normalise_text
from app.registry import SourceRegistry, load_registry
from tests.test_downloader import FakeClient, FakeResponse, copy_registry

TODAY = date(2026, 10, 16)
VALID_KIM = (
    "ICICI Prudential Large Cap Fund Key Information Memorandum. "
    "This KIM is dated October 31, 2025. Exit load is 1% of the applicable NAV within one month."
)


def pdf(*page_texts: str) -> bytes:
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        if text:
            page.insert_textbox(page.rect + (72, 72, -72, -72), text, fontsize=10)
    return document.tobytes()


class Pipeline:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.registry_file = copy_registry(tmp_path)
        self.raw_dir = tmp_path / "raw"
        self.extracted_dir = tmp_path / "extracted"
        self.processed_dir = tmp_path / "processed"

    def ingest(self, source_id: str, content: bytes, ocr=lambda _: "") -> None:
        client = FakeClient(FakeResponse(url="https://www.icicipruamc.com/current.pdf", content=content))
        SourceDownloader(registry_file=self.registry_file, raw_sources_dir=self.raw_dir, client=client).download(source_id)
        SourceExtractor(
            registry_file=self.registry_file, raw_sources_dir=self.raw_dir, extracted_text_dir=self.extracted_dir, ocr=ocr
        ).extract(source_id)

    def check(self) -> dict[str, object]:
        results = QualityChecker(
            registry_file=self.registry_file, extracted_text_dir=self.extracted_dir, processed_dir=self.processed_dir, today=TODAY
        ).check_all()
        return {result.source_id: result for result in results}

    def source(self, source_id: str):
        return next(item for item in load_registry(self.registry_file).sources if item.source_id == source_id)


def test_valid_source_passes_records_date_and_writes_normalised_pages(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM, "", "Riskometer: the risk of the scheme is very high and benchmark is Nifty 100 TRI."))
    result = pipeline.check()["icici_large_cap_kim"]

    assert result.status == "passed" and result.reasons == []
    assert result.document_date == date(2025, 10, 31)
    assert pipeline.source("icici_large_cap_kim").published_date == date(2025, 10, 31)
    document = json.loads((pipeline.processed_dir / result.output_file_name).read_text())
    # The blank middle page is dropped, but every remaining page keeps its original page number.
    assert [page["page_number"] for page in document["pages"]] == [1, 3]
    assert document["official_url"].startswith("https://www.icicipruamc.com/")


def test_scheme_mismatch_is_quarantined(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM.replace("Large Cap", "Smallcap")))
    result = pipeline.check()["icici_large_cap_kim"]

    assert result.status == "quarantined"
    assert "expected_scheme_name_not_found" in result.reasons
    assert pipeline.source("icici_large_cap_kim").status == "quarantined"
    assert not list(pipeline.processed_dir.glob("icici_large_cap_kim.*.json"))


def test_duplicate_content_hash_quarantines_the_later_source(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    content = pdf(VALID_KIM)
    pipeline.ingest("icici_large_cap_kim", content)
    pipeline.ingest("icici_large_cap_sid", content)
    results = pipeline.check()

    assert results["icici_large_cap_kim"].status == "passed"
    assert results["icici_large_cap_sid"].reasons == ["duplicate_content_hash_of_icici_large_cap_kim"]


def test_unusable_extraction_is_quarantined(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf("THIS PAGE HAS BEEN LEFT INTENTIONALLY BLANK"))
    result = pipeline.check()["icici_large_cap_kim"]

    assert result.status == "quarantined"
    assert "no_usable_pages" in result.reasons


def test_future_document_date_is_quarantined(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM.replace("2025", "2099")))
    assert "implausible_document_date" in pipeline.check()["icici_large_cap_kim"].reasons


def test_missing_extraction_is_quarantined(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM))
    for extracted in pipeline.extracted_dir.glob("*.json"):
        extracted.unlink()
    assert pipeline.check()["icici_large_cap_kim"].reasons == ["missing_extraction"]


def test_pending_sources_never_pass(tmp_path: Path) -> None:
    results = Pipeline(tmp_path).check()
    assert all(result.status == "quarantined" for result in results.values())


def test_registry_rejects_approved_source_without_download_metadata() -> None:
    registry = json.loads(Path("data/source_registry.json").read_text())
    registry["sources"][0].update(status="approved", content_hash=None)
    with pytest.raises(ValidationError, match="complete download metadata"):
        SourceRegistry.model_validate(registry)


def test_unchanged_redownload_keeps_quarantine(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    content = pdf(VALID_KIM.replace("Large Cap", "Smallcap"))
    pipeline.ingest("icici_large_cap_kim", content)
    pipeline.check()
    pipeline.ingest("icici_large_cap_kim", content)
    assert pipeline.source("icici_large_cap_kim").status == "quarantined"


def test_normalisation_cleans_whitespace_without_changing_content() -> None:
    assert normalise_text("Exit Load:\t 1%  \r\n\n\n\nNil  after  12 months ") == "Exit Load: 1%\n\nNil after 12 months"
