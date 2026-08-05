from __future__ import annotations

import os
import tempfile

import pytest
import yaml

from app.config import AppConfig, RateLimitConfig, load_config


class TestConfig:
    def test_default_config(self):
        config = AppConfig()
        assert config.server.host == "0.0.0.0"
        assert config.server.port == 8000
        assert config.search.backend == "searxng"
        assert config.cache.enabled is True
        assert config.auth.enabled is False
        assert config.rate_limit.enabled is False
        assert config.rerank.enabled is False
        assert config.resilience.backend_fallback is True
        assert config.logging.format == "json"
        assert config.cors.allow_origins == ["*"]

    def test_extraction_defaults(self):
        config = AppConfig()
        assert config.extraction.max_concurrent == 5
        assert config.extraction.timeout == 10
        assert config.extraction.domain_concurrency == 2
        assert config.extraction.domain_semaphore_max_size == 1000
        assert len(config.extraction.user_agents) > 0

    def test_resilience_defaults(self):
        config = AppConfig()
        assert config.resilience.circuit_breaker_failure_threshold == 5
        assert config.resilience.circuit_breaker_recovery_timeout == 30
        assert config.resilience.retry_max_attempts == 3
        assert config.resilience.retry_backoff_base == 0.5
        assert 429 in config.resilience.retry_on_status_codes
        assert 503 in config.resilience.retry_on_status_codes
        assert config.resilience.request_timeout == 30

    def test_load_from_yaml(self):
        data = {
            "server": {"port": 9999},
            "auth": {"enabled": True, "api_keys": ["key1"]},
            "rerank": {"enabled": True, "model": "test-model"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            config = load_config(f.name)

        os.unlink(f.name)

        assert config.server.port == 9999
        assert config.auth.enabled is True
        assert config.auth.api_keys == ["key1"]
        assert config.rerank.enabled is True
        assert config.rerank.model == "test-model"
        # Defaults preserved for unset
        assert config.search.backend == "searxng"
        assert config.cache.enabled is True

    def test_load_missing_file_returns_defaults(self):
        config = load_config("/nonexistent/path/config.yaml")
        assert config.server.port == 8000
        assert config.search.backend == "searxng"

    def test_load_empty_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()
            config = load_config(f.name)
        os.unlink(f.name)
        assert config.server.port == 8000

    def test_rate_limit_config(self):
        """Overriding one rate must not disturb the others.

        Compared against the field default rather than a literal — the
        defaults are deliberately tuned for agentic burst traffic and get
        adjusted, which should not break this test.
        """
        config = AppConfig(rate_limit={"enabled": True, "search_rate": "10/minute"})
        assert config.rate_limit.enabled is True
        assert config.rate_limit.search_rate == "10/minute"
        assert config.rate_limit.extract_rate == RateLimitConfig().extract_rate
        assert config.rate_limit.default_rate == RateLimitConfig().default_rate

    def test_proxy_config(self):
        config = AppConfig(proxy={"enabled": True, "url": "http://proxy:8080"})
        assert config.proxy.enabled is True
        assert config.proxy.url == "http://proxy:8080"

    def test_cors_config(self):
        config = AppConfig(cors={"allow_origins": ["https://myapp.com"]})
        assert config.cors.allow_origins == ["https://myapp.com"]

    def test_env_var_config_path(self):
        """ORIO_SEARCH_CONFIG env var should be checked."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"server": {"port": 7777}}, f)
            f.flush()
            os.environ["ORIO_SEARCH_CONFIG"] = f.name
            config = load_config()
            del os.environ["ORIO_SEARCH_CONFIG"]
        os.unlink(f.name)
        assert config.server.port == 7777
