"""Versioned, validated source registry for the approved corpus."""

from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.config import Settings, get_settings
from app.source_policy import validate_source_url

DocumentType = Literal["KIM", "SID", "FACTSHEET", "SAI", "INVESTOR_GUIDE"]
# One of each per supported scheme.
SCHEME_DOCUMENT_TYPES = ("KIM", "SID", "FACTSHEET")
# Not tied to a scheme: AMC-wide rules (SAI) and regulator investor education (SEBI/AMFI).
GENERAL_DOCUMENT_TYPES = frozenset({"SAI", "INVESTOR_GUIDE"})
MIN_SOURCES, MAX_SOURCES = 15, 25
SourceStatus = Literal["pending", "approved", "superseded", "failed", "blocked", "quarantined"]


class Scheme(BaseModel):
    model_config = ConfigDict(frozen=True)
    scheme_id: str = Field(pattern=r"^icici_[a-z0-9_]+$")
    scheme_name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()

    @field_validator("aliases")
    @classmethod
    def aliases_must_be_unique(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        values = [alias.casefold().strip() for alias in aliases]
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("aliases must be non-empty and unique within a scheme")
        return aliases


class SourceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: str = Field(pattern=r"^[a-z0-9_]+_(kim|sid|factsheet|sai|guide)$")
    # None for general (non-scheme) documents; scheme_name then names the publisher.
    scheme_id: str | None = Field(default=None, pattern=r"^icici_[a-z0-9_]+$")
    scheme_name: str = Field(min_length=1)
    title: str | None = None
    document_type: DocumentType
    official_url: HttpUrl
    approved_domain: str
    published_date: date | None = None
    retrieved_at: datetime | None = None
    resolved_url: HttpUrl | None = None
    content_type: str | None = None
    content_size_bytes: int | None = Field(default=None, ge=1)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    raw_file_name: str | None = Field(default=None, pattern=r"^[a-z0-9_.-]+$")
    content_version: int = Field(default=0, ge=0)
    failure_reason: str | None = None
    status: SourceStatus = "pending"

    @model_validator(mode="after")
    def official_url_must_be_approved(self) -> "SourceRecord":
        if (self.scheme_id is None) != (self.document_type in GENERAL_DOCUMENT_TYPES):
            raise ValueError("general documents (SAI, INVESTOR_GUIDE) have no scheme_id; scheme documents require one")
        result = validate_source_url(str(self.official_url))
        if not result.approved:
            raise ValueError(f"official_url is not approved: {result.reason}")
        if self.approved_domain.casefold() != result.host:
            raise ValueError("approved_domain must equal the official URL host")
        if self.resolved_url is not None:
            final_result = validate_source_url(str(self.official_url), resolved_final_url=str(self.resolved_url))
            if not final_result.approved:
                raise ValueError(f"resolved_url is not approved: {final_result.reason}")
        if self.status == "approved":
            required = (self.retrieved_at, self.resolved_url, self.content_type, self.content_size_bytes, self.content_hash, self.raw_file_name)
            if any(value is None for value in required):
                raise ValueError("approved sources require complete download metadata")
        if self.status in {"failed", "blocked"} and not self.failure_reason:
            raise ValueError("failed or blocked sources require a failure_reason")
        return self


class ServiceLink(BaseModel):
    """An official page used only as a link (e.g. where to request a statement), never as evidence:
    JavaScript-only pages cannot be ingested, so no fact may be quoted from them."""

    model_config = ConfigDict(frozen=True)
    link_id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    url: HttpUrl
    topics: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def url_must_be_approved(self) -> "ServiceLink":
        result = validate_source_url(str(self.url))
        if not result.approved:
            raise ValueError(f"service link is not approved: {result.reason}")
        return self


class SourceRegistry(BaseModel):
    model_config = ConfigDict(frozen=True)
    registry_version: int = Field(ge=1)
    generated_at: date
    schemes: tuple[Scheme, ...]
    sources: tuple[SourceRecord, ...]
    service_links: tuple[ServiceLink, ...] = ()

    @model_validator(mode="after")
    def validate_catalogue(self) -> "SourceRegistry":
        scheme_ids = [scheme.scheme_id for scheme in self.schemes]
        source_ids = [source.source_id for source in self.sources]
        if len(scheme_ids) != 5 or len(scheme_ids) != len(set(scheme_ids)):
            raise ValueError("registry must contain exactly five unique schemes")
        if not MIN_SOURCES <= len(source_ids) <= MAX_SOURCES or len(source_ids) != len(set(source_ids)):
            raise ValueError(f"registry must contain {MIN_SOURCES}-{MAX_SOURCES} unique sources")
        scheme_sources = [source for source in self.sources if source.scheme_id is not None]
        if any(source.scheme_id not in scheme_ids for source in scheme_sources):
            raise ValueError("every scheme source must refer to a registered scheme")
        if any(source.scheme_name != self.scheme_map[source.scheme_id].scheme_name for source in scheme_sources):
            raise ValueError("source scheme_name must match its registered scheme")
        expected = {(scheme_id, document_type) for scheme_id in scheme_ids for document_type in SCHEME_DOCUMENT_TYPES}
        actual = [(source.scheme_id, source.document_type) for source in scheme_sources]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError("each scheme must have exactly one KIM, SID, and FACTSHEET source")
        link_ids = [link.link_id for link in self.service_links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("service link ids must be unique")
        aliases = [alias.casefold().strip() for scheme in self.schemes for alias in scheme.aliases]
        if len(aliases) != len(set(aliases)):
            raise ValueError("aliases must be unique across schemes")
        return self

    @property
    def scheme_map(self) -> dict[str, Scheme]:
        return {scheme.scheme_id: scheme for scheme in self.schemes}

    def resolve_scheme(self, name_or_alias: str) -> Scheme | None:
        candidate = name_or_alias.casefold().strip()
        for scheme in self.schemes:
            names = {scheme.scheme_id.casefold(), scheme.scheme_name.casefold(), *(alias.casefold() for alias in scheme.aliases)}
            if candidate in names:
                return scheme
        return None

    def sources_for_scheme(self, scheme_id: str) -> tuple[SourceRecord, ...]:
        return tuple(source for source in self.sources if source.scheme_id == scheme_id)

    def service_link(self, topic: str) -> ServiceLink | None:
        return next((link for link in self.service_links if topic in link.topics), None)


def registry_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.resolved_path(settings.source_registry_path)


def load_registry(path: Path | None = None) -> SourceRegistry:
    path = path or registry_path()
    try:
        contents = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Source registry does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Source registry is not valid JSON: {path}") from error
    return SourceRegistry.model_validate(contents)


def write_registry(registry: SourceRegistry, path: Path | None = None) -> None:
    """Atomically persist a fully validated registry without writing source files."""
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(registry.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


@lru_cache
def get_registry() -> SourceRegistry:
    return load_registry()
