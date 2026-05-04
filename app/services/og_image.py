"""On-demand og:image fallback for search results that lack a thumbnail.

Bing-news (and other engines) supply per-result thumbnails for some sources
but not others — Bloomberg yes, MarketWatch no, etc. When a /search/news
caller renders a panel of cards the inconsistency is visible. This service
fills the gap by fetching the article URL and pulling its og:image
(or twitter:image fallback) meta tag.

Each lookup is cached in Redis (positive *and* negative) so repeat calls
to the same URL don't re-hit the origin. Misses are time-bounded so a
slow/dead site can't stall the search response — whatever has filled in
when the timeout fires is what we return.
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional, Sequence

import httpx
import structlog
from lxml import html as lxml_html

from app.services.cache import CacheService

logger = structlog.get_logger(__name__)


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
]


def _extract_og_image(html_bytes: bytes, base_url: str) -> Optional[str]:
    """Parse a small chunk of HTML and return the first og:image-style URL."""
    try:
        doc = lxml_html.fromstring(html_bytes)
    except Exception:
        return None
    # Try og:image, og:image:secure_url, twitter:image, twitter:image:src
    candidates = [
        '//meta[@property="og:image"]/@content',
        '//meta[@name="og:image"]/@content',
        '//meta[@property="og:image:secure_url"]/@content',
        '//meta[@property="twitter:image"]/@content',
        '//meta[@name="twitter:image"]/@content',
        '//meta[@name="twitter:image:src"]/@content',
    ]
    for xp in candidates:
        try:
            values = doc.xpath(xp)
        except Exception:
            continue
        for v in values:
            v = (v or "").strip()
            if not v:
                continue
            # Resolve protocol-relative or relative URLs against the page URL
            if v.startswith("//"):
                v = "https:" + v
            elif v.startswith("/"):
                # naive base-prefix; good enough for og:image which is
                # almost always absolute
                from urllib.parse import urlparse
                p = urlparse(base_url)
                v = f"{p.scheme}://{p.netloc}{v}"
            return v
    return None


class OgImageService:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        cache: Optional[CacheService] = None,
    ) -> None:
        self.client = http_client
        self.cache = cache

    async def fetch(self, url: str, *, timeout: float = 4.0) -> Optional[str]:
        """Returns an og:image URL for the page, or None if unavailable."""
        if self.cache:
            cached = await self.cache.get_og_image(url)
            if cached is not None:
                return cached or None  # "" → None for negative cache
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = await self.client.get(
                url,
                headers=headers,
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                logger.debug("og_image_fetch_status", url=url, status=resp.status_code)
                if self.cache:
                    await self.cache.set_og_image(url, None)
                return None
            # Most pages put og:image in <head>; we don't need the full body.
            # httpx already loaded it, just parse what we have.
            image = _extract_og_image(resp.content, url)
        except Exception as e:
            logger.debug("og_image_fetch_failed", url=url, error=str(e))
            if self.cache:
                await self.cache.set_og_image(url, None)
            return None

        if self.cache:
            await self.cache.set_og_image(url, image)
        return image

    async def fill_missing_thumbnails(
        self,
        results: Sequence,  # any object with .url and .thumbnail attrs
        *,
        per_request_timeout: float = 4.0,
        overall_timeout: float = 6.0,
    ) -> None:
        """Mutate `results` in place, filling `thumbnail` from og:image where
        currently empty. Bounded by `overall_timeout` so a couple of slow
        origins don't drag the whole response."""
        candidates = [r for r in results if not getattr(r, "thumbnail", None)]
        if not candidates:
            return

        async def _fill(r):
            try:
                image = await self.fetch(r.url, timeout=per_request_timeout)
            except Exception as e:
                logger.debug("og_image_fill_error", url=r.url, error=str(e))
                return
            if image:
                r.thumbnail = image

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_fill(r) for r in candidates], return_exceptions=True),
                timeout=overall_timeout,
            )
        except asyncio.TimeoutError:
            logger.info(
                "og_image_fill_timeout",
                pending=sum(1 for r in candidates if not getattr(r, "thumbnail", None)),
                total=len(candidates),
            )
