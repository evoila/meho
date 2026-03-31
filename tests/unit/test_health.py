# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for Phase 24 health monitoring endpoints and check functions.

Tests three-tier health endpoints (/health, /ready, /status),
individual dependency checks, LLM cache behavior, and startup validation.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.helpers.auth import create_mock_user

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Create a fresh FastAPI app with mocked config."""
    with patch("meho_app.core.config.Config"):
        mock_cfg = MagicMock()
        mock_cfg.database_url = "postgresql+asyncpg://test:test@localhost/test"
        mock_cfg.redis_url = "redis://localhost:6379/0"
        mock_cfg.anthropic_api_key = "sk-test"
        mock_cfg.credential_encryption_key = "a" * 44
        mock_cfg.openai_api_key = "sk-openai-test"
        mock_cfg.voyage_api_key = "vk-test"
        mock_cfg.env = "test"
        mock_cfg.cors_origins = ["*"]
        mock_cfg.enable_rate_limiting = False
        mock_cfg.enable_observability_api = False
        mock_cfg.topology_auto_discovery_enabled = False
        mock_cfg.llm_model = "anthropic:claude-sonnet-4-6"

        with (
            patch("meho_app.core.config.get_config", return_value=mock_cfg),
            patch("meho_app.core.config._config", mock_cfg),
            patch("meho_app.main.get_config", return_value=mock_cfg),
            patch("meho_app.main.get_engine"),
            patch("meho_app.main.configure_observability"),
        ):
            from meho_app.main import create_app

            application = create_app()
            yield application


@pytest.fixture
def client(app):
    """Unauthenticated test client."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def authenticated_client(app):
    """Test client with mocked auth."""
    from meho_app.api.auth import get_current_user

    mock_user = create_mock_user(tenant_id="test-tenant", user_id="test@example.com")
    app.dependency_overrides[get_current_user] = lambda: mock_user
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /health endpoint tests
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for the /health liveness endpoint."""

    def test_health_returns_200_no_io(self, client):
        """GET /health returns 200 with {"status": "healthy"} -- no external calls."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"


# ---------------------------------------------------------------------------
# /ready endpoint tests
# ---------------------------------------------------------------------------


class TestReadyEndpoint:
    """Tests for the /ready readiness endpoint."""

    def test_ready_all_pass(self, client):
        """GET /ready returns 200 with status=ready when all checks pass."""
        mock_checks = [
            {"name": "postgres", "status": "pass", "latency_ms": 2},
            {"name": "redis", "status": "pass", "latency_ms": 1},
            {"name": "keycloak", "status": "pass", "latency_ms": 45},
        ]
        with patch(
            "meho_app.core.health.check_ready",
            new_callable=AsyncMock,
            return_value=(True, mock_checks),
        ):
            response = client.get("/ready")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ready"
            assert "checks" in data

    def test_ready_postgres_down(self, client):
        """GET /ready returns 503 when check_postgres returns fail."""
        mock_checks = [
            {
                "name": "postgres",
                "status": "fail",
                "error": "connection refused",
                "latency_ms": 5000,
            },
            {"name": "redis", "status": "pass", "latency_ms": 1},
            {"name": "keycloak", "status": "pass", "latency_ms": 45},
        ]
        with patch(
            "meho_app.core.health.check_ready",
            new_callable=AsyncMock,
            return_value=(False, mock_checks),
        ):
            response = client.get("/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["checks"]["postgres"]["status"] == "fail"

    def test_ready_checks_run_parallel(self, client):
        """Readiness checks use asyncio.gather, not sequential calls."""
        with patch("meho_app.core.health.asyncio.gather", new_callable=AsyncMock) as mock_gather:
            mock_gather.return_value = [
                {"name": "postgres", "status": "pass", "latency_ms": 2},
                {"name": "redis", "status": "pass", "latency_ms": 1},
                {"name": "keycloak", "status": "pass", "latency_ms": 45},
            ]
            client.get("/ready")
            # The gather call proves parallel execution
            mock_gather.assert_called_once()


# ---------------------------------------------------------------------------
# /status endpoint tests
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    """Tests for the /status diagnostic endpoint."""

    def test_status_requires_auth(self, client):
        """GET /status without auth returns 401."""
        response = client.get("/status")
        assert response.status_code == 401

    def test_status_returns_full_diagnostic(self, authenticated_client):
        """GET /status with valid auth returns checks, uptime, version."""
        mock_result = {
            "status": "healthy",
            "version": "1.67.0",
            "uptime_seconds": 100,
            "checks": {
                "postgres": {"name": "postgres", "status": "pass", "latency_ms": 2},
                "redis": {"name": "redis", "status": "pass", "latency_ms": 1},
                "keycloak": {"name": "keycloak", "status": "pass", "latency_ms": 45},
                "llm": {"name": "llm", "status": "pass", "latency_ms": 850, "cached": True},
            },
        }
        with patch(
            "meho_app.core.health.check_status", new_callable=AsyncMock, return_value=mock_result
        ):
            response = authenticated_client.get("/status")
            assert response.status_code == 200
            data = response.json()
            assert "version" in data
            assert "uptime_seconds" in data
            assert "checks" in data


# ---------------------------------------------------------------------------
# Individual check function tests
# ---------------------------------------------------------------------------


class TestCheckPostgres:
    """Tests for check_postgres function."""

    async def test_check_postgres_success(self):
        """check_postgres returns pass with latency when DB is reachable."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        with patch("meho_app.database.get_engine", return_value=mock_engine):
            from meho_app.core.health import check_postgres

            result = await check_postgres()
            assert result["name"] == "postgres"
            assert result["status"] == "pass"
            assert "latency_ms" in result

    async def test_check_postgres_failure(self):
        """check_postgres returns fail with error message when DB is unreachable."""
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(side_effect=ConnectionError("connection refused"))

        with patch("meho_app.database.get_engine", return_value=mock_engine):
            from meho_app.core.health import check_postgres

            result = await check_postgres()
            assert result["name"] == "postgres"
            assert result["status"] == "fail"
            assert "error" in result


class TestCheckRedis:
    """Tests for check_redis function."""

    async def test_check_redis_success(self):
        """check_redis returns pass with latency when Redis responds to PING."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with (
            patch(
                "meho_app.core.redis.get_redis_client",
                new_callable=AsyncMock,
                return_value=mock_client,
            ),
            patch("meho_app.core.config.get_config") as mock_cfg,
        ):
            mock_cfg.return_value = MagicMock(redis_url="redis://localhost:6379/0")
            from meho_app.core.health import check_redis

            result = await check_redis()
            assert result["name"] == "redis"
            assert result["status"] == "pass"
            assert "latency_ms" in result


