# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``targets.probe`` typed op on ``targets-registry-1.x``.

* **Registration** -- ``safe``, no approval, in the ``registry`` group.
* **Success** -- an agent calls the op; the target row then holds the
  detected product and version, and the result shows them.
* **Same code as REST** -- the op and ``POST /api/v1/targets/{name}/probe``
  store the same fingerprint.
* **Unreachable system** -- a connector that reports ``reachable=False`` is
  stored as such; a connector that raises gives an error and keeps the
  last stored fingerprint.
* **Unknown target, other tenant** -- both give an error and store nothing.
* **Role** -- a ``read_only`` login is refused, as on the REST route.
* **Policy on the named target** -- a deny grant for the target stops the
  probe before the system is contacted; another target is still probed.
* **A call that waits for approval** -- an agent with a ``needs-approval``
  grant parks the call; the approved run by a second person probes and
  stores. A deny for the target, before the park or while the request
  waits, still stops the approved run.
* **No extras** -- the connector's ``extras`` never reach the result.
* **Audit** -- every call writes a row with op id ``targets.probe``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.connectors.targets.ops import TARGETS_REGISTRY_CONNECTOR_ID
from meho_backplane.connectors.targets.probe import (
    TARGETS_PROBE_OP_ID,
    register_targets_probe_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    PermissionVerdict,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._targets_helpers import _build_app, _insert_target, _operator_token

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_PRODUCT = "probeop"
_MADE_UP_SECRET = "made-up-secret-value-1234"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered_op(_settings_env: None) -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_targets_probe_operation(embedding_service=service)
    yield


def _fingerprint(*, reachable: bool = True) -> FingerprintResult:
    return FingerprintResult(
        vendor="example-vendor",
        product=_PRODUCT,
        version="2.4.1" if reachable else None,
        build="b77" if reachable else None,
        reachable=reachable,
        probed_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        probe_method="version-endpoint",
        extras={"session_token": _MADE_UP_SECRET},
    )


def _use_connector(answer: FingerprintResult | Exception) -> list[str]:
    """Register a fake connector for ``_PRODUCT`` whose fingerprint gives *answer*.

    Returns the list of target names the fake connector was asked about.
    """
    asked: list[str] = []

    class _FakeConnector(Connector):
        product = _PRODUCT

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            asked.append(str(target.name))
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product=_PRODUCT, version="2.0", impl_id=f"{_PRODUCT}-rest", cls=_FakeConnector
    )
    return asked


def _operator(
    sub: str = "agent-1",
    *,
    kind: PrincipalKind = PrincipalKind.AGENT,
    tenant_id: uuid.UUID = _TENANT_ID,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
    )


async def _seed(
    name: str, *, tenant_id: uuid.UUID = _TENANT_ID, fingerprint: dict[str, Any] | None = None
) -> TargetORM:
    return await _insert_target(
        tenant_id=tenant_id,
        name=name,
        product=_PRODUCT,
        host="203.0.113.7",
        fingerprint=fingerprint,
    )


async def _stored_fingerprint(target_id: uuid.UUID) -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, target_id)
    assert row is not None
    return row.fingerprint


async def _probe(name: str, operator: Operator | None = None) -> dict[str, Any]:
    return await call_operation(
        operator or _operator(),
        {
            "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
            "op_id": TARGETS_PROBE_OP_ID,
            "params": {"name": name},
        },
    )


async def _audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row for row in rows if (row.payload or {}).get("op_id") == TARGETS_PROBE_OP_ID]


def _error_text(result: dict[str, Any]) -> str:
    return str(result.get("extras", {}).get("exception_message"))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_registered_as_a_safe_op_without_approval() -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == TARGETS_PROBE_OP_ID)
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert (row.product, row.version, row.impl_id) == ("targets", "1.x", "targets-registry")
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert "extras" not in row.response_schema["properties"]


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


async def test_an_agent_probes_a_target_and_the_version_is_stored() -> None:
    _use_connector(_fingerprint())
    row = await _seed("new-system")

    result = await _probe("new-system")

    assert result["status"] == "ok", result
    assert result["result"] == {
        "target_id": str(row.id),
        "name": "new-system",
        "vendor": "example-vendor",
        "product": _PRODUCT,
        "version": "2.4.1",
        "build": "b77",
        "edition": None,
        "reachable": True,
        "probed_at": "2026-11-02T12:00:00Z",
        "probe_method": "version-endpoint",
    }
    stored = await _stored_fingerprint(row.id)
    assert stored is not None
    assert (stored["product"], stored["version"]) == (_PRODUCT, "2.4.1")
    assert await _audit_rows()


