from __future__ import annotations

import asyncio
import random
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
import structlog
import trafilatura
from circuitbreaker import CircuitBreakerError

from app.config import AppConfig
from app.services.indexer import IndexerService
from app.services.resilience import extract_circuit, retry_async

logger = structlog.get_logger(__name__)


@dataclass
class ExtractionResult:
    url: str
    content: Optional[str] = None
    error: Optional[str] = None
    title: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    from_index: bool = False

    @property
    def success(self) -> bool:
        return self.content is not None


class ContentExtractor:
    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient,
        indexer: Optional[IndexerService] = None,
    ) -> None:
        self.config = config
        self.client = http_client
        self.indexer = indexer
        self._domain_semaphores: OrderedDict[str, asyncio.Semaphore] = OrderedDict()
        self._domain_concurrency = config.extraction.domain_concurrency
        self._max_domains = config.extraction.domain_semaphore_max_size
        self._global_semaphore = asyncio.Semaphore(config.extraction.max_concurrent)

    def _get_domain_semaphore(self, url: str) -> asyncio.Semaphore:
        domain = urlparse(url).netloc
        if domain in self._domain_semaphores:
            self._domain_semaphores.move_to_end(domain)
            return self._domain_semaphores[domain]
        while len(self._domain_semaphores) >= self._max_domains:
            self._domain_semaphores.popitem(last=False)
        sem = asyncio.Semaphore(self._domain_concurrency)
        self._domain_semaphores[domain] = sem
        return sem

    def _get_headers(self) -> dict[str, str]:
        ua = random.choice(self.config.extraction.user_agents)
        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    async def extract_url(self, url: str, *, output_format: str = "markdown") -> ExtractionResult:
        domain_sem = self._get_domain_semaphore(url)
        async with self._global_semaphore, domain_sem:
            return await self._fetch_and_extract(url, output_format)

    async def _fetch_and_extract(self, url: str, output_format: str) -> ExtractionResult:
        indexed_meta = await self.indexer.get_meta(url) if self.indexer else None

        try:
            resp = await self._fetch_url(url, indexed_meta)

            if resp.status_code == 304 and indexed_meta and indexed_meta.get("content"):
                logger.info("extraction_304_reuse", url=url)
                return ExtractionResult(
                    url=url,
                    content=indexed_meta["content"],
                    title=indexed_meta.get("title"),
                    etag=indexed_meta.get("etag"),
                    last_modified=indexed_meta.get("last_modified"),
                    from_index=True,
                )

            html = resp.text

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                return ExtractionResult(url=url, error=f"Non-HTML content type: {content_type}")

            title = self._extract_title(html)

            # Tier 1: trafilatura
            extracted = self._extract_trafilatura(html, url, output_format)

            # Tier 2: readability-lxml fallback
            if not extracted:
                logger.info("trafilatura_fallback", url=url)
                extracted = self._extract_readability(html, output_format)

            if not extracted:
                return ExtractionResult(url=url, error="No content could be extracted")

            max_len = self.config.extraction.max_content_length
            if len(extracted) > max_len:
                extracted = extracted[:max_len] + "\n\n[Content truncated]"

            return ExtractionResult(
                url=url,
                content=extracted,
                title=title,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
            )

        except CircuitBreakerError:
            return ExtractionResult(url=url, error="Service temporarily unavailable (circuit open)")
        except httpx.TimeoutException:
            return ExtractionResult(url=url, error=f"Timeout after {self.config.extraction.timeout}s")
        except httpx.HTTPStatusError as e:
            return ExtractionResult(url=url, error=f"HTTP {e.response.status_code}")
        except Exception as e:
            logger.exception("extraction_failed", url=url)
            return ExtractionResult(url=url, error=str(e))

    async def _fetch_url(self, url: str, indexed_meta: Optional[dict] = None) -> httpx.Response:
        @extract_circuit
        async def _do():
            return await retry_async(lambda: self._raw_fetch(url, indexed_meta))
        return await _do()

    async def _raw_fetch(self, url: str, indexed_meta: Optional[dict] = None) -> httpx.Response:
        headers = self._get_headers()
        if indexed_meta:
            if indexed_meta.get("etag"):
                headers["If-None-Match"] = indexed_meta["etag"]
            if indexed_meta.get("last_modified"):
                headers["If-Modified-Since"] = indexed_meta["last_modified"]
        resp = await self.client.get(
            url,
            headers=headers,
            timeout=self.config.extraction.timeout,
            follow_redirects=True,
        )
        if resp.status_code == 304:
            return resp
        resp.raise_for_status()
        return resp

    @staticmethod
    def _extract_title(html: str) -> Optional[str]:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else None

    def _extract_trafilatura(self, html: str, url: str, output_format: str) -> str | None:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            url=url,
        )
        if not extracted:
            return None
        if output_format == "markdown":
            return self._to_markdown(extracted, html, url)
        return extracted

    def _extract_readability(self, html: str, output_format: str) -> str | None:
        try:
            from readability import Document as ReadabilityDocument

            doc = ReadabilityDocument(html)
            content_html = doc.summary()
            text = re.sub(r"<[^>]+>", " ", content_html)
            text = re.sub(r"\s+", " ", text).strip()
            if not text or len(text) < 50:
                return None
            if output_format == "markdown":
                title = doc.short_title() or ""
                lines = []
                if title:
                    lines.append(f"# {title}\n")
                lines.append("---\n")
                lines.append(text)
                return "\n".join(lines)
            return text
        except Exception:
            return None

    @staticmethod
    def _to_markdown(text: str, html: str, url: str) -> str:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else ""
        lines = []
        if title:
            lines.append(f"# {title}\n")
        lines.append(f"Source: {url}\n")
        lines.append("---\n")
        lines.append(text)
        return "\n".join(lines)

    async def extract_urls(self, urls: list[str], *, output_format: str = "markdown") -> list[ExtractionResult]:
        tasks = [self.extract_url(url, output_format=output_format) for url in urls]
        return await asyncio.gather(*tasks)
