from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.services.search_backend import EngineHealth, SearXNGBackend


# ---- Payload interpretation ------------------------------------------------
#
# The mapping from a SearXNG response to per-engine health is where the
# subtleties live, so it is tested directly rather than only through the route.

class TestHealthFromPayload:

    def test_counts_results_per_engine(self):
        payload = {
            "results": [
                {"url": "a", "engines": ["bing", "google cse"]},
                {"url": "b", "engines": ["bing"]},
                {"url": "c", "engines": ["yep"]},
            ],
            "unresponsive_engines": [],
        }
        health = {h.name: h for h in SearXNGBackend._health_from_payload(payload)}
        assert health["bing"].results == 2
        assert health["google cse"].results == 1
        assert health["yep"].results == 1
        assert all(h.ok for h in health.values())

    def test_sorted_by_contribution(self):
        """The operator wants to see the workhorses first."""
        payload = {
            "results": [
                {"engines": ["small"]},
                *({"engines": ["big"]} for _ in range(5)),
                *({"engines": ["mid"]} for _ in range(3)),
            ],
            "unresponsive_engines": [],
        }
        names = [h.name for h in SearXNGBackend._health_from_payload(payload)]
        assert names == ["big", "mid", "small"]

    def test_unresponsive_engines_carry_their_reason(self):
        """'CAPTCHA' vs 'too many requests' point at different fixes, so the
        reason has to survive rather than collapsing into a boolean."""
        payload = {
            "results": [],
            "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["brave", "too many requests"]],
        }
        health = {h.name: h for h in SearXNGBackend._health_from_payload(payload)}
        assert health["duckduckgo"].ok is False
        assert health["duckduckgo"].reason == "CAPTCHA"
        assert health["brave"].reason == "too many requests"

    def test_unresponsive_entry_without_reason_does_not_crash(self):
        payload = {"results": [], "unresponsive_engines": [["mystery"]]}
        health = SearXNGBackend._health_from_payload(payload)
        assert health[0].name == "mystery"
        assert health[0].reason is None

    def test_missing_keys_are_tolerated(self):
        """SearXNG omits these keys entirely in some responses."""
        assert SearXNGBackend._health_from_payload({}) == []

    def test_results_without_engine_attribution_are_skipped(self):
        payload = {"results": [{"url": "a"}, {"url": "b", "engines": None}]}
        assert SearXNGBackend._health_from_payload(payload) == []


