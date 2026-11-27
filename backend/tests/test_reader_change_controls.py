# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A caller with the Operator or Admin role keeps every answer it has.

The rule for a Reader who asks for a change does not touch the other
roles. Each case runs one caller with one kind of change through one way
in, and checks the status, the code, the number of stored approval
requests and whether the stand-in ran. The expected values are the ones
the same cases give on a MEHO without the Reader rule.

* Callers: a person's token with the Operator role, a person's token with
  the Admin role, an agent login with a grant, and a service login with
  the Operator role. The person in the web console is in
  ``test_reader_change_controls_console.py``.
* Kinds (stand-in operations that count their runs): a ``caution`` write,
  a ``dangerous`` write, a change of rights, a credential write, a
  ``caution`` multi-step operation and a delete without a preview.
* Ways in: the real ``dispatch``, the REST call route and the MCP tool
  ``call_operation``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Target
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.op_classes import declare_op_classes
from meho_backplane.operations import dispatch, register_typed_operation
from meho_backplane.operations.grant_approval import register_rights_change_op
from tests import _composite_wait_audit_helpers as h
from tests._people_behind_helpers import give_known_person
from tests.test_agent_grant_required import _AGENT, _TENANT, _grant, _operator, _seed_vault_target
from tests.test_approved_run_rights import (
    _required_settings_env,
    _reset_state,
    _token,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _NoOpVaultConnector

__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]

_CONNECTOR = "vault-1.x"
_TARGET = "vault-grant-test"
_PARAMS: dict[str, Any] = {"path": "app/config"}

#: kind -> (op_id, risk level). The multi-step operation is a composite.
KINDS: dict[str, tuple[str, str]] = {
    "write_caution": ("vault.control.write_caution", "caution"),
    "write_dangerous": ("vault.control.write_dangerous", "dangerous"),
    "rights_change": ("vault.control.rights_change", "safe"),
    "credential_write": ("vault.control.credential_write", "safe"),
    "multi_step": ("vault.control.multi_step", "caution"),
    "delete_no_preview": ("vault.control.delete", "destructive"),
}

#: Every run of a stand-in operation.
RUNS: list[str] = []


async def run_stand_in(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """The stand-in of every typed kind; it counts its run."""
    del operator, target, params
    RUNS.append("typed")
    return {"done": True}


async def multi_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """The stand-in multi-step operation; it counts its run."""
    del operator, target, params
    RUNS.append("multi_step")
    return {"done": True}


@pytest.fixture
async def stand_ins(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    """One stand-in operation per kind, and the target they run on."""
    RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    declare_op_classes(credential_write=[KINDS["credential_write"][0]])
    register_rights_change_op(KINDS["rights_change"][0])
    for kind, (op_id, level) in KINDS.items():
        if kind == "multi_step":
            continue
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=run_stand_in,
            summary="Stand-in op.",
            description="Counts its runs.",
            parameter_schema={"type": "object"},
            safety_level=level,
            requires_approval=level == "destructive",
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    async with get_sessionmaker()() as session:
        await h.register_composite(
            session,
            KINDS["multi_step"][0],
            "multi_step_composite",
            safety_level="caution",
            module=__name__,
        )
    await _seed_vault_target()
    yield
    RUNS.clear()


def _caller(name: str) -> Operator:
    if name == "agent":
        return _operator()
    if name == "service":
        return Operator(
            sub="service-1",
            name="Service",
            email=None,
            raw_jwt="<test-jwt>",
            tenant_id=_TENANT,
            tenant_role=TenantRole.OPERATOR,
            principal_kind=PrincipalKind.SERVICE,
            client_id="svc-client",
        )
    role = TenantRole.TENANT_ADMIN if name == "person_admin" else TenantRole.OPERATOR
    return Operator(
        sub=f"{name}-1",
        name="Person",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
    )


def _headers(key: Any, name: str) -> dict[str, str]:
    if name == "agent":
        return _token(key, _operator().sub, kind="agent", client_id=_AGENT)
    if name == "service":
        return _token(key, "service-1", kind="service", client_id="svc-client")
    role = TenantRole.TENANT_ADMIN if name == "person_admin" else TenantRole.OPERATOR
    return _token(key, f"{name}-1", role=role)


async def _approval_requests() -> int:
    async with get_sessionmaker()() as session:
        query = select(func.count()).select_from(ApprovalRequest)
        return int((await session.execute(query)).scalar_one())


def _code(extras: dict[str, Any]) -> str | None:
    code = extras.get("error_code") or extras.get("reason_code")
    return str(code) if code else None


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    from tests._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks

    key = make_rsa_keypair("kid-reader-controls")
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        ) as ac:
            yield ac, key


@pytest.fixture
def mcp_as() -> Iterator[Callable[[Operator], None]]:
    """Bind the MCP login of the next calls; the override is removed at the end."""

    def _bind(operator: Operator) -> None:
        async def _verify() -> Operator:
            return operator

        app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify

    yield _bind
    app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _arguments(op_id: str) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR, "op_id": op_id, "target": _TARGET, "params": _PARAMS}


