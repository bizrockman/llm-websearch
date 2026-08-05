from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import httpx
import structlog
from circuitbreaker import CircuitBreakerError

from app.config import AppConfig
from app.services.resilience import retry_async, search_circuit

logger = structlog.get_logger(__name__)


@dataclass
class RawSearchResult:
    title: str
    url: str
    snippet: str
    score: float = 0.0
    thumbnail: Optional[str] = None


@dataclass
class RawImageResult:
    url: str
    description: str = ""


@dataclass
class RawImageHit:
    """Richer image result for the dedicated /search/images endpoint.
    `url` is the page hosting the image, `img_src` is the actual image URL.
    """
    title: str
    url: str
    img_src: str
    thumbnail_src: Optional[str] = None
    source: Optional[str] = None


@dataclass
class RawVideoHit:
    """Video result for the dedicated /search/videos endpoint."""
    title: str
    url: str
    iframe_src: Optional[str] = None
    img_src: Optional[str] = None
    duration: Optional[str] = None
    author: Optional[str] = None
    source: Optional[str] = None


@dataclass
class BackendSearchResponse:
    results: list[RawSearchResult] = field(default_factory=list)
    images: list[RawImageResult] = field(default_factory=list)


@dataclass
class EngineHealth:
    """Per-engine outcome of a diagnostic query."""
    name: str
    results: int = 0
    ok: bool = False
    reason: Optional[str] = None


class SearchBackend(ABC):
    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        topic: str = "general",
        time_range: Optional[str] = None,
        include_domains: Optional[list[str]] = None,
        exclude_domains: Optional[list[str]] = None,
        include_images: bool = False,
    ) -> BackendSearchResponse:
        ...

    async def image_search_rich(
        self, query: str, max_results: int = 10, time_range: Optional[str] = None,
    ) -> list[RawImageHit]:
        """Optional: dedicated image search with rich per-result fields."""
        raise NotImplementedError(
            "This backend does not implement image_search_rich"
        )

    async def video_search_rich(
        self, query: str, max_results: int = 10, time_range: Optional[str] = None,
    ) -> list[RawVideoHit]:
        """Optional: dedicated video search with rich per-result fields."""
        raise NotImplementedError(
            "This backend does not implement video_search_rich"
        )

    async def engine_status(self, query: str) -> list[EngineHealth]:
        """Optional: report how each *configured* engine fared on one query."""
        raise NotImplementedError(
            "This backend does not implement engine_status"
        )

    async def probe_engines(self, query: str, names: list[str]) -> list[EngineHealth]:
        """Optional: query named engines individually, including disabled ones."""
        raise NotImplementedError(
            "This backend does not implement probe_engines"
        )


