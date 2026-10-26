# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Vault cluster-wide node reads.

``vault.sys.ha_status`` and ``vault.sys.raft.autopilot_state`` list
every node of a Vault cluster. The sibling ``vault.sys.health`` and
``vault.sys.seal_status`` answer for the one node behind the target's
address only, so a standby that came back sealed after a restart was
invisible before these two reads existed.

Coverage:

* Both ops register ``safe`` / no approval / group ``sys`` / empty
  parameter schema, and classify as ``read`` for audit and broadcast.
* ``ha_status`` sends ``GET /v1/sys/ha-status`` through the operator
  client and returns one row per node, from both the enveloped
  (``data.Nodes``) and the bare (``Nodes``) body shape.
* ``autopilot_state`` turns the ``servers`` map into a sorted list,
  keeps ``healthy: false`` for an unhealthy server, and returns the
  cluster's ``failure_tolerance``.
* Neither answer carries fields outside the documented row shape, so no
  secret or unexpected value leaks through.
* A 403 from Vault surfaces as MEHO's normal Vault permission error;
  a login failure surfaces as ``connector_error``; a stray parameter is
  rejected before the handler runs.
* The one-node sentence is present on ``health`` and ``seal_status``.

Vault is faked in-process through the shared ``tests/_vault_fakes.py``
``install_fake_client`` helper (hvac is ``requests``-based, so there is
no HTTP to intercept; the fake replaces the client the handlers use).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultRoleDeniedError
from meho_backplane.broadcast import classify_op
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_sys_typed_operations,
)
from meho_backplane.connectors.vault.ops_sys_cluster import (
    HA_STATUS_API_PATH,
    RAFT_AUTOPILOT_STATE_API_PATH,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_OP_HA_STATUS = "vault.sys.ha_status"
_OP_AUTOPILOT_STATE = "vault.sys.raft.autopilot_state"
_CLUSTER_OP_IDS = (_OP_HA_STATUS, _OP_AUTOPILOT_STATE)

_ONE_NODE_SENTENCE = (
    "Answers for the one node behind the target's address; use "
    "vault.sys.raft.autopilot_state to see every node."
)

_HA_NODES = [
    {
        "hostname": "vault-0",
        "api_address": "https://node-a.example:8200",
        "cluster_address": "https://node-a.example:8201",
        "active_node": True,
        "last_echo": None,
        "version": "1.17.0",
        "clock_skew_ms": 0,
        "echo_duration_ms": 0,
        "upgrade_version": "1.17.0",
        "redundancy_zone": "a",
    },
    {
        "hostname": "vault-1",
        "api_address": "https://node-b.example:8200",
        "cluster_address": "https://node-b.example:8201",
        "active_node": False,
        "last_echo": "2026-10-24T08:05:48.403148+00:00",
        "version": "1.17.0",
        "clock_skew_ms": -1,
        "echo_duration_ms": 2,
    },
    {
        "hostname": "vault-2",
        "api_address": "https://node-c.example:8200",
        "cluster_address": "https://node-c.example:8201",
        "active_node": False,
        "last_echo": "2026-10-24T08:05:49.000000+00:00",
        "version": "1.17.0",
        "clock_skew_ms": 0,
        "echo_duration_ms": 1,
    },
]

_AUTOPILOT_BODY: dict[str, Any] = {
    "failure_tolerance": 1,
    "healthy": False,
    "leader": "vault_1",
    "voters": ["vault_1", "vault_2", "vault_3"],
    "servers": {
        "vault_2": {
            "address": "node-b.example:8201",
            "healthy": False,
            "id": "vault_2",
            "last_contact": "35.2s",
            "last_index": 1200,
            "last_term": 3,
            "name": "vault_2",
            "node_status": "alive",
            "node_type": "voter",
            "stable_since": "2026-10-24T07:00:00Z",
            "status": "voter",
            "version": "1.17.3",
        },
        "vault_1": {
            "address": "node-a.example:8201",
            "healthy": True,
            "id": "vault_1",
            "last_contact": "0s",
            "last_index": 1234,
            "last_term": 3,
            "name": "vault_1",
            "node_status": "alive",
            "node_type": "voter",
            "stable_since": "2026-10-24T07:00:00Z",
            "status": "leader",
            "version": "1.17.3",
        },
        "vault_3": {
            "address": "node-c.example:8201",
            "healthy": True,
            "id": "vault_3",
            "last_contact": "678.6ms",
            "last_index": 1234,
            "last_term": 3,
            "name": "vault_3",
            "node_status": "alive",
            "node_type": "voter",
            "stable_since": "2026-10-24T07:00:00Z",
            "status": "voter",
            "version": "1.17.3",
        },
    },
}


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_sys_ops(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    await register_vault_sys_typed_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_vault(
    op_id: str, params: dict[str, Any], *, jwt: str = "fake.jwt.value"
) -> OperationResult:
    return await dispatch(
        operator=_make_operator(jwt),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
    )


def _ok_body(result: OperationResult) -> dict[str, Any]:
    """Assert a successful dispatch and return its object-shaped result."""
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    return result.result


async def _descriptor(op_id: str) -> tuple[EndpointDescriptor, OperationGroup]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "vault",
                    EndpointDescriptor.version == "1.x",
                    EndpointDescriptor.impl_id == "vault",
                    EndpointDescriptor.op_id == op_id,
                )
            )
        ).scalar_one()
        group = (
            await session.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
        ).scalar_one()
        return row, group


