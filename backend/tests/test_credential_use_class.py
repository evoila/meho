# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op class ``credential_use``: a call that carries or sends a secret.

A connector declares ``credential_use`` for an operation whose request
carries a secret, or that sends a stored secret to the target, but that
writes and makes no credential (for example a read inside a VM guest that
logs in with the stored guest password).

* **Hiding** -- the broadcast shows only an aggregate (also with a "full"
  request override or tenant rule), the flight recorder records no body,
  the vendor's error text is not kept, the reducer copies no scalars, and
  a waiting call keeps its params only in the one-time store.
* **Who waits** (whole path through ``dispatch``, ``approve_request`` and
  ``resume_dispatch_after_approval``, no approval mark on the row):

  - a person in the web console runs ``safe`` and ``caution`` at once;
  - a person with a token runs ``safe`` at once, and ``caution`` waits;
  - an agent waits, also with an ``auto-execute`` grant;
  - a service login waits, with a standing grant and with a run approval.

  After an Admin approves in the web console, the call runs once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload, resolve_op_class
from meho_backplane.broadcast.overrides import compute_effective_broadcast_detail
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    RunApproval,
    ServicePrincipalGrant,
    Target,
    Tenant,
)
from meho_backplane.op_classes import (
    _DECLARED,
    CREDENTIAL_CHANGE_OP_CLASSES,
    CREDENTIAL_OP_CLASSES,
    DECLARABLE_OP_CLASSES,
    SECRET_REQUEST_OP_CLASSES,
    declare_op_classes,
)
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._vendor_text import sends_secret
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.jsonflux_reducer import _is_credential_class
from meho_backplane.operations.service_grants import (
    credential_call_waits,
    credential_grant_reason,
)
from meho_backplane.redaction.flight_recorder.families import classify_body_exclusion
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c05e")
_PRODUCT = "creduse"
_CONNECTOR = "creduse-1.x"
_SAFE_OP = "creduse.guest.process.list"
_CAUTION_OP = "creduse.guest.file.read"
_SERVICE = "svc:creduse"
_AGENT = "agent:creduse"
_RUN = "run-creduse"
_SECRET = "made-up-guest-value-7"
_PARAMS: dict[str, Any] = {"path": "/tmp/notes.txt", "guest_password": _SECRET}

_RUNS: list[str] = []


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _declared() -> Iterator[None]:
    """Declare the two stand-in operations as ``credential_use``, as a connector would."""
    declare_op_classes(credential_use=(_SAFE_OP, _CAUTION_OP))
    try:
        yield
    finally:
        _DECLARED.pop(_SAFE_OP, None)
        _DECLARED.pop(_CAUTION_OP, None)


# ---------------------------------------------------------------------------
# The class and its sets
# ---------------------------------------------------------------------------


def test_the_class_is_declarable_and_in_the_hiding_sets_only() -> None:
    assert "credential_use" in DECLARABLE_OP_CLASSES
    assert "credential_use" in CREDENTIAL_OP_CLASSES
    assert "credential_use" in SECRET_REQUEST_OP_CLASSES
    assert "credential_use" not in CREDENTIAL_CHANGE_OP_CLASSES


def test_a_second_different_class_for_the_same_op_id_fails() -> None:
    with pytest.raises(ValueError, match="already declared"):
        declare_op_classes(credential_write=(_SAFE_OP,))


def test_the_declared_class_wins_and_a_route_class_never_replaces_it() -> None:
    assert classify_op(_SAFE_OP) == "credential_use"
    assert resolve_op_class(_SAFE_OP, "read") == "credential_use"


# ---------------------------------------------------------------------------
# Hiding
# ---------------------------------------------------------------------------


def test_the_broadcast_shows_only_an_aggregate() -> None:
    payload = redact_payload("credential_use", dict(_PARAMS), "ok")

    assert payload == {"op_class": "credential_use", "result_status": "ok"}


@pytest.mark.parametrize("op_id", [_SAFE_OP, _CAUTION_OP])
async def test_a_full_request_override_keeps_the_aggregate(op_id: str) -> None:
    op_class, detail, _origin = await compute_effective_broadcast_detail(
        op_id=op_id,
        tenant_id=_TENANT,
        raw_params=dict(_PARAMS),
        request_override="full",
    )

    assert (op_class, detail) == ("credential_use", "aggregate")


def test_the_flight_recorder_records_no_body() -> None:
    exclusion = classify_body_exclusion(_CAUTION_OP)

    assert exclusion.excluded is True
    assert exclusion.family == "secret-bearing"


def test_the_vendor_error_text_is_not_kept_and_the_reducer_copies_no_scalars() -> None:
    assert sends_secret(_CAUTION_OP, ()) is True
    assert _is_credential_class({"op_id": _CAUTION_OP}) is True


def test_no_grant_covers_the_call() -> None:
    assert credential_call_waits(_SAFE_OP) is True
    assert credential_grant_reason(_SAFE_OP) is not None


# ---------------------------------------------------------------------------
# Who waits: the whole path
# ---------------------------------------------------------------------------


class _UseConnector(Connector):
    """Lets the resolver pick a connector; the typed handlers do the work."""

    product = _PRODUCT
    version = "1.x"
    impl_id = _PRODUCT

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        raise NotImplementedError


