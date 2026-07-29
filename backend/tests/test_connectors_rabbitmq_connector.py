# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector-level unit tests for RabbitMqConnector (#2233).

Exercises the connector primitives directly (no dispatcher / DB): the
HTTP Basic auth header, the read-only method gate (AC: a non-GET/HEAD op
is refused with no upstream call), and the fingerprint round-trip against
a recorded overview + nodes fixture (AC: returns version / cluster / node
/ erlang fields). An injected credentials loader stands in for the Vault
read; respx replays the wire.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.gsm_creds import GcpSecretManagerReadError
from meho_backplane.connectors.rabbitmq.connector import (
    RabbitMqConnector,
    RabbitMqMethodNotAllowedError,
)

_RABBIT_HOST = "rabbitmq.test.invalid"
_RABBIT_BASE_URL = f"https://{_RABBIT_HOST}:15672"
_USERNAME = "monitor"
_PASSWORD = "rabbit-canary-must-not-leak"


class _Target:
    """Target satisfying ``RabbitMqTargetLike``."""

    def __init__(self) -> None:
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
        self.name = "rabbitmq-unit"
        self.host = _RABBIT_HOST
        self.port = 15672
        self.secret_ref = "targets/op/rabbitmq-unit"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-rabbitmq-unit",
        name="RabbitMQ Unit Operator",
        email=None,
        raw_jwt="op.rabbitmq.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _fake_loader(target: Any, operator: Any) -> dict[str, str]:
    """Injected credential loader — no Vault, returns fixed Basic creds."""
    del target, operator
    return {"username": _USERNAME, "password": _PASSWORD}


async def _gsm_failing_loader(target: Any, operator: Any) -> dict[str, str]:
    """Injected loader standing in for a failed ``gsm:`` credential read."""
    del target, operator
    raise GcpSecretManagerReadError("gsm read failed")


def _connector() -> RabbitMqConnector:
    return RabbitMqConnector(credentials_loader=_fake_loader)


# ---------------------------------------------------------------------------
# HTTP Basic auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_emits_http_basic() -> None:
    """auth_headers returns ``Authorization: Basic base64(user:pass)``."""
    headers = await _connector().auth_headers(_Target(), _make_operator())
    expected = base64.b64encode(f"{_USERNAME}:{_PASSWORD}".encode()).decode()
    assert headers["Authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_auth_headers_rejects_unsupported_auth_model() -> None:
    """A non shared_service_account auth model is refused."""
    target = _Target()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError):
        await _connector().auth_headers(target, _make_operator())


# ---------------------------------------------------------------------------
# Read-only method gate (AC #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_refuses_non_read_method_with_no_upstream_call() -> None:
    """AC: a non-GET/HEAD passthrough is refused before any request is issued."""
    connector = _connector()
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        route = mock.route().respond(200, json={})
        with pytest.raises(RabbitMqMethodNotAllowedError):
            await connector.request_passthrough(
                _make_operator(),
                _Target(),
                {"path": "/api/queues", "method": "POST"},
            )
    # The gate fires before the wire: no request reached the (mocked) broker.
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_passthrough_get_reaches_upstream_with_basic_auth() -> None:
    """A GET passthrough hits the path with the Basic header and redacts output."""
    connector = _connector()
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/parameters").respond(200, json=[{"value": {"uri": "amqp://u:p@h"}}])
        result = await connector.request_passthrough(
            _make_operator(), _Target(), {"path": "/api/parameters"}
        )
    assert route.called
    sent = route.calls[0].request.headers.get("authorization")
    assert sent is not None and sent.startswith("Basic ")
    # Passthrough is in the redacted set — the amqp userinfo is blanked.
    assert result == [{"value": {"uri": "amqp://***@h"}}]


# ---------------------------------------------------------------------------
# fingerprint round-trip (AC #4)
# ---------------------------------------------------------------------------

_OVERVIEW_FIXTURE: dict[str, Any] = {
    "management_version": "3.13.7",
    "rabbitmq_version": "3.13.7",
    "erlang_version": "26.2.1",
    "cluster_name": "rabbit@primary",
    "product_name": "RabbitMQ",
    "product_version": "3.13.7",
    "object_totals": {"queues": 5, "connections": 2},
}
_NODES_FIXTURE: list[dict[str, Any]] = [
    {"name": "rabbit@node-a", "running": True, "type": "disc", "erlang_version": "26.2.1"},
    {"name": "rabbit@node-b", "running": False, "type": "disc", "erlang_version": "26.2.1"},
]


@pytest.mark.asyncio
async def test_fingerprint_round_trips_overview_and_nodes() -> None:
    """AC: fingerprint returns version/cluster/node/erlang from a recorded fixture."""
    connector = _connector()
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/overview").respond(200, json=_OVERVIEW_FIXTURE)
        mock.get("/api/nodes").respond(200, json=_NODES_FIXTURE)
        fp = await connector.fingerprint(_Target(), _make_operator())

    assert fp.reachable is True
    assert fp.vendor == "rabbitmq"
    assert fp.product == "rabbitmq"
    assert fp.version == "3.13.7"
    assert fp.extras["cluster_name"] == "rabbit@primary"
    assert fp.extras["erlang_version"] == "26.2.1"
    assert fp.extras["management_version"] == "3.13.7"
    nodes = fp.extras["nodes"]
    assert [n["name"] for n in nodes] == ["rabbit@node-a", "rabbit@node-b"]
    assert nodes[0]["running"] is True
    assert nodes[1]["running"] is False
    assert nodes[0]["type"] == "disc"


@pytest.mark.asyncio
async def test_fingerprint_unreachable_maps_to_reachable_false() -> None:
    """A transport failure yields reachable=False with a structured error, no raise."""
    connector = _connector()
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/overview").mock(side_effect=httpx.ConnectError("boom"))
        fp = await connector.fingerprint(_Target(), _make_operator())
    assert fp.reachable is False
    assert "error" in fp.extras


@pytest.mark.asyncio
async def test_fingerprint_degrades_on_gsm_credential_read_error() -> None:
    """A ``gsm:`` credential-read failure degrades, it does not escape (#2642)."""
    connector = RabbitMqConnector(credentials_loader=_gsm_failing_loader)
    fp = await connector.fingerprint(_Target(), _make_operator())
    assert fp.reachable is False
    assert "GcpSecretManagerReadError" in fp.extras["error"]


@pytest.mark.asyncio
async def test_probe_delegates_to_fingerprint() -> None:
    """probe() reports ok=True when the overview/nodes fingerprint is reachable."""
    connector = _connector()
    async with respx.mock(base_url=_RABBIT_BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/overview").respond(200, json=_OVERVIEW_FIXTURE)
        mock.get("/api/nodes").respond(200, json=_NODES_FIXTURE)
        probe = await connector.probe(_Target())
    assert probe.ok is True