async def _via_dispatch(name: str, op_id: str) -> tuple[str, str | None]:
    async with get_sessionmaker()() as session:
        target = (await session.execute(select(Target).where(Target.name == _TARGET))).scalar_one()
        session.expunge(target)
    result = await dispatch(
        operator=_caller(name),
        connector_id=_CONNECTOR,
        op_id=op_id,
        target=target,
        params=_PARAMS,
    )
    return result.status, _code(result.extras)


async def _via_rest(
    ac: httpx.AsyncClient, key: Any, name: str, op_id: str
) -> tuple[str, str | None]:
    response = await ac.post(
        "/api/v1/operations/call", json=_arguments(op_id), headers=_headers(key, name)
    )
    if response.status_code != 200:
        return f"http {response.status_code}", str(response.json().get("detail"))
    body = response.json()
    return body["status"], _code(body.get("extras") or {})


async def _via_mcp(ac: httpx.AsyncClient, op_id: str) -> tuple[str, str | None]:
    response = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "call_operation", "arguments": _arguments(op_id)},
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    if "error" in body:
        return "mcp error", str(body["error"]["message"]).split(":")[0]
    envelope = json.loads(body["result"]["content"][0]["text"])
    return envelope["status"], _code(envelope.get("extras") or {})


_CALLERS = ("person_operator", "person_admin", "agent", "service")
_WAYS = ("dispatch", "rest", "mcp")

_WAITS = ("awaiting_approval", "awaiting_approval", 1, False)
_RUNS_AT_ONCE = ("ok", None, 0, True)
_NO_PREVIEW = ("denied", "preview_binding_required", 0, False)

#: (caller, kind) -> (status, code, stored approval requests, ran), the same
#: on every way in. These are the answers without the Reader rule.
EXPECTED: dict[tuple[str, str], tuple[str, str | None, int, bool]] = {
    ("person_operator", "write_caution"): _WAITS,
    ("person_operator", "write_dangerous"): _WAITS,
    ("person_operator", "rights_change"): _WAITS,
    ("person_operator", "credential_write"): _RUNS_AT_ONCE,
    ("person_operator", "multi_step"): _WAITS,
    ("person_operator", "delete_no_preview"): _NO_PREVIEW,
    ("person_admin", "write_caution"): _WAITS,
    ("person_admin", "write_dangerous"): _WAITS,
    ("person_admin", "rights_change"): _WAITS,
    ("person_admin", "credential_write"): _RUNS_AT_ONCE,
    ("person_admin", "multi_step"): _WAITS,
    ("person_admin", "delete_no_preview"): _NO_PREVIEW,
    ("agent", "write_caution"): _WAITS,
    ("agent", "write_dangerous"): _WAITS,
    ("agent", "rights_change"): _WAITS,
    ("agent", "credential_write"): _WAITS,
    ("agent", "multi_step"): _WAITS,
    ("agent", "delete_no_preview"): _NO_PREVIEW,
    ("service", "write_caution"): _WAITS,
    ("service", "write_dangerous"): _WAITS,
    ("service", "rights_change"): _WAITS,
    ("service", "credential_write"): _WAITS,
    ("service", "multi_step"): _WAITS,
    ("service", "delete_no_preview"): _NO_PREVIEW,
}


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("way", _WAYS)
@pytest.mark.usefixtures("stand_ins")
async def test_an_operator_or_admin_caller_keeps_its_answer(
    client: tuple[httpx.AsyncClient, Any],
    mcp_as: Callable[[Operator], None],
    way: str,
    caller: str,
    kind: str,
) -> None:
    ac, key = client
    op_id = KINDS[kind][0]
    await give_known_person(_caller(caller))
    if caller == "agent":
        await _grant(op_id, "needs-approval")

    if way == "dispatch":
        status, code = await _via_dispatch(caller, op_id)
    elif way == "rest":
        status, code = await _via_rest(ac, key, caller, op_id)
    else:
        mcp_as(_caller(caller))
        status, code = await _via_mcp(ac, op_id)

    outcome = (status, code, await _approval_requests(), bool(RUNS))
    assert outcome == EXPECTED[(caller, kind)], (way, outcome)
