# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A call that reads, writes or makes a credential always waits for a person.

No standing grant, no agent grant and no run approval covers it. One rule
says so (``service_grants.never_covered_reason``), for the classes
``credential_read``, ``credential_write`` and ``credential_mint``.

* The guard goes over every registered operation of the three classes,
  taken from the registry, plus an ingested row that stores the class and
  an ingested row whose op id is declared with the class. With a live
  standing grant, a live run approval and an agent grant row that name it,
  each call that waits for a person still waits, and the shadow evaluator
  gives the same answer as the policy gate. A new operation of one of
  these classes is covered with no new test line.
* A step of a multi-step operation that makes a credential waits, also
  when a standing grant and a run approval name the step.
* A credential call at level ``safe`` waits for an agent that holds an
  ``auto-execute`` grant row, and for a service login, by op id and by
  stored class. A credential read that returns no secret value still runs.
* The approval of a parent multi-step run does not cover a step or a
  child call that makes a credential: it waits for its own approval.
* Standing grant create, run approval create and agent grant create refuse
  such an operation, with the plain reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.agents.grant_schemas import AgentGrantCreate, GrantPrincipalKind
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import resolve_op_class
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    EndpointDescriptor,
    PermissionVerdict,
    RunApproval,
    ServicePrincipalGrant,
)
from meho_backplane.op_classes import _DECLARED, declare_op_classes, declared_op_ids
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import CREDENTIAL_WAIT_NOTE, policy_gate
from meho_backplane.operations.composite import (
    approved_run_var,
    composite_depth_var,
    enforce_subop_policy,
)
from meho_backplane.operations.governed_subops import classify_subop_grantability
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    CREDENTIAL_CLASSES,
    GrantValidationError,
    ServicePrincipalGrantService,
    credential_call_waits,
    credential_grant_reason,
    never_covered_reason,
)
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.policy.shadow import evaluate_call
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings

_TENANT = uuid.uuid4()
_SERVICE = "svc-credential-calls"
_AGENT = "agent:credential-calls"
_RUN = "run-credential-calls"
_MINT = "vault.token.create"
_STORED_WRITE = "POST:/v1/example/users/{id}/passphrase"
_DECLARED_WRITE = "PUT:/v1/example/service-keys/{id}"

_FROM_TODAY = {
    PermissionVerdict.AUTO_EXECUTE: Verdict.RUN,
    PermissionVerdict.NEEDS_APPROVAL: Verdict.WAIT,
    PermissionVerdict.DENY: Verdict.DENY,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    get_settings.cache_clear()


@pytest.fixture
def declared_write() -> Iterator[str]:
    """Declare one raw API op id as ``credential_write`` for one test, as a connector would."""
    declare_op_classes(credential_write=(_DECLARED_WRITE,))
    try:
        yield _DECLARED_WRITE
    finally:
        _DECLARED.pop(_DECLARED_WRITE, None)


def _service() -> Operator:
    return Operator(
        sub=_SERVICE,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


def _agent() -> Operator:
    return Operator(
        sub=str(uuid.uuid5(uuid.NAMESPACE_URL, _AGENT)),
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=_AGENT,
    )


def _ingested_row(op_id: str, method: str, op_class: str | None) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="example",
        version="1.0",
        impl_id="example-rest",
        op_id=op_id,
        method=method,
        source_kind="ingested",
        safety_level="caution",
        requires_approval=True,
        op_class=op_class,
        parameter_schema={},
        tenant_id=None,
    )


async def _register_all() -> list[EndpointDescriptor]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await run_typed_op_registrars(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(EndpointDescriptor))).scalars())
        session.expunge_all()
    return rows


def _credential_rows(rows: list[EndpointDescriptor]) -> list[EndpointDescriptor]:
    return [row for row in rows if resolve_op_class(row.op_id, row.op_class) in CREDENTIAL_CLASSES]