class SearXNGBackend(SearchBackend):
    def __init__(self, base_url: str, http_client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = http_client

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        topic: str = "general",
        time_range: Optional[str] = None,
        include_domains: Optional[list[str]] = None,
        exclude_domains: Optional[list[str]] = None,
        include_images: bool = False,
    ) -> BackendSearchResponse:
        effective_query = query
        if include_domains:
            effective_query += " " + " ".join(f"site:{d}" for d in include_domains)
        if exclude_domains:
            effective_query += " " + " ".join(f"-site:{d}" for d in exclude_domains)

        categories = "news" if topic == "news" else "general"
        searxng_time_range = time_range if time_range in ("day", "week", "month", "year") else None

        tasks = [self._web_search_with_retry(effective_query, max_results, categories, searxng_time_range)]
        if include_images:
            tasks.append(self._image_search_with_retry(query, max_results=5))

        task_results = await asyncio.gather(*tasks, return_exceptions=True)

        web_results: list[RawSearchResult] = []
        images: list[RawImageResult] = []

        if not isinstance(task_results[0], BaseException):
            web_results = task_results[0]
        else:
            logger.error("web_search_failed", error=str(task_results[0]))
            raise task_results[0]

        if include_images and len(task_results) > 1:
            if not isinstance(task_results[1], BaseException):
                images = task_results[1]
            else:
                logger.warning("image_search_failed", error=str(task_results[1]))

        return BackendSearchResponse(results=web_results, images=images)

    async def _web_search_with_retry(
        self, query: str, max_results: int, categories: str, time_range: Optional[str]
    ) -> list[RawSearchResult]:
        @search_circuit
        async def _do():
            return await retry_async(
                lambda: self._web_search(query, max_results, categories, time_range)
            )
        return await _do()

    async def _image_search_with_retry(self, query: str, max_results: int = 5) -> list[RawImageResult]:
        @search_circuit
        async def _do():
            return await retry_async(lambda: self._image_search(query, max_results))
        return await _do()

    async def _web_search(
        self, query: str, max_results: int, categories: str, time_range: Optional[str]
    ) -> list[RawSearchResult]:
        params: dict = {"q": query, "format": "json", "categories": categories}
        if time_range:
            params["time_range"] = time_range

        resp = await self.client.get(f"{self.base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        results: list[RawSearchResult] = []
        for item in data.get("results", [])[:max_results]:
            raw_score = item.get("score", 0)
            score = min(1.0, raw_score) if raw_score <= 1.0 else min(1.0, raw_score / 10.0)
            # SearXNG engines surface thumbnails under different keys; news
            # engines (Bing-news etc.) tend to use `thumbnail`, image-bearing
            # results sometimes use `thumbnail_src` or `img_src`. Take the
            # first one that exists.
            thumbnail = (
                item.get("thumbnail")
                or item.get("thumbnail_src")
                or item.get("img_src")
            )
            results.append(
                RawSearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    score=round(score, 4),
                    thumbnail=thumbnail,
                )
            )
        return results

    async def _image_search(self, query: str, max_results: int = 5) -> list[RawImageResult]:
        params = {"q": query, "format": "json", "categories": "images"}
        resp = await self.client.get(f"{self.base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        images: list[RawImageResult] = []
        for item in data.get("results", [])[:max_results]:
            img_url = item.get("img_src") or item.get("url", "")
            if img_url:
                images.append(RawImageResult(url=img_url, description=item.get("title", "")))
        return images

    async def image_search_rich(
        self, query: str, max_results: int = 10, time_range: Optional[str] = None,
    ) -> list[RawImageHit]:
        """Image search returning rich per-result fields. Powers /search/images."""
        params: dict = {"q": query, "format": "json", "categories": "images"}
        if time_range in ("day", "week", "month", "year"):
            params["time_range"] = time_range
        resp = await self.client.get(f"{self.base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        hits: list[RawImageHit] = []
        for item in data.get("results", [])[:max_results]:
            img_src = item.get("img_src") or item.get("thumbnail_src") or ""
            if not img_src:
                continue
            hits.append(RawImageHit(
                title=item.get("title", ""),
                url=item.get("url", "") or img_src,
                img_src=img_src,
                thumbnail_src=item.get("thumbnail_src"),
                source=item.get("engine"),
            ))
        return hits

    async def video_search_rich(
        self, query: str, max_results: int = 10, time_range: Optional[str] = None,
    ) -> list[RawVideoHit]:
        """Video search returning rich per-result fields. Powers /search/videos."""
        params: dict = {"q": query, "format": "json", "categories": "videos"}
        if time_range in ("day", "week", "month", "year"):
            params["time_range"] = time_range
        resp = await self.client.get(f"{self.base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        hits: list[RawVideoHit] = []
        for item in data.get("results", [])[:max_results]:
            url = item.get("url", "")
            if not url:
                continue
            # SearXNG's various video engines use slightly different keys
            iframe_src = item.get("iframe_src")
            img_src = (
                item.get("thumbnail")
                or item.get("thumbnail_src")
                or item.get("img_src")
            )
            duration = item.get("length") or item.get("duration")
            author = item.get("author")
            hits.append(RawVideoHit(
                title=item.get("title", ""),
                url=url,
                iframe_src=iframe_src,
                img_src=img_src,
                duration=str(duration) if duration is not None else None,
                author=author,
                source=item.get("engine"),
            ))
        return hits


    # ---- Diagnostics -------------------------------------------------------
    #
    # Which engines a given host can reach changes without notice, and the
    # failure is silent: results just get thinner until they stop. These two
    # methods make that visible on demand. They are deliberately *not* wired
    # into /health — each one costs real upstream queries, and a liveness
    # probe firing searches every few seconds would create the rate limiting
    # it is meant to detect.

    async def _raw_search(
        self, query: str, engines: Optional[str] = None, timeout: float = 25.0
    ) -> dict:
        params: dict = {"q": query, "format": "json"}
        if engines:
            # `categories` and `engines` are a union in SearXNG, not an
            # intersection: sending both runs every engine in the category
            # *plus* the named one, so a probe would report the whole set's
            # results under a single engine's name. Selecting by engine means
            # selecting by engine only.
            params["engines"] = engines
        else:
            params["categories"] = "general"
        resp = await self.client.get(
            f"{self.base_url}/search", params=params, timeout=timeout
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _health_from_payload(data: dict) -> list[EngineHealth]:
        """Turn one SearXNG response into per-engine health.

        Results carry the engines that produced them, so counting gives the
        engines that actually delivered. `unresponsive_engines` covers those
        that errored — note an engine can be in neither: answering normally
        with nothing to say for this particular query.
        """
        counts: dict[str, int] = {}
        for r in data.get("results", []):
            for e in r.get("engines", []) or []:
                counts[e] = counts.get(e, 0) + 1

        health = [
            EngineHealth(name=name, results=n, ok=True)
            for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        for entry in data.get("unresponsive_engines", []) or []:
            name = entry[0] if isinstance(entry, (list, tuple)) and entry else str(entry)
            reason = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None
            health.append(EngineHealth(name=name, results=0, ok=False, reason=reason))
        return health

    async def configured_engines(self, category: str = "general") -> list[str]:
        """Names of the engines currently enabled for a category."""
        resp = await self.client.get(f"{self.base_url}/config", timeout=15.0)
        resp.raise_for_status()
        return sorted(
            e["name"]
            for e in resp.json().get("engines", [])
            if e.get("enabled") and category in (e.get("categories") or [])
        )

    async def engine_status(self, query: str) -> list[EngineHealth]:
        """Status of every configured engine — including the ones that stayed
        quiet.

        A search response alone cannot answer "is this engine still working?":
        engines that answer with nothing simply do not appear in it, and are
        indistinguishable from engines that were never configured. So the
        enabled set is fetched separately and used as the roll call.
        """
        config_task = asyncio.create_task(self.configured_engines())
        data = await self._raw_search(query)
        observed = {h.name: h for h in self._health_from_payload(data)}

        try:
            configured = await config_task
        except Exception as e:
            # Without the roll call we can still report what we saw, we just
            # cannot vouch for completeness. Better than failing outright.
            logger.warning("engine_config_unavailable", error=str(e))
            return list(observed.values())

        health = [
            observed.get(name, EngineHealth(name=name, results=0, ok=True))
            for name in configured
        ]
        # An engine can report as unresponsive without being enabled for this
        # category (SearXNG surfaces failures across the whole request), so
        # keep anything observed that the roll call did not mention.
        health.extend(h for n, h in observed.items() if n not in set(configured))
        return sorted(health, key=lambda h: (-h.results, h.name))

    async def probe_engines(self, query: str, names: list[str]) -> list[EngineHealth]:
        """Query each engine on its own, so one blocked engine cannot mask
        another. Works for engines disabled in settings too — SearXNG honours
        an explicit `engines=` selection regardless.
        """
        sem = asyncio.Semaphore(5)

        async def one(name: str) -> EngineHealth:
            async with sem:
                try:
                    data = await self._raw_search(query, engines=name)
                except Exception as e:  # network, timeout, bad status
                    return EngineHealth(name=name, ok=False, reason=str(e)[:120])

            dead = {
                (u[0] if isinstance(u, (list, tuple)) and u else str(u)): (
                    u[1] if isinstance(u, (list, tuple)) and len(u) > 1 else None
                )
                for u in (data.get("unresponsive_engines") or [])
            }
            if name in dead:
                return EngineHealth(name=name, ok=False, reason=dead[name])
            n = len(data.get("results", []))
            # Answering with zero results is not the same as failing — the
            # engine is reachable, it just had nothing for this query.
            return EngineHealth(name=name, results=n, ok=True)

        return list(await asyncio.gather(*(one(n) for n in names)))


class DuckDuckGoBackend(SearchBackend):
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ddg")

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        topic: str = "general",
        time_range: Optional[str] = None,
        include_domains: Optional[list[str]] = None,
        exclude_domains: Optional[list[str]] = None,
        include_images: bool = False,
    ) -> BackendSearchResponse:
        effective_query = query
        if include_domains:
            effective_query += " " + " ".join(f"site:{d}" for d in include_domains)
        if exclude_domains:
            effective_query += " " + " ".join(f"-site:{d}" for d in exclude_domains)

        timelimit = None
        if time_range:
            timelimit = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_range)

        loop = asyncio.get_running_loop()

        web_results = await loop.run_in_executor(
            self._executor,
            lambda: self._sync_web_search(effective_query, max_results, timelimit),
        )

        images: list[RawImageResult] = []
        if include_images:
            images = await loop.run_in_executor(
                self._executor,
                lambda: self._sync_image_search(query, max_results=5),
            )

        return BackendSearchResponse(results=web_results, images=images)

    def _sync_web_search(
        self, query: str, max_results: int, timelimit: Optional[str]
    ) -> list[RawSearchResult]:
        from duckduckgo_search import DDGS

        results: list[RawSearchResult] = []
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results, timelimit=timelimit))
            for i, item in enumerate(raw):
                score = round(1.0 - (i * 0.05), 4)
                results.append(
                    RawSearchResult(
                        title=item.get("title", ""),
                        url=item.get("href", ""),
                        snippet=item.get("body", ""),
                        score=max(0.1, score),
                    )
                )
        return results

    def _sync_image_search(self, query: str, max_results: int = 5) -> list[RawImageResult]:
        from duckduckgo_search import DDGS

        images: list[RawImageResult] = []
        with DDGS() as ddgs:
            raw = list(ddgs.images(query, max_results=max_results))
            for item in raw:
                images.append(RawImageResult(url=item.get("image", ""), description=item.get("title", "")))
        return images


class FallbackSearchBackend(SearchBackend):
    def __init__(self, primary: SearchBackend, fallback: SearchBackend) -> None:
        self.primary = primary
        self.fallback = fallback

    async def search(self, query: str, **kwargs) -> BackendSearchResponse:
        try:
            return await self.primary.search(query, **kwargs)
        except (Exception, CircuitBreakerError) as e:
            logger.warning("primary_backend_failed", error=str(e), falling_back=True)
            return await self.fallback.search(query, **kwargs)

    async def image_search_rich(self, *args, **kwargs):
        # No fallback for media search yet; delegate straight to primary.
        return await self.primary.image_search_rich(*args, **kwargs)

    async def video_search_rich(self, *args, **kwargs):
        return await self.primary.video_search_rich(*args, **kwargs)

    async def engine_status(self, *args, **kwargs):
        # Diagnostics describe the primary backend; falling back here would
        # report on a different system than the one being asked about.
        return await self.primary.engine_status(*args, **kwargs)

    async def probe_engines(self, *args, **kwargs):
        return await self.primary.probe_engines(*args, **kwargs)


def create_search_backend(config: AppConfig, http_client: httpx.AsyncClient) -> SearchBackend:
    backend_name = config.search.backend.lower()

    if backend_name == "searxng":
        primary: SearchBackend = SearXNGBackend(config.search.searxng_url, http_client)
    elif backend_name == "duckduckgo":
        primary = DuckDuckGoBackend()
    else:
        raise ValueError(f"Unknown search backend: {backend_name}. Supported: searxng, duckduckgo")

    if config.resilience.backend_fallback and backend_name == "searxng":
        fallback = DuckDuckGoBackend()
        return FallbackSearchBackend(primary, fallback)

    return primary
