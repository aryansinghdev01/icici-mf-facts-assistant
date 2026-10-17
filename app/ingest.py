"""Full-corpus ingestion run and audit report (Phase 1.6).

Runs download -> extraction -> quality gate -> chunking for every registered source and
writes ``data/processed/ingestion_report.{json,md}``. Only sources that end the run
``approved`` with chunks are ready for Phase 2 indexing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.chunker import Chunk, SourceChunker
from app.config import Settings, get_settings
from app.downloader import SourceDownloader
from app.extractor import SourceExtractor
from app.quality import QualityChecker
from app.registry import load_registry
from app.source_policy import BLOCKED_DOMAINS


def groww_exclusion_check(registry_file: Path, raw_sources_dir: Path, chunks: list[Chunk]) -> dict[str, int]:
    """Count any blocked-domain reference in the registry, raw storage, or chunk metadata/text."""
    registry_text = registry_file.read_text(encoding="utf-8").casefold()
    raw_names = [path.name.casefold() for path in raw_sources_dir.iterdir()] if raw_sources_dir.is_dir() else []
    counts = {"registry": 0, "raw_files": 0, "chunk_urls": 0, "chunk_text": 0}
    for domain in BLOCKED_DOMAINS:
        counts["registry"] += registry_text.count(domain)
        counts["raw_files"] += sum(domain in name for name in raw_names)
        counts["chunk_urls"] += sum(domain in chunk.official_url.casefold() for chunk in chunks)
        counts["chunk_text"] += sum(domain in chunk.text.casefold() for chunk in chunks)
    return counts


def run(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    with SourceDownloader(settings=settings) as downloader:
        downloads = {result.source_id: result for result in downloader.download_all()}
    extractions = {result.source_id: result for result in SourceExtractor(settings=settings).extract_all()}
    quality = {result.source_id: result for result in QualityChecker(settings=settings).check_all()}
    chunk_results, chunks = SourceChunker(settings=settings).chunk_all()
    chunking = {result.source_id: result for result in chunk_results}

    registry_file = settings.resolved_path(settings.source_registry_path)
    registry = load_registry(registry_file)
    rows = []
    for source in registry.sources:
        reasons = [
            *([downloads[source.source_id].reason] if downloads[source.source_id].reason else []),
            *([extractions[source.source_id].reason] if extractions[source.source_id].reason else []),
            *quality[source.source_id].reasons,
            *chunking[source.source_id].reasons,
        ]
        ready = source.status == "approved" and chunking[source.source_id].status == "chunked"
        rows.append({
            "source_id": source.source_id,
            "scheme_id": source.scheme_id,
            "document_type": source.document_type,
            "status": source.status,
            "ready_for_indexing": ready,
            "document_date": source.published_date.isoformat() if source.published_date else None,
            "content_hash": source.content_hash,
            "content_version": source.content_version,
            "pages": quality[source.source_id].total_pages,
            "usable_pages": quality[source.source_id].usable_pages,
            "chunks": chunking[source.source_id].chunks,
            "warnings": quality[source.source_id].warnings,
            "reasons": reasons,
        })

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sources_ready": sum(row["ready_for_indexing"] for row in rows),
        "sources_total": len(rows),
        "total_chunks": len(chunks),
        "groww_exclusion": groww_exclusion_check(registry_file, settings.resolved_path(settings.raw_sources_dir), chunks),
        "sources": rows,
    }
    processed = settings.resolved_path(settings.processed_dir)
    _write(processed / "ingestion_report.json", json.dumps(report, indent=2) + "\n")
    _write(processed / "ingestion_report.md", _markdown(report))
    return report


def _markdown(report: dict[str, object]) -> str:
    lines = [
        "# Ingestion Report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"**{report['sources_ready']}/{report['sources_total']} sources ready for indexing** · {report['total_chunks']} chunks",
        "",
        f"Groww exclusion check: {report['groww_exclusion']}",
        "",
        "| Source | Type | Status | Ready | Document date | Version | Hash | Pages (usable/total) | Chunks | Warnings / reasons |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["sources"]:  # type: ignore[union-attr]
        notes = ", ".join([*row["reasons"], *row["warnings"]]) or "—"
        lines.append(
            f"| {row['source_id']} | {row['document_type']} | {row['status']} | {'yes' if row['ready_for_indexing'] else 'no'} | "
            f"{row['document_date'] or '—'} | {row['content_version']} | {(row['content_hash'] or '')[:12]} | "
            f"{row['usable_pages']}/{row['pages']} | {row['chunks']} | {notes} |"
        )
    return "\n".join(lines) + "\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    report = run()
    print(Path(get_settings().resolved_path(get_settings().processed_dir) / "ingestion_report.md").read_text(encoding="utf-8"))
    clean = not any(report["groww_exclusion"].values())  # type: ignore[union-attr]
    return 0 if report["sources_ready"] == report["sources_total"] and clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
