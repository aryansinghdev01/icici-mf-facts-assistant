"""Vector (BGE + FAISS) and keyword (BM25) indexes over the approved chunks (Phase 2).

The vector index records which embedding model, dimension, and exact chunk file it was
built from. If any of these no longer match, semantic search is disabled until the
index is rebuilt (keyword search still works and is validated by the same gate).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

# torch must be imported before faiss: on macOS both bundle an OpenMP runtime, and loading
# faiss's first makes the BGE forward pass segfault.
import torch  # noqa: F401  isort: skip
import faiss  # isort: skip
import numpy as np
from rank_bm25 import BM25Okapi

from app.chunker import Chunk
from app.config import Settings, get_settings

# BGE v1.5 retrieval instruction for queries (passages are embedded without it).
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
INDEX_FILE = "chunks.faiss"
MANIFEST_FILE = "index_manifest.json"


class Embedder(Protocol):
    model_name: str

    def dimension(self) -> int: ...

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class BgeEmbedder:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def dimension(self) -> int:
        return int(self.model.get_sentence_embedding_dimension())

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False), dtype="float32")

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(self.model.encode([BGE_QUERY_INSTRUCTION + text], normalize_embeddings=True, show_progress_bar=False)[0], dtype="float32")


def passage_text(chunk: Chunk) -> str:
    """Text that is embedded and reranked: a short provenance header plus the chunk itself."""
    header = f"{chunk.scheme_name} | {chunk.document_type}" + (f" | {chunk.section_title}" if chunk.section_title else "")
    return f"{header}\n{chunk.text}"


_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")


def keyword_tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def load_chunks(path: Path) -> list[Chunk]:
    if not path.is_file():
        return []
    return [Chunk.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass(frozen=True)
class IndexStatus:
    semantic_available: bool
    reason: str | None
    chunk_count: int
    manifest: dict[str, object] | None


class CorpusIndex:
    def __init__(self, chunks: list[Chunk], bm25: BM25Okapi | None, vectors: faiss.Index | None, status: IndexStatus) -> None:
        self.chunks = chunks
        self.bm25 = bm25
        self.vectors = vectors
        self.status = status
        self.position = {chunk.chunk_id: index for index, chunk in enumerate(chunks)}


def build_index(*, embedder: Embedder | None = None, settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    embedder = embedder or BgeEmbedder(settings.bge_model)
    chunks_path = settings.resolved_path(settings.processed_dir) / "chunks.jsonl"
    chunks = load_chunks(chunks_path)
    if not chunks:
        raise RuntimeError("No chunks to index; run `python -m app.ingest` first.")
    vectors = embedder.embed_passages([passage_text(chunk) for chunk in chunks])
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)

    indexes_dir = settings.resolved_path(settings.indexes_dir)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    temporary = indexes_dir / f".{INDEX_FILE}.{uuid4().hex}.tmp"
    faiss.write_index(index, str(temporary))
    temporary.replace(indexes_dir / INDEX_FILE)
    manifest = {
        "built_at": datetime.now(UTC).isoformat(),
        "embedding_model": embedder.model_name,
        "dimension": int(vectors.shape[1]),
        "chunk_count": len(chunks),
        "chunks_sha256": file_sha256(chunks_path),
        "chunk_ids": [chunk.chunk_id for chunk in chunks],
    }
    temporary = indexes_dir / f".{MANIFEST_FILE}.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    temporary.replace(indexes_dir / MANIFEST_FILE)
    return {key: value for key, value in manifest.items() if key != "chunk_ids"}


def check_index(settings: Settings | None = None, *, expected_dimension: int | None = None) -> IndexStatus:
    """Compare the stored vector index with the current chunks and embedding configuration."""
    settings = settings or get_settings()
    chunks_path = settings.resolved_path(settings.processed_dir) / "chunks.jsonl"
    indexes_dir = settings.resolved_path(settings.indexes_dir)
    chunk_count = sum(1 for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()) if chunks_path.is_file() else 0
    manifest_path = indexes_dir / MANIFEST_FILE
    if not chunks_path.is_file():
        return IndexStatus(False, "no_chunks", 0, None)
    if not manifest_path.is_file() or not (indexes_dir / INDEX_FILE).is_file():
        return IndexStatus(False, "vector_index_missing", chunk_count, None)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = {key: value for key, value in manifest.items() if key != "chunk_ids"}
    if manifest.get("embedding_model") != settings.bge_model:
        return IndexStatus(False, "embedding_model_changed", chunk_count, summary)
    if expected_dimension is not None and manifest.get("dimension") != expected_dimension:
        return IndexStatus(False, "embedding_dimension_mismatch", chunk_count, summary)
    if manifest.get("chunks_sha256") != file_sha256(chunks_path):
        return IndexStatus(False, "vector_index_stale", chunk_count, summary)
    return IndexStatus(True, None, chunk_count, summary)


def load_index(settings: Settings | None = None) -> CorpusIndex:
    settings = settings or get_settings()
    chunks = load_chunks(settings.resolved_path(settings.processed_dir) / "chunks.jsonl")
    status = check_index(settings)
    bm25 = BM25Okapi([keyword_tokens(passage_text(chunk)) for chunk in chunks]) if chunks else None
    vectors = None
    if status.semantic_available:
        manifest = json.loads((settings.resolved_path(settings.indexes_dir) / MANIFEST_FILE).read_text(encoding="utf-8"))
        if manifest.get("chunk_ids") == [chunk.chunk_id for chunk in chunks]:
            vectors = faiss.read_index(str(settings.resolved_path(settings.indexes_dir) / INDEX_FILE))
        else:
            status = IndexStatus(False, "vector_index_stale", len(chunks), status.manifest)
    return CorpusIndex(chunks, bm25, vectors, status)


def main() -> int:
    summary = build_index()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
