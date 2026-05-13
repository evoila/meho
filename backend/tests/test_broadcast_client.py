# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G6.1-T1 broadcast substrate.

Covers (issue #307 acceptance criteria):

* :func:`get_broadcast_client` returns a singleton; subsequent calls
  reuse the same object.
* :func:`dispose_broadcast_client` calls ``aclose`` exactly once and
  clears the cache; second dispose is a no-op.
* :func:`broadcast_readiness_probe` returns the expected
  :class:`ProbeResult` shape for every failure class redis-py raises,
  and never leaks the operator-controlled URL.
* :class:`Settings` validates ``BROADCAST_REDIS_URL`` schemes up front
  and exposes the retention knob.
* ``/ready`` surfaces the broadcast probe in its rollup when
  registered.

The Docker-gated suite (``TestBroadcastIntegration``) brings up a real
Valkey via testcontainers and asserts the probe flips between ``ok=True``
and ``ok=False`` against a reachable / stopped container. Mirrors the
:class:`TestPostgresIntegration` pattern from ``test_db_engine``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from redis import exceptions as redis_exceptions

from meho_backplane.broadcast import (
    broadcast_readiness_probe,
    dispose_broadcast_client,
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.health import (
    ProbeResult,
    clear_probes,
    register_probe,
    run_probes_async,
)
from meho_backplane.main import app
from meho_backplane.settings import Settings, get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """Empty the readiness-probe registry around every test."""
    clear_probes()
    yield
    clear_probes()


@pytest.fixture(autouse=True)
def _isolated_broadcast_client() -> Iterator[None]:
    """Clear the cached client around every test.

    Tests that monkey-patch ``BROADCAST_REDIS_URL`` and then call
    :func:`get_broadcast_client` need to observe the patched value;
    without the cache reset, the first test's client lingers.
    """
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()


@pytest.fixture
def _broadcast_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars Settings expects plus a broadcast URL.

    The chassis env vars (``KEYCLOAK_*``, ``VAULT_ADDR``, ``DATABASE_URL``)
    are pinned to dev-shaped values so :func:`get_settings` succeeds
    without leaking real configuration into the test process.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    """``BROADCAST_REDIS_URL`` / ``BROADCAST_RETENTION_HOURS`` wiring."""

    def test_defaults(self) -> None:
        """No env vars → documented defaults."""
        s = Settings(
            keycloak_issuer_url="https://keycloak.test/realms/m",  # type: ignore[arg-type]
            keycloak_audience="meho-backplane",
            vault_addr="https://vault.test",  # type: ignore[arg-type]
            database_url="sqlite+aiosqlite:///:memory:",
        )
        assert s.broadcast_redis_url == "redis://localhost:6379"
        assert s.broadcast_retention_hours == 24

    @pytest.mark.parametrize(
        "url",
        [
            "redis://localhost:6379",
            "rediss://broadcast.evba.lab:6379",
            "unix:///tmp/redis.sock",
        ],
    )
    def test_accepts_supported_schemes(self, url: str) -> None:
        s = Settings(
            keycloak_issuer_url="https://keycloak.test/realms/m",  # type: ignore[arg-type]
            keycloak_audience="meho-backplane",
            vault_addr="https://vault.test",  # type: ignore[arg-type]
            database_url="sqlite+aiosqlite:///:memory:",
            broadcast_redis_url=url,
        )
        assert s.broadcast_redis_url == url

    @pytest.mark.parametrize(
        "url",
        ["valkey://broadcast.evba.lab:6379", "http://localhost", "localhost:6379"],
    )
    def test_rejects_unsupported_schemes(self, url: str) -> None:
        """Validator names the supported schemes in the error message."""
        with pytest.raises(ValueError, match="BROADCAST_REDIS_URL must use"):
            Settings(
                keycloak_issuer_url="https://keycloak.test/realms/m",  # type: ignore[arg-type]
                keycloak_audience="meho-backplane",
                vault_addr="https://vault.test",  # type: ignore[arg-type]
                database_url="sqlite+aiosqlite:///:memory:",
                broadcast_redis_url=url,
            )

    def test_retention_hours_rejects_non_positive(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                keycloak_issuer_url="https://keycloak.test/realms/m",  # type: ignore[arg-type]
                keycloak_audience="meho-backplane",
                vault_addr="https://vault.test",  # type: ignore[arg-type]
                database_url="sqlite+aiosqlite:///:memory:",
                broadcast_retention_hours=0,
            )


# ---------------------------------------------------------------------------
# Client singleton + dispose
# ---------------------------------------------------------------------------


class TestClientLifecycle:
    """Singleton + dispose semantics — AC #1, #2."""

    def test_singleton_returns_same_instance(self, _broadcast_env: None) -> None:
        first = get_broadcast_client()
        second = get_broadcast_client()
        assert first is second

    def test_construction_reads_settings(self, _broadcast_env: None) -> None:
        """The client is built from ``BROADCAST_REDIS_URL`` at first call."""
        client = get_broadcast_client()
        # redis-py exposes the parsed URL via the connection-pool kwargs.
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs.get("host") == "broadcast.test"
        assert kwargs.get("port") == 6379

    async def test_dispose_calls_aclose_and_clears_cache(self, _broadcast_env: None) -> None:
        client = get_broadcast_client()
        with patch.object(client, "aclose", new=AsyncMock()) as aclose:
            await dispose_broadcast_client()
        aclose.assert_awaited_once()
        # Cache cleared: next call builds a fresh client.
        assert get_broadcast_client() is not client

    async def test_dispose_before_first_get_is_noop(self) -> None:
        """``dispose`` before any ``get`` does not raise."""
        await dispose_broadcast_client()  # nothing to close — must not raise

    async def test_dispose_twice_only_calls_aclose_once(self, _broadcast_env: None) -> None:
        client = get_broadcast_client()
        with patch.object(client, "aclose", new=AsyncMock()) as aclose:
            await dispose_broadcast_client()
            await dispose_broadcast_client()
        aclose.assert_awaited_once()

    async def test_dispose_swallows_aclose_failure_and_clears_cache(
        self,
        _broadcast_env: None,
    ) -> None:
        """``aclose`` raising must not leak a stale client into the module cache.

        The docstring's "idempotent: calling twice is a silent no-op"
        contract has to hold even when the first ``aclose`` fails — a
        stale ``_CLIENT`` reference would otherwise re-enter ``aclose``
        on the broken object every subsequent dispose call, defeating
        the idempotency guarantee the lifespan shutdown relies on.
        """
        client = get_broadcast_client()
        with patch.object(
            client,
            "aclose",
            new=AsyncMock(side_effect=RuntimeError("aclose boom")),
        ) as aclose:
            # First dispose: aclose raises; the swallow-log path keeps it
            # silent so the lifespan shutdown chain continues cleanly.
            await dispose_broadcast_client()
            # Second dispose: cache is already clear, aclose is not
            # reached again — the AsyncMock proves it by recording the
            # call count.
            await dispose_broadcast_client()
        aclose.assert_awaited_once()
        # Cache cleared: next get builds a fresh client rather than
        # handing back the broken one.
        assert get_broadcast_client() is not client


# ---------------------------------------------------------------------------
# Probe outcomes (mocked client) — AC #3
# ---------------------------------------------------------------------------


async def _patched_ping(monkeypatch: pytest.MonkeyPatch, side_effect: object) -> None:
    """Wire a synthetic ``Redis.ping`` so the probe sees *side_effect*.

    ``side_effect`` is either the value :class:`AsyncMock` should return
    or an exception instance / class it should raise.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/m")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    client = get_broadcast_client()
    if isinstance(side_effect, type | BaseException):
        fake: AsyncMock = AsyncMock(side_effect=side_effect)
    else:
        fake = AsyncMock(return_value=side_effect)
    monkeypatch.setattr(client, "ping", fake)


class TestProbeOutcomes:
    """Every branch of :func:`broadcast_readiness_probe`."""

    async def test_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        await _patched_ping(monkeypatch, True)
        result = await broadcast_readiness_probe()
        assert result == ProbeResult(name="broadcast", ok=True, detail="reachable")

    async def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        await _patched_ping(monkeypatch, redis_exceptions.TimeoutError())
        result = await broadcast_readiness_probe()
        assert result.name == "broadcast"
        assert result.ok is False
        assert result.detail == "timeout"

    async def test_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        await _patched_ping(monkeypatch, redis_exceptions.ConnectionError("refused"))
        result = await broadcast_readiness_probe()
        assert result.ok is False
        assert result.detail == "unreachable: ConnectionError"

    async def test_busy_loading_error_treated_as_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BusyLoadingError`` is a ``ConnectionError`` subclass in redis-py.

        Server still warming up post-restart counts as "broadcast is not
        ready for traffic yet", which is the same operational shape as
        an unreachable host from the ``/ready`` consumer's perspective.
        """
        await _patched_ping(monkeypatch, redis_exceptions.BusyLoadingError("loading"))
        result = await broadcast_readiness_probe()
        assert result.ok is False
        assert result.detail == "unreachable: BusyLoadingError"

    async def test_response_error_uses_redis_error_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ResponseError`` (non-``ConnectionError``) lands on the redis_error branch."""
        await _patched_ping(monkeypatch, redis_exceptions.ResponseError("WRONGTYPE"))
        result = await broadcast_readiness_probe()
        assert result.ok is False
        assert result.detail == "redis_error: ResponseError"

    async def test_unknown_exception_safety_net(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anything outside the redis-py hierarchy → ``check_failed``."""
        await _patched_ping(monkeypatch, RuntimeError("boom"))
        result = await broadcast_readiness_probe()
        assert result.ok is False
        assert result.detail == "check_failed: RuntimeError"

    @pytest.mark.parametrize(
        "side_effect",
        [
            redis_exceptions.TimeoutError(),
            redis_exceptions.ConnectionError("broadcast.evba.lab:6379 refused"),
            redis_exceptions.AuthenticationError("bad creds"),
            RuntimeError("broadcast.evba.lab:6379 went sideways"),
        ],
    )
    async def test_detail_never_echoes_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        side_effect: BaseException,
    ) -> None:
        """Operator-controlled substrings (host/port) must never leak."""
        await _patched_ping(monkeypatch, side_effect)
        result = await broadcast_readiness_probe()
        detail = result.detail or ""
        assert "broadcast.evba.lab" not in detail
        assert "broadcast.test" not in detail
        assert "6379" not in detail

    async def test_probe_registers_with_async_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``run_probes_async`` awaits the probe under the registry contract."""
        await _patched_ping(monkeypatch, True)
        register_probe("broadcast", broadcast_readiness_probe)
        results = await run_probes_async()
        assert len(results) == 1
        assert results[0].name == "broadcast"
        assert isinstance(results[0], ProbeResult)


# ---------------------------------------------------------------------------
# /ready integration — AC #4
# ---------------------------------------------------------------------------


def test_ready_includes_broadcast_probe(
    _broadcast_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` surfaces the broadcast probe verdict in its rollup."""

    async def _fake_probe() -> ProbeResult:
        return ProbeResult(name="broadcast", ok=True, detail="reachable")

    register_probe("broadcast", _fake_probe)
    response = TestClient(app).get("/ready")
    assert response.status_code == 200
    body = response.json()
    broadcast_check = next(c for c in body["checks"] if c["name"] == "broadcast")
    assert broadcast_check == {
        "name": "broadcast",
        "ok": True,
        "detail": "reachable",
    }


# ---------------------------------------------------------------------------
# Optional testcontainers integration suite — AC #3 against a real server
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    """Heuristic: Docker is usable if the unix socket is present.

    Mirrors the same check used by ``TestPostgresIntegration`` in
    ``test_db_engine``; avoids the cost of importing testcontainers'
    docker client just to discover availability.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE = _docker_socket_present()
_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestBroadcastIntegration:
    """End-to-end smoke against a real Valkey via testcontainers.

    Valkey serves the Redis wire protocol on port 6379 under the
    ``redis://`` scheme; the testcontainers ``RedisContainer`` works
    against either Redis or Valkey images. The image is env-overridable
    via ``MEHO_TEST_VALKEY_IMAGE`` so operators can point at an internal
    mirror without re-rolling the test on the first Docker Hub
    rate-limit.
    """

    @pytest.fixture
    async def valkey_url(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
        """Start a Valkey container and pin BROADCAST_REDIS_URL to it."""
        from testcontainers.redis import RedisContainer

        image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
        with RedisContainer(image) as container:
            host = container.get_container_host_ip()
            port = container.get_exposed_port(6379)
            url = f"redis://{host}:{port}"
            monkeypatch.setenv("BROADCAST_REDIS_URL", url)
            monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/m")
            monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
            monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
            monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
            get_settings.cache_clear()
            reset_broadcast_client_for_testing()
            try:
                yield url
            finally:
                await dispose_broadcast_client()
                get_settings.cache_clear()

    async def test_probe_ok_against_real_valkey(self, valkey_url: str) -> None:
        result = await broadcast_readiness_probe()
        assert result == ProbeResult(name="broadcast", ok=True, detail="reachable")

    async def test_probe_unreachable_after_container_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stopping the container before probing flips ``ok`` to ``False``."""
        from testcontainers.redis import RedisContainer

        image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
        container = RedisContainer(image)
        container.start()
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        container.stop()

        monkeypatch.setenv("BROADCAST_REDIS_URL", f"redis://{host}:{port}")
        monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/m")
        monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
        monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        get_settings.cache_clear()
        reset_broadcast_client_for_testing()
        try:
            result = await broadcast_readiness_probe()
        finally:
            await dispose_broadcast_client()
            get_settings.cache_clear()

        assert result.ok is False
        # Either a timeout or an unreachable verdict is acceptable —
        # which one the OS surfaces depends on whether the kernel
        # closes the port immediately (refused) or lets it linger
        # (timeout). Both detail strings are URL-redacted, which is
        # the load-bearing contract.
        assert result.detail in {
            "timeout",
            "unreachable: ConnectionError",
        }