async def _name_in_every_grant(rows: list[EndpointDescriptor]) -> None:
    """A live standing grant per row, one live run approval that lists every row,
    and an ``auto-execute`` agent grant row per op id, all written straight to the
    tables, as rows made before this rule would be."""
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        for row in rows:
            session.add(
                ServicePrincipalGrant(
                    tenant_id=_TENANT,
                    principal_sub=_SERVICE,
                    op_id=row.op_id,
                    connector_id=connector_id_of(row),
                    target_id=None,
                    reason="test: every credential call",
                    created_by_sub="test-admin",
                )
            )
        for op_id in sorted({row.op_id for row in rows}):
            session.add(
                AgentPermission(
                    tenant_id=_TENANT,
                    principal_sub=_AGENT,
                    op_pattern=op_id,
                    verdict=PermissionVerdict.AUTO_EXECUTE.value,
                    target_scope="*",
                    created_by_sub="test-admin",
                )
            )
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                work_ref=_RUN,
                entries=[{"connector_id": connector_id_of(r), "op_id": r.op_id} for r in rows],
                reason="test: every credential call",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )


async def _today_and_shadow(
    operator: Operator, row: EndpointDescriptor
) -> tuple[PermissionVerdict, str, Verdict]:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    connector_id = connector_id_of(descriptor)
    token = work_ref_var.set(_RUN)
    try:
        today, reason = await policy_gate(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id,
            decide_only=True,
        )
        shadow = await evaluate_call(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id,
            decide_only=True,
        )
    finally:
        work_ref_var.reset(token)
    return today, reason or "", shadow.verdict


@pytest.mark.asyncio
async def test_the_registry_lists_every_credential_write_and_mint() -> None:
    """Print every registered and declared operation that writes or makes a credential.

    Run with ``-s`` to see the list.
    """
    rows = await _register_all()
    found: dict[str, set[str]] = {"credential_mint": set(), "credential_write": set()}
    for row in rows:
        op_class = resolve_op_class(row.op_id, row.op_class)
        if op_class in found:
            found[op_class].add(row.op_id)
    for op_class, op_ids in found.items():
        op_ids |= declared_op_ids(op_class)
    for op_class, op_ids in found.items():
        print(f"\n{op_class} ({len(op_ids)}):")
        for op_id in sorted(op_ids):
            print(f"  {op_id}")
    assert {"vault.token.create", "secret.handoff.export"} <= found["credential_mint"]
    assert {"vault.kv.put", "k8s.secret.create"} <= found["credential_write"]


@pytest.mark.asyncio
async def test_no_grant_covers_any_registered_credential_call(declared_write: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(_ingested_row(_STORED_WRITE, "POST", "credential_write"))
        session.add(_ingested_row(declared_write, "PUT", None))
    rows = _credential_rows(await _register_all())
    classes = {resolve_op_class(row.op_id, row.op_class) for row in rows}
    assert classes == set(CREDENTIAL_CLASSES)
    assert {_STORED_WRITE, declared_write, _MINT} <= {row.op_id for row in rows}
    await _name_in_every_grant(rows)

    for row in rows:
        never = never_covered_reason(row)
        assert never is not None and never[0] == "credential", row.op_id
        waits = credential_call_waits(row.op_id, row.op_class)
        for operator in (_service(), _agent()):
            today, reason, shadow = await _today_and_shadow(operator, row)
            assert shadow is _FROM_TODAY[today], (row.op_id, operator.principal_kind, reason)
            assert "auto-granted" not in reason, (row.op_id, reason)
            assert "by run approval" not in reason, (row.op_id, reason)
            if waits:
                assert today is PermissionVerdict.NEEDS_APPROVAL, (row.op_id, reason)
            if operator.principal_kind is PrincipalKind.SERVICE and today is not (
                PermissionVerdict.AUTO_EXECUTE
            ):
                assert reason.endswith(CREDENTIAL_WAIT_NOTE), (row.op_id, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_MINT, "rke2.token.rotate", "vault.kv.put"])
