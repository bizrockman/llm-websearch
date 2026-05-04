import asyncio
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import verify_api_key
from app.config import settings
from app.models.schemas import (
    ExtractRequest,
    ExtractResponse,
    ExtractResult,
    FailedResult,
)
from app.rate_limit import limiter

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/extract", response_model=ExtractResponse)
@limiter.limit(settings.rate_limit.extract_rate)
async def extract(
    request: Request,
    body: ExtractRequest,
    response: Response,
    api_key: str | None = Depends(verify_api_key),
) -> ExtractResponse:
    try:
        return await asyncio.wait_for(
            _do_extract(request, body, response),
            timeout=settings.resilience.request_timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timed out")


async def _do_extract(
    request: Request, body: ExtractRequest, response: Response
) -> ExtractResponse:
    """Cache hierarchy: Redis (hot) -> Meilisearch (persistent index, freshness-checked)
    -> origin fetch. On origin failure we fall back to stale Meili content if any.

    force_refresh skips both Redis and Meili reads and always pulls from origin.
    """
    start = time.perf_counter()

    cache = request.app.state.cache
    extractor = request.app.state.extractor
    indexer = request.app.state.indexer

    results: list[ExtractResult] = []
    failed: list[FailedResult] = []
    sources: list[str] = []

    requested = list(dict.fromkeys(body.urls))  # dedupe, preserve order
    urls_to_fetch: list[str] = list(requested)

    # ---- 1. Redis hot cache ---------------------------------------------------
    if not body.force_refresh and cache:
        cached_map = await cache.get_extract_batch(urls_to_fetch)
        remaining: list[str] = []
        for url in urls_to_fetch:
            content = cached_map.get(url)
            if content is not None:
                results.append(ExtractResult(url=url, raw_content=content, source="redis"))
                sources.append("redis")
            else:
                remaining.append(url)
        urls_to_fetch = remaining

    # ---- 2. Meilisearch index (freshness-checked) -----------------------------
    if not body.force_refresh and indexer.enabled and urls_to_fetch:
        freshness = settings.meilisearch.freshness_seconds
        now = int(time.time())
        remaining = []
        warm_redis: list[tuple[str, str]] = []
        for url in urls_to_fetch:
            meta = await indexer.get_meta(url)
            content = (meta or {}).get("content")
            indexed_at = (meta or {}).get("indexed_at")
            if content and indexed_at and (now - int(indexed_at)) <= freshness:
                results.append(ExtractResult(url=url, raw_content=content, source="index"))
                sources.append("index")
                warm_redis.append((url, content))
            else:
                remaining.append(url)
        urls_to_fetch = remaining
        if warm_redis and cache:
            await cache.set_extract_batch(warm_redis)

    # ---- 3. Origin fetch (with Meili-stale fallback on failure) ---------------
    if urls_to_fetch:
        extractions = await extractor.extract_urls(
            urls_to_fetch, output_format=body.format.value,
        )
        to_cache: list[tuple[str, str]] = []
        for extraction in extractions:
            if extraction.success:
                results.append(ExtractResult(
                    url=extraction.url, raw_content=extraction.content, source="web",
                ))
                sources.append("web")
                to_cache.append((extraction.url, extraction.content))
                await indexer.index_document(
                    url=extraction.url,
                    title=extraction.title,
                    content=extraction.content,
                    etag=extraction.etag,
                    last_modified=extraction.last_modified,
                )
                continue

            # Origin failed. Try stale Meili content (any age) as fallback,
            # unless force_refresh — then a stale fallback would defeat the
            # whole point of the request.
            if not body.force_refresh and indexer.enabled:
                meta = await indexer.get_meta(extraction.url)
                stale_content = (meta or {}).get("content")
                if stale_content:
                    results.append(ExtractResult(
                        url=extraction.url,
                        raw_content=stale_content,
                        source="stale",
                        stale=True,
                    ))
                    sources.append("stale")
                    logger.info("extract_stale_fallback", url=extraction.url,
                                error=extraction.error)
                    continue

            failed.append(FailedResult(
                url=extraction.url, error=extraction.error or "Unknown error"
            ))
            sources.append("error")
        if to_cache and cache:
            await cache.set_extract_batch(to_cache)

    # ---- Header summarising the path mix --------------------------------------
    if sources:
        unique = set(sources)
        response.headers["X-Orio-Source"] = (
            next(iter(unique)) if len(unique) == 1 else "mixed"
        )

    elapsed = time.perf_counter() - start
    return ExtractResponse(
        results=results, failed_results=failed,
        response_time=round(elapsed, 3),
    )