async def _safe_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _RUNS.append(_SAFE_OP)
    return {"read": True}


async def _caution_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _RUNS.append(_CAUTION_OP)
    return {"read": True}


@pytest_asyncio.fixture(autouse=True)
async def _registered(_settings_env: None) -> AsyncIterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_UseConnector)
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    for op_id, level, handler in (
        (_SAFE_OP, "safe", _safe_handler),
        (_CAUTION_OP, "caution", _caution_handler),
    ):
        await register_typed_operation(
            product=_PRODUCT,
            version="1.x",
            impl_id=_PRODUCT,
            op_id=op_id,
            handler=handler,
            summary="A read inside a guest that logs in with a stored password.",
            description="Stand-in operation.",
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "guest_password": {"type": "string"},
                },
            },
            when_to_use=None,
            safety_level=level,
            requires_approval=False,
            embedding_service=embedding,
        )
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT, slug="tenant-creduse", name="Tenant creduse"))
    yield
    reset_dispatcher_caches()
    clear_registry()


async def _target() -> Target:
    """A stored target, so an approved call can load it again when it resumes."""
    async with get_sessionmaker()() as session:
        target = Target(
            tenant_id=_TENANT,
            name="creduse-target",
            aliases=[],
            product=_PRODUCT,
            host="creduse.example",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "1.0"},
            notes="stand-in target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _operator(
    sub: str,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.OPERATOR,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
    )


async def _call(operator: Operator, op_id: str) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=op_id,
        target=await _target(),
        params=dict(_PARAMS),
    )


async def _web_person(op_id: str) -> Any:
    return await _call(_operator("person-web", web_session=True), op_id)


async def _token_person(op_id: str) -> Any:
    return await _call(_operator("person-token"), op_id)


async def _agent(op_id: str) -> Any:
    return await _call(_operator(_AGENT, kind=PrincipalKind.AGENT), op_id)


async def _agent_with_grant(op_id: str) -> Any:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern=op_id,
                target_scope="*",
                verdict="auto-execute",
                created_by_sub="admin-person",
            )
        )
    return await _agent(op_id)


def _service() -> Operator:
    return _operator(_SERVICE, kind=PrincipalKind.SERVICE, role=TenantRole.TENANT_ADMIN)


async def _service_with_grant(op_id: str) -> Any:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                op_id=op_id,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="test: a standing grant made earlier",
                created_by_sub="admin-person",
            )
        )
    return await _call(_service(), op_id)


async def _service_in_approved_run(op_id: str) -> Any:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                work_ref=_RUN,
                entries=[{"connector_id": _CONNECTOR, "op_id": op_id}],
                reason="test: a run approval made earlier",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="admin-person",
            )
        )
    token = work_ref_var.set(_RUN)
    try:
        return await _call(_service(), op_id)
    finally:
        work_ref_var.reset(token)


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.scalars())


@pytest.mark.parametrize(
    ("call", "op_id"),
    [
        (_web_person, _SAFE_OP),
        (_web_person, _CAUTION_OP),
        (_token_person, _SAFE_OP),
    ],
    ids=["web-person-safe", "web-person-caution", "token-person-safe"],
)
async def test_a_person_runs_the_call_at_once_by_its_risk_level(call: Any, op_id: str) -> None:
    result = await call(op_id)

    assert result.status == "ok", result
    assert [op_id] == _RUNS
    assert await _requests() == []
    rows = await _audit_rows(op_id)
    assert rows, "the call writes an audit row"
    assert {row.payload["op_class"] for row in rows} == {"credential_use"}
    assert _SECRET not in repr([row.payload for row in rows])


_WAITING_CALLS = [
    (_token_person, _CAUTION_OP),
    (_agent, _SAFE_OP),
    (_agent, _CAUTION_OP),
    (_agent_with_grant, _SAFE_OP),
    (_agent_with_grant, _CAUTION_OP),
    (_service_with_grant, _SAFE_OP),
    (_service_with_grant, _CAUTION_OP),
    (_service_in_approved_run, _SAFE_OP),
    (_service_in_approved_run, _CAUTION_OP),
]
_WAITING_IDS = [
    "token-person-caution",
    "agent-safe",
    "agent-caution",
    "agent-grant-safe",
    "agent-grant-caution",
    "service-standing-grant-safe",
    "service-standing-grant-caution",
    "service-run-approval-safe",
    "service-run-approval-caution",
]


@pytest.mark.parametrize(("call", "op_id"), _WAITING_CALLS, ids=_WAITING_IDS)
async def test_the_call_waits_keeps_its_params_hidden_and_runs_once_after_approval(
    call: Any, op_id: str
) -> None:
    result = await call(op_id)

    assert result.status == "awaiting_approval", result
    assert _RUNS == []
    (request,) = await _requests()
    assert request.op_id == op_id
    assert request.params is None
    assert _SECRET not in repr(request.proposed_effect)

    approver = _operator("admin-web", role=TenantRole.TENANT_ADMIN, web_session=True)
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request.id, operator=approver)
        await session.commit()
    ran = await resume_dispatch_after_approval(operator=approver, request=approved)

    assert ran.status == "ok", ran
    assert [op_id] == _RUNS
    rows = await _audit_rows(op_id)
    assert _SECRET not in repr([row.payload for row in rows])
