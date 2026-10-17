"""LLM adapter for answer generation (Gemini REST API), behind a small provider-neutral protocol.

Only the backend calls the model, and it only ever sends the question plus approved evidence
snippets. Any failure (missing key, timeout, rate limit, malformed output) raises
``LlmUnavailable`` so the caller can return a safe response instead of an uncited answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings, get_settings

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Transient server states worth retrying (overloaded / rate limited / internal error).
RETRYABLE_STATUS = frozenset({429, 500, 503})
RETRY_DELAYS_SECONDS = (2.0, 5.0)

# Structured output the model must return; parsed and validated before anything is shown.
ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "used_sources": {"type": "ARRAY", "items": {"type": "STRING"}},
        "supported": {"type": "BOOLEAN"},
    },
    "required": ["answer", "used_sources", "supported"],
}


class LlmUnavailable(Exception):
    """The model could not produce a usable structured reply."""


@dataclass(frozen=True)
class DraftAnswer:
    answer: str
    used_sources: tuple[str, ...]
    supported: bool


class AnswerModel(Protocol):
    model_name: str

    def draft(self, system_prompt: str, user_prompt: str) -> DraftAnswer: ...


class GeminiModel:
    def __init__(self, settings: Settings | None = None, *, client: httpx.Client | None = None, sleep=time.sleep) -> None:
        self.settings = settings or get_settings()
        self.model_name = self.settings.gemini_model
        fallbacks = [m.strip() for m in self.settings.gemini_fallback_models.split(",") if m.strip()]
        self.models = [self.model_name, *(m for m in fallbacks if m != self.model_name)]
        self._client = client
        self._sleep = sleep

    def draft(self, system_prompt: str, user_prompt: str) -> DraftAnswer:
        if not self.settings.llm_configured:
            raise LlmUnavailable("model_not_configured")
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": ANSWER_SCHEMA, "maxOutputTokens": 2048},
        }
        client = self._client or httpx.Client(timeout=self.settings.llm_timeout_seconds)
        last_error = "no_model_attempted"
        try:
            # The primary model gets retries with backoff; fallbacks get one attempt each.
            attempts = [(self.models[0], 0.0), *((self.models[0], d) for d in RETRY_DELAYS_SECONDS), *((m, 0.0) for m in self.models[1:])]
            exhausted: set[str] = set()
            for model, delay in attempts:
                if model in exhausted:
                    continue
                if delay:
                    self._sleep(delay)
                status, body = self._post(client, model, payload)
                if status == 200:
                    return parse_gemini_response(body)
                last_error = "rate_limited" if status == 429 else f"http_{status}"
                if status == 429:
                    exhausted.add(model)  # quota used up: retrying this model cannot help, move on
                elif status not in RETRYABLE_STATUS and status != 0:
                    break  # e.g. 400/403: retrying cannot help
        finally:
            if self._client is None:
                client.close()
        raise LlmUnavailable(last_error)

    def _post(self, client: httpx.Client, model: str, payload: dict) -> tuple[int, dict]:
        assert self.settings.gemini_api_key is not None
        try:
            response = client.post(
                GEMINI_ENDPOINT.format(model=model),
                headers={"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()},
                json=payload,
            )
        except httpx.HTTPError:
            return 0, {}  # transport error/timeout: treated as retryable
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}


def parse_gemini_response(body: dict) -> DraftAnswer:
    try:
        parts = body["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        data = json.loads(text)
        answer = data["answer"]
        sources = data["used_sources"]
        supported = data["supported"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise LlmUnavailable("malformed_model_output") from error
    if not isinstance(answer, str) or not isinstance(supported, bool) or not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise LlmUnavailable("malformed_model_output")
    return DraftAnswer(answer.strip(), tuple(sources), supported)
