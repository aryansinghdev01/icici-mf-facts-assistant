import json
from pathlib import Path

from app.downloader import SourceDownloader
from app.registry import load_registry


class FakeResponse:
    def __init__(self, *, url: str, content: bytes, content_type: str = "application/pdf") -> None:
        self.url = url
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.calls.append(url)
        return self.response


_DOWNLOAD_FIELDS = ("retrieved_at", "resolved_url", "content_type", "content_size_bytes", "content_hash", "raw_file_name", "failure_reason")


def copy_registry(tmp_path: Path) -> Path:
    """Copy the real registry with every source reset to pending, isolating tests from local downloads."""
    registry = json.loads(Path("data/source_registry.json").read_text())
    for source in registry["sources"]:
        source.update({field: None for field in _DOWNLOAD_FIELDS}, status="pending", content_version=0)
    destination = tmp_path / "source_registry.json"
    destination.write_text(json.dumps(registry))
    return destination


def test_successful_pdf_download_records_complete_metadata_and_is_idempotent(tmp_path: Path) -> None:
    registry_file = copy_registry(tmp_path)
    raw_dir = tmp_path / "raw_sources"
    client = FakeClient(FakeResponse(url="https://www.icicipruamc.com/current.pdf", content=b"%PDF-1.7 official fixture"))
    downloader = SourceDownloader(registry_file=registry_file, raw_sources_dir=raw_dir, client=client)

    first = downloader.download("icici_large_cap_kim")
    second = downloader.download("icici_large_cap_kim")
    source = next(item for item in load_registry(registry_file).sources if item.source_id == "icici_large_cap_kim")

    assert first.status == "approved"
    assert second.reused
    assert source.status == "approved"
    assert source.resolved_url is not None
    assert source.content_type == "application/pdf"
    assert source.content_size_bytes == len(b"%PDF-1.7 official fixture")
    assert source.content_hash is not None
    assert source.raw_file_name is not None and (raw_dir / source.raw_file_name).is_file()


def test_redirect_to_groww_is_blocked_without_writing_raw_content(tmp_path: Path) -> None:
    registry_file = copy_registry(tmp_path)
    raw_dir = tmp_path / "raw_sources"
    client = FakeClient(FakeResponse(url="https://groww.in/mutual-funds/example", content=b"%PDF-1.7 blocked"))
    result = SourceDownloader(registry_file=registry_file, raw_sources_dir=raw_dir, client=client).download("icici_large_cap_kim")

    source = next(item for item in load_registry(registry_file).sources if item.source_id == "icici_large_cap_kim")
    assert result.status == "blocked"
    assert source.status == "blocked"
    assert "Groww" in (source.failure_reason or "")
    assert not raw_dir.exists()


def test_empty_or_invalid_content_is_failed_without_writing_raw_content(tmp_path: Path) -> None:
    registry_file = copy_registry(tmp_path)
    raw_dir = tmp_path / "raw_sources"
    client = FakeClient(FakeResponse(url="https://www.icicipruamc.com/current.pdf", content=b""))
    result = SourceDownloader(registry_file=registry_file, raw_sources_dir=raw_dir, client=client).download("icici_large_cap_kim")

    source = next(item for item in load_registry(registry_file).sources if item.source_id == "icici_large_cap_kim")
    assert result.status == "failed"
    assert source.status == "failed"
    assert "empty" in (source.failure_reason or "").lower()
    assert not raw_dir.exists()
