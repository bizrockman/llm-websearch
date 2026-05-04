# OrioSearch — bizrockman fork

A fork of [vkfolio/orio-search](https://github.com/vkfolio/orio-search)
(MIT-licensed, [original README here](README.upstream.md)) extended into
a richer search-and-content backbone for downstream UIs and AI agents.
The Tavily-compatible API surface is preserved; the additions slot in as
new endpoints and as enrichment passes on the existing ones.

## What this fork changes vs. upstream

The headline differences are listed up front so it's immediately clear
what's new — every item is implemented, on `main`, and verified against
a live deployment.

### New endpoints

| Endpoint | Role | Schema additions vs. generic `/search` |
|---|---|---|
| `POST /search/news` | News-only search; forces SearXNG `topic=news` and runs the `og:image` fallback (see below) | `thumbnail` populated for every result that has one anywhere in the chain |
| `POST /search/images` | Image search via SearXNG image engines | `[{title, url, img_src, thumbnail_src?, source?}]` — `url` is the page hosting the image, `img_src` is the actual image URL |
| `POST /search/videos` | Video search via SearXNG video engines | `[{title, url, iframe_src?, img_src?, duration?, author?, source?}]` — embed-friendly fields directly on each hit |
| `POST /search/local` | Index-only search (no external call) | unchanged from earlier addition |

Image and video search are intentionally outside the `search_depth`
contract: those hits point to non-text content, indexing their pages
would pollute the text-only Meili corpus. Generic `/search` and
`/search/news` still honour `search_depth`.

### Enrichment & data passes

| Change | Effect |
|---|---|
| **Engine thumbnails passed through on `/search`** | When the underlying SearXNG engine ships `thumbnail` / `thumbnail_src` / `img_src` per result (Bing-news, Wikipedia, etc.), it now reaches the client instead of being dropped at the Tavily-shape mapping. |
| **`og:image` fallback on `/search/news`** | For results without an engine thumbnail, OrioSearch fetches the article URL, parses `og:image` / `twitter:image` from the `<head>`, and fills the field. Bounded parallel (6 s ceiling), Redis-cached for 7 days including negative results so dead/JS-only origins are tried only once. |
| **`X-Orio-Source` response header** | Every search response surfaces which path served it: `web` (live engine), `redis` (hot cache), `stale` (expired cache fallback), `index` (Meilisearch fallback). |
| **`force_refresh` on `/extract`** | Bypass both Redis and Meili, always re-fetch from origin and re-index. For when the cached version is stale and the caller knows it. |
| **Per-result `source` + `stale` flags on `/extract`** | Each result reports whether it came from Redis, Meili, the web, or stale Meili content (when origin fetch failed). |
| **Stale-on-origin-failure for `/extract`** | If the origin times out / 5xx's and an older Meili copy exists, return that with `stale: true` instead of erroring. |
| **Single source of truth for indexing** | Only `POST /extract` writes to Meili. `/search` no longer auto-indexes in `advanced` mode — the index now contains exactly the pages someone explicitly chose to read, no search-side noise. |

### Caching layer additions

The Redis-backed cache that upstream uses for `/search` responses is now
joined by a dedicated **`og:image` cache** (key: page URL, TTL: 7 days,
negative-cached so JS-only sites are tried once and skipped) and a
**Meili-First-Cache** strategy on `/extract` (Redis-hot → Meili
freshness-checked → origin → Meili-stale fallback).

### Operations

| Change | Effect |
|---|---|
| **API healthcheck** | The `api` service now reports `(healthy)` to Docker / Coolify (sub-millisecond `/health` probe via Python urllib — the slim base image has no curl/wget). |
| **Rate-limit knobs via ENV** | `ORIO_RATE_LIMIT_ENABLED`, `ORIO_DEFAULT_RATE`, `ORIO_SEARCH_RATE`, `ORIO_EXTRACT_RATE` are env-overridable so you can tune in Coolify without rebuilding. Defaults raised to 200/minute search, 100/minute extract — the previous 30/minute didn't survive bursty agent workloads. |

## Architectural goals

> Building a "web index" would be insane. But **the web you actually read**
> is small enough to fit on a laptop.

OrioSearch sits between consumers (Vane, AI agents, custom CLI tools) and
the search/extract substrate (SearXNG + Trafilatura + Redis +
Meilisearch). It's a single point for caching, auth, rate-limiting,
indexing decisions, and fallback orchestration — so multiple consumers
can rely on the same growing knowledge layer without each reinventing
the cache or hammering origin servers.

The rule of thumb when self-hosting search: SearXNG is free, but **quota
against the upstream engines (Bing, Brave, Google) is limited**.
Aggressive caching saves quota and speeds up the stack. Meilisearch as
a persistent BM25 layer survives the Redis TTL and lets follow-up
queries on related material hit local content.

## Architecture

```
                      ┌──────────────┐
   Client ──[Bearer]─▶│   FastAPI    │
                      │   orio-api   │
                      └──┬───┬───┬───┘
                         │   │   │
              ┌──────────┘   │   └────────────┐
              ▼              ▼                ▼
        ┌──────────┐   ┌──────────┐   ┌──────────────┐
        │  Redis   │   │ SearXNG  │   │ Meilisearch  │
        │ (response│   │ (meta-   │   │ (persistent  │
        │  + og:   │   │  search) │   │  BM25 index) │
        │  image)  │   │          │   │              │
        └──────────┘   └────┬─────┘   └──────▲───────┘
                            │                │
                            │ top-N URLs     │ index on extract
                            ▼                │
                       ┌─────────┐           │
                       │ httpx + │───────────┘
                       │ Trafil. │
                       └─────────┘
                       (with ETag /
                        If-Mod-Since)
```

## Endpoints (full list)

| Method + path | Purpose |
|---|---|
| `POST /search` | General web; thumbnail pass-through where the engine supplies it |
| `POST /search/news` | News-only; with `og:image` fallback |
| `POST /search/images` | Image hits with `img_src` per result |
| `POST /search/videos` | Video hits with `iframe_src` + `img_src` per result |
| `POST /search/local` | Meilisearch only, no external call |
| `POST /extract` | Fetch + extract + index, with `force_refresh`, source tracking, stale-on-failure |

Every search response includes `X-Orio-Source: web | redis | stale | index`
to indicate the path that served it.

Full schemas: `GET /docs` (Swagger), `GET /openapi.json`,
`GET /tool-schema` (OpenAI function-calling format).

## Quick start (local)

```bash
git clone https://github.com/bizrockman/llm-websearch.git orio-search
cd orio-search
docker compose up -d --build
```

Four containers: API (8010 → 8000), SearXNG (8080), Redis (6379),
Meilisearch (7700).

```bash
# News search with thumbnails
curl -X POST http://localhost:8010/search/news \
  -H 'Content-Type: application/json' \
  -d '{"query":"NVIDIA RTX","max_results":3}'

# Image search
curl -X POST http://localhost:8010/search/images \
  -H 'Content-Type: application/json' \
  -d '{"query":"red panda","max_results":3}'

# Index search (after any /extract calls have populated Meili)
curl -X POST http://localhost:8010/search/local \
  -H 'Content-Type: application/json' \
  -d '{"query":"docker compose"}'

# Meili dashboard (master key: orio-dev-master-key)
open http://localhost:7700
```

## Configuration

Defaults live in [`config.yaml`](config.yaml). All operationally
relevant values are overridable at runtime via ENV:

| Env var | Purpose |
|---|---|
| `ORIO_AUTH_API_KEYS` | Comma-separated Bearer keys; auto-enables auth |
| `ORIO_RATE_LIMIT_ENABLED` | `true` / `false` (set explicitly to disable) |
| `ORIO_DEFAULT_RATE` | e.g. `120/minute` |
| `ORIO_SEARCH_RATE` | e.g. `200/minute` |
| `ORIO_EXTRACT_RATE` | e.g. `100/minute` |
| `ORIO_REDIS_URL` | Redis connection string |
| `ORIO_SEARXNG_URL` | Internal SearXNG URL |
| `ORIO_MEILI_URL` | Meilisearch URL |
| `ORIO_MEILI_API_KEY` | Meilisearch master key |
| `ORIO_MEILI_ENABLED` | Index on/off |
| `ORIO_MEILI_FRESHNESS_DAYS` | Treat indexed pages as fresh for N days (default 30) |
| `ORIO_LLM_*` | LLM provider/model/key/base URL for `include_answer: true` |
| `ORIO_CORS_ORIGINS` | CORS whitelist |

The override logic lives in [`app/config.py`](app/config.py) and is
applied to the `AppConfig` object loaded from YAML.

## Production deployment on Coolify

See [`COOLIFY.md`](COOLIFY.md) for the step-by-step guide.

Key points:
- Dedicated compose variant: [`docker-compose.coolify.yml`](docker-compose.coolify.yml)
- Only the API is exposed externally; SearXNG/Redis/Meili stay on the internal network
- Generate four secrets (`openssl rand -hex 32`): API auth, SearXNG, Meili, Redis
- Coolify-Traefik handles HTTPS via `SERVICE_FQDN_API_8000`
- All rate-limit knobs are interpolated through `${VAR:-default}` so the
  Coolify UI's env panel actually overrides
- The SearXNG secret is substituted at runtime, never committed

## Example use cases

**As a Tavily drop-in** (LangChain, LlamaIndex, custom LLM tools):

```python
import os
os.environ["TAVILY_API_KEY"] = "<your API key>"
os.environ["TAVILY_API_URL"] = "https://orio.example.com"
# Existing Tavily clients work unchanged
```

**As an answering engine** (with `include_answer: true` + LLM):

```bash
curl https://orio.example.com/search \
  -H 'Authorization: Bearer <key>' \
  -d '{"query":"why is redis faster than postgres for caches",
       "include_answer":true,"search_depth":"advanced"}'
```

**As a backbone for a custom UI** — see
[bizrockman/Vane](https://github.com/bizrockman/Vane), a Vane fork that
strips the bundled SearXNG and routes all search/extract through this
project. Discover, image and video search panels all consume the typed
endpoints above.

**Browse the index manually** (Meili dashboard via SSH tunnel to prod):

```bash
ssh -L 7700:localhost:7700 user@coolify-host
# Browser: http://localhost:7700 → enter master key → search the index
```

## What was deliberately *not* built

- **Custom web crawler** — we only index what's been explicitly extracted.
  The Marginalia pattern is more efficient than rebuilding Google.
- **Vector search / embeddings** — BM25 is enough for looking up
  already-extracted material. Embeddings could be added later as a second
  index.
- **Auth UI for API keys** — keys are managed statically via ENV
  (comma-separated for multiple clients).

## Roadmap (open)

- `local_first: bool` flag on `/search` — try the Meili index first,
  fall back to SearXNG only on miss/low-score
- `/extract/dynamic` with Playwright/Chromium for SPAs and JS-rendered
  sites that Trafilatura can't see
- `/search/raw` debug endpoint exposing the SearXNG response unfiltered
- APScheduler job for lazy refresh of stale index entries
- Sitemap / RSS subscriber for targeted re-crawling of favourite domains
- Index stats endpoint (doc count, oldest/newest, top domains)

## Credits

- **Upstream:** [vkfolio/orio-search](https://github.com/vkfolio/orio-search) (MIT)
- **Search aggregation:** [SearXNG](https://github.com/searxng/searxng)
- **Index:** [Meilisearch](https://github.com/meilisearch/meilisearch)
- **Extraction:** [Trafilatura](https://github.com/adbar/trafilatura) +
  [readability-lxml](https://github.com/buriy/python-readability)

## License

This fork is licensed as a **combined work under Apache License 2.0**
([`LICENSE`](LICENSE)). The original upstream files come from MIT-licensed
OrioSearch ([`LICENSE-UPSTREAM-MIT`](LICENSE-UPSTREAM-MIT)).

[`NOTICE`](NOTICE) documents which file belongs to which license. The
MIT → Apache direction is explicitly permitted; Apache 2.0 adds a patent
grant and attribution requirements that make sense for original
contributions.
