import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.registry import SourceRegistry, load_registry


def test_registry_contains_exactly_the_approved_catalogue() -> None:
    registry = load_registry()
    assert registry.registry_version == 2
    assert len(registry.schemes) == 5
    assert len([s for s in registry.sources if s.scheme_id]) == 15
    assert 15 <= len(registry.sources) <= 25
    assert {source.status for source in registry.sources} <= {"pending", "approved"}
    assert {source.document_type for source in registry.sources} == {"KIM", "SID", "FACTSHEET", "SAI", "INVESTOR_GUIDE"}
    assert all(source.scheme_id is None for source in registry.sources if source.document_type in {"SAI", "INVESTOR_GUIDE"})
    assert all("groww.in" not in str(source.official_url) for source in registry.sources)


def test_every_scheme_has_one_of_each_required_document_type() -> None:
    registry = load_registry()
    for scheme in registry.schemes:
        assert {source.document_type for source in registry.sources_for_scheme(scheme.scheme_id)} == {"KIM", "SID", "FACTSHEET"}


def test_top_100_resolves_to_large_and_mid_cap_fund() -> None:
    scheme = load_registry().resolve_scheme("top 100")
    assert scheme is not None
    assert scheme.scheme_id == "icici_large_mid_cap"


def test_registry_rejects_groww_url() -> None:
    invalid = deepcopy(json.loads(Path("data/source_registry.json").read_text()))
    invalid["sources"][0]["official_url"] = "https://groww.in/mutual-funds/example"
    invalid["sources"][0]["approved_domain"] = "groww.in"
    with pytest.raises(ValidationError, match="Groww"):
        SourceRegistry.model_validate(invalid)


def test_registry_rejects_duplicate_source_ids() -> None:
    invalid = deepcopy(json.loads(Path("data/source_registry.json").read_text()))
    invalid["sources"][1]["source_id"] = invalid["sources"][0]["source_id"]
    with pytest.raises(ValidationError, match="15-25 unique sources"):
        SourceRegistry.model_validate(invalid)


def test_general_documents_cannot_claim_a_scheme() -> None:
    invalid = deepcopy(json.loads(Path("data/source_registry.json").read_text()))
    general = next(source for source in invalid["sources"] if source["document_type"] == "SAI")
    general["scheme_id"] = "icici_flexicap"
    with pytest.raises(ValidationError, match="general documents"):
        SourceRegistry.model_validate(invalid)


def test_service_links_must_use_approved_domains() -> None:
    invalid = deepcopy(json.loads(Path("data/source_registry.json").read_text()))
    invalid["service_links"][0]["url"] = "https://groww.in/statements"
    with pytest.raises(ValidationError, match="not approved"):
        SourceRegistry.model_validate(invalid)


def test_registry_rejects_more_than_25_sources() -> None:
    invalid = deepcopy(json.loads(Path("data/source_registry.json").read_text()))
    template = next(source for source in invalid["sources"] if source["document_type"] == "INVESTOR_GUIDE")
    invalid["sources"] += [{**template, "source_id": f"extra_{i}_guide"} for i in range(10)]
    with pytest.raises(ValidationError, match="15-25 unique sources"):
        SourceRegistry.model_validate(invalid)
