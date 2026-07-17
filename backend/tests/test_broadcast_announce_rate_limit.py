# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-principal announce rate limit (G6.5-T6, #2546).

Acceptance-criteria coverage (per issue body):

* The 11th announce in one window from one principal is rejected with
  the typed ``-32000`` error; the error names the window and carries a
  retry-after.
* Another principal in the same tenant is unaffected by the first
  principal hitting its cap (per-``(tenant, principal)`` counter).
* ``broadcast_announce_rate_per_minute == 0`` disables the limit
  entirely -- no Valkey round-trip on the announce hot path.
* The announce happy path is unaffected under the default limit
  (regression: calls within the cap pass through to the publish).

The unit suite drives :func:`enforce_announce_rate_limit` against an
in-memory fake Valkey (no socket) plus the handler/dispatcher
translation into ``McpRateLimitedError`` / JSON-RPC ``-32000``. The
Docker-gated integration suite spins up ``valkey/valkey:8`` via
testcontainers and drives the real ``INCR``/``EXPIRE`` counter through
the MCP handler.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS,
    AnnounceRateLimitError,
    dispose_broadcast_client,
    enforce_announce_rate_limit,
    reset_broadcast_client_for_testing,
)
from meho_backplane.mcp.schemas import RATE_LIMITED
from meho_backplane.mcp.server import McpRateLimitedError
from meho_backplane.mcp.tools.broadcast import _handler_announce
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_TENANT = UUID("11110000-0000-0000-0000-000000000001")
_FIXED_NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------
# In-memory fake Valkey (pipeline INCR/EXPIRE only)
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Records ``incr``/``expire`` and applies them on ``execute``."""

    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str, amount: int = 1) -> None:
        self._ops.append(("incr", name, amount))

    def expire(self, name: str, time: int, *args: object, **kwargs: object) -> None:
        self._ops.append(("expire", name, time))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, name, arg in self._ops:
            if op == "incr":
                self._store[name] = self._store.get(name, 0) + arg
                results.append(self._store[name])
            else:
                results.append(True)
        return results


class _FakeValkey:
    """Minimal broadcast-client stand-in exposing ``pipeline`` only."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.pipeline_calls = 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        self.pipeline_calls += 1
        return _FakePipeline(self.store)


@pytest.fixture
def fake_valkey(monkeypatch: pytest.MonkeyPatch) -> _FakeValkey:
    """Patch the limiter's client + clock; return the in-memory fake."""
    fake = _FakeValkey()
    monkeypatch.setattr(
        "meho_backplane.broadcast.rate_limit.get_broadcast_client",
        lambda: fake,
    )
    monkeypatch.setattr(
        "meho_backplane.broadcast.rate_limit.time.time",
        lambda: _FIXED_NOW,
    )
    return fake


