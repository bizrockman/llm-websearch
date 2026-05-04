from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import redis.asyncio as redis
import structlog

from app.config import AppConfig

logger = structlog.get_logger(__name__)


class CacheService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.enabled = config.cache.enabled
        self._redis: Optional[redis.Redis] = None
        self.search_ttl = config.cache.search_ttl
        self.extract_ttl = config.cache.extract_ttl
        self.answer_ttl = config.llm.answer_ttl
        self.og_image_ttl = config.cache.og_image_ttl

    async def connect(self) -> None:
        if not self.enabled:
            return
        try:
            self._redis = redis.from_url(
                self.config.cache.redis_url,
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info("redis_connected", url=self.config.cache.redis_url)
        except Exception as e:
            logger.warning("redis_connection_failed", error=str(e))
            self.enabled = False
            self._redis = None

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()

    @staticmethod
    def _hash_key(prefix: str, value: str) -> str:
        h = hashlib.sha256(value.encode()).hexdigest()[:16]
        return f"{prefix}:{h}"

    # --- Search cache ---

    async def get_search(self, query: str, params_hash: str) -> Optional[dict[str, Any]]:
        if not self.enabled or not self._redis:
            return None
        key = self._hash_key("search", f"{query}|{params_hash}")
        try:
            data = await self._redis.get(key)
            if data:
                logger.debug("cache_hit", type="search", query=query)
                return json.loads(data)
        except Exception as e:
            logger.warning("cache_read_error", error=str(e))
        return None

    async def set_search(self, query: str, params_hash: str, data: dict[str, Any]) -> None:
        if not self.enabled or not self._redis:
            return
        key = self._hash_key("search", f"{query}|{params_hash}")
        try:
            await self._redis.setex(key, self.search_ttl, json.dumps(data))
        except Exception as e:
            logger.warning("cache_write_error", error=str(e))

    # --- Extract cache (single) ---

    async def get_extract(self, url: str) -> Optional[str]:
        if not self.enabled or not self._redis:
            return None
        key = self._hash_key("extract", url)
        try:
            data = await self._redis.get(key)
            if data:
                logger.debug("cache_hit", type="extract", url=url)
                return data
        except Exception as e:
            logger.warning("cache_read_error", error=str(e))
        return None

    async def set_extract(self, url: str, content: str) -> None:
        if not self.enabled or not self._redis:
            return
        key = self._hash_key("extract", url)
        try:
            await self._redis.setex(key, self.extract_ttl, content)
        except Exception as e:
            logger.warning("cache_write_error", error=str(e))

    # --- Extract cache (batch with pipeline) ---

    async def get_extract_batch(self, urls: list[str]) -> dict[str, str | None]:
        if not self.enabled or not self._redis:
            return {url: None for url in urls}
        try:
            keys = [self._hash_key("extract", url) for url in urls]
            async with self._redis.pipeline(transaction=False) as pipe:
                for key in keys:
                    pipe.get(key)
                values = await pipe.execute()
            return dict(zip(urls, values))
        except Exception as e:
            logger.warning("cache_batch_read_error", error=str(e))
            return {url: None for url in urls}

    # --- Answer cache ---

    async def get_answer(self, query: str, params_hash: str) -> Optional[str]:
        if not self.enabled or not self._redis:
            return None
        key = self._hash_key("answer", f"{query}|{params_hash}")
        try:
            data = await self._redis.get(key)
            if data:
                logger.debug("cache_hit", type="answer", query=query)
                return data
        except Exception as e:
            logger.warning("cache_read_error", error=str(e))
        return None

    async def set_answer(self, query: str, params_hash: str, answer: str) -> None:
        if not self.enabled or not self._redis:
            return
        key = self._hash_key("answer", f"{query}|{params_hash}")
        try:
            await self._redis.setex(key, self.answer_ttl, answer)
        except Exception as e:
            logger.warning("cache_write_error", error=str(e))

    # --- Extract cache (batch with pipeline) ---

    async def set_extract_batch(self, pairs: list[tuple[str, str]]) -> None:
        if not self.enabled or not self._redis or not pairs:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for url, content in pairs:
                    key = self._hash_key("extract", url)
                    pipe.setex(key, self.extract_ttl, content)
                await pipe.execute()
        except Exception as e:
            logger.warning("cache_batch_write_error", error=str(e))

    # --- og:image cache --------------------------------------------------
    # Small URL-only mapping: page_url -> og:image_url (or empty string for
    # negative cache when the page has no og:image / fetch failed). We use
    # a sentinel rather than a missing key so a negative result also gets
    # cached and we don't re-fetch sites that simply don't expose og:image.

    _OG_NEGATIVE = "\x00"  # Stored when fetch produced no usable og:image

    async def get_og_image(self, url: str) -> Optional[str]:
        """Returns the cached og:image URL, "" if cached negative, or None
        if not cached at all."""
        if not self.enabled or not self._redis:
            return None
        key = self._hash_key("ogimg", url)
        try:
            data = await self._redis.get(key)
            if data is None:
                return None
            if data == self._OG_NEGATIVE:
                return ""
            return data
        except Exception as e:
            logger.warning("cache_read_error", error=str(e))
            return None

    async def set_og_image(self, url: str, image: Optional[str]) -> None:
        if not self.enabled or not self._redis:
            return
        key = self._hash_key("ogimg", url)
        value = image if image else self._OG_NEGATIVE
        try:
            await self._redis.setex(key, self.og_image_ttl, value)
        except Exception as e:
            logger.warning("cache_write_error", error=str(e))
