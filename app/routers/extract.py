import asyncio
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

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
    api_key: str | None = Depends(verify_api_key),
) -> ExtractResponse:
    try:
        return await asyncio.wait_for(
            _do_extract(request, body),
            timeout=settings.resilience.request_timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Request timed out")


async def _do_extract(request: Request, body: ExtractRequest) -> ExtractResponse:
    start = time.perf_counter()

    cache = request.app.state.cache
    extractor = request.app.state.extractor

    results: list[ExtractResult] = []
    failed: list[FailedResult] = []

    # Batch cache lookup (single Redis pipeline round-trip)
    cached_map = await cache.get_extract_batch(body.urls)
    urls_to_fetch: list[str] = []
    for url, content in cached_map.items():
        if content is not None:
            results.append(ExtractResult(url=url, raw_content=content))
        else:
            urls_to_fetch.append(url)

    # Fetch uncached URLs
    if urls_to_fetch:
        extractions = await extractor.extract_urls(
            urls_to_fetch, output_format=body.format.value,
        )
        indexer = request.app.state.indexer
        to_cache: list[tuple[str, str]] = []
        for extraction in extractions:
            if extraction.success:
                results.append(ExtractResult(url=extraction.url, raw_content=extraction.content))
                to_cache.append((extraction.url, extraction.content))
                await indexer.index_document(
                    url=extraction.url,
                    title=extraction.title,
                    content=extraction.content,
                    etag=extraction.etag,
                    last_modified=extraction.last_modified,
                )
            else:
                failed.append(FailedResult(url=extraction.url, error=extraction.error or "Unknown error"))
        await cache.set_extract_batch(to_cache)

    elapsed = time.perf_counter() - start
    return ExtractResponse(
        results=results, failed_results=failed,
        response_time=round(elapsed, 3),
    )
