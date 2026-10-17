from datetime import date
from pathlib import Path

from app.chunker import Chunk
from app.ingest import groww_exclusion_check
from tests.test_downloader import copy_registry


def chunk(text: str) -> Chunk:
    return Chunk(
        chunk_id="icici_large_cap_kim-v1-p001-c000", chunk_index=0, source_id="icici_large_cap_kim", scheme_id="icici_large_cap",
        scheme_name="ICICI Prudential Large Cap Fund", document_type="KIM", official_url="https://www.icicipruamc.com/a.pdf",
        page_number=1, section_title=None, text=text, token_count=3, fact_tags=(), source_updated_date=date(2025, 10, 31),
        source_date_basis="document", extraction_method="native", ocr_unconfirmed_numbers=None, content_hash="a" * 64, content_version=1,
    )


def test_groww_exclusion_check_is_clean_for_official_data(tmp_path: Path) -> None:
    counts = groww_exclusion_check(copy_registry(tmp_path), tmp_path / "raw", [chunk("Exit load is 1%.")])
    assert not any(counts.values())


def test_groww_exclusion_check_detects_blocked_domain_in_chunk_text(tmp_path: Path) -> None:
    counts = groww_exclusion_check(copy_registry(tmp_path), tmp_path / "raw", [chunk("See groww.in for details.")])
    assert counts["chunk_text"] == 1
