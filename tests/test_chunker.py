import json
import re
from datetime import date
from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError

from app.chunker import MAX_TOKENS, Chunk, SourceChunker, fact_tags
from tests.test_quality import VALID_KIM, Pipeline, pdf


def whitespace_encoder(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in re.finditer(r"\S+", text)]


def chunker(pipeline: Pipeline) -> SourceChunker:
    return SourceChunker(registry_file=pipeline.registry_file, processed_dir=pipeline.processed_dir, encoder=whitespace_encoder)


def long_page(words: int, marker: str) -> str:
    lines = [" ".join(f"{marker}{i * 12 + j}" for j in range(12)) for i in range(words // 12)]
    return "\n".join(lines)


def dense_pdf(*page_texts: str) -> bytes:
    """Small-font, line-by-line pages (insert_textbox silently drops text that overflows its box)."""
    document = pymupdf.open()
    for text in page_texts:
        document.new_page().insert_text((20, 20), text, fontsize=4)
    return document.tobytes()


def test_chunks_respect_page_boundaries_size_limit_and_overlap(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", dense_pdf(VALID_KIM + "\n" + long_page(1300, "a"), long_page(1300, "b")))
    pipeline.check()
    results, chunks = chunker(pipeline).chunk_all()

    source_chunks = [chunk for chunk in chunks if chunk.source_id == "icici_large_cap_kim"]
    assert len(source_chunks) >= 4
    for chunk in source_chunks:
        assert chunk.token_count <= MAX_TOKENS
        page_marker = "b" if chunk.page_number == 2 else "a"
        other_marker = "a" if page_marker == "b" else "b"
        # No chunk ever contains text from the other page.
        assert not re.search(rf"\b{other_marker}\d+\b", chunk.text)
        assert page_marker in chunk.text
    first, second = [chunk for chunk in source_chunks if chunk.page_number == 1][:2]
    assert set(first.text.split()[-20:]) & set(second.text.split()[:80]), "consecutive chunks should overlap"


def test_every_chunk_carries_complete_provenance(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM))
    pipeline.check()
    _, chunks = chunker(pipeline).chunk_all()

    (chunk,) = chunks
    assert chunk.chunk_id == "icici_large_cap_kim-v1-p001-c000"
    assert (chunk.scheme_id, chunk.document_type, chunk.page_number) == ("icici_large_cap", "KIM", 1)
    assert chunk.official_url == str(pipeline.source("icici_large_cap_kim").official_url)
    assert (chunk.source_updated_date, chunk.source_date_basis) == (date(2025, 10, 31), "document")
    assert "exit_load" in chunk.fact_tags
    assert chunk.ocr_unconfirmed_numbers is None
    written = [json.loads(line) for line in (pipeline.processed_dir / "chunks.jsonl").read_text().splitlines()]
    assert written[0]["chunk_id"] == chunk.chunk_id


def test_missing_document_date_falls_back_to_fetch_date(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM.replace("This KIM is dated October 31, 2025. ", "")))
    pipeline.check()
    (chunk,) = chunker(pipeline).chunk_all()[1]
    assert chunk.source_date_basis == "fetch"


def test_ocr_numbers_not_confirmed_by_second_pass_are_flagged(tmp_path: Path) -> None:
    passes = iter([
        "ICICI Prudential Large Cap Fund factsheet. Total Expense Ratio Other : 1.13% p. a. Direct : 0.71% p. a.",
        "ICICI Prudential Large Cap Fund factsheet. Total Expense Ratio Other : 1.18% p. a. Direct : 0.71% p. a.",
    ])
    image_only = pymupdf.open()
    page = image_only.new_page()
    page.draw_rect(pymupdf.Rect(72, 72, 300, 200), fill=(0, 0, 0))

    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_factsheet", image_only.tobytes(), ocr=lambda _: next(passes))
    pipeline.check()
    (chunk,) = chunker(pipeline).chunk_all()[1]

    assert chunk.extraction_method == "ocr"
    assert chunk.ocr_unconfirmed_numbers == ("1.13",)


def test_unapproved_sources_produce_no_chunks(tmp_path: Path) -> None:
    pipeline = Pipeline(tmp_path)
    pipeline.ingest("icici_large_cap_kim", pdf(VALID_KIM.replace("Large Cap", "Smallcap")))
    pipeline.check()
    results, chunks = chunker(pipeline).chunk_all()

    assert chunks == []
    assert {result.status for result in results} == {"skipped"}


def test_chunk_model_rejects_groww_and_cross_source_ids() -> None:
    valid = dict(
        chunk_id="icici_large_cap_kim-v1-p001-c000", chunk_index=0, source_id="icici_large_cap_kim", scheme_id="icici_large_cap",
        scheme_name="ICICI Prudential Large Cap Fund", document_type="KIM", official_url="https://www.icicipruamc.com/a.pdf",
        page_number=1, section_title=None, text="Exit load 1%", token_count=3, fact_tags=("exit_load",),
        source_updated_date=date(2025, 10, 31), source_date_basis="document", extraction_method="native",
        ocr_unconfirmed_numbers=None, content_hash="a" * 64, content_version=1,
    )
    Chunk(**valid)
    with pytest.raises(ValidationError, match="approved source"):
        Chunk(**{**valid, "official_url": "https://groww.in/mutual-funds/example"})
    with pytest.raises(ValidationError, match="derived from its source_id"):
        Chunk(**{**valid, "chunk_id": "icici_smallcap_kim-v1-p001-c000"})


def test_fact_tags_cover_core_faq_topics() -> None:
    assert set(fact_tags("Exit load and Total Expense Ratio; Minimum application amount; SIP; Riskometer; Benchmark")) >= {
        "exit_load", "expense_ratio", "minimum_investment", "sip", "riskometer", "benchmark",
    }
