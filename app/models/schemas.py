from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --- Enums ---

class SearchDepth(str, Enum):
    basic = "basic"
    advanced = "advanced"


class Topic(str, Enum):
    general = "general"
    news = "news"


class TimeRange(str, Enum):
    day = "day"
    week = "week"
    month = "month"
    year = "year"


class ExtractDepth(str, Enum):
    basic = "basic"
    advanced = "advanced"


class ContentFormat(str, Enum):
    markdown = "markdown"
    text = "text"


# --- Search ---

class SearchRequest(BaseModel):
    query: str = Field(..., description="The search query")
    search_depth: SearchDepth = Field(
        default=SearchDepth.basic,
        description="basic = fast snippets only, advanced = fetch and extract full content",
    )
    topic: Topic = Field(default=Topic.general, description="Search category")
    max_results: int = Field(default=5, ge=1, le=20, description="Number of results to return")
    include_answer: bool = Field(default=False, description="Generate an AI answer from search results (requires LLM config)")
    include_raw_content: bool = Field(default=False, description="Include full extracted page content")
    include_images: bool = Field(default=False, description="Include image search results")
    include_domains: list[str] = Field(default_factory=list, description="Only include results from these domains")
    exclude_domains: list[str] = Field(default_factory=list, description="Exclude results from these domains")
    time_range: Optional[TimeRange] = Field(default=None, description="Filter results by time range")


class SearchResult(BaseModel):
    title: str
    url: str
    content: str = Field(description="Short snippet / description")
    score: float = Field(default=0.0, description="Relevance score (0-1)")
    raw_content: Optional[str] = Field(default=None, description="Full extracted page content")
    thumbnail: Optional[str] = Field(
        default=None,
        description="Thumbnail/preview image URL when the underlying engine "
                    "supplies one (Bing-news etc.). Not all engines do.",
    )


class ImageResult(BaseModel):
    url: str = Field(description="Direct image URL")
    description: str = Field(default="", description="Image description or alt text")


class SearchResponse(BaseModel):
    query: str
    answer: Optional[str] = Field(default=None, description="AI-generated answer based on search results")
    results: list[SearchResult]
    images: list[ImageResult] = Field(default_factory=list)
    response_time: float = Field(description="Total time in seconds")


# --- Extract ---

class ExtractRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=20, description="URLs to extract content from")
    extract_depth: ExtractDepth = Field(default=ExtractDepth.basic, description="Extraction depth")
    format: ContentFormat = Field(default=ContentFormat.markdown, description="Output format")
    force_refresh: bool = Field(
        default=False,
        description="Bypass Redis and Meilisearch caches; always fetch from origin and re-index.",
    )


class ExtractResult(BaseModel):
    url: str
    raw_content: str = Field(description="Extracted page content")
    source: Optional[str] = Field(
        default=None,
        description="Where the content came from: 'redis', 'index', 'web', or 'stale'.",
    )
    stale: bool = Field(
        default=False,
        description="True when origin fetch failed and content was served from the index as fallback.",
    )


class FailedResult(BaseModel):
    url: str
    error: str


class ExtractResponse(BaseModel):
    results: list[ExtractResult]
    failed_results: list[FailedResult] = Field(default_factory=list)
    response_time: float


# --- Media search (images / videos) ---

class MediaSearchRequest(BaseModel):
    """Request body for /search/images and /search/videos.

    Media search ignores search_depth: there is no `advanced` mode that fetches
    pages, and results are never indexed into Meilisearch (the index is text-
    only and owned by /extract).
    """

    query: str = Field(..., description="The search query")
    max_results: int = Field(default=10, ge=1, le=30)
    time_range: Optional[TimeRange] = Field(default=None)


class ImageSearchResult(BaseModel):
    title: str
    url: str = Field(description="Page URL hosting the image")
    img_src: str = Field(description="Direct image URL")
    thumbnail_src: Optional[str] = Field(
        default=None, description="Smaller preview image URL when the engine supplies one"
    )
    source: Optional[str] = Field(default=None, description="Engine name that produced the hit")


class ImageSearchResponse(BaseModel):
    query: str
    results: list[ImageSearchResult]
    response_time: float


class VideoSearchResult(BaseModel):
    title: str
    url: str = Field(description="Video page URL")
    iframe_src: Optional[str] = Field(default=None, description="Embed URL when available")
    img_src: Optional[str] = Field(default=None, description="Thumbnail URL")
    duration: Optional[str] = Field(default=None, description="Engine-provided duration string, e.g. '12:34'")
    author: Optional[str] = Field(default=None, description="Channel/author when available")
    source: Optional[str] = Field(default=None)


class VideoSearchResponse(BaseModel):
    query: str
    results: list[VideoSearchResult]
    response_time: float
