"""Phase 2 retrieval evaluation against the real corpus (docs/eval.md, Phase 2 metrics).

Run: ``python -m app.eval_retrieval``. Writes ``data/processed/retrieval_eval.{json,md}``.
Expected values were checked by hand against the cited official PDF pages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.retrieval import RetrievalResult, Retriever


@dataclass(frozen=True)
class Case:
    case_id: str
    query: str
    expected_status: str
    expected_schemes: tuple[str, ...] = ()
    # Expected primary evidence: source and page, and values that must appear in it.
    expected_source: str | None = None
    expected_page: int | None = None
    must_contain: tuple[str, ...] = ()
    note: str = ""
    expected_fallback: str | None = None


CASES: tuple[Case, ...] = (
    Case("R01", "What is the exit load of ICICI Prudential Flexicap Fund?", "verified", ("icici_flexicap",), "icici_flexicap_factsheet", 1, ("1%",), "Factsheet (newest) wins over KIM's 12-month rule"),
    Case("R02", "What is the expense ratio of ICICI Large Cap?", "verified", ("icici_large_cap",), "icici_large_cap_factsheet", 1, ("1.13%", "0.71%")),
    Case("R03", "large and midcap expense ratio", "verified", ("icici_large_mid_cap",), "icici_large_mid_cap_factsheet", 1, ("1.33%", "0.67%")),
    Case("R04", "ICICI flexycap expense ratio", "verified", ("icici_flexicap",), "icici_flexicap_factsheet", 1, ("1.38%", "0.66%"), "Typo-tolerant scheme match"),
    Case("R05", "What is Top 100's benchmark?", "verified", ("icici_large_mid_cap",), "icici_large_mid_cap_factsheet", 1, ("LargeMidcap 250",), "Alias resolution"),
    Case("R06", "Flexi Cap minimum SIP?", "verified", ("icici_flexicap",), must_contain=("100",)),
    Case("R07", "Silver ETF FoF minimum investment", "verified", ("icici_silver_etf_fof",), must_contain=("100",)),
    Case("R08", "Silver ETF fund of fund riskometer?", "verified", ("icici_silver_etf_fof",)),
    Case("R09", "Who is the fund manager of Smallcap fund?", "verified", ("icici_smallcap",), "icici_smallcap_factsheet", 1),
    Case("R10", "Compare Large Cap and Flexicap exit loads.", "verified", ("icici_large_cap", "icici_flexicap"), note="Each scheme verified separately"),
    Case("R11", "What is the benchmark of ICICI Prudential Smallcap Fund?", "verified", ("icici_smallcap",)),
    Case("R12", "What is the expense ratio of the Bluechip fund?", "verified", ("icici_large_cap",), "icici_large_cap_factsheet", 1, ("1.13%",), "Former scheme name"),
    # Negative paths: must never produce verified evidence.
    Case("N01", "What is the exit load?", "clarify", note="RET-01 no scheme"),
    Case("N02", "What is the expense ratio of Fund X?", "out_of_scope", note="RET-08"),
    Case("N03", "HDFC Flexi Cap expense ratio", "out_of_scope", note="Other AMC must not match our Flexicap"),
    Case("N04", "ICICI Midcap exit load", "out_of_scope", note="Unsupported ICICI scheme"),
    Case("N05", "Explain tax benefit of ELSS", "out_of_scope", note="SAFE-13"),
    Case("N06", "SIP?", "clarify", note="RET-17"),
    Case("N07", "What colour is the ICICI Flexicap logo?", "unverified", ("icici_flexicap",), note="Low-confidence leakage check"),
    Case("N08", "How do I download my capital gains statement?", "unverified", note="No ingestible official steps; answered with the official service link",
         expected_fallback="https://www.icicipruamc.com/investor-services"),
    # General (scheme-independent) questions answered from the SAI and SEBI investor guide.
    Case("G01", "What is an expense ratio?", "verified", expected_source="sebi_mf_investor_faq_guide", note="Definition (RET-20)"),
    Case("G02", "What does exit load mean?", "verified", expected_source="sebi_mf_investor_faq_guide"),
    Case("G03", "What is a consolidated account statement?", "verified", note="Statement rules"),
    Case("G04", "What is a riskometer?", "verified"),
)


@dataclass
class CaseResult:
    case: Case
    result: RetrievalResult
    passed: bool
    failures: list[str] = field(default_factory=list)
    recall_hit: bool | None = None


def evaluate_case(retriever: Retriever, case: Case) -> CaseResult:
    result = retriever.retrieve(case.query)
    failures: list[str] = []
    if result.status != case.expected_status:
        failures.append(f"status {result.status} != {case.expected_status} ({result.reason})")
    if case.expected_fallback and result.fallback_url != case.expected_fallback:
        failures.append(f"fallback {result.fallback_url} != {case.expected_fallback}")
    if case.expected_schemes and tuple(result.analysis.scheme_ids) != case.expected_schemes:
        failures.append(f"schemes {result.analysis.scheme_ids} != {list(case.expected_schemes)}")
    # Cross-scheme check: no evidence may come from a scheme other than the one it was gated for.
    for item in result.evidence:
        if case.expected_schemes and item.primary.chunk.scheme_id is None:
            failures.append(f"scheme question answered from general document {item.primary.chunk.chunk_id}")
        if item.primary.chunk.scheme_id != item.scheme_id:
            failures.append(f"cross-scheme evidence {item.primary.chunk.chunk_id}")
    recall_hit = None
    if case.expected_status == "verified" and result.evidence:
        primary = result.evidence[0].primary.chunk
        if case.expected_source and primary.source_id != case.expected_source:
            failures.append(f"primary source {primary.source_id} != {case.expected_source}")
        if case.expected_page and primary.page_number != case.expected_page:
            failures.append(f"primary page {primary.page_number} != {case.expected_page}")
        # The answer may need the neighbouring chunk of the same page (values split by overlap).
        page_text = " ".join(
            c.text for c in retriever.index.chunks if c.source_id == primary.source_id and c.page_number == primary.page_number
        )
        for value in case.must_contain:
            if value.replace(" ", "") not in page_text.replace(" ", ""):
                failures.append(f"value {value!r} not on cited page")
        if case.expected_source:
            top10 = result.candidates[:10]
            recall_hit = any(c.chunk.source_id == case.expected_source and (case.expected_page is None or c.chunk.page_number == case.expected_page) for c in top10)
    return CaseResult(case, result, not failures, failures, recall_hit)


def run() -> dict[str, object]:
    retriever = Retriever()
    results = [evaluate_case(retriever, case) for case in CASES]
    scheme_cases = [r for r in results if r.case.expected_schemes]
    recall_cases = [r for r in results if r.recall_hit is not None]
    scheme_specific = [r for r in results if r.result.evidence]
    negatives = [r for r in results if r.case.expected_status != "verified"]
    metrics = {
        "scheme_resolution_accuracy": sum(tuple(r.result.analysis.scheme_ids) == r.case.expected_schemes for r in scheme_cases) / len(scheme_cases),
        "top10_evidence_recall": sum(bool(r.recall_hit) for r in recall_cases) / len(recall_cases) if recall_cases else None,
        "cross_scheme_retrieval_rate": sum(any(e.primary.chunk.scheme_id != e.scheme_id for e in r.result.evidence) for r in scheme_specific) / max(len(scheme_specific), 1),
        "low_confidence_leakage": sum(r.result.status == "verified" for r in negatives) / len(negatives),
        "cases_passed": f"{sum(r.passed for r in results)}/{len(results)}",
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "reranker": retriever.settings.reranker_model,
        "rerank_min_score": retriever.min_score,
        "semantic_index": retriever.index.status.reason or "available",
        "metrics": metrics,
        "cases": [
            {
                "id": r.case.case_id,
                "query": r.case.query,
                "expected": r.case.expected_status,
                "actual": r.result.status,
                "reason": r.result.reason,
                "schemes": r.result.analysis.scheme_ids,
                "evidence": [
                    {"chunk_id": e.primary.chunk.chunk_id, "score": round(e.primary.rerank_score, 3), "date": e.primary.chunk.source_updated_date.isoformat(), "url": e.primary.chunk.official_url}
                    for e in r.result.evidence
                ],
                "passed": r.passed,
                "failures": r.failures,
                "note": r.case.note,
            }
            for r in results
        ],
    }
    out = get_settings().resolved_path(get_settings().processed_dir)
    (out / "retrieval_eval.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "retrieval_eval.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict[str, object]) -> str:
    m = report["metrics"]
    lines = [
        "# Phase 2 Retrieval Evaluation", "", f"Generated: {report['generated_at']} · reranker `{report['reranker']}` (min score {report['rerank_min_score']}) · semantic index: {report['semantic_index']}", "",
        "| Metric | Target | Result |", "| --- | --- | --- |",
        f"| Scheme-resolution accuracy | 100% | {m['scheme_resolution_accuracy']:.0%} |",
        f"| Top-10 evidence recall | 100% | {m['top10_evidence_recall']:.0%} |",
        f"| Cross-scheme retrieval rate | 0% | {m['cross_scheme_retrieval_rate']:.0%} |",
        f"| Low-confidence leakage | 0% | {m['low_confidence_leakage']:.0%} |",
        f"| Cases passed | all | {m['cases_passed']} |", "",
        "| ID | Query | Expected | Actual | Evidence | Result |", "| --- | --- | --- | --- | --- | --- |",
    ]
    for c in report["cases"]:
        evidence = "; ".join(f"{e['chunk_id']} ({e['score']})" for e in c["evidence"]) or (c["reason"] or "—")
        verdict = "pass" if c["passed"] else "FAIL: " + "; ".join(c["failures"])
        lines.append(f"| {c['id']} | {c['query']} | {c['expected']} | {c['actual']} | {evidence} | {verdict} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    report = run()
    print(Path(get_settings().resolved_path(get_settings().processed_dir) / "retrieval_eval.md").read_text(encoding="utf-8"))
    m = report["metrics"]
    ok = m["scheme_resolution_accuracy"] == 1 and m["cross_scheme_retrieval_rate"] == 0 and m["low_confidence_leakage"] == 0 and m["top10_evidence_recall"] == 1
    return 0 if ok and m["cases_passed"].split("/")[0] == m["cases_passed"].split("/")[1] else 1


if __name__ == "__main__":
    raise SystemExit(main())
