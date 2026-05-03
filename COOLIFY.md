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

## What must NOT be committed

The repo version of [searxng/settings.prod.yml](searxng/settings.prod.yml)
intentionally contains the placeholder `REPLACE_SEARXNG_SECRET` — the real
secret is substituted at runtime via `sed` by the container entrypoint.
Never write the real key into this file.