async def test_an_agent_auto_execute_row_never_runs_a_safe_credential_call(op_id: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern=op_id,
                verdict=PermissionVerdict.AUTO_EXECUTE.value,
                target_scope="*",
                created_by_sub="test-admin",
            )
        )
    descriptor = EndpointDescriptor(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        source_kind="typed",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={},
    )
    verdict, reason = await policy_gate(
        operator=_agent(), descriptor=descriptor, target=None, connector_id="vault-1.x"
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert reason is not None and CREDENTIAL_WAIT_NOTE in reason
    shadow = await evaluate_call(
        operator=_agent(), descriptor=descriptor, target=None, connector_id="vault-1.x"
    )
    assert shadow.verdict is Verdict.WAIT


@pytest.mark.asyncio
async def test_an_agent_auto_execute_row_still_runs_a_safe_plain_write() -> None:
    """The floor holds only for credential calls: a plain safe write runs at once."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern="vault.*",
                verdict=PermissionVerdict.AUTO_EXECUTE.value,
                target_scope="*",
                created_by_sub="test-admin",
            )
        )
    descriptor = EndpointDescriptor(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.sys.mount.tune",
        source_kind="typed",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={},
    )
    verdict, _ = await policy_gate(
        operator=_agent(), descriptor=descriptor, target=None, connector_id="vault-1.x"
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [_service(), _agent()], ids=["service", "agent"])
async def test_a_step_that_makes_a_credential_waits_under_a_grant_and_a_run_approval(
    operator: Operator,
) -> None:
    step = EndpointDescriptor(product="vault", version="1.x", impl_id="vault", op_id=_MINT)
    await _name_in_every_grant([step])
    assert classify_subop_grantability(_MINT).grantable is False

    token = work_ref_var.set(_RUN)
    try:
        result = await enforce_subop_policy(
            operator=operator,
            connector_id="vault-1.x",
            op_id=_MINT,
            safety_level="caution",
            requires_approval=False,
            target=None,
            params={"policies": ["<policy-name>"]},
        )
    finally:
        work_ref_var.reset(token)
    assert result is not None
    assert result.status == "awaiting_approval", result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["k8s.secret.read_to_ref", "vault.kv.put", _MINT])
async def test_grant_create_refuses_a_credential_call(op_id: str) -> None:
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE,
                op_id=op_id,
                connector_id="vault-1.x",
                reason="unattended build",
            ),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
async def test_grant_create_refuses_a_stored_credential_class() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(_ingested_row(_STORED_WRITE, "POST", "credential_write"))
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE,
                op_id=_STORED_WRITE,
                connector_id="example-rest-1.0",
                reason="unattended build",
            ),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert "it reads, writes or makes a credential" in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["k8s.secret.read_to_ref", "vault.kv.put", _MINT])
async def test_run_approval_create_refuses_a_credential_call(op_id: str) -> None:
    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id="vault-1.x", op_id=op_id)],
                reason="one credential call",
            ),
        )
    assert refused.value.message == f"entries[0]: {credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
async def test_run_approval_create_refuses_a_stored_credential_class() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(_ingested_row(_STORED_WRITE, "POST", "credential_write"))
    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id="example-rest-1.0", op_id=_STORED_WRITE)],
                reason="one credential call",
            ),
        )
    assert "it reads, writes or makes a credential" in refused.value.message


def _agent_grant(op_pattern: str, verdict: PermissionVerdict) -> AgentGrantCreate:
    return AgentGrantCreate(
        principal_sub=_AGENT,
        principal_kind=GrantPrincipalKind.USER_AGENT,
        op_pattern=op_pattern,
        verdict=verdict,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["k8s.secret.read_to_ref", "vault.kv.put", _MINT])
async def test_agent_grant_create_refuses_auto_execute_for_a_credential_call(op_id: str) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(op_id, PermissionVerdict.AUTO_EXECUTE)
        )
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_pattern", "verdict"),
    [
        ("vault.*", PermissionVerdict.AUTO_EXECUTE),
        (_MINT, PermissionVerdict.NEEDS_APPROVAL),
        (_MINT, PermissionVerdict.DENY),
        ("vault.kv.list", PermissionVerdict.AUTO_EXECUTE),
    ],
)
async def test_agent_grant_create_keeps_rows_that_run_no_credential_call_at_once(
    op_pattern: str, verdict: PermissionVerdict
) -> None:
    """A pattern, a row that never runs a call at once, and a read with no secret value."""
    created = await AgentGrantService().grant(
        _TENANT, "op-admin", _agent_grant(op_pattern, verdict)
    )
    assert created.op_pattern == op_pattern


def _safe_row(op_id: str, *, op_class: str | None = None) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="example",
        version="1.0",
        impl_id="example-rest",
        op_id=op_id,
        method="POST",
        source_kind="ingested",
        safety_level="safe",
        requires_approval=False,
        op_class=op_class,
        parameter_schema={},
        tenant_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "op_class"),
    [(_MINT, None), ("vault.kv.put", None), (_STORED_WRITE, "credential_write")],
    ids=["mint-by-op-id", "write-by-op-id", "write-by-stored-class"],
)
async def test_a_service_call_of_a_safe_credential_op_waits(
    op_id: str, op_class: str | None
) -> None:
    descriptor = _safe_row(op_id, op_class=op_class)
    verdict, reason = await policy_gate(
        operator=_service(),
        descriptor=descriptor,
        target=None,
        connector_id="example-rest-1.0",
        decide_only=True,
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert reason is not None and reason.count(CREDENTIAL_WAIT_NOTE) == 1, reason
    shadow = await evaluate_call(
        operator=_service(),
        descriptor=descriptor,
        target=None,
        connector_id="example-rest-1.0",
        decide_only=True,
    )
    assert shadow.verdict is Verdict.WAIT


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["vault.kv.list", "vault.sys.mount.tune"])
async def test_a_service_call_of_a_safe_op_with_no_secret_value_runs(op_id: str) -> None:
    """A credential read marked as one that returns no secret value, and a plain write."""
    descriptor = _safe_row(op_id)
    verdict, reason = await policy_gate(
        operator=_service(),
        descriptor=descriptor,
        target=None,
        connector_id="example-rest-1.0",
        decide_only=True,
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    shadow = await evaluate_call(
        operator=_service(),
        descriptor=descriptor,
        target=None,
        connector_id="example-rest-1.0",
        decide_only=True,
    )
    assert shadow.verdict is Verdict.RUN


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [_service(), _agent()], ids=["service", "agent"])
@pytest.mark.parametrize("safety_level", ["safe", "caution"])
async def test_an_approved_run_does_not_cover_a_step_that_makes_a_credential(
    operator: Operator, safety_level: str
) -> None:
    token = approved_run_var.set(True)
    try:
        result = await enforce_subop_policy(
            operator=operator,
            connector_id="vault-1.x",
            op_id=_MINT,
            safety_level=safety_level,
            requires_approval=False,
            target=None,
            params={"policies": ["<policy-name>"]},
        )
    finally:
        approved_run_var.reset(token)
    assert result is not None
    assert result.status == "awaiting_approval", result.error


@pytest.mark.asyncio
async def test_an_approved_run_still_covers_a_plain_caution_step() -> None:
    token = approved_run_var.set(True)
    try:
        result = await enforce_subop_policy(
            operator=_service(),
            connector_id="vault-1.x",
            op_id="vault.sys.mount.tune",
            safety_level="caution",
            requires_approval=False,
            target=None,
            params={},
        )
    finally:
        approved_run_var.reset(token)
    assert result is None


class _VaultStub(Connector):
    """Lets the resolver pick a connector; the typed handlers never call it."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


class _Target:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = None
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.name = "test-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ran": True}


@pytest.fixture
def vault_stub() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="", impl_id="", cls=_VaultStub)
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault_stub")
@pytest.mark.parametrize(
    ("op_id", "status"), [(_MINT, "awaiting_approval"), ("vault.sys.mount.tune", "ok")]
)
async def test_an_approved_run_does_not_cover_a_child_call_that_makes_a_credential(
    op_id: str, status: str
) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_child_handler,
        summary="A child step.",
        description="A child step.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        embedding_service=embedding,
    )
    approved = approved_run_var.set(True)
    depth = composite_depth_var.set(1)
    try:
        result = await dispatch(
            operator=_service(),
            connector_id="vault-1.x",
            op_id=op_id,
            target=_Target(),
            params={},
        )
    finally:
        composite_depth_var.reset(depth)
        approved_run_var.reset(approved)
    assert result.status == status, result.error
