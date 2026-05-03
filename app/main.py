from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.logging_setup import setup_logging
from app.middleware import RequestIDMiddleware, TimingMiddleware
from app.rate_limit import limiter
from app.routers import extract, search, search_stream
from app.services.cache import CacheService
from app.services.extractor import ContentExtractor
from app.services.indexer import IndexerService
from app.services.llm import LLMService
from app.services.reranker import RerankerService
from app.services.search_backend import create_search_backend

setup_logging()
logger = structlog.get_logger(__name__)

TOOL_SCHEMA = {
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web and return relevant results with optional content extraction.",
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "search_depth": {
                            "type": "string",
                            "enum": ["basic", "advanced"],
                            "default": "basic",
                            "description": "basic = snippets only, advanced = full content extraction",
                        },
                        "topic": {
                            "type": "string",
                            "enum": ["general", "news"],
                            "default": "general",
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "default": 5,
                        },
                        "include_answer": {
                            "type": "boolean",
                            "default": False,
                            "description": "Generate an AI answer from search results (requires LLM config)",
                        },
                        "include_raw_content": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include full extracted page content",
                        },
                        "include_images": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include image search results",
                        },
                        "include_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Only include results from these domains",
                        },
                        "exclude_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exclude results from these domains",
                        },
                        "time_range": {
                            "type": "string",
                            "enum": ["day", "week", "month", "year"],
                            "description": "Filter by time range",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_extract",
                "description": "Extract clean content from one or more URLs.",
                "parameters": {
                    "type": "object",
                    "required": ["urls"],
                    "properties": {
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 20,
                            "description": "URLs to extract content from",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "text"],
                            "default": "markdown",
                        },
                    },
                },
            },
        },
    ]
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting", service="OrioSearch")

    proxy_url = settings.proxy.url if settings.proxy.enabled else None

    # Separate HTTP clients for search vs extraction
    app.state.search_http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        timeout=httpx.Timeout(15.0),
        proxy=proxy_url,
    )
    app.state.extract_http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(settings.extraction.timeout + 5),
        proxy=proxy_url,
    )

    # Cache
    app.state.cache = CacheService(settings)
    await app.state.cache.connect()

    # Search backend (with fallback if configured)
    app.state.search_backend = create_search_backend(settings, app.state.search_http_client)

    # Indexer (Meilisearch)
    app.state.indexer = IndexerService(settings)
    await app.state.indexer.initialize()

    # Content extractor
    app.state.extractor = ContentExtractor(
        settings, app.state.extract_http_client, indexer=app.state.indexer
    )

    # Reranker
    app.state.reranker = RerankerService()
    app.state.reranker.initialize()

    # LLM (AI answer generation)
    app.state.llm = LLMService()
    app.state.llm.initialize()

    logger.info(
        "ready",
        service="OrioSearch",
        backend=settings.search.backend,
        cache="enabled" if app.state.cache.enabled else "disabled",
        auth="enabled" if settings.auth.enabled else "disabled",
        rate_limit="enabled" if settings.rate_limit.enabled else "disabled",
        rerank="enabled" if settings.rerank.enabled else "disabled",
        llm="enabled" if settings.llm.enabled else "disabled",
        indexer="enabled" if app.state.indexer.enabled else "disabled",
        fallback="enabled" if settings.resilience.backend_fallback else "disabled",
        host=settings.server.host,
        port=settings.server.port,
    )

    yield

    await app.state.search_http_client.aclose()
    await app.state.extract_http_client.aclose()
    await app.state.llm.close()
    await app.state.cache.close()
    await app.state.indexer.close()
    logger.info("shutdown", service="OrioSearch")


app = FastAPI(
    title="OrioSearch",
    description="OrioSearch — self-hosted Tavily-compatible web search and content extraction API",
    version="2.0.0",
    lifespan=lifespan,
)

# Middleware (added in reverse order — Starlette processes bottom-up)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TimingMiddleware)
app.add_middleware(RequestIDMiddleware)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Routers
app.include_router(search.router)
app.include_router(extract.router)
app.include_router(search_stream.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "orio-search"}


@app.get("/tool-schema")
async def tool_schema():
    return TOOL_SCHEMA
