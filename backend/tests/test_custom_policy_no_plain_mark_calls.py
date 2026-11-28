# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Whole-path runs: with ``CUSTOM_POLICY`` on, the plain mark counts as ``dangerous``.

Each call goes the way a real caller sends it: a signed token on
``POST /api/v1/operations/call`` (a person's token, an agent login, a
service login), or the dispatcher with the login the web console, a hosted
agent run or MEHO's own code builds. The real gate decides, and the test
reads the stored request and the audit rows.

* A ``caution`` operation with the plain mark waits for every caller, with
  the switch on and off. With the switch on its request carries the level
  ``dangerous``. A service login with a standing grant runs it in both.
* An imported ``safe`` operation with the plain mark still waits.
* An Operator can no longer approve another person's request for it; an
  Admin can.
* "Approve each call", "wait when set", a credential call, a change of
  rights, a caller address and a delete keep their wait and their level.
  A change of the policy waits at ``caution``, and a ``destructive``
  operation gets the same answer in both switch states.
* When a platform admin turned approval off, the operation stays
  ``caution`` and runs at once in the web console.
* An admin's override that turns approval on keeps the wait and the level.
* The call contract, a search hit and ``preview_operation`` show the level
  the gate uses.

The connector, the product and the target are made up.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from meho_backplane import op_classes
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    OperationPolicyOverride,
    ServicePrincipalGrant,
)
from meho_backplane.operations import dispatch, grant_approval, register_typed_operation
from meho_backplane.operations._contract import build_operation_contract
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._search import hybrid_search
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.policy import fixed_rules
from meho_backplane.targets.resolver import resolve_target
from tests._hard_to_undo_helpers import (
    RUNS,
    TENANT,
    _StubConnector,
    custom_policy,
    finish,
    seed_target,
    service_client,
    start,
)
from tests._oidc_jwt_helpers import APPROVAL_CLIENT_ID, mint_token
from tests._sandbox_mode_helpers import seed_grant_row

_PRODUCT, _VERSION, _IMPL = "plainmark", "1.0", "plainmark-api"
_CONNECTOR = "plainmark-api-1.0"
_TARGET = "plainmark-widget-01"
_OP = "plainmark.widget.tune"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "off")
    # The marks a test declares are dropped after it.
    monkeypatch.setattr(op_classes, "_PER_CALL_APPROVAL", set(op_classes._PER_CALL_APPROVAL))
    monkeypatch.setattr(op_classes, "_WAIT_WHEN_SET", dict(op_classes._WAIT_WHEN_SET))
    monkeypatch.setattr(op_classes, "_CREDENTIAL_CAPABLE", set(op_classes._CREDENTIAL_CAPABLE))
    # The connector runs imported operations too.
    cls = type(
        "_PlainMarkConnector",
        (_StubConnector,),
        {
            "product": _PRODUCT,
            "version": _VERSION,
            "impl_id": _IMPL,
            "accepts_ingested_operations": True,
        },
    )
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL, cls=cls)
    yield
    finish()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with service_client() as c:
        yield c


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target
    RUNS.append(params)
    return {"done": True}


async def _register(
    op_id: str = _OP,
    *,
    level: str = "caution",
    mark: bool = True,
    schema: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        op_id=op_id,
        handler=_handler,
        summary="Tune a widget.",
        description="Tunes one widget of a made-up product.",
        parameter_schema=schema or {"type": "object"},
        when_to_use=None,
        safety_level=level,  # type: ignore[arg-type]
        requires_approval=mark,
        embedding_service=embedding,
        **extra,
    )


# --- Callers -----------------------------------------------------------------


async def _rest(
    client: tuple[httpx.AsyncClient, Any],
    *,
    kind: str | None,
    sub: str,
    op_id: str = _OP,
    params: dict[str, Any] | None = None,
) -> str:
    """A token login calls the operation over REST; returns the status."""
    ac, key = client
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(TENANT),
        principal_kind=kind,
        extra_claims={"client_id": f"{sub}-client"} if kind else None,
    )
    response = await ac.post(
        "/api/v1/operations/call",
        json={
            "connector_id": _CONNECTOR,
            "op_id": op_id,
            "target": _TARGET,
            "params": params or {},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    status: str = response.json()["status"]
    return status


def _login(sub: str, **fields: Any) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        acr="1",
        auth_time=int(time.time()),
        **fields,
    )


async def _dispatch(
    operator: Operator, op_id: str = _OP, params: dict[str, Any] | None = None
) -> str:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, TENANT, _TARGET)
    result = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=op_id,
        target=target,
        params=params or {},
    )
    return result.status


