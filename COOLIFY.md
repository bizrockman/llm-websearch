# Coolify Deployment

Production-ready self-hosting on Coolify with Traefik, HTTPS, and a fully
isolated internal network.

## Security architecture

| Service       | Externally reachable             | Auth                |
|---------------|----------------------------------|---------------------|
| orio-search-api | via Coolify-Traefik + HTTPS    | Bearer token per key |
| SearXNG       | Docker-internal network only     | —                   |
| Redis         | Docker-internal network only     | Password            |
| Meilisearch   | Docker-internal network only     | Master key          |

**Important:** SearXNG, Redis, and Meilisearch are not reachable from the
outside (no host port mappings). Only the API talks to Traefik.

## 1. Generate secrets

Four random keys:

```bash
openssl rand -hex 32  # -> API_AUTH_KEYS
openssl rand -hex 32  # -> SEARXNG_SECRET
openssl rand -hex 32  # -> MEILI_MASTER_KEY
openssl rand -hex 32  # -> REDIS_PASSWORD
```

## 2. Create the Coolify service

1. **New Resource → Docker Compose**
2. Provide repo URL, pick a branch
3. Compose file path: `docker-compose.coolify.yml`
4. Under **Environment Variables**, set values from `.env.coolify.example`
   (the four secrets + API key + optional LLM)
5. Under **Domains** for the `api` service, set an FQDN — Coolify
   automatically populates `SERVICE_FQDN_API_8000`

## 3. Build the image

Two options:

**A) Coolify builds it itself** (recommended to start with):
- Remove or comment out `image:` in `docker-compose.coolify.yml`, leave
  only `build: .`. Coolify will then build per deploy.

**B) CI/CD builds, Coolify pulls:**
- GitHub Actions build → push to `ghcr.io/<user>/orio-search:tag`
- Set `ORIO_IMAGE` in Coolify to the tag
- Remove `build: .` from the compose

## 4. Usage

Every request needs an API key:

```bash
curl https://orio.example.com/search \
  -H "Authorization: Bearer <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"query":"docker compose best practices","search_depth":"advanced"}'
```

Rate limiting is **active** in production (30/min for search + extract —
configurable in `config.yaml`).

## 5. Updates

With auto-deploy enabled, Coolify polls the branch. For manual deploys:
**Redeploy button** in the Coolify UI.

Data volumes (`redis_data`, `meili_data`) survive redeploys and image updates.

## Persistent storage

Two services hold persistent state:

| Service | Container path | What lives here |
|---|---|---|
| Meilisearch | `/meili_data` | The full-text index of every extracted page — grows over time |
| Redis | `/data` | TTL cache + rate-limit counters — bounded by `--maxmemory 256mb` |

The compose declares **bind mounts to absolute host paths**:

```yaml
redis:
  volumes:
    - /mnt/HC_Volume_105588883/orio/redis:/data
meilisearch:
  volumes:
    - /mnt/HC_Volume_105588883/orio/meili:/meili_data
```

**Before first deploy, make sure these directories exist** on the host:

```bash
sudo mkdir -p /mnt/HC_Volume_105588883/orio/meili \
              /mnt/HC_Volume_105588883/orio/redis
sudo chown -R 999:999 /mnt/HC_Volume_105588883/orio/redis  # redis runs as UID 999
```

**Why hardcoded?** Coolify's compose parser does not reliably substitute
`${VAR}` inside volume mappings ([issue #8854](https://github.com/coollabsio/coolify/issues/8854)),
and for compose-based applications the Persistent Storage UI is read-only.
Hardcoding is the only mechanism that works deterministically. If you
deploy on a different host, edit the two paths in
`docker-compose.coolify.yml` to match your mount layout, commit, and
redeploy.

**Migration from a previous named-volume deploy** (only if you've already
deployed and have data to preserve):

```bash
# Find current data dir
docker volume inspect <project>_meili_data
# Stop the meilisearch container first, then copy preserving permissions
sudo cp -a /var/lib/docker/volumes/<project>_meili_data/_data/. \
           /mnt/HC_Volume_105588883/orio/meili/
# Repeat for redis. Then redeploy with the new compose.
```

## Admin access to Meilisearch

Since Meilisearch is not externally reachable, there are two ways into the
dashboard:

**Option A (recommended) — Separate Coolify service "meili-admin":**
Add another Coolify domain for Meilisearch and configure it as a second
service exposure (`SERVICE_FQDN_MEILISEARCH_7700`). Only makes sense if
you have the master key handy — otherwise prefer VPN-only access.

**Option B — SSH tunnel:**
```bash
ssh -L 7700:localhost:7700 user@coolify-host
# Dashboard then on http://localhost:7700
```

## Troubleshooting

- `SEARXNG_SECRET must be set` → env variable missing in Coolify
- `401 Missing API key` → `Authorization: Bearer <key>` header missing
- `403 Invalid API key` → key doesn't match `API_AUTH_KEYS`
- `503 Search service unavailable` → SearXNG isn't healthy, check logs
- Meilisearch index gone after redeploy → `meili_data` volume was wiped,
  enable "Preserve Volumes" in the Coolify UI

### `/search` returns 200 with an empty `results` array

Nothing is broken in this service — SearXNG's upstream engines are refusing
it. `GET /engines` reports every configured engine and its current state:

```bash
curl -sS https://orio.example.com/engines -H "Authorization: Bearer <key>"
```

Engines are grouped as `delivering`, `silent` (reachable but nothing for
this query) and `failing` (with the reason SearXNG gave). Utility engines
like `wikipedia` or `dictzone` sitting in `silent` for a technical query is
normal, not a fault.

Typical entries and what they mean:

| Reason | Cause |
|---|---|
| `CAPTCHA` (duckduckgo, startpage) | The host IP is being challenged. Datacenter ranges get this far more often, and it escalates with request volume. |
| `too many requests` (brave) | Rate limited. Recovers on its own. |
| `access denied` (some engine) | Often an engine upstream has dropped or changed — check whether the running SearXNG version still ships it. |

**First thing to check: is the SearXNG image current?** Engine scrapers break
constantly and are fixed continuously upstream. A stale image degrades
quietly — engines fail one by one until searches return nothing. This
deployment intentionally tracks `latest`; if someone pins it, that pin needs
bumping every few weeks.

If engines still fail on a current image, don't guess which to swap in —
measure. The `probe` parameter tests engines one at a time, including ones
disabled in the config, so candidates can be evaluated before enabling
anything:

```bash
curl -sS "https://orio.example.com/engines?probe=duckduckgo%20web,bing,yahoo,mojeek,yep,mwmbl,seznam,qwant,yacy" \
  -H "Authorization: Bearer <key>"
```

Then enable what actually delivers, in the `engines:` block of the inline
settings in `docker-compose.coolify.yml`.

A single `failing` entry in the combined view is worth re-checking with
`probe` before acting — upstream engines produce occasional one-off parse
failures that clear by themselves.

Worth knowing: `duckduckgo web` and `duckduckgo` are two different engines.
The former uses the plain HTML endpoint and generally works; the latter uses
a guarded POST/vqd flow and gets challenged. Same for `bing`, which often
works where `yahoo` (Bing-powered) does not.

If nothing mainstream survives, the honest remaining options are an official
API (Brave Search API has a free tier and needs no scraping) or a proxy in
front of SearXNG's outgoing requests.

## What must NOT be committed

The repo version of [searxng/settings.prod.yml](searxng/settings.prod.yml)
intentionally contains the placeholder `REPLACE_SEARXNG_SECRET` — the real
secret is substituted at runtime via `sed` by the container entrypoint.
Never write the real key into this file.
