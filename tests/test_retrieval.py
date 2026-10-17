import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from rank_bm25 import BM25Okapi

from app.chunker import Chunk
from app.index import CorpusIndex, IndexStatus, check_index, keyword_tokens, passage_text
from app.query import analyse_query
from app.registry import SourceRegistry, load_registry
from app.retrieval import Retriever, fact_values, values_conflict

HASH = {"kim": "a" * 64, "sid": "b" * 64, "factsheet": "c" * 64, "sai": "e" * 64, "guide": "f" * 64}
OLD, NEW = date(2025, 10, 31), date(2026, 7, 31)
QUERY = "What is the exit load of ICICI Flexicap?"


def registry() -> SourceRegistry:
    """The real catalogue with every source approved (synthetic download metadata)."""
    data = json.loads(Path("data/source_registry.json").read_text())
    for source in data["sources"]:
        kind = source["source_id"].rsplit("_", 1)[1]
        source.update(
            status="approved", retrieved_at="2026-10-16T00:00:00Z", resolved_url=source["official_url"], content_type="application/pdf",
            content_size_bytes=10, content_hash=HASH[kind], raw_file_name=f"{source['source_id']}.pdf", content_version=1,
        )
    return SourceRegistry.model_validate(data)


def chunk(source_id: str, text: str, *, day: date, tags: tuple[str, ...] = ("exit_load",), ocr_unconfirmed=None, page: int = 1, index: int = 0) -> Chunk:
    scheme_id, kind = source_id.rsplit("_", 1)
    source = next(s for s in registry().sources if s.source_id == source_id)
    return Chunk(
        chunk_id=f"{source_id}-v1-p{page:03d}-c{index:03d}", chunk_index=index, source_id=source_id, scheme_id=scheme_id,
        scheme_name=source.scheme_name, document_type=source.document_type, official_url=str(source.official_url), page_number=page,
        section_title=None, text=text, token_count=len(text.split()), fact_tags=tags, source_updated_date=day, source_date_basis="document",
        extraction_method="ocr" if ocr_unconfirmed is not None else "native", ocr_unconfirmed_numbers=ocr_unconfirmed,
        content_hash=HASH[kind], content_version=1,
    )


