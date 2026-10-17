"""LLM adapters for answer generation (Gemini, with Sarvam AI as a backup), behind a provider-neutral protocol.

Only the backend calls the model, and it only ever sends the question plus approved evidence
snippets. Any failure (missing key, timeout, rate limit, malformed output) raises
``LlmUnavailable`` so the caller can return a safe response instead of an uncited answer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings, get_settings

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
SARVAM_ENDPOINT = "https://api.sarvam.ai/v1/chat/completions"
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
    model: str = ""  # which model produced it (audit log only)


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
        if not self.settings.gemini_configured:
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
                    draft = parse_gemini_response(body)
                    return DraftAnswer(draft.answer, draft.used_sources, draft.supported, model)
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


class SarvamModel:
    """Sarvam AI chat completions (OpenAI-style). It is asked for the same JSON contract as Gemini;
    its reply may be wrapped in a Markdown code block, which is stripped before parsing."""

    def __init__(self, settings: Settings | None = None, *, client: httpx.Client | None = None, sleep=time.sleep) -> None:
        self.settings = settings or get_settings()
        self.model_name = self.settings.sarvam_model
        self._client = client
        self._sleep = sleep

    def draft(self, system_prompt: str, user_prompt: str) -> DraftAnswer:
        if not self.settings.sarvam_configured:
            raise LlmUnavailable("model_not_configured")
        assert self.settings.sarvam_api_key is not None
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt + "\nRespond with the JSON object only."},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            # Without reasoning it mislabelled facts (e.g. "Investor" for "Other"); with the default
            # 2048-token budget it spent everything on reasoning and returned no content.
            "reasoning_effort": self.settings.sarvam_reasoning_effort,
            "max_tokens": self.settings.sarvam_max_tokens,
            "response_format": {"type": "json_object"},
        }
        client = self._client or httpx.Client(timeout=self.settings.sarvam_timeout_seconds)
        last_error = "no_model_attempted"
        try:
            for delay in (0.0, *RETRY_DELAYS_SECONDS[:1]):
                if delay:
                    self._sleep(delay)
                try:
                    response = client.post(SARVAM_ENDPOINT, headers={"api-subscription-key": self.settings.sarvam_api_key.get_secret_value()}, json=payload)
                except httpx.HTTPError:
                    last_error = "transport_error"
                    continue
                if response.status_code == 200:
                    try:
                        choice = response.json()["choices"][0]
                        text = choice["message"]["content"] or ""
                    except (KeyError, IndexError, TypeError, ValueError) as error:
                        raise LlmUnavailable("malformed_model_output") from error
                    if choice.get("finish_reason") == "length":
                        raise LlmUnavailable("sarvam_truncated_output")
                    draft = parse_answer_json(text)
                    return DraftAnswer(draft.answer, draft.used_sources, draft.supported, f"sarvam:{self.model_name}")
                last_error = "rate_limited" if response.status_code == 429 else f"http_{response.status_code}"
                if response.status_code not in RETRYABLE_STATUS:
                    break
        finally:
            if self._client is None:
                client.close()
        raise LlmUnavailable(f"sarvam_{last_error}")


class FallbackModel:
    """Tries each provider in order; only if all fail is the request reported as unavailable."""

    def __init__(self, models: list[AnswerModel]) -> None:
        self.models = models
        self.model_name = " -> ".join(m.model_name for m in models) or "none"

    def draft(self, system_prompt: str, user_prompt: str) -> DraftAnswer:
        errors = []
        for model in self.models:
            try:
                return model.draft(system_prompt, user_prompt)
            except LlmUnavailable as error:
                errors.append(str(error))
        raise LlmUnavailable("all_providers_failed:" + ",".join(errors) if errors else "model_not_configured")


def build_answer_model(settings: Settings | None = None) -> AnswerModel:
    settings = settings or get_settings()
    models: list[AnswerModel] = []
    if settings.gemini_configured:
        models.append(GeminiModel(settings))
    if settings.sarvam_configured:
        models.append(SarvamModel(settings))
    return FallbackModel(models)


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_answer_json(text: str) -> DraftAnswer:
    """Parse the {"answer", "used_sources", "supported"} contract from model text."""
    cleaned = _CODE_FENCE.sub("", text.strip())
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        cleaned = match.group(0) if match else cleaned
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise LlmUnavailable("malformed_model_output") from error
    return _validated_draft(data)


def parse_gemini_response(body: dict) -> DraftAnswer:
    try:
        parts = body["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    except (KeyError, IndexError, TypeError) as error:
        raise LlmUnavailable("malformed_model_output") from error
    return parse_answer_json(text)


def _validated_draft(data: object) -> DraftAnswer:
    try:
        answer = data["answer"]  # type: ignore[index]
        sources = data["used_sources"]  # type: ignore[index]
        supported = data["supported"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as error:
        raise LlmUnavailable("malformed_model_output") from error
    if not isinstance(answer, str) or not isinstance(supported, bool) or not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise LlmUnavailable("malformed_model_output")
    return DraftAnswer(answer.strip(), tuple(sources), supported)