class _RecordingClient:
    """Captures the params SearXNG would receive."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"results": []}
        self.calls: list[dict] = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append(params or {})

        class _Resp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        return _Resp(self.payload)


class TestQueryParameters:
    """SearXNG treats `categories` and `engines` as a union, not a filter.
    Sending both runs the whole category *plus* the named engine, which would
    make a probe attribute every engine's results to whichever one was named.
    """

    @pytest.mark.asyncio
    async def test_probing_selects_by_engine_only(self):
        client = _RecordingClient()
        backend = SearXNGBackend("http://searxng:8080", client)
        await backend.probe_engines("q", ["mojeek"])
        params = client.calls[0]
        assert params["engines"] == "mojeek"
        assert "categories" not in params

    @pytest.mark.asyncio
    async def test_configured_status_selects_by_category(self):
        client = _RecordingClient()
        backend = SearXNGBackend("http://searxng:8080", client)
        await backend.engine_status("q")
        params = client.calls[0]
        assert params["categories"] == "general"
        assert "engines" not in params

    @pytest.mark.asyncio
    async def test_each_probed_engine_gets_its_own_query(self):
        client = _RecordingClient()
        backend = SearXNGBackend("http://searxng:8080", client)
        await backend.probe_engines("q", ["a", "b", "c"])
        assert sorted(c["engines"] for c in client.calls) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_probe_failure_is_isolated_to_that_engine(self):
        """One unreachable engine must not sink the whole report."""
        class _Flaky(_RecordingClient):
            async def get(self, url, params=None, timeout=None):
                if params.get("engines") == "bad":
                    raise RuntimeError("connection reset")
                return await super().get(url, params=params, timeout=timeout)

        backend = SearXNGBackend("http://searxng:8080", _Flaky())
        health = {h.name: h for h in await backend.probe_engines("q", ["good", "bad"])}
        assert health["good"].ok is True
        assert health["bad"].ok is False
        assert "connection reset" in health["bad"].reason


class _ConfigClient(_RecordingClient):
    """Serves both /config and /search so engine_status can cross-reference."""

    def __init__(self, enabled, search_payload):
        super().__init__(search_payload)
        self.enabled = enabled

    async def get(self, url, params=None, timeout=None):
        if url.endswith("/config"):
            self.calls.append({"_config": True})

            class _Resp:
                def __init__(self, names):
                    self._names = names

                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "engines": [
                            {"name": n, "enabled": True, "categories": ["general"]}
                            for n in self._names
                        ]
                    }

            return _Resp(self.enabled)
        return await super().get(url, params=params, timeout=timeout)


class TestConfiguredRollCall:
    """The point of the endpoint: answer "are my configured engines working?"
    A search response alone cannot — an engine that returns nothing is absent
    from it, and so looks identical to one that was never configured.
    """

    @pytest.mark.asyncio
    async def test_configured_engine_with_no_results_is_still_listed(self):
        backend = SearXNGBackend("http://searxng:8080", _ConfigClient(
            enabled=["bing", "mojeek"],
            search_payload={"results": [{"engines": ["bing"]}], "unresponsive_engines": []},
        ))
        health = {h.name: h for h in await backend.engine_status("q")}
        assert set(health) == {"bing", "mojeek"}
        assert health["bing"].results == 1
        assert health["mojeek"].ok is True      # reachable, just quiet
        assert health["mojeek"].results == 0

    @pytest.mark.asyncio
    async def test_failure_reason_wins_over_the_roll_call_default(self):
        backend = SearXNGBackend("http://searxng:8080", _ConfigClient(
            enabled=["bing", "duckduckgo"],
            search_payload={
                "results": [],
                "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
            },
        ))
        health = {h.name: h for h in await backend.engine_status("q")}
        assert health["duckduckgo"].ok is False
        assert health["duckduckgo"].reason == "CAPTCHA"
        assert health["bing"].ok is True

    @pytest.mark.asyncio
    async def test_failures_outside_the_category_are_kept(self):
        """SearXNG reports failures across the whole request, not just the
        category asked for — dropping those would hide real problems."""
        backend = SearXNGBackend("http://searxng:8080", _ConfigClient(
            enabled=["bing"],
            search_payload={
                "results": [],
                "unresponsive_engines": [["some other engine", "timeout"]],
            },
        ))
        health = {h.name: h for h in await backend.engine_status("q")}
        assert "some other engine" in health
        assert health["some other engine"].ok is False

    @pytest.mark.asyncio
    async def test_unreachable_config_degrades_instead_of_failing(self):
        """Losing the roll call costs completeness, not the whole report."""
        class _NoConfig(_ConfigClient):
            async def get(self, url, params=None, timeout=None):
                if url.endswith("/config"):
                    raise RuntimeError("config endpoint down")
                return await _RecordingClient.get(self, url, params=params, timeout=timeout)

        backend = SearXNGBackend("http://searxng:8080", _NoConfig(
            enabled=["bing"],
            search_payload={"results": [{"engines": ["bing"]}]},
        ))
        health = await backend.engine_status("q")
        assert [h.name for h in health] == ["bing"]


# ---- Endpoint --------------------------------------------------------------

class _FakeBackend:
    def __init__(self, status=None, probe=None, raises=None):
        self._status = status or []
        self._probe = probe or []
        self._raises = raises
        self.probe_calls: list[tuple[str, list[str]]] = []
        self.status_calls: list[str] = []

    async def engine_status(self, query):
        if self._raises:
            raise self._raises
        self.status_calls.append(query)
        return self._status

    async def probe_engines(self, query, names):
        if self._raises:
            raise self._raises
        self.probe_calls.append((query, names))
        return self._probe


async def _get(app, url):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(url)


@pytest.mark.asyncio
async def test_configured_mode_splits_delivering_from_failing(app):
    app.state.search_backend = _FakeBackend(status=[
        EngineHealth(name="bing", results=10, ok=True),
        EngineHealth(name="duckduckgo", results=0, ok=False, reason="CAPTCHA"),
    ])
    resp = await _get(app, "/engines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "configured"
    assert [e["name"] for e in body["delivering"]] == ["bing"]
    assert [e["name"] for e in body["failing"]] == ["duckduckgo"]
    assert body["failing"][0]["reason"] == "CAPTCHA"


@pytest.mark.asyncio
async def test_probe_mode_forwards_the_requested_engines(app):
    backend = _FakeBackend(probe=[EngineHealth(name="mojeek", results=3, ok=True)])
    app.state.search_backend = backend
    resp = await _get(app, "/engines?probe=mojeek,%20yahoo%20,,qwant")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "probe"
    # Whitespace trimmed, empty entries dropped.
    assert backend.probe_calls[0][1] == ["mojeek", "yahoo", "qwant"]


@pytest.mark.asyncio
async def test_silent_engines_are_not_reported_as_failing(app):
    """An engine that answers with nothing is reachable. Filing it under
    'failing' would send the operator chasing a block that isn't there."""
    app.state.search_backend = _FakeBackend(probe=[
        EngineHealth(name="mojeek", results=0, ok=True),
        EngineHealth(name="yahoo", results=0, ok=False, reason="blocked"),
    ])
    body = (await _get(app, "/engines?probe=mojeek,yahoo")).json()
    assert [e["name"] for e in body["silent"]] == ["mojeek"]
    assert [e["name"] for e in body["failing"]] == ["yahoo"]
    assert body["delivering"] == []


@pytest.mark.asyncio
async def test_custom_query_is_used(app):
    backend = _FakeBackend(status=[])
    app.state.search_backend = backend
    resp = await _get(app, "/engines?q=berlin+wetter")
    assert backend.status_calls == ["berlin wetter"]
    assert resp.json()["query"] == "berlin wetter"


@pytest.mark.asyncio
async def test_probe_list_is_capped(app):
    """Each probed engine costs an upstream query; an unbounded list would
    turn one request into an outbound flood."""
    app.state.search_backend = _FakeBackend()
    resp = await _get(app, "/engines?probe=" + ",".join(f"e{i}" for i in range(26)))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_backend_without_diagnostics_reports_501(app):
    app.state.search_backend = _FakeBackend(raises=NotImplementedError())
    assert (await _get(app, "/engines")).status_code == 501


@pytest.mark.asyncio
async def test_unreachable_backend_reports_503(app):
    app.state.search_backend = _FakeBackend(raises=RuntimeError("connection refused"))
    assert (await _get(app, "/engines")).status_code == 503
