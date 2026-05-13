# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G6.1-T3 publish-on-write hook.

Covers (issue #309 acceptance criteria):

* :func:`publish_event` calls ``XADD`` against ``meho:feed:{tenant_id}``
  with the redacted JSON event as a single ``event`` field, with
  ``maxlen=BROADCAST_MAXLEN`` and ``approximate=True`` (the ``MAXLEN ~``
  form).
* Fail-open: a redis-py exception during XADD never propagates; the
  ``broadcast_publish_errors_total`` counter increments.
* Success path increments
  ``broadcast_events_published_total{op_class,result_status}``.
* Per-tenant isolation: two events for distinct tenants land on
  distinct stream keys.
* The Docker-gated integration suite drives the same publisher against
  a real Valkey and asserts ``XREAD`` returns the published event back
  out — mirrors ``TestBroadcastIntegration`` in
  ``tests.test_broadcast_client``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.broadcast import (
    BROADCAST_EVENTS_PUBLISHED_TOTAL,
    BROADCAST_MAXLEN,
    BROADCAST_PUBLISH_ERRORS_TOTAL,
    BroadcastEvent,
    dispose_broadcast_client,
    get_broadcast_client,
    publish_event,
    reset_broadcast_client_for_testing,
)
from meho_backplane.settings import get_settings

_TENANT_A: UUID = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B: UUID = UUID("22222222-2222-2222-2222-222222222222")
_AUDIT_ID: UUID = UUID("33333333-3333-3333-3333-333333333333")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_broadcast_client() -> Iterator[None]:
    """Clear the cached client around every test."""
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()


@pytest.fixture
def _broadcast_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars + a broadcast URL so ``get_broadcast_client`` succeeds."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_event(
    *,
    tenant_id: UUID = _TENANT_A,
    op_id: str = "vsphere.vm.list",
    op_class: str = "read",
    result_status: str = "ok",
    payload: dict[str, object] | None = None,
) -> BroadcastEvent:
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime(2026, 5, 13, tzinfo=UTC),
        tenant_id=tenant_id,
        principal_sub="op-test",
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=_AUDIT_ID,
        payload=payload or {"op_class": op_class, "params": {}, "result_status": result_status},
    )


def _counter_value(counter: object, **labels: str) -> float:
    """Read a Prometheus counter's current value.

    ``Counter._value`` is private but stable across prometheus-client
    versions; the public surface only emits exposition text. The
    metric is module-scoped so the value persists across tests in the
    same process — every test that asserts on a counter must capture
    the baseline before calling :func:`publish_event` and compute the
    delta.
    """
    if labels:
        child = counter.labels(**labels)  # type: ignore[attr-defined]
        return float(child._value.get())  # type: ignore[attr-defined, no-any-return]
    return float(counter._value.get())  # type: ignore[attr-defined, no-any-return]


# ---------------------------------------------------------------------------
# publish_event happy path
# ---------------------------------------------------------------------------


class TestPublishEventSuccess:
    """``publish_event`` calls XADD and increments the success counter."""

    async def test_xadd_called_with_canonical_args(self, _broadcast_env: None) -> None:
        """Stream key, fields, maxlen, approximate — all per task body."""
        event = _make_event()
        client = get_broadcast_client()
        with patch.object(client, "xadd", new=AsyncMock(return_value="1234-0")) as xadd:
            await publish_event(event)
        xadd.assert_awaited_once()
        kwargs = xadd.await_args.kwargs
        args = xadd.await_args.args
        # xadd's call signature: xadd(name, fields, maxlen=..., approximate=...).
        # Pinning here so a redis-py upgrade that re-orders positional
        # args trips the test rather than silently shifting the
        # broadcast key.
        assert args[0] == f"meho:feed:{_TENANT_A}"
        assert "event" in args[1]
        decoded = json.loads(args[1]["event"])
        assert decoded["tenant_id"] == str(_TENANT_A)
        assert decoded["op_id"] == "vsphere.vm.list"
        assert kwargs["maxlen"] == BROADCAST_MAXLEN
        assert kwargs["approximate"] is True

    async def test_success_counter_increments(self, _broadcast_env: None) -> None:
        event = _make_event(op_class="read", result_status="ok")
        client = get_broadcast_client()
        baseline = _counter_value(
            BROADCAST_EVENTS_PUBLISHED_TOTAL,
            op_class="read",
            result_status="ok",
        )
        with patch.object(client, "xadd", new=AsyncMock(return_value="1234-0")):
            await publish_event(event)
        delta = (
            _counter_value(BROADCAST_EVENTS_PUBLISHED_TOTAL, op_class="read", result_status="ok")
            - baseline
        )
        assert delta == 1

    async def test_per_tenant_isolation(self, _broadcast_env: None) -> None:
        """Two events for distinct tenants → distinct stream keys."""
        event_a = _make_event(tenant_id=_TENANT_A)
        event_b = _make_event(tenant_id=_TENANT_B)
        client = get_broadcast_client()
        with patch.object(client, "xadd", new=AsyncMock(return_value="1234-0")) as xadd:
            await publish_event(event_a)
            await publish_event(event_b)
        keys = [call.args[0] for call in xadd.await_args_list]
        assert keys == [f"meho:feed:{_TENANT_A}", f"meho:feed:{_TENANT_B}"]


