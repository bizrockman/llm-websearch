# OrioSearch — Personal Cache Fork

A fork of [vkfolio/orio-search](https://github.com/vkfolio/orio-search)
(MIT-licensed, [original README here](README.upstream.md)) with extensions
for **personal caching, quota saving, and a growing search index over
everything you've ever read**.

The upstream project already provides a Tavily-compatible API on top of
SearXNG + Trafilatura + Redis. This fork adds a persistence layer that
shifts the tool from "stateless proxy" to "growing personal knowledge
base" — without changing the original API.

## What this fork adds

| Addition | Purpose |
|---|---|
| **Meilisearch index** | Every successfully extracted page is persisted in a BM25 full-text index — still searchable after the Redis TTL expires |
| **`/search/local` endpoint** | Index-only search, no external call — typically 5–10 ms instead of 1–3 s |
| **HTTP conditional gets** | Re-fetches send `If-None-Match` / `If-Modified-Since` → `304 Not Modified` is enough, no re-parsing |
| **Env-var overrides** | All security-critical values (auth keys, Redis password, Meili key, LLM credentials) overridable via ENV — required for container orchestrators |
| **Coolify deployment** | Production compose with strict service isolation: only the API is exposed, everything else stays on the Docker network |

## The idea behind it

> Building a "web index" would be insane. But **the web you actually read**
> is small enough to fit on a laptop.

The rule of thumb when self-hosting web search: SearXNG is free, but
**quota against the upstream engines (Bing, Brave, Google) is limited**.
Aggressive caching saves quota and speeds up the stack.

The existing Redis cache only handles the trivial case: *same query within
the TTL*. A **slightly different phrasing** still triggers a full roundtrip
— even though the relevant page has long been extracted, parsed, and is
sitting in storage.

With Meilisearch, every successfully extracted document is added to a
searchable index. Three effects:

1. **Semantically similar queries** find existing hits without an external
   call (BM25 matching on `title` + `content`).
2. **You save twice**: no SearXNG hit *and* no re-extraction of the
   same URLs.
3. **You build a personal index** that you own, that's searchable (also
   manually via the Meili dashboard), and that survives every container
   restart.

ETag / `If-Modified-Since` complete the picture for *intentional refreshes*:
when you update an entry (background job or manual), an unchanged page
costs only the HTTP roundtrip — the CDN replies `304`, the code reads the
content from the index. No wasted TLS handshakes, no Trafilatura parsing,
no tokens against per-domain rate limits.

## Architecture

```
                      ┌─────────────┐
   Client ───[Bearer]─▶   FastAPI   │
                      │  orio-api   │
                      └──┬───┬───┬──┘
                         │   │   │
              ┌──────────┘   │   └────────────┐
              ▼              ▼                ▼
        ┌──────────┐   ┌──────────┐   ┌──────────────┐
        │  Redis   │   │ SearXNG  │   │ Meilisearch  │
        │ (TTL    │   │ (meta-   │   │ (persistent  │
        │ cache)  │   │ search)  │   │ BM25 index)  │
        └──────────┘   └────┬─────┘   └──────▲───────┘
                            │                │
                            │ top-N URLs     │ index on success
                            ▼                │
                       ┌─────────┐           │
                       │ httpx + │───────────┘
                       │ Trafil. │
                       └─────────┘
                       (with ETag /
                        If-Mod-Since)
```

## Endpoints

All as in upstream — plus:

| Method + path | Purpose |
|---|---|
| `POST /search` | Same as upstream, **plus** automatic indexing of successful extractions |
| `POST /search/local` | **New** — Meilisearch only, no SearXNG |
| `POST /extract` | Same as upstream, **plus** indexing |

Full schemas: `GET /docs` (Swagger), `GET /openapi.json`,
`GET /tool-schema` (OpenAI function-calling format).

## Quick start (local)

```bash
git clone <your-fork-url> orio-search
cd orio-search
docker compose up -d --build
```

Four containers: API (8000), SearXNG (8080), Redis (6379), Meilisearch (7700).

```bash
# First search — populates the index along the way
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query":"docker compose syntax","search_depth":"advanced"}'

# Index search (noticeable after several searches)
curl -X POST http://localhost:8000/search/local \
  -H "Content-Type: application/json" \
  -d '{"query":"docker compose"}'

# Meili dashboard (master key: orio-dev-master-key)
open http://localhost:7700
```

## Configuration

Defaults live in [`config.yaml`](config.yaml). All important secrets are
**overridable at runtime via ENV**:

| Env var | Override target |
|---|---|
| `ORIO_AUTH_API_KEYS` | Comma-separated bearer keys, auto-enables auth |
| `ORIO_RATE_LIMIT_ENABLED` | Turns rate limiting on |
| `ORIO_REDIS_URL` | Redis connection string (incl. password) |
| `ORIO_SEARXNG_URL` | SearXNG URL |
| `ORIO_MEILI_URL` | Meilisearch URL |
| `ORIO_MEILI_API_KEY` | Meilisearch master key |
| `ORIO_MEILI_ENABLED` | Index on/off |
| `ORIO_LLM_*` | LLM provider, model, key, base URL |
| `ORIO_CORS_ORIGINS` | CORS whitelist |

The env-override logic lives in [`app/config.py`](app/config.py) and is
applied to the `AppConfig` object loaded from YAML.

## Production deployment on Coolify

See [`COOLIFY.md`](COOLIFY.md) for a step-by-step guide.

Key points:
- Dedicated compose variant: [`docker-compose.coolify.yml`](docker-compose.coolify.yml)
- Only the API is exposed externally; SearXNG/Redis/Meili stay on the internal network
- Generate four secrets (`openssl rand -hex 32`): API auth, SearXNG, Meili, Redis
- Coolify-Traefik handles HTTPS via `SERVICE_FQDN_API_8000`
- The SearXNG secret is substituted at runtime via `sed` from a template,
  never committed to git

## Example use cases

**As a Tavily drop-in** (LangChain, LlamaIndex, custom LLM tools):

```python
import os
os.environ["TAVILY_API_KEY"] = "<your API key>"
os.environ["TAVILY_API_URL"] = "https://orio.example.com"
# Existing Tavily clients work without modification
```

**As a personal Perplexity** (with `include_answer: true` + LLM):

```bash
curl https://orio.example.com/search \
  -H "Authorization: Bearer <key>" \
  -d '{"query":"why is redis faster than postgres for caches",
       "include_answer":true,"search_depth":"advanced"}'
```

**Browse the index manually** (Meili dashboard via SSH tunnel to prod):

```bash
ssh -L 7700:localhost:7700 user@coolify-host
# Browser: http://localhost:7700 → enter master key → search the index
```

## What was deliberately *not* built

- **Custom web crawler** — we only index what you actually query for.
  The Marginalia pattern is more efficient than rebuilding Google.
- **Vector search / embeddings** — BM25 is enough for looking up your own
  read material. Embeddings could be added later as a second index.
- **Auth UI for API keys** — keys are managed statically via ENV
  (comma-separated for multiple clients). For 1–5 clients this is plenty;
  for more, a small admin DB makes sense.

## Roadmap (open)

- `local_first: bool` flag on `/search` — try the index first, fall back
  to SearXNG
- APScheduler job for lazy refresh of stale index entries
- Sitemap / RSS subscriber for targeted re-crawling of favorite domains
- Index stats endpoint (number of docs, oldest/newest, top domains)

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