async def test_the_target_is_found_by_id_too() -> None:
    _use_connector(_fingerprint())
    row = await _seed("by-id")
    result = await _probe(str(row.id))
    assert result["status"] == "ok", result
    assert result["result"]["name"] == "by-id"


async def test_connector_extras_never_reach_the_result() -> None:
    _use_connector(_fingerprint())
    await _seed("with-extras")
    result = await _probe("with-extras")
    assert result["status"] == "ok", result
    assert _MADE_UP_SECRET not in str(result)


def _rest_probe(name: str) -> int:
    key = make_rsa_keypair("kid-probe")
    client = TestClient(_build_app())
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/targets/{name}/probe",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    return response.status_code


async def test_op_and_rest_store_the_same_fingerprint() -> None:
    _use_connector(_fingerprint())
    rest_row = await _seed("rest-side")
    op_row = await _seed("op-side")

    assert _rest_probe("rest-side") == 200
    result = await _probe("op-side")

    assert result["status"] == "ok", result
    assert await _stored_fingerprint(rest_row.id) == await _stored_fingerprint(op_row.id)


# ---------------------------------------------------------------------------
# Unreachable system
# ---------------------------------------------------------------------------


async def test_an_unreachable_system_is_stored_as_unreachable() -> None:
    _use_connector(_fingerprint(reachable=False))
    row = await _seed("offline")

    result = await _probe("offline")

    assert result["status"] == "ok", result
    assert result["result"]["reachable"] is False
    assert result["result"]["version"] is None
    stored = await _stored_fingerprint(row.id)
    assert stored is not None
    assert stored["reachable"] is False


async def test_a_failing_connector_is_an_error_and_keeps_the_last_fingerprint() -> None:
    last = _fingerprint().model_dump(mode="json")
    _use_connector(ConnectionError("connection refused"))
    row = await _seed("refuses", fingerprint=last)

    result = await _probe("refuses")

    assert result["status"] == "error", result
    assert "fingerprint_failed" in _error_text(result)
    assert "connection refused" in _error_text(result)
    assert await _stored_fingerprint(row.id) == last
    assert await _audit_rows()


async def test_a_product_without_a_connector_is_an_error() -> None:
    row = await _insert_target(
        tenant_id=_TENANT_ID, name="no-connector", product="noconnectorproduct", host="203.0.113.8"
    )
    result = await _probe("no-connector")
    assert result["status"] == "error", result
    assert await _stored_fingerprint(row.id) is None


# ---------------------------------------------------------------------------
# Unknown target and other tenant
# ---------------------------------------------------------------------------


async def test_an_unknown_target_is_an_error() -> None:
    _use_connector(_fingerprint())
    result = await _probe("no-such-target")
    assert result["status"] == "error", result
    assert "no_target" in _error_text(result)


@pytest.mark.parametrize("by", ["name", "id"])
async def test_a_target_of_another_tenant_is_refused(by: str) -> None:
    _use_connector(_fingerprint())
    row = await _seed("elsewhere", tenant_id=uuid.uuid4())

    result = await _probe("elsewhere" if by == "name" else str(row.id))

    assert result["status"] == "error", result
    assert "no_target" in _error_text(result)
    assert await _stored_fingerprint(row.id) is None


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


async def test_a_read_only_login_is_refused() -> None:
    """MEHO's policy makes a read_only call wait for approval; the op itself refuses it too."""
    _use_connector(_fingerprint())
    row = await _seed("read-only-asks")
    read_only = _operator(role=TenantRole.READ_ONLY)

    asked = await _probe("read-only-asks", read_only)
    assert asked["status"] != "ok", asked

    run = await dispatch(
        operator=read_only,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_PROBE_OP_ID,
        target=None,
        params={"name": "read-only-asks"},
        _approved=True,
    )
    assert run.status == "error", run
    assert "read_only" in str(run.extras.get("exception_message"))
    assert await _stored_fingerprint(row.id) is None


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN])
async def test_operator_and_tenant_admin_may_probe(role: TenantRole) -> None:
    _use_connector(_fingerprint())
    await _seed(f"role-{role.value}")
    result = await _probe(
        f"role-{role.value}", _operator("person-1", kind=PrincipalKind.USER, role=role)
    )
    assert result["status"] == "ok", result


# ---------------------------------------------------------------------------
# Policy on the named target
# ---------------------------------------------------------------------------


