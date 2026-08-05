from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import AppConfig
from app.services.cache import CacheService
from app.services.extractor import ContentExtractor, ExtractionResult
from app.services.reranker import RerankerService
from app.services.search_backend import (
    BackendSearchResponse,
    RawImageResult,
    RawSearchResult,
    SearchBackend,
)


# ---- Fake search backend ----

class FakeSearchBackend(SearchBackend):
    def __init__(self, fail: bool = False, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.last_kwargs: dict = {}
        self.call_count = 0

    async def search(self, query: str, **kwargs) -> BackendSearchResponse:
        self.last_kwargs = {"query": query, **kwargs}
        self.call_count += 1
        if self.fail:
            raise RuntimeError("Backend unavailable")
        if self.empty:
            return BackendSearchResponse(results=[], images=[])
        results = [
            RawSearchResult(title=f"Result {i}", url=f"https://example.com/{i}", snippet=f"Snippet {i}", score=round(1.0 - i * 0.1, 2))
            for i in range(kwargs.get("max_results", 3))
        ]
        images = []
        if kwargs.get("include_images"):
            images = [
                RawImageResult(url="https://example.com/img1.jpg", description="Test image"),
            ]
        return BackendSearchResponse(results=results, images=images)


# ---- Fake extractor ----

class FakeExtractor:
    async def extract_url(self, url: str, *, output_format: str = "markdown") -> ExtractionResult:
        if "fail" in url:
            return ExtractionResult(url=url, error="Extraction failed")
        return ExtractionResult(url=url, content=f"# Extracted content from {url}\n\nFull text here.")

    async def extract_urls(self, urls: list[str], *, output_format: str = "markdown") -> list[ExtractionResult]:
        return await asyncio.gather(*[self.extract_url(url, output_format=output_format) for url in urls])


# ---- Fake cache ----

class FakeCache:
    def __init__(self) -> None:
        self._search: dict[str, dict] = {}
        self._extract: dict[str, str] = {}
        self.enabled = True

    async def get_search(self, query: str, params_hash: str) -> dict | None:
        return self._search.get(f"{query}|{params_hash}")

    async def set_search(self, query: str, params_hash: str, data: dict) -> None:
        self._search[f"{query}|{params_hash}"] = data

    async def get_extract(self, url: str) -> str | None:
        return self._extract.get(url)

    async def set_extract(self, url: str, content: str) -> None:
        self._extract[url] = content

    async def get_extract_batch(self, urls: list[str]) -> dict[str, str | None]:
        return {url: self._extract.get(url) for url in urls}

    async def set_extract_batch(self, pairs: list[tuple[str, str]]) -> None:
        for url, content in pairs:
            self._extract[url] = content

    async def get_answer(self, query: str, params_hash: str) -> str | None:
        return self._search.get(f"answer|{query}|{params_hash}")

    async def set_answer(self, query: str, params_hash: str, answer: str) -> None:
        self._search[f"answer|{query}|{params_hash}"] = answer

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass


# ---- Fake LLM service ----

class FakeLLMService:
    """Fake LLM for testing. Returns a canned answer or simulates failure."""

    def __init__(self, answer: str = "This is a test answer based on search results [1].", fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self._client = True  # looks "enabled"
        self.call_count = 0

    async def generate_answer(self, query: str, results: list) -> str | None:
        self.call_count += 1
        if self.fail:
            return None
        return self.answer

    async def generate_answer_stream(self, query: str, results: list):
        self.call_count += 1
        if self.fail:
            return
        for word in self.answer.split():
            yield word + " "

    async def close(self) -> None:
        pass


class DisabledLLMService:
    """LLM service that is disabled (no client initialized)."""

    def __init__(self) -> None:
        self._client = None

    async def generate_answer(self, query: str, results: list) -> str | None:
        return None

    async def generate_answer_stream(self, query: str, results: list):
        return
        yield  # make it an async generator

    async def close(self) -> None:
        pass


# ---- Fake indexer ----

class FakeIndexer:
    """No-op indexer for tests. Records calls for assertions."""

    def __init__(self) -> None:
        self.enabled = False   # keeps search_local behavior predictable if used
        self.indexed: list[dict] = []
        self._meta: dict[str, dict] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_meta(self, url: str):
        return self._meta.get(url)

    async def index_document(self, *, url, title, content, etag=None, last_modified=None) -> None:
        self.indexed.append({"url": url, "title": title, "content": content})

    async def search(self, query: str, limit=None):
        return []


# ---- Reset sse_starlette AppStatus across tests ----

@pytest_asyncio.fixture(autouse=True)
async def _reset_sse_app_status():
    """Reset sse_starlette's module-level Event to the current event loop."""
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit_event = asyncio.Event()


# ---- App fixture ----

@pytest_asyncio.fixture
async def app():
    """Create a test app with faked services."""
    # Import here to avoid module-level config loading issues
    from app.main import app as _app

    _app.state.search_backend = FakeSearchBackend()
    _app.state.extractor = FakeExtractor()
    _app.state.cache = FakeCache()
    _app.state.reranker = RerankerService()  # disabled by default
    _app.state.llm = FakeLLMService()
    _app.state.indexer = FakeIndexer()

    yield _app


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def app_with_failing_backend(app):
    """App with a search backend that always fails."""
    app.state.search_backend = FakeSearchBackend(fail=True)
    return app


@pytest_asyncio.fixture
async def client_failing_backend(app_with_failing_backend) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app_with_failing_backend)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
