"""Streamlit front end for hosting on Streamlit Community Cloud.

It calls the same ChatService as the FastAPI app (guardrails, retrieval gate, validation, and
registry citations are unchanged); only the page is different. Run locally with:

    streamlit run streamlit_app.py
"""

from __future__ import annotations

import os
import re
import time

import streamlit as st

# Streamlit Cloud provides keys as st.secrets; the app reads configuration from the environment.
try:
    for _name in ("GEMINI_API_KEY", "SARVAM_API_KEY"):
        if _name in st.secrets and not os.environ.get(_name):
            os.environ[_name] = str(st.secrets[_name])
except Exception:  # noqa: BLE001 - no secrets file locally is fine (.env is used instead)
    pass

from app.answer import ChatService  # noqa: E402  (after secrets are exported)
from app.config import get_settings  # noqa: E402
from app.query import ConversationContext  # noqa: E402
from app.source_policy import validate_source_url  # noqa: E402

DISCLAIMER = "Facts-only. No investment advice."
EXAMPLES = (
    "What is the exit load of ICICI Prudential Flexicap Fund?",
    "What is the expense ratio of ICICI Prudential Large Cap Fund?",
    "Tell me about the Silver ETF FoF",
)
MAX_PER_MINUTE = 20
# response_type -> (Streamlit box, label); refusals and failures look different from facts.
STATES = {
    "fact": ("success", "Answer"),
    "navigation": ("info", "Official page"),
    "clarify": ("info", "Which scheme?"),
    "out_of_scope": ("info", "Outside supported schemes"),
    "unverified": ("warning", "Couldn't verify"),
    "refusal_advice": ("warning", "No investment advice"),
    "refusal_performance": ("warning", "No return calculations"),
    "refusal_pii": ("warning", "Personal information not accepted"),
    "refusal_policy": ("warning", "Not available"),
    "invalid": ("warning", "Please rephrase"),
    "unavailable": ("error", "Temporarily unavailable"),
    "rate_limited": ("error", "Slow down"),
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~$<])")


def md_escape(text: str) -> str:
    """Show model/document text literally: no links, formatting, or HTML can be injected."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


@st.cache_resource(show_spinner="Loading the official documents and search models (first visit takes about a minute)…")
def get_service() -> ChatService:
    get_settings().ensure_storage_directories()
    return ChatService()


def render(response: dict) -> None:
    kind = response.get("response_type", "unverified")
    source = response.get("source")
    # Never present text as a factual answer without an official citation and date.
    if kind == "fact" and not (source and validate_source_url(source["url"]).approved and response.get("last_updated_from_sources")):
        kind, response = "unverified", {**response, "answer": "I couldn't verify that from the approved official sources."}
    box, label = STATES.get(kind, STATES["unverified"])
    lines = [f"**{label}**", "", md_escape(response.get("answer", ""))]
    sources = [s for s in [source, *(response.get("additional_sources") or [])] if s and validate_source_url(s["url"]).approved]
    for index, s in enumerate(sources):
        prefix = ("Source" if index == 0 else "Also") if kind == "fact" else "Official link"
        page = f" (page {s['page']})" if s.get("page") else ""
        lines.append(f"{prefix}: [{md_escape(s['title'] + page)}]({s['url']})")
    if kind == "fact":
        lines.append(f"Last updated from sources: {response['last_updated_from_sources']}")
    getattr(st, box)("  \n".join(lines))
    st.caption(DISCLAIMER)


def allowed() -> bool:
    now = time.monotonic()
    hits = [t for t in st.session_state.get("hits", []) if now - t < 60]
    st.session_state.hits = hits + [now]
    return len(hits) < MAX_PER_MINUTE


def ask(question: str) -> None:
    st.session_state.messages.append(("user", question))
    if not allowed():
        response = {"answer": "Too many questions in a short time. Please wait a minute and try again.", "response_type": "rate_limited"}
    else:
        context = st.session_state.get("context")
        with st.spinner("Checking the official documents…"):
            result = get_service().answer(question, ConversationContext(tuple(context["scheme_ids"]), tuple(context["facts"])) if context else None)
        response = result.to_dict()
        if response.get("context"):
            st.session_state.context = response["context"]  # session memory only, for follow-ups
    st.session_state.messages.append(("assistant", response))


st.set_page_config(page_title="MF Facts Assistant", page_icon="📊", layout="centered")
st.session_state.setdefault("messages", [])

st.caption("ICICI Prudential Mutual Fund · Official documents only")
st.title("Mutual Fund Facts Assistant")
st.markdown(
    "Hi! Ask a factual question, such as an expense ratio, exit load, minimum SIP, benchmark or riskometer, about ICICI Prudential "
    "**Silver ETF FoF**, **Large Cap**, **Large & Mid Cap**, **Flexicap** or **Smallcap** Fund. Every answer links to its official source."
)
st.info(f"ⓘ {DISCLAIMER}")

if not st.session_state.messages:
    st.markdown("**Try an example**")
    for example in EXAMPLES:
        if st.button(example, use_container_width=True):
            ask(example)
            st.rerun()

for role, payload in st.session_state.messages:
    with st.chat_message(role):
        if role == "user":
            st.markdown(md_escape(payload))
        else:
            render(payload)

question = st.chat_input("e.g. What is the minimum SIP for ICICI Prudential Smallcap Fund?", max_chars=500)
if question:
    ask(question)
    st.rerun()

st.caption("Please don't enter PAN, Aadhaar, account numbers, OTPs, email or phone.")
st.divider()
st.caption(
    "Answers come only from official ICICI Prudential AMC, SEBI and AMFI documents, and are limited to three sentences. "
    "The assistant can't access accounts, recommend funds, or calculate returns. Independent portfolio project, not affiliated with or "
    "endorsed by ICICI Prudential AMC or SEBI. [Source code](https://github.com/aryansinghdev01/icici-mf-facts-assistant)"
)
