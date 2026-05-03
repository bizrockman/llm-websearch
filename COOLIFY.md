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

By default the compose uses plain **named Docker volumes**. Coolify
creates them under its managed data dir (e.g. `/var/lib/docker/volumes/<project>_meili_data`),
which sits on the host's root disk. Fine for small deployments.

To put data on a **separately mounted disk** (more space, dedicated SSD,
separate backup policy), use Coolify's **Persistent Storage UI**:

1. **Prepare the host once** (OS-level, not Coolify):
   ```bash
   # Make sure your extra disk is mounted (e.g. /mnt/HC_Volume_xxx)
   sudo mkdir -p /mnt/HC_Volume_xxx/orio/meili /mnt/HC_Volume_xxx/orio/redis
   sudo chown -R 999:999 /mnt/HC_Volume_xxx/orio/redis  # redis runs as UID 999
   ```

2. **In Coolify**, open your service → **Persistent Storage** tab → **+ Add**:

   For **Meilisearch**:
   - Name: `meili-bind`
   - Source path: `/mnt/HC_Volume_xxx/orio/meili`
   - Destination path: `/meili_data`
   - Type: Directory

   For **Redis** (separate entry):
   - Name: `redis-bind`
   - Source path: `/mnt/HC_Volume_xxx/orio/redis`
   - Destination path: `/data`
   - Type: Directory

3. **Redeploy.** The UI-defined bind mounts take precedence over the named
   volumes declared in compose; data lands on your chosen path.

Why not put paths directly in the compose? Coolify's compose parser
[doesn't reliably substitute](https://github.com/coollabsio/coolify/issues/8854)
`${VAR}` inside volume definitions, especially in `driver_opts.device`.
The Persistent Storage UI bypasses that and is the documented
[Coolify approach](https://coolify.io/docs/knowledge-base/persistent-storage).

**Migration from existing named volume to bind mount** (only if you've
already deployed and have data to preserve):

```bash
# Stop the service first
# Find current data dir
docker volume inspect <project>_meili_data
# Copy preserving permissions
sudo cp -a /var/lib/docker/volumes/<project>_meili_data/_data/. /mnt/HC_Volume_xxx/orio/meili/
# Add the bind mount in Coolify Persistent Storage UI, redeploy
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
