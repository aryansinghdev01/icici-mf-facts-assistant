"""HTTP API: health, approved sources, and the facts-only chat endpoint."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from threading import Lock

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictStr

from app.config import Settings, get_settings
from app.index import check_index
from app.registry import load_registry
from app.source_policy import APPROVED_DOMAINS, BLOCKED_DOMAINS


STATIC_DIR = Path(__file__).parent / "static"
# Same-origin scripts/styles only: the UI renders server text as plain text, and this blocks
# injected scripts as a second layer (UI-06). Links open official sites in a new tab.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class HealthResponse(BaseModel):
    status: str
    environment: str
    configuration: dict[str, object]


class ChatRequest(BaseModel):
    # Only a question is accepted: no identity, account, or upload fields (API-03).
    model_config = ConfigDict(extra="forbid")
    question: StrictStr


@lru_cache
def get_chat_service():
    """Loaded once on first use: the index and models are large."""
    from app.answer import ChatService

    return ChatService()


class RateLimiter:
    """Small in-memory sliding-window limit per client (API-05)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[client]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        get_settings().ensure_storage_directories()
        yield

    app = FastAPI(
        title="ICICI Prudential Facts-Only RAG Assistant",
        version="0.1.0",
        lifespan=lifespan,
    )
    limiter = RateLimiter(get_settings().chat_rate_limit_per_minute)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/chat"):
            response.headers["Cache-Control"] = "no-store"  # answers are never cached by browsers
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
        paths = settings.storage_paths
        paths_ready = all(path.is_dir() and path.exists() for path in paths.values())
        index = check_index(settings)
        return HealthResponse(
            # Semantic search is blocked until a stale or incompatible index is rebuilt (P2-11).
            status="healthy" if paths_ready and index.semantic_available else "degraded",
            environment=settings.app_env,
            configuration={
                "llm": {"provider": "Gemini", "model": settings.gemini_model, "configured": settings.llm_configured},
                "embeddings": {"provider": "BGE", "model": settings.bge_model},
                "reranker": {"model": settings.reranker_model, "min_score": settings.rerank_min_score},
                "index": {"semantic_search": index.semantic_available, "reason": index.reason, "chunks": index.chunk_count},
                "storage": {name: path.is_dir() and path.exists() for name, path in paths.items()},
                "source_policy": {"approved_domains": sorted(APPROVED_DOMAINS), "blocked_domains": sorted(BLOCKED_DOMAINS)},
            },
        )

    @app.get("/api/sources", tags=["sources"])
    def sources(settings: Settings = Depends(get_settings)) -> dict[str, object]:
        """Approved source metadata for transparency; raw documents are never exposed."""
        registry = load_registry(settings.resolved_path(settings.source_registry_path))
        return {
            "sources": [
                {
                    "source_id": s.source_id, "title": s.title or s.scheme_name, "scheme_id": s.scheme_id, "document_type": s.document_type,
                    "official_url": str(s.official_url), "document_date": s.published_date.isoformat() if s.published_date else None,
                }
                for s in registry.sources
                if s.status == "approved"
            ],
            "service_links": [{"title": link.title, "url": str(link.url), "topics": list(link.topics)} for link in registry.service_links],
        }

    @app.post("/api/chat", tags=["chat"])
    def chat(body: ChatRequest, request: Request, service=Depends(get_chat_service)):
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return JSONResponse(
                status_code=429,
                content={"answer": "Too many questions in a short time. Please wait a minute and try again.", "response_type": "rate_limited",
                         "source": None, "additional_sources": [], "last_updated_from_sources": None, "disclaimer": "Facts-only. No investment advice."},
            )
        return service.answer(body.question).to_dict()

    return app


app = create_app()
