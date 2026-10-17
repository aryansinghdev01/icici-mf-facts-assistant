"""Hybrid retrieval and the evidence-confidence gate (Phase 2).

Flow: query analysis -> scheme-filtered BM25 + BGE search -> reciprocal-rank fusion ->
cross-encoder rerank -> gate. The gate returns ``verified`` evidence only when a
relevant chunk for the exact requested fact exists for every named scheme; otherwise
``unverified`` with a reason (and an official fallback link where one is known).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

import numpy as np

from app.chunker import FACT_TAG_PATTERNS, Chunk, numbers_in
from app.config import Settings, get_settings
from app.index import BgeEmbedder, CorpusIndex, Embedder, keyword_tokens, load_index, passage_text
from app.query import ConversationContext, QueryAnalysis, SchemeMatch, analyse_query
from app.registry import SourceRegistry, load_registry

CANDIDATES_PER_RETRIEVER = 30
RERANK_CANDIDATES = 12
RRF_K = 60

# Values that a fact is actually stated in; used to detect conflicts between same-date sources.
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")
_AMOUNT = re.compile(r"(?:rs\.?|₹|inr)\s?(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_WORD_NUMBERS = {"one": "1", "two": "2", "three": "3", "six": "6", "twelve": "12"}
_DURATION = re.compile(r"\b(\d+|one|two|three|six|twelve)\s+(day|month|year)s?\b", re.IGNORECASE)
FACT_WINDOW_CHARS = 400
# Text kept before a fact's wording when reranking: definitions often precede the term's mention.
RERANK_LEAD_CHARS = 250
# Numeric facts must be evidenced by a stated value of the right kind near the fact's wording,
# so a footnote that merely mentions "expense ratio" cannot count as the answer.
_NIL = re.compile(r"\bnil\b|not applicable|\bno lock", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"\d+\.\d+")
REQUIRED_VALUE_KINDS: dict[str, tuple[str, ...]] = {
    "expense_ratio": ("percent",),
    "exit_load": ("percent", "nil"),
    "minimum_investment": ("amount",),
    "sip": ("amount",),
    "nav": ("amount", "number"),
    "lock_in": ("duration", "nil"),
}
# Descriptive facts must actually be stated, not just referred to ("For Investment Objective: refer page 160").
REQUIRED_STATEMENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "investment_objective": re.compile(
        r"\bseeks? to\b|\baims? to\b|\bobjective of the (?:scheme|fund) is\b|\bto generate\b|\bto provide\b|\bto achieve\b",
        re.IGNORECASE,
    ),
}
FACT_LABELS = {
    "expense_ratio": "total expense ratio", "exit_load": "exit load", "minimum_investment": "minimum investment amount",
    "sip": "minimum SIP amount", "benchmark": "benchmark index", "riskometer": "riskometer risk level", "lock_in": "lock-in period",
    "nav": "NAV", "fund_manager": "fund manager", "investment_objective": "investment objective", "statement": "account statement",
    "plans_options": "plans and options",
}


class Reranker(Protocol):
    def score(self, query: str, passages: list[str]) -> list[float]: ...


class CrossEncoderReranker:
    """BGE reranker; returns calibrated 0-1 relevance probabilities."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None

    def score(self, query: str, passages: list[str]) -> list[float]:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name, device="cpu")
        scores = self._model.predict([(query, passage) for passage in passages], show_progress_bar=False)
        if not np.all(np.isfinite(scores)):
            raise RuntimeError("Reranker returned non-finite scores")
        return [float(value) for value in scores]


@dataclass(frozen=True)
class Candidate:
    chunk: Chunk
    rerank_score: float
    fused_score: float
    fact_match: bool


@dataclass(frozen=True)
class Evidence:
    scheme_id: str | None
    primary: Candidate
    corroborating: Candidate | None
    fact_values: dict[str, tuple[str, ...]]


@dataclass
class RetrievalResult:
    status: Literal["verified", "unverified", "clarify", "out_of_scope", "invalid"]
    analysis: QueryAnalysis
    evidence: list[Evidence] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    reason: str | None = None
    fallback_url: str | None = None
    retrievers_used: tuple[str, ...] = ()