async def _console(op_id: str = _OP, params: dict[str, Any] | None = None) -> str:
    return await _dispatch(_login("person-console", web_session=True), op_id, params)


async def _hosted_run() -> str:
    with actor_delegation("agent:plain-mark-helper"):
        return await _dispatch(_login("person-hosted", web_session=True))


async def _meho_own_login() -> str:
    return await _dispatch(_login("meho-system", in_code_kind="system"))


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _request_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == "approval.request"))
        return list(rows.scalars())


async def _waits_at_level(status: str, level: str) -> None:
    """The call waits, nothing ran, and its request and audit row carry *level*."""
    assert status == "awaiting_approval"
    assert RUNS == []
    requests = await _requests()
    assert [r.proposed_effect["safety_level"] for r in requests] == [level]
    assert len(await _request_rows()) == 1


# --- Every kind of caller waits, at the new level ---------------------------------


_CALLERS = (
    "person in the console",
    "person's token",
    "agent login",
    "service login",
    "hosted agent run",
    "MEHO's own login",
)


async def _call_as(caller: str, client: tuple[httpx.AsyncClient, Any]) -> str:
    if caller == "person in the console":
        return await _console()
    if caller == "person's token":
        return await _rest(client, kind=None, sub="person-token")
    if caller == "agent login":
        return await _rest(client, kind="agent", sub="agent-plain")
    if caller == "service login":
        return await _rest(client, kind="service", sub="svc-plain")
    if caller == "hosted agent run":
        return await _hosted_run()
    return await _meho_own_login()


@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
@pytest.mark.parametrize("caller", _CALLERS)
async def test_each_caller_waits_and_the_request_shows_the_level(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], caller: str, on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register()
    await seed_target(_TARGET, _PRODUCT, _VERSION)

    await _waits_at_level(await _call_as(caller, client), "dangerous" if on else "caution")


@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_service_login_with_a_grant_runs_it_in_both_states(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register()
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=TENANT,
        principal_sub="svc-granted",
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product=_PRODUCT,
    )

    assert await _rest(client, kind="service", sub="svc-granted") == "ok"
    assert RUNS == [{}]


async def test_an_imported_safe_op_with_the_plain_mark_still_waits(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    op_id = "POST:/api/widgets/{id}/tune"
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL,
                op_id=op_id,
                method="POST",
                path="/api/widgets/{id}/tune",
                source_kind="ingested",
                safety_level="safe",
                requires_approval=True,
                parameter_schema={"type": "object"},
                tags=[],
                tenant_id=None,
            )
        )

    await _waits_at_level(
        await _rest(client, kind=None, sub="person-token", op_id=op_id), "dangerous"
    )


# --- Who may approve ----------------------------------------------------------------


async def _approve(client: tuple[httpx.AsyncClient, Any], request_id: Any, role: TenantRole) -> int:
    ac, key = client
    token = mint_token(
        key,
        sub=f"approver-{role.value}",
        tenant_role=role.value,
        tenant_id=str(TENANT),
        extra_claims={"azp": APPROVAL_CLIENT_ID, "acr": "1", "auth_time": int(time.time())},
    )
    response = await ac.post(
        f"/api/v1/approvals/{request_id}/approve",
        json={"params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    return response.status_code


@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_only_an_admin_approves_another_person_s_request_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register()
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    assert await _rest(client, kind=None, sub="person-requester") == "awaiting_approval"
    (request,) = await _requests()

    operator_status = await _approve(client, request.id, TenantRole.OPERATOR)
    if on:
        assert operator_status == 403
        assert RUNS == []
        assert await _approve(client, request.id, TenantRole.TENANT_ADMIN) == 200
    else:
        assert operator_status == 200
    assert RUNS == [{}]
    async with get_sessionmaker()() as session:
        decisions = await session.execute(
            select(AuditLog).where(AuditLog.path == "approval.decision")
        )
        assert [row.payload["approval_request_id"] for row in decisions.scalars()] == [
            str(request.id)
        ]


# --- Marks with a reason, and the two edits of today ------------------------------------


@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_approve_each_call_behaves_as_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    op_classes.declare_per_call_approval([_OP])
    await _register()
    await seed_target(_TARGET, _PRODUCT, _VERSION)

    await _waits_at_level(await _console(), "caution")


async def test_wait_when_set_still_waits_when_the_param_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_policy(monkeypatch, on=True)
    op_classes.declare_wait_when_set(_OP, ["force"])
    await _register(mark=False)
    await seed_target(_TARGET, _PRODUCT, _VERSION)

    assert await _console(params={}) == "ok"
    assert RUNS == [{}]
    RUNS.clear()
    await _waits_at_level(await _console(params={"force": True}), "caution")


_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"address": {"type": "string", CALLER_ADDRESS_MARKER: True}},
}


