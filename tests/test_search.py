from __future__ import annotations

import pytest


# ---- Basic search ----

@pytest.mark.asyncio
async def test_search_basic(client):
    resp = await client.post("/search", json={"query": "python fastapi", "max_results": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "python fastapi"
    assert len(data["results"]) == 3
    assert "response_time" in data
    assert data["response_time"] >= 0


@pytest.mark.asyncio
async def test_search_result_fields(client):
    resp = await client.post("/search", json={"query": "test", "max_results": 1})
    result = resp.json()["results"][0]
    assert "title" in result
    assert "url" in result
    assert "content" in result
    assert "score" in result
    assert isinstance(result["score"], float)
    assert result["raw_content"] is None  # basic depth


@pytest.mark.asyncio
async def test_search_default_max_results(client):
    resp = await client.post("/search", json={"query": "test"})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 5  # default


@pytest.mark.asyncio
async def test_search_max_results_range(client):
    resp = await client.post("/search", json={"query": "test", "max_results": 1})
    assert len(resp.json()["results"]) == 1

    resp = await client.post("/search", json={"query": "test", "max_results": 20})
    assert len(resp.json()["results"]) == 20


# ---- Search with images ----

@pytest.mark.asyncio
async def test_search_with_images(client):
    resp = await client.post("/search", json={"query": "cats", "include_images": True})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["images"]) > 0
    assert "url" in data["images"][0]
    assert "description" in data["images"][0]


@pytest.mark.asyncio
async def test_search_without_images(client):
    resp = await client.post("/search", json={"query": "test", "include_images": False})
    assert resp.json()["images"] == []


# ---- Search with advanced depth ----

@pytest.mark.asyncio
async def test_search_advanced_depth(client):
    resp = await client.post("/search", json={
        "query": "test", "search_depth": "advanced", "max_results": 2,
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    for r in results:
        assert r["raw_content"] is not None
        assert "Extracted content" in r["raw_content"]


@pytest.mark.asyncio
async def test_search_include_raw_content(client):
    resp = await client.post("/search", json={
        "query": "test", "include_raw_content": True, "max_results": 1,
    })
    assert resp.json()["results"][0]["raw_content"] is not None


# ---- Search with filters ----

@pytest.mark.asyncio
async def test_search_topic_news(client, app):
    resp = await client.post("/search", json={"query": "ai", "topic": "news", "max_results": 1})
    assert resp.status_code == 200
    backend = app.state.search_backend
    assert backend.last_kwargs["topic"] == "news"


@pytest.mark.asyncio
async def test_search_time_range(client, app):
    resp = await client.post("/search", json={"query": "test", "time_range": "week", "max_results": 1})
    assert resp.status_code == 200
    assert app.state.search_backend.last_kwargs["time_range"] == "week"


@pytest.mark.asyncio
async def test_search_include_domains(client, app):
    resp = await client.post("/search", json={
        "query": "test", "include_domains": ["example.com"], "max_results": 1,
    })
    assert resp.status_code == 200
    assert app.state.search_backend.last_kwargs["include_domains"] == ["example.com"]


@pytest.mark.asyncio
async def test_search_exclude_domains(client, app):
    resp = await client.post("/search", json={
        "query": "test", "exclude_domains": ["spam.com"], "max_results": 1,
    })
    assert resp.status_code == 200
    assert app.state.search_backend.last_kwargs["exclude_domains"] == ["spam.com"]


# ---- Search caching ----

@pytest.mark.asyncio
async def test_search_cache_hit(client):
    # First request populates cache
    resp1 = await client.post("/search", json={"query": "cache test", "max_results": 2})
    assert resp1.status_code == 200

    # Second identical request should hit cache (faster)
    resp2 = await client.post("/search", json={"query": "cache test", "max_results": 2})
    assert resp2.status_code == 200
    assert resp2.json()["results"] == resp1.json()["results"]


@pytest.mark.asyncio
async def test_empty_results_are_not_cached(app):
    """A zero-result response (SearXNG engines rate-limited, etc.) must not
    pin the cache for the full TTL — otherwise every follow-up query with
    matching params keeps getting the empty response until the TTL expires.
    """
    from tests.conftest import FakeSearchBackend
    from httpx import ASGITransport, AsyncClient

    backend = FakeSearchBackend(empty=True)
    app.state.search_backend = backend
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp1 = await ac.post("/search", json={"query": "no hits", "max_results": 3})
        resp2 = await ac.post("/search", json={"query": "no hits", "max_results": 3})

    assert resp1.status_code == 200
    assert resp1.json()["results"] == []
    assert resp2.status_code == 200
    assert resp2.json()["results"] == []
    # Both requests must have hit the real backend, not the cache.
    assert backend.call_count == 2, "empty response should not have been cached"


# ---- Search validation errors ----

@pytest.mark.asyncio
async def test_search_missing_query(client):
    resp = await client.post("/search", json={"max_results": 5})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_max_results_too_high(client):
    resp = await client.post("/search", json={"query": "test", "max_results": 50})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_max_results_too_low(client):
    resp = await client.post("/search", json={"query": "test", "max_results": 0})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_invalid_depth(client):
    resp = await client.post("/search", json={"query": "test", "search_depth": "ultra"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_invalid_topic(client):
    resp = await client.post("/search", json={"query": "test", "topic": "finance"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_invalid_time_range(client):
    resp = await client.post("/search", json={"query": "test", "time_range": "century"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_empty_body(client):
    resp = await client.post("/search", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_no_body(client):
    resp = await client.post("/search")
    assert resp.status_code == 422


# ---- Graceful degradation ----

@pytest.mark.asyncio
async def test_search_backend_failure_returns_503(client_failing_backend):
    resp = await client_failing_backend.post("/search", json={"query": "test", "max_results": 1})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_search_backend_failure_serves_stale_cache(app_with_failing_backend):
    """When backend fails but stale cache exists, serve it."""
    from httpx import ASGITransport, AsyncClient
    from tests.conftest import FakeSearchBackend

    # First, populate cache with working backend
    app_with_failing_backend.state.search_backend = FakeSearchBackend(fail=False)
    transport = ASGITransport(app=app_with_failing_backend)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/search", json={"query": "stale test", "max_results": 2})
        assert resp.status_code == 200
        original_results = resp.json()["results"]

    # Now break backend and expect stale cache
    app_with_failing_backend.state.search_backend = FakeSearchBackend(fail=True)
    transport = ASGITransport(app=app_with_failing_backend)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/search", json={"query": "stale test", "max_results": 2})
        assert resp.status_code == 200
        assert resp.json()["results"] == original_results