def _pin_limit(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """Pin ``broadcast_announce_rate_per_minute`` for the limiter."""
    monkeypatch.setattr(
        "meho_backplane.broadcast.rate_limit.get_settings",
        lambda: SimpleNamespace(broadcast_announce_rate_per_minute=limit),
    )


# ---------------------------------------------------------------------------
# Limiter unit tests
# ---------------------------------------------------------------------------


async def test_eleventh_announce_in_window_is_rejected(
    fake_valkey: _FakeValkey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten announces pass; the eleventh trips the limit with window detail."""
    _pin_limit(monkeypatch, 10)
    for _ in range(10):
        await enforce_announce_rate_limit(_TENANT, "principal-a")

    with pytest.raises(AnnounceRateLimitError) as excinfo:
        await enforce_announce_rate_limit(_TENANT, "principal-a")

    exc = excinfo.value
    assert exc.limit == 10
    assert exc.window_seconds == ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS
    assert 0 < exc.retry_after_seconds <= ANNOUNCE_RATE_LIMIT_WINDOW_SECONDS
    assert "10 per 60s window" in str(exc)


async def test_second_principal_unaffected_by_first_principals_cap(
    fake_valkey: _FakeValkey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tripped cap for principal A leaves principal B's budget intact."""
    _pin_limit(monkeypatch, 3)
    for _ in range(3):
        await enforce_announce_rate_limit(_TENANT, "principal-a")
    with pytest.raises(AnnounceRateLimitError):
        await enforce_announce_rate_limit(_TENANT, "principal-a")

    # Principal B in the same tenant still has its full window.
    for _ in range(3):
        await enforce_announce_rate_limit(_TENANT, "principal-b")


async def test_limit_zero_disables_and_skips_valkey(
    fake_valkey: _FakeValkey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limit of 0 means unlimited -- no Valkey round-trip at all."""
    _pin_limit(monkeypatch, 0)
    for _ in range(50):
        await enforce_announce_rate_limit(_TENANT, "principal-a")
    assert fake_valkey.pipeline_calls == 0


# ---------------------------------------------------------------------------
# Handler + dispatcher translation
# ---------------------------------------------------------------------------


async def test_handler_maps_domain_error_to_mcp_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_handler_announce`` translates the domain error to ``-32000`` shape."""
    monkeypatch.setattr(
        "meho_backplane.mcp.tools.broadcast.enforce_announce_rate_limit",
        AsyncMock(
            side_effect=AnnounceRateLimitError(
                limit=10,
                window_seconds=60,
                retry_after_seconds=42,
            ),
        ),
    )
    op = build_operator(TenantRole.OPERATOR)
    with pytest.raises(McpRateLimitedError) as excinfo:
        await _handler_announce(op, {"activity": "looping"})

    assert excinfo.value.data == {
        "limit": 10,
        "window_seconds": 60,
        "retry_after_seconds": 42,
    }
    assert "rate limit exceeded" in str(excinfo.value)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_wire_rate_limited_surfaces_as_minus_32000(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-limit announce returns a JSON-RPC ``-32000`` with structured data."""
    monkeypatch.setattr(
        "meho_backplane.mcp.tools.broadcast.enforce_announce_rate_limit",
        AsyncMock(
            side_effect=AnnounceRateLimitError(
                limit=10,
                window_seconds=60,
                retry_after_seconds=17,
            ),
        ),
    )
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho.broadcast.announce",
                "arguments": {"activity": "looping"},
            },
        },
    )
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == RATE_LIMITED
    assert body["error"]["data"]["retry_after_seconds"] == 17
    assert body["error"]["data"]["limit"] == 10


# ---------------------------------------------------------------------------
# Optional testcontainers integration suite
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE = _docker_socket_present()
_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestAnnounceRateLimitIntegration:
    """End-to-end rate limit against a real Valkey container."""

    @pytest.fixture
    def _valkey_env(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
        from testcontainers.redis import RedisContainer

        image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
        with RedisContainer(image) as container:
            host = container.get_container_host_ip()
            port = container.get_exposed_port(6379)
            url = f"redis://{host}:{port}"
            monkeypatch.setenv("BROADCAST_REDIS_URL", url)
            yield url

    async def test_cap_then_other_principal_ok_and_zero_disables(
        self,
        _valkey_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """11th announce rejected; a peer principal unaffected; 0 disables."""
        monkeypatch.setenv("BROADCAST_ANNOUNCE_RATE_PER_MINUTE", "10")
        get_settings.cache_clear()
        reset_broadcast_client_for_testing()
        try:
            op_a = Operator(
                sub="rl-principal-a",
                raw_jwt="x",
                tenant_id=_TENANT,
                tenant_role=TenantRole.OPERATOR,
            )
            op_b = Operator(
                sub="rl-principal-b",
                raw_jwt="x",
                tenant_id=_TENANT,
                tenant_role=TenantRole.OPERATOR,
            )
            for _ in range(10):
                await _handler_announce(op_a, {"activity": "work"})
            with pytest.raises(McpRateLimitedError):
                await _handler_announce(op_a, {"activity": "work"})

            # Peer principal in the same tenant is unaffected.
            await _handler_announce(op_b, {"activity": "peer work"})

            # 0 disables: reconfigure, and a burst well over the old cap passes.
            monkeypatch.setenv("BROADCAST_ANNOUNCE_RATE_PER_MINUTE", "0")
            get_settings.cache_clear()
            for _ in range(20):
                await _handler_announce(op_b, {"activity": "unlimited"})
        finally:
            await dispose_broadcast_client()
            get_settings.cache_clear()
