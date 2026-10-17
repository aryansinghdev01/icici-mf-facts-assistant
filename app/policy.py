"""Input guardrails that run before any retrieval or model call (Phase 3).

Order matters: PII is checked first so that a blocked request never reaches the index,
the model, or the logs. Classification returns only a category, never the matched text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.privacy import detect_pii

PolicyCategory = Literal["pii", "advice", "performance", "injection", "secrets", "allowed"]

# Recommendations, suitability, and allocation, including hypothetical or "educational" framing.
_ADVICE = re.compile(
    r"\bshould\s+(?:i|we|one|a person|someone|my)\b"
    r"|\b(?:buy|sell|hold|exit|redeem|switch|invest)\b.{0,40}\bor\s+not\b"
    r"|\bis\s+(?:it|this|the|icici\b.{0,60})\s*.{0,40}\b(?:good|bad|worth|safe|wise|right|suitable|a good)\b"
    r"|\b(?:good|bad|right|wise|suitable)\s+(?:investment|fund|scheme|choice|option|time)\b"
    r"|\b(?:which|what)\s+(?:fund|scheme|one|of these)\b.{0,60}\b(?:best|better|choose|pick|buy|invest|prefer|safer|suitable)\b"
    r"|\b(?:best|top|safest|safer|better)\s+(?:fund|scheme|option|choice|investment|mutual fund)s?\b"
    r"|\bsafer\s+than\b|\bbetter\s+than\b"
    r"|\b(?:recommend|suggest|advise|advice)\b"
    r"|\bworth\s+(?:it|investing|buying)\b"
    r"|\b(?:allocate|allocation|portfolio|rebalance|diversify)\b"
    r"|\binvest\b.{0,40}\bin\s+which\b|\bwhere\s+(?:should|to|can)\s+i\s+invest\b"
    r"|\bsuitable\s+for\s+(?:me|a beginner|beginners|my)\b|\bfor\s+a\s+beginner\b"
    r"|\bi\s+am\s+\d{1,2}\b",
    re.IGNORECASE,
)
# Returns, performance, and calculations; the official factsheet is linked instead.
_PERFORMANCE = re.compile(
    r"\breturns?\b|\bperform(?:ance|ed|ing)?\b|\bcagr\b|\bxirr\b|\byield(?:ed)?\b"
    r"|\b(?:nav|price)\s+(?:growth|history|chart|trend)\b"
    r"|\b(?:how much|what)\b.{0,40}\b(?:earn|gain|grow|grown|make|made|profit)\b"
    r"|\bcalculate\b|\bprofit\b|\boutperform|\bbeat(?:s|en)?\s+the\s+(?:benchmark|market|index)\b"
    r"|\b(?:highest|lowest|top)[- ]?(?:performing|return|gain)",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"\bignore\b.{0,40}\b(?:instructions?|rules?|guidelines?|polic(?:y|ies)|prompt|safeguards?)\b"
    r"|\b(?:disregard|override|bypass|forget)\b.{0,40}\b(?:instructions?|rules?|polic(?:y|ies)|restrictions?)\b"
    r"|\b(?:pretend|act as|you are now|roleplay|jailbreak|developer mode)\b"
    r"|\b(?:use|cite|search|check)\b.{0,30}\b(?:groww|zerodha|google|wikipedia|blogs?|moneycontrol|value research|internet|web)\b",
    re.IGNORECASE,
)
_SECRETS = re.compile(
    r"\b(?:system|hidden|internal)\s+(?:prompt|instructions?|message)\b|\bapi[\s_-]?keys?\b|\bcredentials?\b|\bpassword\b"
    r"|\b(?:show|reveal|print|dump|list)\b.{0,30}\b(?:chunks?|prompt|metadata|database|index|raw (?:text|documents?))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyDecision:
    category: PolicyCategory
    detail: str | None = None  # e.g. the PII category name; never the matched value


def classify(question: str) -> PolicyDecision:
    pii = detect_pii(question)
    if pii:
        return PolicyDecision("pii", pii)
    if _SECRETS.search(question):
        return PolicyDecision("secrets")
    # Advice is refused even when wrapped in "ignore your rules" or "hypothetically" framing.
    if _ADVICE.search(question):
        return PolicyDecision("advice")
    if _PERFORMANCE.search(question):
        return PolicyDecision("performance")
    if _INJECTION.search(question):
        return PolicyDecision("injection")
    return PolicyDecision("allowed")
