import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import app.answer
from app.answer import ChatResponse, Citation

CITATION = Citation(
    "ICICI Prudential Flexicap Fund — Factsheet", "https://digitalfactsheet.icicipruamc.com/fact/pdf/icici-prudential-flexicap-fund.pdf",
    "2026-07-31", "document", "FACTSHEET", "icici_flexicap_factsheet", 1,
)


class FakeService:
    calls: list[tuple[str, object]] = []

    def answer(self, question, context=None):
        FakeService.calls.append((question, context))
        if "evil" in question:
            return ChatResponse("Click [here](https://groww.in) now", "fact", source=CITATION, last_updated_from_sources="2026-07-31",
                                context={"scheme_ids": ["icici_flexicap"], "facts": ["exit_load"]})
        return ChatResponse("The exit load is 1% within one month.", "fact", source=CITATION, last_updated_from_sources="2026-07-31",
                            context={"scheme_ids": ["icici_flexicap"], "facts": ["exit_load"]})


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(app.answer, "ChatService", FakeService)
    st.cache_resource.clear()
    FakeService.calls = []
    test = AppTest.from_file("../streamlit_app.py", default_timeout=30)
    test.run()
    return test


def test_page_shows_disclaimer_and_three_examples(page) -> None:
    assert any("Facts-only. No investment advice." in info.value for info in page.info)
    assert len(page.button) == 3
    assert not page.exception


def test_answer_shows_source_link_and_date_and_keeps_follow_up_context(page) -> None:
    page.chat_input[0].set_value("What is the exit load of ICICI Flexicap?").run()
    answer = page.success[0].value
    assert "1% within one month" in answer.replace("\\", "")
    assert "digitalfactsheet.icicipruamc.com" in answer and "Last updated from sources: 2026-07-31" in answer

    page.chat_input[0].set_value("riskometer").run()
    question, context = FakeService.calls[-1]
    assert question == "riskometer" and context.scheme_ids == ("icici_flexicap",)


def test_model_text_cannot_inject_links(page) -> None:
    page.chat_input[0].set_value("evil question").run()
    answer = page.success[0].value
    assert "[here](https://groww.in)" not in answer
    assert "\\[here\\]" in answer