# ---------------------------------------------------------------------------
# publish_event fail-open path
# ---------------------------------------------------------------------------


class TestPublishEventFailOpen:
    """A publish failure must never propagate; the error counter increments."""

    async def test_redis_error_is_swallowed(self, _broadcast_env: None) -> None:
        from redis import exceptions as redis_exceptions

        event = _make_event()
        client = get_broadcast_client()
        baseline = _counter_value(BROADCAST_PUBLISH_ERRORS_TOTAL)
        with patch.object(
            client,
            "xadd",
            new=AsyncMock(side_effect=redis_exceptions.ConnectionError("refused")),
        ):
            # No raise — the audit-middleware caller must see a clean return.
            await publish_event(event)
        assert _counter_value(BROADCAST_PUBLISH_ERRORS_TOTAL) - baseline == 1

    async def test_unknown_exception_also_swallowed(self, _broadcast_env: None) -> None:
        """``except Exception`` covers redis-py-foreign errors too."""
        event = _make_event()
        client = get_broadcast_client()
        baseline = _counter_value(BROADCAST_PUBLISH_ERRORS_TOTAL)
        with patch.object(client, "xadd", new=AsyncMock(side_effect=RuntimeError("kaboom"))):
            await publish_event(event)
        assert _counter_value(BROADCAST_PUBLISH_ERRORS_TOTAL) - baseline == 1

    async def test_failure_does_not_increment_success_counter(self, _broadcast_env: None) -> None:
        """A swallowed publish must NOT count as a published event."""
        event = _make_event(op_class="write", result_status="ok")
        client = get_broadcast_client()
        baseline = _counter_value(
            BROADCAST_EVENTS_PUBLISHED_TOTAL,
            op_class="write",
            result_status="ok",
        )
        with patch.object(client, "xadd", new=AsyncMock(side_effect=RuntimeError("kaboom"))):
            await publish_event(event)
        assert (
            _counter_value(BROADCAST_EVENTS_PUBLISHED_TOTAL, op_class="write", result_status="ok")
            - baseline
            == 0
        )


# ---------------------------------------------------------------------------
# Optional testcontainers integration suite — XADD + XREAD against real Valkey
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE = _docker_socket_present()
_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestPublishEventIntegration:
    """End-to-end smoke against a real Valkey via testcontainers.

    XADD writes one entry; XRANGE reads it back; the deserialised
    :class:`BroadcastEvent` must equal the published one (modulo the
    field ordering JSON serialisation already normalises away).
    """

    @pytest.fixture
    async def valkey_url(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
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

    async def test_xadd_xread_round_trip(self, valkey_url: str) -> None:
        """One publish → one entry in the per-tenant stream, JSON round-trip clean."""
        event = _make_event(op_id="vsphere.host.info")
        await publish_event(event)

        client = get_broadcast_client()
        stream_key = f"meho:feed:{_TENANT_A}"
        entries = await client.xrange(stream_key)
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        # The stream stores the JSON event in the ``event`` field
        # exactly as ``model_dump_json`` produced it.
        decoded = json.loads(fields["event"])
        rebuilt = BroadcastEvent.model_validate(decoded)
        assert rebuilt == event

    async def test_distinct_tenants_land_on_distinct_streams(self, valkey_url: str) -> None:
        """Per-tenant isolation: events for tenant-A don't appear in tenant-B's stream."""
        await publish_event(_make_event(tenant_id=_TENANT_A, op_id="vsphere.host.info"))
        await publish_event(_make_event(tenant_id=_TENANT_B, op_id="k8s.pod.get"))

        client = get_broadcast_client()
        a_entries = await client.xrange(f"meho:feed:{_TENANT_A}")
        b_entries = await client.xrange(f"meho:feed:{_TENANT_B}")
        assert len(a_entries) == 1
        assert len(b_entries) == 1
        a_event = BroadcastEvent.model_validate(json.loads(a_entries[0][1]["event"]))
        b_event = BroadcastEvent.model_validate(json.loads(b_entries[0][1]["event"]))
        assert a_event.op_id == "vsphere.host.info"
        assert b_event.op_id == "k8s.pod.get"
        assert a_event.tenant_id == _TENANT_A
        assert b_event.tenant_id == _TENANT_B