class FakeReranker:
    """Relevant passages are marked with the word 'exit load' or 'expense ratio'; others score low."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def score(self, query: str, passages: list[str]) -> list[float]:
        if self.fail:
            raise RuntimeError("reranker down")
        return [0.95 if ("exit load" in p.lower() or "expense ratio" in p.lower()) else 0.05 for p in passages]


class FakeEmbedder:
    model_name = "fake"

    def embed_query(self, text: str) -> np.ndarray:
        raise RuntimeError("embedding service unavailable")


FILLER = [
    chunk("icici_silver_etf_fof_sid", text, day=OLD, tags=("nav",), page=page)
    for page, text in enumerate(
        ["The scheme invests in units of a silver exchange traded fund.", "NAV is declared on every business day.",
         "Units are allotted at the applicable NAV.", "Statements are dispatched to unitholders by email."], start=1
    )
]


def retriever(chunks: list[Chunk], *, reranker=None) -> Retriever:
    # Unrelated filler keeps BM25's IDF weights realistic in these tiny indexes.
    chunks = chunks + FILLER
    bm25 = BM25Okapi([keyword_tokens(passage_text(c)) for c in chunks])
    index = CorpusIndex(chunks, bm25, None, IndexStatus(False, "test_keyword_only", len(chunks), None))
    return Retriever(index=index, registry=registry(), embedder=FakeEmbedder(), reranker=reranker or FakeReranker())




def test_newest_dated_document_wins_a_conflict() -> None:
    result = retriever([
        chunk("icici_flexicap_kim", "Exit load: 1% if redeemed within 12 months from allotment.", day=OLD),
        chunk("icici_flexicap_factsheet", "Exit load: 1% if redeemed within one month from allotment.", day=NEW),
    ]).retrieve(QUERY)

    assert result.status == "verified"
    assert result.evidence[0].primary.chunk.source_id == "icici_flexicap_factsheet"
    assert result.evidence[0].fact_values["duration"] == ("1 month",)
    assert result.evidence[0].corroborating is None, "a disagreeing older source must not corroborate"


def test_same_date_conflict_is_unverified() -> None:
    result = retriever([
        chunk("icici_flexicap_kim", "Exit load: 1% if redeemed within 12 months.", day=OLD),
        chunk("icici_flexicap_sid", "Exit load: 2% if redeemed within 6 months.", day=OLD),
    ]).retrieve(QUERY)

    assert (result.status, result.reason) == ("unverified", "conflicting_current_sources")
    assert result.fallback_url and "digitalfactsheet.icicipruamc.com" in result.fallback_url


def test_ocr_value_not_confirmed_by_second_pass_is_not_evidence() -> None:
    result = retriever([
        chunk("icici_flexicap_kim", "Exit load: 1% if redeemed within 12 months.", day=OLD),
        chunk("icici_flexicap_factsheet", "Exit load: 7% if redeemed within one month.", day=NEW, ocr_unconfirmed=("7",)),
    ]).retrieve(QUERY)

    assert result.status == "verified"
    assert result.evidence[0].primary.chunk.source_id == "icici_flexicap_kim"


def test_mention_without_a_stated_value_is_not_evidence() -> None:
    result = retriever([chunk("icici_flexicap_factsheet", "@@ Base expense ratio is as on the last day of the month.", day=NEW, tags=("expense_ratio",))]).retrieve(
        "What is the expense ratio of ICICI Flexicap?"
    )
    assert (result.status, result.reason) == ("unverified", "no_sufficiently_relevant_evidence")


def test_low_relevance_never_verifies() -> None:
    result = retriever([chunk("icici_flexicap_kim", "Redemption proceeds are paid within 3 working days.", day=OLD)]).retrieve(QUERY)
    assert result.status == "unverified"


def test_only_the_named_scheme_is_searched() -> None:
    result = retriever([chunk("icici_smallcap_kim", "Exit load: 1% if redeemed within 12 months.", day=OLD)]).retrieve(QUERY)
    assert result.status == "unverified"
    assert all(c.chunk.scheme_id == "icici_flexicap" for c in result.candidates)


def test_chunks_from_a_superseded_source_version_are_excluded() -> None:
    stale = chunk("icici_flexicap_kim", "Exit load: 1% if redeemed within 12 months.", day=OLD).model_copy(update={"content_hash": "d" * 64})
    assert retriever([stale]).retrieve(QUERY).status == "unverified"


def test_embedder_outage_degrades_to_keyword_search() -> None:
    result = retriever([chunk("icici_flexicap_kim", "Exit load: 1% if redeemed within 12 months.", day=OLD)]).retrieve(QUERY)
    assert result.status == "verified"
    assert result.retrievers_used == ("keyword",)


def test_reranker_outage_is_unverified() -> None:
    result = retriever([chunk("icici_flexicap_kim", "Exit load: 1% within 12 months.", day=OLD)], reranker=FakeReranker(fail=True)).retrieve(QUERY)
    assert (result.status, result.reason) == ("unverified", "retrieval_unavailable")


def test_unrecognised_fact_is_unverified_even_if_relevant() -> None:
    result = retriever([chunk("icici_flexicap_kim", "Exit load: 1% within 12 months.", day=OLD)]).retrieve("Tell me something about ICICI Flexicap")
    assert (result.status, result.reason) == ("unverified", "fact_not_recognised")


def test_comparison_requires_every_scheme_to_verify() -> None:
    chunks = [chunk("icici_flexicap_kim", "Exit load: 1% within 12 months.", day=OLD)]
    result = retriever(chunks).retrieve("Compare Large Cap and Flexicap exit loads")
    assert result.status == "unverified"
    chunks.append(chunk("icici_large_cap_kim", "Exit load: 1% within 1 month.", day=OLD))
    assert retriever(chunks).retrieve("Compare Large Cap and Flexicap exit loads").status == "verified"


def test_ambiguous_and_out_of_scope_questions_skip_retrieval() -> None:
    r = retriever([chunk("icici_flexicap_kim", "Exit load: 1% within 12 months.", day=OLD)])
    assert r.retrieve("What is the exit load?").status == "clarify"
    assert r.retrieve("HDFC Flexi Cap exit load").status == "out_of_scope"
    assert r.retrieve("").status == "invalid"
    assert r.retrieve(42).status == "invalid"


@pytest.mark.parametrize(
    ("query", "schemes", "route"),
    [
        ("What is Top 100's benchmark?", ["icici_large_mid_cap"], "retrieve"),
        ("Flexi Cap minimum SIP?", ["icici_flexicap"], "retrieve"),
        ("ICICI flexycap expense ratio", ["icici_flexicap"], "retrieve"),
        ("large and midcap expense ratio", ["icici_large_mid_cap"], "retrieve"),
        ("ICICI Midcap exit load", [], "out_of_scope"),
        ("ICICI US Bluechip expense ratio", ["icici_large_cap"], "out_of_scope"),
        ("What is the net asset value of smallcap?", ["icici_smallcap"], "retrieve"),
        ("x" * 501 + " exit load", [], "invalid"),
    ],
)
def test_scheme_resolution(query: str, schemes: list[str], route: str) -> None:
    analysis = analyse_query(query, load_registry())
    assert (analysis.scheme_ids, analysis.route) == (schemes, route)


def test_value_conflict_ignores_long_ambiguous_value_lists() -> None:
    sip_table = fact_values("SIP: Daily Rs. 100, Monthly Rs. 1,000, Quarterly Rs. 5,000", ["sip"])
    other = fact_values("SIP minimum Rs. 250", ["sip"])
    assert not values_conflict(sip_table, other)
    assert values_conflict(fact_values("Exit load 1% within 12 months", ["exit_load"]), fact_values("Exit load 2% within 6 months", ["exit_load"]))


def test_index_check_detects_model_change_and_stale_chunks(tmp_path: Path) -> None:
    from app.config import Settings

    settings = Settings(processed_dir=tmp_path / "processed", indexes_dir=tmp_path / "indexes", bge_model="BAAI/bge-small-en-v1.5")
    (tmp_path / "processed").mkdir()
    chunks_file = tmp_path / "processed" / "chunks.jsonl"
    chunks_file.write_text(chunk("icici_flexicap_kim", "Exit load 1%", day=OLD).model_dump_json() + "\n")
    assert check_index(settings).reason == "vector_index_missing"

    from app.index import build_index

    class TinyEmbedder:
        model_name = "BAAI/bge-small-en-v1.5"

        def embed_passages(self, texts: list[str]) -> np.ndarray:
            return np.ones((len(texts), 4), dtype="float32") / 2

    build_index(embedder=TinyEmbedder(), settings=settings)
    assert check_index(settings).semantic_available
    assert check_index(settings, expected_dimension=384).reason == "embedding_dimension_mismatch"
    assert check_index(Settings(**{**settings.model_dump(), "bge_model": "BAAI/bge-base-en-v1.5"})).reason == "embedding_model_changed"
    chunks_file.write_text(chunks_file.read_text() + chunk("icici_flexicap_sid", "Exit load 1%", day=OLD).model_dump_json() + "\n")
    assert check_index(settings).reason == "vector_index_stale"


def general_chunk(source_id: str, text: str, *, day: date, tags: tuple[str, ...], page: int = 1) -> Chunk:
    source = next(s for s in registry().sources if s.source_id == source_id)
    kind = source_id.rsplit("_", 1)[1]
    return Chunk(
        chunk_id=f"{source_id}-v1-p{page:03d}-c000", chunk_index=0, source_id=source_id, scheme_id=None, scheme_name=source.scheme_name,
        document_type=source.document_type, official_url=str(source.official_url), page_number=page, section_title=None, text=text,
        token_count=len(text.split()), fact_tags=tags, source_updated_date=day, source_date_basis="document", extraction_method="native",
        ocr_unconfirmed_numbers=None, content_hash=HASH[kind], content_version=1,
    )


SEBI_DEFINITION = "Exit load is a fee charged when units are redeemed; SEBI caps the exit load of open-ended schemes."


def test_definitions_use_general_documents_and_prefer_relevance_over_recency() -> None:
    chunks = [
        general_chunk("sebi_mf_investor_faq_guide", SEBI_DEFINITION, day=date(2024, 8, 31), tags=("exit_load",)),
        general_chunk("icici_mf_sai", "Redemption requests are processed at the applicable NAV.", day=date(2026, 6, 30), tags=("exit_load",)),
        chunk("icici_flexicap_kim", "Exit load: 1% within 12 months.", day=OLD),
    ]
    result = retriever(chunks).retrieve("What does exit load mean?")

    assert result.status == "verified"
    assert result.evidence[0].primary.chunk.source_id == "sebi_mf_investor_faq_guide"
    assert all(c.chunk.scheme_id is None for c in result.candidates), "general questions never search scheme documents"


def test_scheme_questions_never_use_general_documents() -> None:
    result = retriever([general_chunk("sebi_mf_investor_faq_guide", "Exit load: 1% within 90 days.", day=NEW, tags=("exit_load",))]).retrieve(QUERY)
    assert result.status == "unverified"
    assert all(c.chunk.scheme_id == "icici_flexicap" for c in result.candidates)


def test_statement_questions_fall_back_to_the_official_service_page() -> None:
    result = retriever([]).retrieve("How do I download my capital gains statement?")
    assert result.status == "unverified"
    assert result.fallback_url == "https://www.icicipruamc.com/investor-services"


@pytest.mark.parametrize(
    ("query", "route", "definition"),
    [
        ("What is an expense ratio?", "retrieve", True),
        ("What does exit load mean?", "retrieve", True),
        ("Explain riskometer", "retrieve", True),
        ("What is the exit load?", "clarify_scheme", False),
        ("What is ICICI exit load?", "clarify_scheme", False),
    ],
)
def test_definition_questions_are_distinguished_from_unnamed_scheme_questions(query: str, route: str, definition: bool) -> None:
    analysis = analyse_query(query, load_registry())
    assert (analysis.route, analysis.definition) == (route, definition)