class TestCheckKeycloak:
    """Tests for check_keycloak function."""

    async def test_check_keycloak_success(self):
        """check_keycloak returns pass when Keycloak health endpoint responds."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("meho_app.core.health.httpx.AsyncClient", return_value=mock_client),
            patch("meho_app.api.config.get_api_config") as mock_cfg,
        ):
            mock_cfg.return_value = MagicMock(keycloak_url="http://localhost:8080")
            from meho_app.core.health import check_keycloak

            result = await check_keycloak()
            assert result["name"] == "keycloak"
            assert result["status"] == "pass"
            assert "latency_ms" in result


# ---------------------------------------------------------------------------
# LLM cache tests
# ---------------------------------------------------------------------------


class TestLLMCache:
    """Tests for LLM availability check with caching."""

    async def test_llm_cache_miss(self):
        """check_llm probes LLM when cache is expired or empty."""
        # Reset the module-level cache
        import meho_app.core.health as health_mod

        health_mod._llm_cache.clear()

        mock_result = MagicMock()
        mock_result.data = "pong"

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with (
            patch("pydantic_ai.Agent", return_value=mock_agent),
            patch("meho_app.core.config.get_config") as mock_cfg,
        ):
            mock_cfg.return_value = MagicMock(llm_model="anthropic:claude-sonnet-4-6")
            result = await health_mod.check_llm()
            assert result["name"] == "llm"
            assert result["status"] == "pass"
            assert result.get("cached") is not True
            mock_agent.run.assert_called_once()

    async def test_llm_cache_hit(self):
        """check_llm returns cached result when called within 60s window."""
        import meho_app.core.health as health_mod

        # Pre-populate cache
        health_mod._llm_cache["result"] = {
            "name": "llm",
            "status": "pass",
            "latency_ms": 500,
        }
        health_mod._llm_cache["timestamp"] = time.monotonic()  # fresh

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()

        with patch("pydantic_ai.Agent", return_value=mock_agent):
            result = await health_mod.check_llm()
            assert result["name"] == "llm"
            assert result["cached"] is True
            # Agent.run should NOT be called because cache is fresh
            mock_agent.run.assert_not_called()


# ---------------------------------------------------------------------------
# Startup validation tests
# ---------------------------------------------------------------------------


class TestStartupValidation:
    """Tests for validate_startup_config."""

    def test_validate_missing_critical_config_exits(self):
        """Startup validation exits with code 1 when critical vars are missing."""
        from pydantic import BaseModel, ValidationError

        # Create a real ValidationError by building one from a model with required fields
        class _RequiredModel(BaseModel):
            database_url: str
            redis_url: str

        real_error: ValidationError | None = None
        try:
            _RequiredModel.model_validate({})
        except ValidationError as e:
            real_error = e

        assert real_error is not None, "Should have raised ValidationError"

        with (  # noqa: PT012 -- multi-statement raises block is intentional
            patch("meho_app.core.config.Config", side_effect=real_error),
            pytest.raises(SystemExit) as exc_info,
        ):
            from meho_app.core.health import validate_startup_config

            validate_startup_config()

        assert exc_info.value.code == 1

    def test_validate_success_returns_config(self):
        """Startup validation returns Config instance when all critical vars present."""
        mock_cfg = MagicMock()
        mock_cfg.database_url = "postgresql+asyncpg://test:test@localhost/test"

        with patch("meho_app.core.config.Config", return_value=mock_cfg):
            from meho_app.core.health import validate_startup_config

            result = validate_startup_config()
            assert result is mock_cfg
