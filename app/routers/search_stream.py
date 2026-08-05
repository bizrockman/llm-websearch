import asyncio
import json
import time

import structlog
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from app.auth import verify_api_key
from app.config import settings
from app.models.schemas import SearchRequest, SearchResult
from app.rate_limit import limiter

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/search/stream")
@limiter.limit(settings.rate_limit.search_rate)
async def search_stream(
    request: Request,
    body: SearchRequest,
    api_key: str | None = Depends(verify_api_key),
):
    async def event_generator():
        start = time.perf_counter()
        backend = request.app.state.search_backend
        extractor = request.app.state.extractor

        # Search
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
            yield {"event": "error", "data": json.dumps({"error": str(e)})}
            return

        # Stream each result
        results = []
        for raw in backend_resp.results:
            result = SearchResult(
                title=raw.title, url=raw.url, content=raw.snippet,
                score=raw.score, raw_content=None,
                thumbnail=raw.thumbnail,
            )
            results.append(result)
            yield {"event": "result", "data": json.dumps(result.model_dump())}

        # Stream images
        for img in backend_resp.images:
            yield {
                "event": "image",
                "data": json.dumps({"url": img.url, "description": img.description}),
            }

        # If advanced, stream extractions as they complete
        if body.search_depth.value == "advanced" or body.include_raw_content:
            tasks = {
                url: asyncio.create_task(extractor.extract_url(r.url))
                for r, url in ((r, r.url) for r in results)
            }
            for url, task in tasks.items():
                extraction = await task
                if extraction.success:
                    yield {
                        "event": "extraction",
                        "data": json.dumps({"url": url, "raw_content": extraction.content}),
                    }

        # AI answer generation (streamed)
        if body.include_answer:
            llm = request.app.state.llm
            async for chunk in llm.generate_answer_stream(body.query, results):
                yield {"event": "answer_chunk", "data": json.dumps({"text": chunk})}
            yield {"event": "answer_done", "data": "{}"}

        elapsed = time.perf_counter() - start
        yield {"event": "done", "data": json.dumps({"response_time": round(elapsed, 3)})}

    return EventSourceResponse(event_generator())
