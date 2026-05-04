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