def _fact_window(text: str, facts: list[str], *, lead: int = 0) -> str:
    """Merged spans of text around each mention of the requested facts."""
    spans: list[list[int]] = []
    for fact in facts or []:
        pattern = FACT_TAG_PATTERNS.get(fact)
        if pattern:
            spans += [[max(0, m.start() - lead), m.start() + FACT_WINDOW_CHARS] for m in pattern.finditer(text)]
    if not spans:
        return text
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return "\n".join(text[start:end] for start, end in merged)


def states_required_values(text: str, facts: list[str]) -> bool:
    """Every requested numeric fact has a value of an expected kind near its wording, and every
    descriptive fact with a statement pattern is actually stated rather than referred to."""
    for fact in facts:
        statement = REQUIRED_STATEMENT_PATTERNS.get(fact)
        if statement and not statement.search(_fact_window(text, [fact], lead=RERANK_LEAD_CHARS)):
            return False
        kinds = REQUIRED_VALUE_KINDS.get(fact)
        if not kinds:
            continue
        window = _fact_window(text, [fact])
        values = fact_values(text, [fact])
        present = {kind for kind in ("percent", "amount", "duration") if values[kind]}
        if _NIL.search(window):
            present.add("nil")
        if _PLAIN_NUMBER.search(window):
            present.add("number")
        if not present & set(kinds):
            return False
    return True


def fact_values(text: str, facts: list[str]) -> dict[str, tuple[str, ...]]:
    """Percentages, rupee amounts, and durations stated near the requested fact's wording."""
    windows = []
    for fact in facts or []:
        pattern = FACT_TAG_PATTERNS.get(fact)
        if pattern:
            windows += [text[match.start() : match.start() + FACT_WINDOW_CHARS] for match in pattern.finditer(text)]
    window = "\n".join(windows) if windows else text
    durations = {f"{_WORD_NUMBERS.get(n.casefold(), n)} {unit.casefold()}" for n, unit in _DURATION.findall(window)}
    return {
        "percent": tuple(sorted({value.replace(" ", "") for value in _PERCENT.findall(window)})),
        "amount": tuple(sorted({value.replace(",", "") for value in _AMOUNT.findall(window)})),
        "duration": tuple(sorted(durations)),
    }


# A passage listing more values than this (e.g. daily/weekly/monthly SIP amounts) is too
# ambiguous to declare a conflict from; only clearly stated values are compared.
MAX_VALUES_FOR_CONFLICT = 2


def values_conflict(first: dict[str, tuple[str, ...]], second: dict[str, tuple[str, ...]]) -> bool:
    """Two sources conflict when both clearly state the same kind of value with none in common."""
    return any(
        first.get(kind) and second.get(kind)
        and len(first[kind]) <= MAX_VALUES_FOR_CONFLICT and len(second[kind]) <= MAX_VALUES_FOR_CONFLICT
        and not set(first[kind]) & set(second[kind])
        for kind in ("percent", "amount", "duration")
    )


def values_agree(first: dict[str, tuple[str, ...]], second: dict[str, tuple[str, ...]]) -> bool:
    """Corroboration requires a shared stated value and no conflicting one."""
    shared = any(set(first.get(kind, ())) & set(second.get(kind, ())) for kind in ("percent", "amount", "duration"))
    return shared and not values_conflict(first, second)


def search_query(analysis: QueryAnalysis, scheme_id: str | None, registry: SourceRegistry) -> str:
    """The text actually searched/reranked: aliases ('Top 100') become the official scheme name,
    and a question naming several schemes becomes one focused question per scheme."""
    if scheme_id is None:
        return analysis.query
    scheme_name = registry.scheme_map[scheme_id].scheme_name
    match = next(m for m in analysis.schemes if m.scheme_id == scheme_id)
    if (len(analysis.schemes) > 1 or match.from_context) and analysis.facts:
        return f"What is the {' and '.join(FACT_LABELS[fact] for fact in analysis.facts)} of {scheme_name}?"
    rewritten = re.sub(r"\s+".join(map(re.escape, match.matched_text.split())), scheme_name, analysis.query, count=1, flags=re.IGNORECASE)
    # Matching works on normalised text ("large and mid cap"); fall back to appending the name.
    return rewritten if scheme_name in rewritten else f"{analysis.query} ({scheme_name})"


