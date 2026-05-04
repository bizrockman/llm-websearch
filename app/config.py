from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class SearchConfig(BaseModel):
    backend: str = "searxng"
    searxng_url: str = "http://searxng:8080"
    google_api_key: str = ""
    google_cx: str = ""


class ExtractionConfig(BaseModel):
    max_concurrent: int = 5
    timeout: int = 10
    max_content_length: int = 50000
    domain_concurrency: int = 2
    domain_semaphore_max_size: int = 1000
    user_agents: list[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    ]


class CacheConfig(BaseModel):
    enabled: bool = True
    redis_url: str = "redis://redis:6379"
    search_ttl: int = 3600
    extract_ttl: int = 86400


class ProxyConfig(BaseModel):
    enabled: bool = False
    url: str = ""


class AuthConfig(BaseModel):
    enabled: bool = False
    api_keys: list[str] = []


class RateLimitConfig(BaseModel):
    enabled: bool = False
    default_rate: str = "60/minute"
    search_rate: str = "30/minute"
    extract_rate: str = "30/minute"


class RerankConfig(BaseModel):
    enabled: bool = False
    model: str = "ms-marco-MiniLM-L-12-v2"
    top_k: int = 5


class ResilienceConfig(BaseModel):
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: int = 30
    retry_max_attempts: int = 3
    retry_backoff_base: float = 0.5
    retry_on_status_codes: list[int] = [429, 503, 502, 504]
    request_timeout: int = 30
    backend_fallback: bool = True


class LoggingConfig(BaseModel):
    format: str = "json"
    level: str = "INFO"


class CorsConfig(BaseModel):
    allow_origins: list[str] = ["*"]


class MeilisearchConfig(BaseModel):
    enabled: bool = False
    url: str = "http://meilisearch:7700"
    api_key: str = ""
    index_name: str = "orio_pages"
    search_limit: int = 20
    min_score: float = 0.0
    # How long an indexed page is considered "fresh" enough to serve from
    # Meili in /extract without re-fetching the origin. Default: 30 days.
    freshness_seconds: int = 30 * 24 * 3600


class LLMConfig(BaseModel):
    enabled: bool = False
    provider: str = "ollama"
    base_url: str = "http://ollama:11434/v1"
    api_key: str = "ollama"
    model: str = "llama3.1"
    max_tokens: int = 1024
    temperature: float = 0.1
    timeout: int = 30
    system_prompt: str = (
        "You are a helpful search assistant. Answer the user's question concisely "
        "based on the provided search results. Cite sources by number [1], [2], etc."
    )
    max_context_results: int = 5
    max_context_chars: int = 8000
    answer_ttl: int = 3600


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    search: SearchConfig = SearchConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    cache: CacheConfig = CacheConfig()
    proxy: ProxyConfig = ProxyConfig()
    auth: AuthConfig = AuthConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    rerank: RerankConfig = RerankConfig()
    resilience: ResilienceConfig = ResilienceConfig()
    logging: LoggingConfig = LoggingConfig()
    cors: CorsConfig = CorsConfig()
    llm: LLMConfig = LLMConfig()
    meilisearch: MeilisearchConfig = MeilisearchConfig()


def _apply_env_overrides(cfg: AppConfig) -> AppConfig:
    env = os.environ.get

    if v := env("ORIO_SEARXNG_URL"):
        cfg.search.searxng_url = v
    if v := env("ORIO_REDIS_URL"):
        cfg.cache.redis_url = v

    if v := env("ORIO_AUTH_API_KEYS"):
        cfg.auth.api_keys = [k.strip() for k in v.split(",") if k.strip()]
        cfg.auth.enabled = True
    elif env("ORIO_AUTH_ENABLED", "").lower() in ("1", "true", "yes"):
        cfg.auth.enabled = True

    if env("ORIO_RATE_LIMIT_ENABLED", "").lower() in ("1", "true", "yes"):
        cfg.rate_limit.enabled = True

    if v := env("ORIO_MEILI_URL"):
        cfg.meilisearch.url = v
    if v := env("ORIO_MEILI_API_KEY"):
        cfg.meilisearch.api_key = v
    if env("ORIO_MEILI_ENABLED"):
        cfg.meilisearch.enabled = env("ORIO_MEILI_ENABLED", "").lower() in ("1", "true", "yes")
    if v := env("ORIO_MEILI_FRESHNESS_DAYS"):
        try:
            cfg.meilisearch.freshness_seconds = int(float(v) * 24 * 3600)
        except ValueError:
            pass

    if v := env("ORIO_LLM_BASE_URL"):
        cfg.llm.base_url = v
    if v := env("ORIO_LLM_API_KEY"):
        cfg.llm.api_key = v
    if v := env("ORIO_LLM_MODEL"):
        cfg.llm.model = v
    if env("ORIO_LLM_ENABLED"):
        cfg.llm.enabled = env("ORIO_LLM_ENABLED", "").lower() in ("1", "true", "yes")

    if v := env("ORIO_CORS_ORIGINS"):
        cfg.cors.allow_origins = [o.strip() for o in v.split(",") if o.strip()]

    return cfg


def load_config(config_path: str | None = None) -> AppConfig:
    if config_path is None:
        config_path = os.environ.get(
            "ORIO_SEARCH_CONFIG",
            str(Path(__file__).parent.parent / "config.yaml"),
        )
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        cfg = AppConfig(**data)
    else:
        cfg = AppConfig()
    return _apply_env_overrides(cfg)


settings = load_config()
