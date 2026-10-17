"""Approved-source boundary checks used before any ingestion or citation."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


APPROVED_DOMAINS = frozenset({
    "icicipruamc.com",
    "digitalfactsheet.icicipruamc.com",
    # Regulator / industry-body investor material (architecture: AMC, SEBI, and AMFI only).
    "sebi.gov.in",
    "amfiindia.com",
})
BLOCKED_DOMAINS = frozenset({"groww.in"})


@dataclass(frozen=True)
class UrlValidation:
    approved: bool
    reason: str | None = None
    host: str | None = None


def _normalise_host(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None, "Only HTTPS source URLs are permitted."
    if not parsed.hostname:
        return None, "The URL has no valid host."
    return parsed.hostname.lower().rstrip("."), None


def _matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def validate_source_url(url: str, *, resolved_final_url: str | None = None) -> UrlValidation:
    """Validate the supplied and, when available, final redirect URL.

    Callers that download a source must pass the final URL returned by their HTTP
    client, ensuring redirects cannot cross the approved-host boundary.
    """
    for candidate, label in ((url, "source"), (resolved_final_url, "resolved")):
        if candidate is None:
            continue
        host, error = _normalise_host(candidate)
        if error:
            return UrlValidation(False, f"Invalid {label} URL: {error}")
        assert host is not None
        if any(_matches_domain(host, blocked) for blocked in BLOCKED_DOMAINS):
            return UrlValidation(False, "Groww is blocked from ingestion and citations.", host)
        if not any(_matches_domain(host, approved) for approved in APPROVED_DOMAINS):
            return UrlValidation(False, "Host is not in the approved source allowlist.", host)
    host, _ = _normalise_host(resolved_final_url or url)
    return UrlValidation(True, host=host)
