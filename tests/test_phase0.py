import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.index import file_sha256
from app.main import app
from app.privacy import detect_pii
from app.source_policy import validate_source_url


def health_with(settings: Settings) -> dict:
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            response = client.get("/api/health")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    return response.json()


def built_index_settings(tmp_path: Path) -> Settings:
    settings = Settings(**{name: tmp_path / name for name in ("raw_sources_dir", "extracted_text_dir", "processed_dir", "indexes_dir", "logs_dir")})
    settings.ensure_storage_directories()
    (settings.processed_dir / "chunks.jsonl").write_text("{}\n")
    (settings.indexes_dir / "chunks.faiss").write_bytes(b"index")
    manifest = {"embedding_model": settings.bge_model, "dimension": 384, "chunks_sha256": file_sha256(settings.processed_dir / "chunks.jsonl")}
    (settings.indexes_dir / "index_manifest.json").write_text(json.dumps(manifest))
    return settings


def test_health_is_degraded_when_the_vector_index_is_missing(tmp_path: Path) -> None:
    settings = Settings(**{name: tmp_path / name for name in ("raw_sources_dir", "extracted_text_dir", "processed_dir", "indexes_dir", "logs_dir")})
    payload = health_with(settings)
    assert payload["status"] == "degraded"
    assert payload["configuration"]["index"]["semantic_search"] is False


def test_health_is_degraded_after_an_embedding_model_change(tmp_path: Path) -> None:
    settings = built_index_settings(tmp_path)
    assert health_with(settings)["status"] == "healthy"
    changed = Settings(**{**settings.model_dump(), "bge_model": "BAAI/bge-base-en-v1.5"})
    assert health_with(changed)["configuration"]["index"]["reason"] == "embedding_model_changed"


def test_health_reports_models_and_sanitised_configuration(tmp_path: Path) -> None:
    payload = health_with(built_index_settings(tmp_path))
    assert payload["status"] == "healthy"
    assert payload["configuration"]["llm"]["provider"] == "Gemini"
    assert payload["configuration"]["llm"]["model"].startswith("gemini")
    assert payload["configuration"]["embeddings"] == {"provider": "BGE", "model": "BAAI/bge-small-en-v1.5"}
    assert all(payload["configuration"]["storage"].values())
    assert "key" not in str(payload).lower()


def test_approved_icici_url_is_accepted() -> None:
    result = validate_source_url("https://www.icicipruamc.com/blob/factsheet.pdf")
    assert result.approved


def test_groww_and_lookalike_domains_are_rejected() -> None:
    assert not validate_source_url("https://groww.in/mutual-funds/example").approved
    assert not validate_source_url("https://icicipruamc.com.evil.example/document.pdf").approved


def test_redirect_must_end_on_approved_domain() -> None:
    result = validate_source_url(
        "https://www.icicipruamc.com/document.pdf",
        resolved_final_url="https://groww.in/document.pdf",
    )
    assert not result.approved


def test_privacy_detector_returns_category_without_echoing_value() -> None:
    assert detect_pii("My PAN is ABCDE1234F") == "PAN"
    assert detect_pii("Email me at person@example.com") == "email"
    assert detect_pii("What is the Flexicap exit load?") is None