async def _grant(agent_sub: str, verdict: PermissionVerdict, target_scope: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=agent_sub,
                op_pattern=TARGETS_PROBE_OP_ID,
                target_scope=target_scope,
                verdict=verdict,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


async def _deny_on_target(agent_sub: str, target_id: uuid.UUID) -> None:
    await _grant(agent_sub, PermissionVerdict.DENY, str(target_id))


@pytest.mark.parametrize("by", ["name", "id"])
async def test_a_deny_grant_for_the_target_stops_the_probe(by: str) -> None:
    """The dispatcher checks policy with no target; the handler checks it on the found target."""
    asked = _use_connector(_fingerprint())
    denied = await _seed("denied-system")
    other = await _seed("other-system")
    agent = _operator("agent-denied")
    await _deny_on_target(agent.sub, denied.id)

    result = await _probe("denied-system" if by == "name" else str(denied.id), agent)

    assert result["status"] == "error", result
    assert "policy_denied" in _error_text(result)
    assert "denied-system" in _error_text(result)
    assert await _stored_fingerprint(denied.id) is None
    assert asked == []

    allowed = await _probe("other-system", agent)
    assert allowed["status"] == "ok", allowed
    assert asked == ["other-system"]
    assert await _stored_fingerprint(other.id) is not None


# ---------------------------------------------------------------------------
# A call that waits for approval
# ---------------------------------------------------------------------------


def _reviewer() -> Operator:
    return _operator("op-reviewer", kind=PrincipalKind.USER)


async def _park(agent: Operator, name: str) -> ApprovalRequest:
    """Dispatch as *agent*, whose grant makes the call wait for approval."""
    parked = await dispatch(
        operator=agent,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_PROBE_OP_ID,
        target=None,
        params={"name": name},
    )
    assert parked.status == "awaiting_approval", parked
    async with get_sessionmaker()() as session:
        return await session.get_one(
            ApprovalRequest, uuid.UUID(parked.extras["approval_request_id"])
        )


async def _approve_and_run(request_id: uuid.UUID) -> OperationResult:
    """Approve as a second person and run the request, as the REST approve routes do."""
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=_reviewer())
        await session.commit()
    result: OperationResult = await resume_dispatch_after_approval(
        operator=_reviewer(), request=request, params=None
    )
    return result


async def test_an_approved_probe_runs_and_stores_the_version() -> None:
    asked = _use_connector(_fingerprint())
    row = await _seed("waits-for-approval")
    agent = _operator("agent-asks")
    await _grant(agent.sub, PermissionVerdict.NEEDS_APPROVAL, "*")

    request = await _park(agent, "waits-for-approval")

    assert asked == []
    assert await _stored_fingerprint(row.id) is None
    preflight = dict(request.proposed_effect)["permission_preflight"]
    assert preflight["requester"]["sub"] == agent.sub
    assert preflight["requester"]["principal_kind"] == "agent"
    assert preflight["requester_denied"] is False
    assert preflight["requester_reason"] is None

    result = await _approve_and_run(request.id)

    assert result.status == "ok", result
    assert result.result["version"] == "2.4.1"
    assert asked == ["waits-for-approval"]
    stored = await _stored_fingerprint(row.id)
    assert stored is not None
    assert stored["version"] == "2.4.1"


@pytest.mark.parametrize("deny_when", ["before the park", "while the request waits"])
async def test_a_target_deny_still_stops_the_approved_probe(deny_when: str) -> None:
    """The reviewer runs the approved call; the requester's policy on the target still counts."""
    asked = _use_connector(_fingerprint())
    row = await _seed("denied-later")
    agent = _operator("agent-denied-later")
    await _grant(agent.sub, PermissionVerdict.NEEDS_APPROVAL, "*")
    if deny_when == "before the park":
        await _deny_on_target(agent.sub, row.id)

    request = await _park(agent, "denied-later")

    preflight = dict(request.proposed_effect)["permission_preflight"]
    assert preflight["requester_denied"] is (deny_when == "before the park")
    if deny_when == "while the request waits":
        await _deny_on_target(agent.sub, row.id)

    result = await _approve_and_run(request.id)

    assert result.status == "error", result
    message = str(result.extras.get("exception_message"))
    assert message.startswith(f"policy_denied: requester {agent.sub!r}: {TARGETS_PROBE_OP_ID}")
    assert "denied-later" in message
    assert asked == []
    assert await _stored_fingerprint(row.id) is None
