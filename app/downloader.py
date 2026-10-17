"""Safe, idempotent acquisition of registered official source files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import ssl
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import certifi

from app.config import Settings, get_settings
from app.registry import SourceRecord, SourceRegistry, load_registry, write_registry
from app.source_policy import validate_source_url


@dataclass(frozen=True)
class DownloadResult:
    source_id: str
    status: str
    reason: str | None = None
    raw_file_name: str | None = None
    reused: bool = False


class DownloadTransportError(Exception):
    """A download failed before a usable response could be inspected."""


@dataclass(frozen=True)
class DownloadResponse:
    url: str
    content: bytes
    headers: dict[str, str]


class SourceHttpClient(Protocol):
    def get(self, url: str) -> DownloadResponse: ...


class UrllibClient:
    """Minimal HTTPS client whose final URL is available after redirects."""

    # Verify against certifi's CA bundle: python.org macOS builds ship without system roots.
    # digitalfactsheet.icicipruamc.com omits its Sectigo intermediate, so ship that public
    # intermediate (verified to chain to a certifi root) to let verification stay on.
    _ssl_context = ssl.create_default_context(cafile=certifi.where())
    _ssl_context.load_verify_locations(cafile=Path(__file__).parent / "certs" / "sectigo_public_server_auth_ca_ov_r36.pem")

    def get(self, url: str) -> DownloadResponse:
        request = Request(url, headers={"User-Agent": "ICICI-Prudential-Facts-RAG/0.1"})
        try:
            with urlopen(request, timeout=30, context=self._ssl_context) as response:  # noqa: S310 - URL was allowlisted before this call.
                if not 200 <= response.status < 300:
                    raise DownloadTransportError(f"Unexpected HTTP status: {response.status}")
                return DownloadResponse(
                    url=response.geturl(),
                    content=response.read(),
                    headers={key.casefold(): value for key, value in response.headers.items()},
                )
        except (HTTPError, URLError) as error:
            raise DownloadTransportError(str(error)) from error


class SourceDownloader:
    """Downloads one pre-registered source; arbitrary URLs are never accepted."""

    def __init__(
        self,
        *,
        registry_file: Path | None = None,
        raw_sources_dir: Path | None = None,
        client: SourceHttpClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.registry_file = registry_file or settings.resolved_path(settings.source_registry_path)
        self.raw_sources_dir = raw_sources_dir or settings.resolved_path(settings.raw_sources_dir)
        self.client = client or UrllibClient()

    def close(self) -> None:
        return None

    def __enter__(self) -> "SourceDownloader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def download(self, source_id: str) -> DownloadResult:
        registry = load_registry(self.registry_file)
        source = next((item for item in registry.sources if item.source_id == source_id), None)
        if source is None:
            raise ValueError(f"Unknown registered source_id: {source_id}")

        preflight = validate_source_url(str(source.official_url))
        if not preflight.approved:
            return self._record_failure(registry, source, "blocked", preflight.reason or "URL is blocked")

        try:
            response = self.client.get(str(source.official_url))
        except DownloadTransportError as error:
            return self._record_failure(registry, source, "failed", f"Download failed: {error}")

        final_url = str(response.url)
        final_check = validate_source_url(str(source.official_url), resolved_final_url=final_url)
        if not final_check.approved:
            return self._record_failure(registry, source, "blocked", final_check.reason or "Final URL is blocked")

        content = response.content
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        invalid_reason = self._invalid_content_reason(source, content_type, content)
        if invalid_reason:
            return self._record_failure(registry, source, "failed", invalid_reason)

        digest = sha256(content).hexdigest()
        extension = ".pdf" if content.startswith(b"%PDF-") else ".html"
        raw_file_name = f"{source.source_id}.{digest[:16]}{extension}"
        # An unchanged file keeps its current verdict: a quarantined source is not silently re-approved.
        existing = source.status in {"approved", "quarantined"} and source.content_hash == digest and source.raw_file_name == raw_file_name
        if existing and (self.raw_sources_dir / raw_file_name).is_file():
            return DownloadResult(source.source_id, source.status, reason=source.failure_reason, raw_file_name=raw_file_name, reused=True)

        self.raw_sources_dir.mkdir(parents=True, exist_ok=True)
        self._write_raw_file(raw_file_name, content)
        version = source.content_version if source.content_hash == digest else source.content_version + 1
        updated = self._updated_source(
            source,
            status="approved",
            retrieved_at=datetime.now(UTC),
            resolved_url=final_url,
            content_type=content_type,
            content_size_bytes=len(content),
            content_hash=digest,
            raw_file_name=raw_file_name,
            content_version=version,
            failure_reason=None,
            # A new file version must have its document date re-read by the quality checks.
            published_date=source.published_date if source.content_hash == digest else None,
        )
        self._write_source_update(registry, updated)
        return DownloadResult(source.source_id, "approved", raw_file_name=raw_file_name)

    def _invalid_content_reason(self, source: SourceRecord, content_type: str, content: bytes) -> str | None:
        if not content:
            return "Download returned an empty file"
        is_pdf = content.startswith(b"%PDF-")
        is_html = content_type in {"text/html", "application/xhtml+xml"}
        if source.document_type != "FACTSHEET" and not is_pdf:
            return f"{source.document_type} sources must be valid PDF content"
        if source.document_type == "FACTSHEET" and not (is_pdf or is_html):
            return "Factsheet content must be PDF or HTML"
        if not content_type:
            return "Download has no content type"
        return None

    def _write_raw_file(self, raw_file_name: str, content: bytes) -> None:
        destination = self.raw_sources_dir / raw_file_name
        temporary = self.raw_sources_dir / f".{raw_file_name}.{uuid4().hex}.tmp"
        temporary.write_bytes(content)
        temporary.replace(destination)

    def _record_failure(self, registry: SourceRegistry, source: SourceRecord, status: str, reason: str) -> DownloadResult:
        updated = self._updated_source(source, status=status, failure_reason=reason)
        self._write_source_update(registry, updated)
        return DownloadResult(source.source_id, status, reason=reason)

    @staticmethod
    def _updated_source(source: SourceRecord, **updates: object) -> SourceRecord:
        data = source.model_dump(mode="json")
        data.update(updates)
        return SourceRecord.model_validate(data)

    def _write_source_update(self, registry: SourceRegistry, updated_source: SourceRecord) -> None:
        updated_registry = registry.model_copy(
            update={"sources": tuple(updated_source if source.source_id == updated_source.source_id else source for source in registry.sources)}
        )
        write_registry(SourceRegistry.model_validate(updated_registry.model_dump(mode="json")), self.registry_file)

    def download_all(self) -> list[DownloadResult]:
        """Download every registered source; one failure never stops the rest."""
        return [self.download(source.source_id) for source in load_registry(self.registry_file).sources]


def main() -> int:
    with SourceDownloader() as downloader:
        results = downloader.download_all()
    for result in results:
        detail = result.reason or ("unchanged" if result.reused else result.raw_file_name)
        print(f"{result.status:<9} {result.source_id:<34} {detail}")
    approved = sum(result.status == "approved" for result in results)
    print(f"\n{approved}/{len(results)} sources approved")
    return 0 if approved == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
