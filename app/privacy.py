"""Conservative, local PII detection. Detected content must not be logged."""

from __future__ import annotations

import re


_PATTERNS: dict[str, re.Pattern[str]] = {
    "PAN": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE),
    "Aadhaar": re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
    "OTP": re.compile(r"\b(?:otp|one[ -]?time password)\s*(?:is|:)?\s*\d{4,8}\b", re.IGNORECASE),
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "phone number": re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"),
    "account number": re.compile(r"\b(?:account|folio)\s*(?:number|no\.?|#)?\s*(?:is|:)?\s*\d{6,18}\b", re.IGNORECASE),
}


def detect_pii(text: str) -> str | None:
    """Return only a category name, never the matched sensitive value."""
    for category, pattern in _PATTERNS.items():
        if pattern.search(text):
            return category
    if re.search(r"\b(?:my\s+)?(?:pan|aadhaar|account|folio)\s+(?:is|number|no\.?|:)\s*\*+", text, re.IGNORECASE):
        return "sensitive account information"
    return None