# ---------------------------------------------------------------------------
# Registration + classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _CLUSTER_OP_IDS)
async def test_cluster_op_registers_safe_no_approval_in_sys_group(
    op_id: str, _registered_sys_ops: None
) -> None:
    row, group = await _descriptor(op_id)

    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert row.parameter_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert group.group_key == "sys"


@pytest.mark.parametrize("op_id", _CLUSTER_OP_IDS)
def test_cluster_op_classifies_as_read(op_id: str) -> None:
    assert classify_op(op_id) == "read"


@pytest.mark.parametrize("op_id", ["vault.sys.health", "vault.sys.seal_status"])
async def test_one_node_ops_carry_the_one_node_sentence(
    op_id: str, _registered_sys_ops: None
) -> None:
    row, _ = await _descriptor(op_id)

    assert row.description is not None
    assert _ONE_NODE_SENTENCE in row.description
    assert row.llm_instructions is not None
    assert _ONE_NODE_SENTENCE in str(row.llm_instructions["when_to_use"])


# ---------------------------------------------------------------------------
# vault.sys.ha_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"request_id": "abc", "data": {"Nodes": _HA_NODES}}, id="envelope"),
        pytest.param({"Nodes": _HA_NODES}, id="bare"),
    ],
)
async def test_ha_status_returns_one_row_per_node(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], _registered_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[HA_STATUS_API_PATH] = body

    result = await _dispatch_vault(_OP_HA_STATUS, {}, jwt="op-jwt")

    body = _ok_body(result)
    assert fake.adapter.get_calls == [HA_STATUS_API_PATH]
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    nodes = body["nodes"]
    assert [n["hostname"] for n in nodes] == ["vault-0", "vault-1", "vault-2"]
    assert [n["active_node"] for n in nodes] == [True, False, False]
    assert nodes[1]["last_echo"] == "2026-10-24T08:05:48.403148+00:00"
    assert nodes[0]["api_address"] == "https://node-a.example:8200"
    assert nodes[0]["version"] == "1.17.0"


async def test_ha_status_rows_carry_only_documented_fields(
    monkeypatch: pytest.MonkeyPatch, _registered_sys_ops: None
) -> None:
    """Enterprise-only extras are dropped, so the row shape is fixed."""
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[HA_STATUS_API_PATH] = {"data": {"Nodes": _HA_NODES}}

    body = _ok_body(await _dispatch_vault(_OP_HA_STATUS, {}))

    expected = {
        "hostname",
        "api_address",
        "cluster_address",
        "active_node",
        "last_echo",
        "version",
        "clock_skew_ms",
        "echo_duration_ms",
    }
    assert all(set(row) == expected for row in body["nodes"])


async def test_ha_status_without_nodes_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch, _registered_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[HA_STATUS_API_PATH] = {"data": {}}

    body = _ok_body(await _dispatch_vault(_OP_HA_STATUS, {}))

    assert body == {"nodes": []}


# ---------------------------------------------------------------------------
# vault.sys.raft.autopilot_state
# ---------------------------------------------------------------------------


