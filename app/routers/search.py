import asyncio
import hashlib
import json
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import verify_api_key
from app.config import settings
from app.models.schemas import (
    ImageResult,
    ImageSearchResponse,
    ImageSearchResult,
    MediaSearchRequest,
    SearchRequest,
    SearchResponse,
    SearchResult,
    Topic,
    VideoSearchResponse,
    VideoSearchResult,
)
from app.rate_limit import limiter

logger = structlog.get_logger(__name__)
router = APIRouter()


def _params_hash(req: SearchRequest) -> str:
    data = req.model_dump(exclude={"query"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


@router.post("/search", response_model=SearchResponse)
@limiter.limit(settings.rate_limit.search_rate)
async def search(
    request: Request,
    body: SearchRequest,
    http_response: Response,
    api_key: str | None = Depends(verify_api_key),
) -> SearchResponse:
    try:
        return await asyncio.wait_for(
            _do_search(request, body, http_response),
            timeout=settings.resilience.request_timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timed out")


async def _do_search(
    request: Request, body: SearchRequest, http_response: Response
) -> SearchResponse:
    start = time.perf_counter()

    cache = request.app.state.cache
    backend = request.app.state.search_backend
    extractor = request.app.state.extractor
    reranker = request.app.state.reranker
    indexer = request.app.state.indexer

    # Check cache
    ph = _params_hash(body)
    cached = await cache.get_search(body.query, ph)
    if cached:
        elapsed = time.perf_counter() - start
        http_response.headers["X-Orio-Source"] = "redis"
        return SearchResponse(**{**cached, "response_time": round(elapsed, 3)})

    # Query search backend with graceful degradation
    try:
        backend_resp = await backend.search(
            body.query,
            max_results=body.max_results,
            topic=body.topic.value,
            time_range=body.time_range.value if body.time_range else None,
            include_domains=body.include_domains or None,
            exclude_domains=body.exclude_domains or None,
            include_images=body.include_images,
        )
    except Exception as e:
        logger.error("search_backend_failed", error=str(e))

        # 1) Try stale Redis cache (same query+params, expired)
        stale = await cache.get_search(body.query, ph)
        if stale:
            elapsed = time.perf_counter() - start
            logger.info("serving_stale_cache", query=body.query)
            http_response.headers["X-Orio-Source"] = "stale"
            return SearchResponse(**{**stale, "response_time": round(elapsed, 3)})

        # 2) Last resort: full-text query the local Meili index (BM25)
        if indexer.enabled:
            hits = await indexer.search(body.query, limit=body.max_results)
            if hits:
                idx_results: list[SearchResult] = []
                for h in hits:
                    content = h.get("content") or ""
                    snippet = content[:400]
                    idx_results.append(SearchResult(
                        title=h.get("title") or h.get("url"),
                        url=h.get("url"),
                        content=snippet,
                        score=h.get("_rankingScore"),
                        raw_content=content if body.include_raw_content else None,
                    ))
                elapsed = time.perf_counter() - start
                logger.info("serving_index_fallback", query=body.query, hits=len(hits))
                http_response.headers["X-Orio-Source"] = "index"
                return SearchResponse(
                    query=body.query, answer=None, results=idx_results,
                    images=[], response_time=round(elapsed, 3),
                )

        raise HTTPException(503, "Search service unavailable")

    # Build results
    results: list[SearchResult] = []
    for raw in backend_resp.results:
        results.append(
            SearchResult(
                title=raw.title, url=raw.url, content=raw.snippet,
                score=raw.score, raw_content=None,
                thumbnail=raw.thumbnail,
            )
        )

    # Rerank results
    if reranker and settings.rerank.enabled and results:
        result_dicts = [r.model_dump() for r in results]
        reranked = reranker.rerank(body.query, result_dicts, top_k=body.max_results)
        results = [SearchResult(**r) for r in reranked]

    # Advanced depth: fetch and attach raw_content. Indexing is intentionally
    # NOT done here — the Meilisearch index is owned exclusively by /extract,
    # so the wiki only contains pages a caller explicitly chose to read. The
    # Redis content cache is still warmed because that's free and benefits a
    # subsequent /extract on the same URL.
    if body.search_depth.value == "advanced" or body.include_raw_content:
        urls = [r.url for r in results]
        extractions = await extractor.extract_urls(urls)
        to_cache: list[tuple[str, str]] = []
        for result, extraction in zip(results, extractions):
            if extraction.success:
                result.raw_content = extraction.content
                to_cache.append((result.url, extraction.content))
        await cache.set_extract_batch(to_cache)

    # Images
    images = [ImageResult(url=img.url, description=img.description) for img in backend_resp.images]

    # AI answer generation
    answer = None
    if body.include_answer:
        llm = request.app.state.llm
        # Check answer cache first
        answer = await cache.get_answer(body.query, ph)
        if answer is None:
            answer = await llm.generate_answer(body.query, results)
            if answer:
                await cache.set_answer(body.query, ph, answer)

    elapsed = time.perf_counter() - start
    payload = SearchResponse(
        query=body.query, answer=answer, results=results, images=images,
        response_time=round(elapsed, 3),
    )

    # Cache response
    cache_data = payload.model_dump()
    del cache_data["response_time"]
    await cache.set_search(body.query, ph, cache_data)

    http_response.headers["X-Orio-Source"] = "web"
    return payload


@router.post("/search/local", response_model=SearchResponse)
async def search_local(
    request: Request,
    body: SearchRequest,
    http_response: Response,
    api_key: str | None = Depends(verify_api_key),
) -> SearchResponse:
    start = time.perf_counter()
    indexer = request.app.state.indexer
    if not indexer.enabled:
        raise HTTPException(503, "Local index is disabled")

    hits = await indexer.search(body.query, limit=body.max_results)
    results: list[SearchResult] = []
    for h in hits:
        content = h.get("content") or ""
        snippet = content[:400]
        results.append(
            SearchResult(
                title=h.get("title") or h.get("url"),
                url=h.get("url"),
                content=snippet,
                score=h.get("_rankingScore"),
                raw_content=content if body.include_raw_content else None,
            )
        )
    elapsed = time.perf_counter() - start
    http_response.headers["X-Orio-Source"] = "index"
    return SearchResponse(
        query=body.query, answer=None, results=results, images=[],
        response_time=round(elapsed, 3),
    )


# ---------------------------------------------------------------------------
# /search/news — thin wrapper around /search with topic forced to "news".
# Cache-keying still flows through _do_search via the params hash, which now
# differs from a general /search call because topic is news.
# ---------------------------------------------------------------------------

@router.post("/search/news", response_model=SearchResponse)
@limiter.limit(settings.rate_limit.search_rate)
async def search_news(
    request: Request,
    body: SearchRequest,
    http_response: Response,
    api_key: str | None = Depends(verify_api_key),
) -> SearchResponse:
    body.topic = Topic.news
    try:
        result = await asyncio.wait_for(
            _do_search(request, body, http_response),
            timeout=settings.resilience.request_timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timed out")

    # og:image fallback: Bing-news doesn't return per-result thumbnails for
    # all sources (Bloomberg yes, MarketWatch no, etc.). For results without
    # one, fetch the page once and pull og:image. Cached in Redis with a
    # week TTL so repeat panels are cheap. The fill is bounded so a slow
    # origin can't stall the search response.
    og = getattr(request.app.state, "og_image", None)
    missing = [r for r in result.results if not r.thumbnail]
    if og and missing:
        await og.fill_missing_thumbnails(result.results)
        # Refresh the search cache so a follow-up call with the same query
        # serves an already-enriched response without re-running og fetches.
        ph = _params_hash(body)
        cache = request.app.state.cache
        cache_data = result.model_dump()
        cache_data.pop("response_time", None)
        await cache.set_search(body.query, ph, cache_data)

    return result


# ---------------------------------------------------------------------------
# /search/images — dedicated image search. Returns rich per-result fields
# (img_src, thumbnail_src, source). Ignores search_depth; never indexes.
# ---------------------------------------------------------------------------

@router.post("/search/images", response_model=ImageSearchResponse)
@limiter.limit(settings.rate_limit.search_rate)
async def search_images(
    request: Request,
    body: MediaSearchRequest,
    http_response: Response,
    api_key: str | None = Depends(verify_api_key),
) -> ImageSearchResponse:
    start = time.perf_counter()
    backend = request.app.state.search_backend

    try:
        hits = await asyncio.wait_for(
            backend.image_search_rich(
                body.query,
                max_results=body.max_results,
                time_range=body.time_range.value if body.time_range else None,
            ),
            timeout=settings.resilience.request_timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timed out")
    except NotImplementedError:
        raise HTTPException(501, "Image search is not available on this backend")
    except Exception as e:
        logger.error("image_search_failed", error=str(e))
        raise HTTPException(503, "Image search service unavailable")

    results = [
        ImageSearchResult(
            title=h.title,
            url=h.url,
            img_src=h.img_src,
            thumbnail_src=h.thumbnail_src,
            source=h.source,
        )
        for h in hits
    ]
    elapsed = time.perf_counter() - start
    http_response.headers["X-Orio-Source"] = "web"
    return ImageSearchResponse(
        query=body.query, results=results, response_time=round(elapsed, 3),
    )


# ---------------------------------------------------------------------------
# /search/videos — dedicated video search. Returns iframe_src + thumbnail.
# Ignores search_depth; never indexes.
# ---------------------------------------------------------------------------

@router.post("/search/videos", response_model=VideoSearchResponse)
@limiter.limit(settings.rate_limit.search_rate)
async def search_videos(
    request: Request,
    body: MediaSearchRequest,
    http_response: Response,
    api_key: str | None = Depends(verify_api_key),
) -> VideoSearchResponse:
    start = time.perf_counter()
    backend = request.app.state.search_backend

    try:
        hits = await asyncio.wait_for(
            backend.video_search_rich(
                body.query,
                max_results=body.max_results,
                time_range=body.time_range.value if body.time_range else None,
            ),
            timeout=settings.resilience.request_timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timed out")
    except NotImplementedError:
        raise HTTPException(501, "Video search is not available on this backend")
    except Exception as e:
        logger.error("video_search_failed", error=str(e))
        raise HTTPException(503, "Video search service unavailable")

    results = [
        VideoSearchResult(
            title=h.title,
            url=h.url,
            iframe_src=h.iframe_src,
            img_src=h.img_src,
            duration=h.duration,
            author=h.author,
            source=h.source,
        )
        for h in hits
    ]
    elapsed = time.perf_counter() - start
    http_response.headers["X-Orio-Source"] = "web"
    return VideoSearchResponse(
        query=body.query, results=results, response_time=round(elapsed, 3),
    )
