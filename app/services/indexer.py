from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

import httpx
import structlog

from app.config import AppConfig

logger = structlog.get_logger(__name__)


def _doc_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


class IndexerService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config.meilisearch
        self.enabled = self.config.enabled
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        if not self.enabled:
            return
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.config.url,
            headers=headers,
            timeout=httpx.Timeout(10.0),
        )
        try:
            await self._client.get("/health", timeout=3.0)
        except Exception as e:
            logger.warning("meilisearch_unreachable", error=str(e))
            self.enabled = False
            await self._client.aclose()
            self._client = None
            return

        await self._ensure_index()
        logger.info("indexer_ready", index=self.config.index_name)

    async def _ensure_index(self) -> None:
        assert self._client
        idx = self.config.index_name
        r = await self._client.get(f"/indexes/{idx}")
        if r.status_code == 404:
            await self._client.post(
                "/indexes",
                json={"uid": idx, "primaryKey": "id"},
            )
        await self._client.patch(
            f"/indexes/{idx}/settings",
            json={
                "searchableAttributes": ["title", "content", "url"],
                "filterableAttributes": ["url", "domain", "indexed_at"],
                "sortableAttributes": ["indexed_at"],
            },
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def get_meta(self, url: str) -> Optional[dict[str, Any]]:
        if not self.enabled or not self._client:
            return None
        try:
            r = await self._client.get(
                f"/indexes/{self.config.index_name}/documents/{_doc_id(url)}"
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return {
                "etag": data.get("etag"),
                "last_modified": data.get("last_modified"),
                "indexed_at": data.get("indexed_at"),
                "content": data.get("content"),
                "title": data.get("title"),
            }
        except Exception as e:
            logger.warning("indexer_get_meta_failed", url=url, error=str(e))
            return None

    async def index_document(
        self,
        *,
        url: str,
        title: Optional[str],
        content: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        if not self.enabled or not self._client or not content:
            return
        from urllib.parse import urlparse

        doc = {
            "id": _doc_id(url),
            "url": url,
            "domain": urlparse(url).netloc,
            "title": title or "",
            "content": content,
            "etag": etag,
            "last_modified": last_modified,
            "indexed_at": int(time.time()),
        }
        try:
            await self._client.post(
                f"/indexes/{self.config.index_name}/documents",
                json=[doc],
            )
        except Exception as e:
            logger.warning("indexer_write_failed", url=url, error=str(e))

    async def search(self, query: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
        if not self.enabled or not self._client:
            return []
        try:
            r = await self._client.post(
                f"/indexes/{self.config.index_name}/search",
                json={
                    "q": query,
                    "limit": limit or self.config.search_limit,
                    "attributesToRetrieve": ["url", "title", "content", "indexed_at"],
                    "showRankingScore": True,
                },
            )
            if r.status_code != 200:
                return []
            hits = r.json().get("hits", [])
            min_score = self.config.min_score
            if min_score > 0:
                hits = [h for h in hits if h.get("_rankingScore", 0) >= min_score]
            return hits
        except Exception as e:
            logger.warning("indexer_search_failed", error=str(e))
            return []