def rerank_passage(chunk: Chunk, facts: list[str]) -> str:
    """What the cross-encoder scores: the text around the requested fact's wording when present,
    so long or OCR-noisy chunks are judged on the relevant passage rather than diluted."""
    window = _fact_window(chunk.text, facts, lead=RERANK_LEAD_CHARS) if facts else chunk.text
    header = passage_text(chunk).split("\n", 1)[0]
    return f"{header}\n{window}"


def _numbers_in_values(values: dict[str, tuple[str, ...]]) -> set[str]:
    return {number for items in values.values() for item in items for number in numbers_in(item)}


class Retriever:
    def __init__(
        self,
        *,
        index: CorpusIndex | None = None,
        registry: SourceRegistry | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.index = index or load_index(self.settings)
        self.registry = registry or load_registry(self.settings.resolved_path(self.settings.source_registry_path))
        self.embedder = embedder or BgeEmbedder(self.settings.bge_model)
        self.reranker = reranker or CrossEncoderReranker(self.settings.reranker_model)
        self.min_score = self.settings.rerank_min_score
        self.sources = {source.source_id: source for source in self.registry.sources}

    # -- candidate selection -------------------------------------------------------------
    def _eligible_positions(self, scheme_ids: list[str]) -> list[int]:
        """Chunks of the named schemes whose source is still the current approved version."""
        positions = []
        for position, chunk in enumerate(self.index.chunks):
            source = self.sources.get(chunk.source_id)
            if source is None or source.status != "approved" or source.content_hash != chunk.content_hash:
                continue
            # Scheme questions use only that scheme's documents; general questions only general ones,
            # so a regulator's generic figure can never answer a scheme-specific question.
            if (chunk.scheme_id not in scheme_ids) if scheme_ids else (chunk.scheme_id is not None):
                continue
            positions.append(position)
        return positions

    def _keyword_ranking(self, query: str, positions: list[int]) -> list[int]:
        if self.index.bm25 is None or not positions:
            return []
        scores = self.index.bm25.get_scores(keyword_tokens(query))
        ranked = sorted(positions, key=lambda position: scores[position], reverse=True)
        return [position for position in ranked[:CANDIDATES_PER_RETRIEVER] if scores[position] > 0]

    def _vector_ranking(self, query: str, positions: list[int]) -> list[int] | None:
        if self.index.vectors is None or not positions:
            return None
        try:
            query_vector = self.embedder.embed_query(query)
        except Exception:  # noqa: BLE001 - an unavailable embedder degrades to keyword-only search.
            return None
        import faiss

        selector = faiss.IDSelectorBatch(np.asarray(positions, dtype="int64"))
        params = faiss.SearchParameters(sel=selector)
        k = min(CANDIDATES_PER_RETRIEVER, len(positions))
        _, ids = self.index.vectors.search(query_vector.reshape(1, -1), k, params=params)
        return [int(position) for position in ids[0] if position >= 0]

    def _candidates(self, analysis: QueryAnalysis, scheme_id: str | None) -> tuple[list[Candidate], tuple[str, ...]]:
        query = search_query(analysis, scheme_id, self.registry)
        positions = self._eligible_positions([scheme_id] if scheme_id else [])
        rankings = {"keyword": self._keyword_ranking(query, positions)}
        vector = self._vector_ranking(query, positions)
        if vector is not None:
            rankings["semantic"] = vector
        fused: dict[int, float] = {}
        for ranking in rankings.values():
            for rank, position in enumerate(ranking):
                fused[position] = fused.get(position, 0.0) + 1.0 / (RRF_K + rank + 1)
        wanted = set(analysis.facts)
        ranked = sorted(fused, key=fused.get, reverse=True)
        if wanted:
            # Only chunks tagged with the requested fact can be evidence, so rerank those first.
            ranked = [p for p in ranked if wanted & set(self.index.chunks[p].fact_tags)] or ranked
        top = ranked[:RERANK_CANDIDATES]
        if not top:
            return [], tuple(rankings)
        chunks = [self.index.chunks[position] for position in top]
        scores = self.reranker.score(query, [rerank_passage(chunk, analysis.facts) for chunk in chunks])
        candidates = [
            Candidate(chunk, score, fused[position], fact_match=not wanted or bool(wanted & set(chunk.fact_tags)))
            for chunk, score, position in zip(chunks, scores, top)
        ]
        candidates.sort(key=lambda candidate: candidate.rerank_score, reverse=True)
        return candidates, tuple(rankings)

    # -- confidence gate -------------------------------------------------------------------
    def _usable(self, candidate: Candidate, facts: list[str], *, definition: bool = False) -> bool:
        if candidate.rerank_score < self.min_score or not candidate.fact_match:
            return False
        # A definition ("what is an exit load?") is stated in words; scheme facts need a value.
        if not definition and not states_required_values(candidate.chunk.text, facts):
            return False
        if candidate.chunk.extraction_method == "ocr":
            # OCR evidence is only usable if the values stating the fact were read identically twice.
            unconfirmed = set(candidate.chunk.ocr_unconfirmed_numbers or ())
            if unconfirmed & _numbers_in_values(fact_values(candidate.chunk.text, facts)):
                return False
        return True

    def _gate_scheme(self, scheme_id: str | None, candidates: list[Candidate], facts: list[str], *, definition: bool = False) -> Evidence | str:
        usable = [c for c in candidates if c.chunk.scheme_id == scheme_id and self._usable(c, facts, definition=definition)]
        if not usable:
            return "no_sufficiently_relevant_evidence"
        if scheme_id is None:
            # General questions (definitions, statement rules) do not go stale like scheme facts,
            # so the most relevant official passage is used rather than the newest document.
            primary = usable[0]
            corroborating = next((c for c in usable if c.chunk.source_id != primary.chunk.source_id), None)
            return Evidence(None, primary, corroborating, fact_values(primary.chunk.text, facts))
        # Newest dated document wins (see docs/architecture.md, "Source Authority and OCR Verification").
        newest = max(c.chunk.source_updated_date for c in usable)
        current = [c for c in usable if c.chunk.source_updated_date == newest]
        primary = current[0]
        primary_values = fact_values(primary.chunk.text, facts)
        # Compare against the best chunk of each other same-date document (candidates are score-sorted).
        best_per_source: dict[str, Candidate] = {}
        for candidate in current:
            if candidate.chunk.source_id != primary.chunk.source_id:
                best_per_source.setdefault(candidate.chunk.source_id, candidate)
        for other in best_per_source.values():
            if values_conflict(primary_values, fact_values(other.chunk.text, facts)):
                return "conflicting_current_sources"
        corroborating = next(
            (c for c in usable if c.chunk.source_id != primary.chunk.source_id and values_agree(primary_values, fact_values(c.chunk.text, facts))),
            None,
        )
        return Evidence(scheme_id or primary.chunk.scheme_id, primary, corroborating, primary_values)

    def _retrieve_overview(self, analysis: QueryAnalysis, fallback: str | None) -> RetrievalResult:
        """"Tell me about <scheme>": each overview fact is searched and gated on its own, so the
        snapshot only contains facts that individually pass the same evidence checks."""
        evidence: list[Evidence] = []
        all_candidates: list[Candidate] = []
        used: tuple[str, ...] = ()
        for scheme_id in analysis.scheme_ids:
            name = self.registry.scheme_map[scheme_id].scheme_name
            for fact in analysis.facts:
                focused = replace(
                    analysis, query=f"What is the {FACT_LABELS[fact]} of {name}?", facts=[fact],
                    schemes=[SchemeMatch(scheme_id, name)], overview=False,
                )
                try:
                    candidates, used = self._candidates(focused, scheme_id)
                except Exception:  # noqa: BLE001 - without calibrated scores nothing can be verified.
                    return RetrievalResult("unverified", analysis, reason="retrieval_unavailable", fallback_url=fallback)
                all_candidates += candidates
                outcome = self._gate_scheme(scheme_id, candidates, [fact])
                if not isinstance(outcome, str) and all(e.primary.chunk.chunk_id != outcome.primary.chunk.chunk_id for e in evidence):
                    evidence.append(outcome)
        if not evidence:
            return RetrievalResult("unverified", analysis, candidates=all_candidates, reason="no_sufficiently_relevant_evidence", fallback_url=fallback, retrievers_used=used)
        return RetrievalResult("verified", analysis, evidence, all_candidates, fallback_url=fallback, retrievers_used=used)

    def _fallback_url(self, analysis: QueryAnalysis) -> str | None:
        scheme_ids = analysis.scheme_ids
        if not scheme_ids and "statement" in analysis.facts:
            link = self.registry.service_link("statement")
            return str(link.url) if link else None
        if not scheme_ids and analysis.general:
            link = self.registry.service_link("education")
            return str(link.url) if link else None
        if len(scheme_ids) != 1:
            return None
        source = self.sources.get(f"{scheme_ids[0]}_factsheet")
        return str(source.official_url) if source and source.status == "approved" else None

    def retrieve(self, query: object, context: ConversationContext | None = None) -> RetrievalResult:
        analysis = analyse_query(query, self.registry, context)
        if analysis.route == "invalid":
            return RetrievalResult("invalid", analysis, reason=analysis.reason)
        if analysis.route == "out_of_scope":
            return RetrievalResult("out_of_scope", analysis, reason=analysis.reason)
        if analysis.route == "clarify_scheme":
            return RetrievalResult("clarify", analysis, reason=analysis.reason)

        scheme_ids = analysis.scheme_ids
        fallback = self._fallback_url(analysis)
        # W1 "identify the exact fact": without a recognised fact type nothing can be checked
        # against the evidence, so relevance alone is never enough to verify an answer.
        if not analysis.facts:
            return RetrievalResult("unverified", analysis, reason="fact_not_recognised", fallback_url=fallback)
        if analysis.overview:
            return self._retrieve_overview(analysis, fallback)
        evidence: list[Evidence] = []
        all_candidates: list[Candidate] = []
        used: tuple[str, ...] = ()
        # Each named scheme is retrieved and gated separately; all must verify (RET-09).
        for scheme_id in scheme_ids or [None]:
            try:
                candidates, used = self._candidates(analysis, scheme_id)
            except Exception:  # noqa: BLE001 - without calibrated scores nothing can be verified.
                return RetrievalResult("unverified", analysis, reason="retrieval_unavailable", fallback_url=fallback)
            all_candidates += candidates
            outcome = self._gate_scheme(scheme_id, candidates, analysis.facts, definition=analysis.definition)
            if isinstance(outcome, str):
                return RetrievalResult("unverified", analysis, candidates=all_candidates, reason=outcome, fallback_url=fallback, retrievers_used=used)
            evidence.append(outcome)
        return RetrievalResult("verified", analysis, evidence, all_candidates, fallback_url=fallback, retrievers_used=used)


def main() -> int:
    import sys

    retriever = Retriever()
    for query in sys.argv[1:] or ["What is the exit load of ICICI Prudential Flexicap Fund?"]:
        result = retriever.retrieve(query)
        print(f"\nQ: {query}\n  status={result.status} reason={result.reason} retrievers={result.retrievers_used}")
        for item in result.evidence:
            c = item.primary.chunk
            print(f"  [{item.scheme_id}] {c.chunk_id} score={item.primary.rerank_score:.3f} date={c.source_updated_date} values={item.fact_values}")
            if item.corroborating:
                print(f"    corroborated by {item.corroborating.chunk.chunk_id} ({item.corroborating.rerank_score:.3f})")
        for candidate in result.candidates[:4]:
            print(f"    cand {candidate.chunk.chunk_id} {candidate.rerank_score:.3f} fact={candidate.fact_match}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