async def test_autopilot_state_reports_unhealthy_server_and_failure_tolerance(
    monkeypatch: pytest.MonkeyPatch, _registered_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[RAFT_AUTOPILOT_STATE_API_PATH] = {"data": _AUTOPILOT_BODY}

    result = await _dispatch_vault(_OP_AUTOPILOT_STATE, {}, jwt="op-jwt")

    body = _ok_body(result)
    assert fake.adapter.get_calls == [RAFT_AUTOPILOT_STATE_API_PATH]
    assert fake.auth.jwt.login_calls == [{"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}]
    assert body["healthy"] is False
    assert body["failure_tolerance"] == 1
    assert body["leader"] == "vault_1"
    assert body["voters"] == ["vault_1", "vault_2", "vault_3"]
    servers = body["servers"]
    assert [s["id"] for s in servers] == ["vault_1", "vault_2", "vault_3"]
    by_id = {s["id"]: s for s in servers}
    assert by_id["vault_2"]["healthy"] is False
    assert by_id["vault_2"]["last_contact"] == "35.2s"
    assert by_id["vault_2"]["status"] == "voter"
    assert by_id["vault_1"]["status"] == "leader"
    assert by_id["vault_1"]["address"] == "node-a.example:8201"


async def test_autopilot_state_rows_carry_only_documented_fields(
    monkeypatch: pytest.MonkeyPatch, _registered_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    body = {
        **_AUTOPILOT_BODY,
        "servers": {
            "vault_1": {**_AUTOPILOT_BODY["servers"]["vault_1"], "unexpected": "x"},
        },
    }
    fake.adapter.payloads[RAFT_AUTOPILOT_STATE_API_PATH] = {"data": body}

    body = _ok_body(await _dispatch_vault(_OP_AUTOPILOT_STATE, {}))

    assert set(body) == {"healthy", "failure_tolerance", "leader", "voters", "servers"}
    assert set(body["servers"][0]) == {
        "id",
        "name",
        "address",
        "status",
        "node_status",
        "node_type",
        "healthy",
        "last_contact",
        "last_index",
        "last_term",
        "stable_since",
        "version",
    }


async def test_autopilot_state_without_servers_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch, _registered_sys_ops: None
) -> None:
    """Enterprise bodies may omit the servers map; the answer still has the shape."""
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[RAFT_AUTOPILOT_STATE_API_PATH] = {
        "data": {"healthy": True, "failure_tolerance": 2, "leader": "a", "voters": ["a"]}
    }

    body = _ok_body(await _dispatch_vault(_OP_AUTOPILOT_STATE, {}))

    assert body["servers"] == []
    assert body["failure_tolerance"] == 2


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "path"),
    [
        (_OP_HA_STATUS, HA_STATUS_API_PATH),
        (_OP_AUTOPILOT_STATE, RAFT_AUTOPILOT_STATE_API_PATH),
    ],
)
async def test_cluster_op_403_surfaces_vault_permission_error(
    monkeypatch: pytest.MonkeyPatch, op_id: str, path: str, _registered_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.raise_on_get[path] = hvac.exceptions.Forbidden(
        f"permission denied, on GET https://vault.test{path}"
    )

    result = await _dispatch_vault(op_id, {})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_vault_forbidden:")
    assert result.extras["error_code"] == "connector_vault_forbidden"
    assert result.extras["exception_class"] == "Forbidden"


@pytest.mark.parametrize("op_id", _CLUSTER_OP_IDS)
async def test_cluster_op_login_failure_surfaces_vault_client_error(
    monkeypatch: pytest.MonkeyPatch, op_id: str, _registered_sys_ops: None
) -> None:
    install_fake_client(monkeypatch, login_exc=hvac.exceptions.Forbidden("role denied"))

    result = await _dispatch_vault(op_id, {})

    assert result.status == "error"
    assert result.extras.get("error_code") == "connector_error"
    assert result.extras.get("exception_class") == VaultRoleDeniedError.__name__


@pytest.mark.parametrize("op_id", _CLUSTER_OP_IDS)
async def test_cluster_op_rejects_stray_param(
    monkeypatch: pytest.MonkeyPatch, op_id: str, _registered_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(op_id, {"node": "vault-1"})

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"
    assert fake.adapter.get_calls == []