@pytest.mark.parametrize(
    ("op_id", "extra"),
    [
        ("plainmark.widget.rotate_key", {"credential": True}),
        ("plainmark.widget.grant_access", {"rights_change": True}),
        ("plainmark.widget.fetch", {"schema": _ADDRESS_SCHEMA}),
        ("plainmark.widget.delete", {}),
        ("plainmark.widget.wipe", {"hard_to_undo": "deletes"}),
    ],
    ids=[
        "credential",
        "change-of-rights",
        "caller-address",
        "delete-name",
        "hard-to-undo",
    ],
)
async def test_a_mark_with_a_reason_keeps_its_wait_and_its_level(
    monkeypatch: pytest.MonkeyPatch, op_id: str, extra: dict[str, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    if extra.pop("credential", False):
        op_classes.declare_credential_capable([op_id])
    if extra.pop("rights_change", False):
        monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {op_id})
    await _register(op_id, **extra)
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    params = {"address": "widget.example.invalid"} if "schema" in extra else {}

    await _waits_at_level(await _console(op_id, params), "caution")


async def _denied(status: str, op_id: str) -> None:
    """The call is refused, nothing ran, no request waits, and the audit row says so."""
    assert status == "denied"
    assert RUNS == []
    assert await _requests() == []
    async with get_sessionmaker()() as session:
        rows = list(
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars()
        )
    assert [r.payload["result_status"] for r in rows] == ["denied"]


@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
async def test_a_policy_change_with_the_plain_mark_waits_at_caution(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    op_id = "plainmark.widget.set_policy"
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {op_id})
    monkeypatch.setattr(fixed_rules, "POLICY_CHANGE_OPS", frozenset({op_id}))
    await _register(op_id)
    await seed_target(_TARGET, _PRODUCT, _VERSION)

    await _waits_at_level(await _console(op_id), "caution")


@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
async def test_a_destructive_operation_with_the_plain_mark_gets_the_same_answer(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """The console Operator is refused in both switch states, and nothing runs."""
    custom_policy(monkeypatch, on=on)
    op_id = "plainmark.widget.erase"
    await _register(op_id, level="destructive")
    await seed_target(_TARGET, _PRODUCT, _VERSION)

    await _denied(await _console(op_id), op_id)


async def _override(*, tenant_id: uuid.UUID | None, requires_approval: bool) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL,
                op_id=_OP,
                requires_approval=requires_approval,
                reason="test: an admin's edit",
                set_by_sub="test-admin",
            )
        )


async def test_a_platform_admin_s_approval_off_keeps_caution_and_runs_in_the_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register()
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    await _override(tenant_id=None, requires_approval=False)

    assert await _console() == "ok"
    assert RUNS == [{}]
    assert await _requests() == []
    contract = await _contract()
    assert (contract.safety_level, contract.requires_approval) == ("caution", False)


async def test_an_admin_s_override_with_approval_on_keeps_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register(mark=False)
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    await _override(tenant_id=TENANT, requires_approval=True)

    await _waits_at_level(await _console(), "caution")


# --- What an agent sees -------------------------------------------------------------


async def _contract() -> Any:
    return await build_operation_contract(
        tenant_id=TENANT,
        connector_id=_CONNECTOR,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        op_id=_OP,
    )


@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_contract_search_and_preview_show_the_level_the_gate_uses(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register()
    await seed_target(_TARGET, _PRODUCT, _VERSION)
    level = "dangerous" if on else "caution"

    assert (await _contract()).safety_level == level
    async with get_sessionmaker()() as session:
        hits = await hybrid_search(
            session,
            tenant_id=TENANT,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL,
            group_id=None,
            query="tune widget",
            limit=5,
        )
    assert [(h.op_id, h.safety_level) for h in hits] == [(_OP, level)]
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, TENANT, _TARGET)
    preview = await preview_dispatch(
        operator=_login("person-token", token_checked=True),
        connector_id=_CONNECTOR,
        op_id=_OP,
        target=target,
        params={},
    )
    assert (preview["status"], preview["policy_verdict"]) == ("ok", "needs-approval"), preview
    assert preview["preview_hash"], preview
    assert preview["safety_level"] == level, preview
