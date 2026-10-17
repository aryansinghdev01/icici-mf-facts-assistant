"""Query understanding: which supported scheme(s) and which fact(s) a question asks about.

Runs before retrieval so that ambiguous questions are clarified and unsupported funds
are declined without ever searching (or substituting) a similar supported scheme.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from app.registry import Scheme, SourceRegistry

Route = Literal["retrieve", "clarify_scheme", "out_of_scope", "invalid"]

# Facts that are the same for every scheme (e.g. how to get a statement) need no scheme.
SCHEME_INDEPENDENT_FACTS = frozenset({"statement"})

QUERY_FACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "expense_ratio": re.compile(r"expense ratio|\bter\b|total expense|\bfees?\b|\bcharges?\b|\bcosts?\b", re.IGNORECASE),
    "exit_load": re.compile(r"exit load|exit charge|redemption (?:charge|fee|load)|\bload\b", re.IGNORECASE),
    "minimum_investment": re.compile(r"\bmin(?:imum)?\b|how much (?:do i need|to invest|money)|least amount", re.IGNORECASE),
    "sip": re.compile(r"\bsip\b|systematic investment", re.IGNORECASE),
    "benchmark": re.compile(r"benchmark|which index|index (?:does|is)", re.IGNORECASE),
    "riskometer": re.compile(r"riskometer|risk-?o-?meter|risk level|how risky|risk (?:category|rating|grade)|\brisk\b", re.IGNORECASE),
    "lock_in": re.compile(r"lock[- ]?in", re.IGNORECASE),
    "nav": re.compile(r"\bnav\b|net asset value", re.IGNORECASE),
    "fund_manager": re.compile(r"fund manager|who manages|managed by|manager", re.IGNORECASE),
    "investment_objective": re.compile(r"objective|invests? in|investment strategy|what does .* invest", re.IGNORECASE),
    "statement": re.compile(r"statement|capital gains?|\bcas\b", re.IGNORECASE),
    "plans_options": re.compile(r"direct plan|regular plan|\bidcw\b|growth option|dividend", re.IGNORECASE),
}

# Other fund houses: a question about "HDFC Flexi Cap" must not match our Flexicap scheme.
OTHER_AMCS = (
    "sbi", "hdfc", "axis", "nippon", "kotak", "mirae", "parag parikh", "ppfas", "quant", "uti", "dsp", "tata",
    "aditya birla", "absl", "franklin", "templeton", "invesco", "edelweiss", "motilal", "canara", "bandhan",
    "idfc", "hsbc", "sundaram", "pgim", "baroda", "lic", "mahindra", "union", "navi", "groww", "zerodha", "whiteoak",
)
# ICICI fund names/categories we do not cover; checked only on words that are not already part of
# a supported scheme name, so "Large & Mid Cap" is not mistaken for the (unsupported) Midcap Fund.
# Phrases are fund names, not plain words, so "net asset value" or "invests in debt" are not blocked.
UNSUPPORTED_SCHEME_WORDS = (
    "elss", "tax saver", "midcap", "mid cap", "multicap", "multi cap", "multi asset", "value fund", "value discovery",
    "focused equity", "dividend yield", "balanced advantage", "equity and debt", "arbitrage fund", "equity arbitrage",
    "liquid fund", "gilt fund", "debt fund", "bond fund", "overnight fund", "money market fund", "index fund",
    "nifty next 50", "gold etf", "gold fund", "technology fund", "pharma", "banking and financial", "infrastructure fund",
    "bharat 22", "savings fund", "hybrid fund", "psu equity", "fmcg fund", "consumption fund", "manufacturing fund",
    "innovation fund", "quant fund", "commodities fund",
)
# Unsupported names that contain a supported alias; checked on the full question.
UNSUPPORTED_CONTAINING_ALIAS = ("us bluechip",)
MAX_QUERY_CHARS = 500
# "What is an expense ratio?" / "What does exit load mean?" ask for a general definition, answered
# from investor-education sources. "What is the exit load?" is scheme-specific and is clarified.
_DEFINITION = re.compile(
    r"^\s*(?:what\s+(?:is|are)\s+(?:an?\s+|meant by\s+)?(?!the\b)|what does\b.*\bmean|define\b|definition of\b|explain\b|meaning of\b|how does\b.*\bwork)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SchemeMatch:
    scheme_id: str
    matched_text: str
    fuzzy: bool = False


@dataclass
class QueryAnalysis:
    query: str
    route: Route
    schemes: list[SchemeMatch] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    reason: str | None = None
    # Scheme-independent question (definition or statement process), answered from general documents.
    general: bool = False
    definition: bool = False

    @property
    def scheme_ids(self) -> list[str]:
        return [match.scheme_id for match in self.schemes]


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%]+", " ", text)).strip()


def _variants(scheme: Scheme) -> set[str]:
    """Spaced name variants for a scheme, without the AMC prefix and generic suffixes."""
    names = {scheme.scheme_name, *scheme.aliases}
    variants: set[str] = set()
    for name in names:
        value = _normalise(name)
        value = re.sub(r"^(?:icici prudential|icici pru|icici)\s+", "", value)
        value = re.sub(r"\s+(?:fund|scheme)$", "", value)
        if value:
            variants.add(value)
            variants.add(value.replace(" and ", " "))  # "large mid cap"
    if scheme.scheme_id == "icici_silver_etf_fof":
        variants |= {"silver etf fund of fund", "silver etf fund of funds", "silver fof"}
    return variants


def _ngrams(words: list[str], max_n: int = 5) -> list[tuple[int, int, str]]:
    return [(i, i + n, "".join(words[i : i + n])) for n in range(max_n, 0, -1) for i in range(len(words) - n + 1)]


def resolve_schemes(query: str, registry: SourceRegistry) -> tuple[list[SchemeMatch], set[int]]:
    """Return supported schemes named in the query and the word positions their names cover."""
    words = _normalise(query).split()
    compact_variants = {
        "".join(variant.split()): scheme.scheme_id for scheme in registry.schemes for variant in _variants(scheme)
    }
    matches: dict[str, SchemeMatch] = {}
    covered: set[int] = set()
    # Exact matches first (longest n-grams first), tolerant of spacing: "flexi cap" == "flexicap".
    for start, end, gram in _ngrams(words):
        if covered & set(range(start, end)):
            continue
        scheme_id = compact_variants.get(gram)
        if scheme_id:
            matches.setdefault(scheme_id, SchemeMatch(scheme_id, " ".join(words[start:end])))
            covered |= set(range(start, end))
    # Typo tolerance (e.g. "flexycap") only when exactly one scheme is a close match.
    if not matches:
        candidates: dict[str, tuple[float, int, int]] = {}
        for start, end, gram in _ngrams(words, max_n=3):
            if len(gram) < 6:
                continue
            for variant, scheme_id in compact_variants.items():
                ratio = SequenceMatcher(None, gram, variant).ratio()
                if ratio >= 0.86 and ratio > candidates.get(scheme_id, (0, 0, 0))[0]:
                    candidates[scheme_id] = (ratio, start, end)
        if len(candidates) == 1:
            scheme_id, (_, start, end) = next(iter(candidates.items()))
            matches[scheme_id] = SchemeMatch(scheme_id, " ".join(words[start:end]), fuzzy=True)
            covered |= set(range(start, end))
    return list(matches.values()), covered


def detect_facts(query: str) -> list[str]:
    facts = [fact for fact, pattern in QUERY_FACT_PATTERNS.items() if pattern.search(query)]
    # "Minimum SIP" asks for the SIP minimum specifically, not the lump-sum minimum application amount.
    if "sip" in facts and "minimum_investment" in facts:
        facts.remove("minimum_investment")
    return facts


def analyse_query(query: object, registry: SourceRegistry) -> QueryAnalysis:
    if not isinstance(query, str) or not re.search(r"[A-Za-z]{2,}", query):
        return QueryAnalysis(str(query), "invalid", reason="Please ask a question in words, e.g. the exit load of a named scheme.")
    if len(query) > MAX_QUERY_CHARS:
        return QueryAnalysis(query[:50], "invalid", reason=f"Questions are limited to {MAX_QUERY_CHARS} characters.")

    normalised = _normalise(query)
    words = normalised.split()
    schemes, covered = resolve_schemes(query, registry)
    facts = detect_facts(query)
    uncovered = " ".join(word for index, word in enumerate(words) if index not in covered)

    other_amc = next((amc for amc in OTHER_AMCS if re.search(rf"\b{re.escape(amc)}\b", normalised)), None)
    if other_amc:
        return QueryAnalysis(query, "out_of_scope", schemes, facts, reason=f"Only five ICICI Prudential schemes are supported, not {other_amc.upper()} funds.")
    unsupported = next((term for term in UNSUPPORTED_CONTAINING_ALIAS if re.search(rf"\b{re.escape(term)}\b", normalised)), None)
    unsupported = unsupported or next((term for term in UNSUPPORTED_SCHEME_WORDS if re.search(rf"\b{re.escape(term)}\b", uncovered)), None)
    if unsupported:
        return QueryAnalysis(query, "out_of_scope", schemes, facts, reason=f"'{unsupported}' is not one of the five supported schemes.")
    if not schemes:
        if facts and set(facts) <= SCHEME_INDEPENDENT_FACTS:
            return QueryAnalysis(query, "retrieve", [], facts, general=True)
        if facts and _DEFINITION.search(query) and not re.search(r"\bicici\b", normalised):
            return QueryAnalysis(query, "retrieve", [], facts, general=True, definition=True)
        # "Fund X" or "ICICI fund" names something, but not a supported scheme unambiguously.
        if re.search(r"\bfund\s+[a-z0-9]\b", normalised) and not re.search(r"\bicici\b", normalised):
            return QueryAnalysis(query, "out_of_scope", [], facts, reason="That fund is not one of the five supported schemes.")
        return QueryAnalysis(query, "clarify_scheme", [], facts, reason="Which scheme do you mean?")
    return QueryAnalysis(query, "retrieve", schemes, facts)
