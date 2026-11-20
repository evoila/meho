# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A run approval never covers an operation that reads a credential.

* Every registered operation of the ``credential_read`` class, taken from
  the registry: with a live run approval that lists it, the call gets the
  same answer as with no run approval at all. A read that shows a secret
  waits for a person. The run approval is written straight to the table,
  as a row approved before an upgrade would be.
* The policy gate and the shadow evaluator give the same answer for these
  calls, and for every registered operation under a live standing grant and
  a live run approval that list them all. Neither clears an operation that
  :func:`never_covered_reason` names.
* A request that lists such an operation is refused. Show and approve of
  a request made before this rule name the entry in ``entries_that_wait``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    PermissionVerdict,
    RunApproval,
    ServicePrincipalGrant,
)
from meho_backplane.op_classes import is_root_shell
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import CREDENTIAL_WAIT_NOTE, policy_gate
from meho_backplane.operations.credential_read import (
    has_credential_read_floor,
    raise_descriptor_for_reveal,
)
from meho_backplane.operations.role_floors import requester_check_denial
from meho_backplane.operations.run_approval_gate import _never_covered
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grants import (
    credential_grant_reason,
    credential_refusal,
    never_covered_reason,
)
from meho_backplane.operations.typed_register import (
    clear_typed_op_snapshot,
    run_typed_op_registrars,
)
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.policy.shadow import evaluate_call
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings

_TENANT = uuid.uuid4()
_PRINCIPAL = "svc-run-credential-reads"
_RUN = "run-credential-reads"
_K8S_READ = "k8s.secret.read_to_ref"
_ONLY_GRANT = "svc-only-a-standing-grant"
_ONLY_RUN_APPROVAL = "svc-only-a-run-approval"

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


def _operator(
    sub: str = _PRINCIPAL, kind: PrincipalKind = PrincipalKind.SERVICE, **extra: bool
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN if extra else TenantRole.OPERATOR,
        principal_kind=kind,
        **extra,
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


def _reads_a_credential(row: EndpointDescriptor) -> bool:
    return credential_grant_reason(row.op_id, row.op_class) is not None


async def _approved_before_upgrade(
    rows: list[EndpointDescriptor], principal_sub: str = _PRINCIPAL
) -> uuid.UUID:
    """A live run approval that lists *rows*, written straight to the table."""
    approval_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=approval_id,
                tenant_id=_TENANT,
                principal_sub=principal_sub,
                work_ref=_RUN,
                entries=[{"connector_id": connector_id_of(r), "op_id": r.op_id} for r in rows],
                reason="test: credential reads and one write",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
    return approval_id


async def _gate(
    row: EndpointDescriptor, work_ref: str | None, *, reveal_secret: bool = False
) -> tuple[PermissionVerdict, str]:
    """The answer of the policy gate, as the dispatcher asks it for one call."""
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    raise_descriptor_for_reveal(descriptor, reveal_secret=reveal_secret)
    token = work_ref_var.set(work_ref)
    try:
        verdict, reason = await policy_gate(
            operator=_operator(),
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
        )
    finally:
        work_ref_var.reset(token)
    return verdict, reason or ""


async def _use_rows(approval_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.path == "approval.decision"))
        ).all()
    return [r for r in rows if r.payload.get("run_approval_id") == str(approval_id)]


def _a_covered_write(rows: list[EndpointDescriptor]) -> EndpointDescriptor:
    """A ``caution`` operation that waits for approval and that a run approval covers."""
    for row in sorted(rows, key=lambda r: (r.impl_id, r.op_id)):
        if row.safety_level == "caution" and row.requires_approval and not _never_covered(row):
            return row
    raise AssertionError("the registry has no caution operation that a run approval covers")


@pytest.mark.asyncio
async def test_a_run_approval_covers_no_registered_credential_read() -> None:
    rows = await _register_all()
    reads = [row for row in rows if _reads_a_credential(row)]
    assert _K8S_READ in {row.op_id for row in reads}
    assert len(reads) > 5, "the registrars registered too few credential reads"

    write = _a_covered_write(rows)
    calls = [(row, reveal) for row in reads for reveal in (False, True)]
    without = {
        (row.op_id, reveal): await _gate(row, None, reveal_secret=reveal) for row, reveal in calls
    }
    approval_id = await _approved_before_upgrade([*reads, write])

    for row, reveal in calls:
        verdict, reason = await _gate(row, _RUN, reveal_secret=reveal)
        assert (verdict, reason) == without[(row.op_id, reveal)], (row.op_id, reveal)
        assert "by run approval" not in reason, (row.op_id, reveal)
        if has_credential_read_floor(row.op_id, reveal_secret=reveal, stored_op_class=row.op_class):
            assert verdict is PermissionVerdict.NEEDS_APPROVAL, (row.op_id, reveal)
        if verdict is PermissionVerdict.NEEDS_APPROVAL:
            assert reason.endswith(CREDENTIAL_WAIT_NOTE), (row.op_id, reveal, reason)
    assert await _use_rows(approval_id) == []
    assert (await _gate(next(r for r in reads if r.op_id == _K8S_READ), _RUN))[0] is (
        PermissionVerdict.NEEDS_APPROVAL
    )

    # The same run approval is live: it covers the write on its list.
    without_write = await _gate(write, None)

    assert without_write[0] is PermissionVerdict.NEEDS_APPROVAL
    verdict, reason = await _gate(write, _RUN)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, (write.op_id, reason)
    assert f"run approval {approval_id}" in reason
    assert len(await _use_rows(approval_id)) == 1


@pytest.mark.asyncio
async def test_gate_and_shadow_evaluator_agree_on_every_credential_read() -> None:
    rows = await _register_all()
    reads = [row for row in rows if _reads_a_credential(row)]
    await _approved_before_upgrade(reads)

    token = work_ref_var.set(_RUN)
    try:
        for row in reads:
            descriptor = await lookup_descriptor(
                tenant_id=_TENANT,
                product=row.product,
                version=row.version,
                impl_id=row.impl_id,
                op_id=row.op_id,
            )
            assert descriptor is not None, row.op_id
            connector_id = connector_id_of(descriptor)
            today, _ = await policy_gate(
                operator=_operator(),
                descriptor=descriptor,
                target=None,
                connector_id=connector_id,
                decide_only=True,
            )
            shadow = await evaluate_call(
                operator=_operator(),
                descriptor=descriptor,
                target=None,
                connector_id=connector_id,
                decide_only=True,
            )
            assert shadow.verdict is _FROM_TODAY[today], (row.op_id, today, shadow.trace)
    finally:
        work_ref_var.reset(token)


@pytest.mark.asyncio
async def test_a_stored_credential_read_class_also_waits() -> None:
    """An ingested operation whose row stores the class waits too; its op id says nothing."""
    op_id = "GET:/v1/example/tokens/{name}"
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                product="example",
                version="1.0",
                impl_id="example-rest",
                op_id=op_id,
                method="GET",
                source_kind="ingested",
                safety_level="caution",
                requires_approval=True,
                op_class="credential_read",
                parameter_schema={},
                tenant_id=None,
            )
        )
    row = EndpointDescriptor(product="example", version="1.0", impl_id="example-rest", op_id=op_id)
    assert credential_grant_reason(op_id) is None
    approval_id = await _approved_before_upgrade([row])

    verdict, reason = await _gate(row, _RUN)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL
    assert reason.endswith(CREDENTIAL_WAIT_NOTE)
    assert await _use_rows(approval_id) == []

    read = await RunApprovalService().get(_TENANT, approval_id)
    assert [note.op_id for note in read.entries_that_wait or []] == [op_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_K8S_READ, "vault.kv.read", "vault.kv.list"])
async def test_a_request_that_lists_a_credential_read_is_refused(op_id: str) -> None:
    rows = await _register_all()
    secret_read = next(row for row in rows if row.op_id == op_id)
    write = _a_covered_write(rows)

    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _operator(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[
                    RunApprovalEntry(connector_id=connector_id_of(write), op_id=write.op_id),
                    RunApprovalEntry(connector_id=connector_id_of(secret_read), op_id=op_id),
                ],
                reason="read one secret into a reference",
            ),
        )
    assert refused.value.message == f"entries[1]: {credential_refusal(op_id)}"


async def _pending_before_upgrade(rows: list[EndpointDescriptor]) -> uuid.UUID:
    """A pending request that lists *rows*, written straight to the table."""
    approval_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=approval_id,
                tenant_id=_TENANT,
                principal_sub=_PRINCIPAL,
                work_ref=_RUN,
                entries=[{"connector_id": connector_id_of(r), "op_id": r.op_id} for r in rows],
                reason="test: asked before the upgrade",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    return approval_id


@pytest.mark.asyncio
async def test_show_and_approve_name_a_credential_entry_of_an_older_request() -> None:
    rows = await _register_all()
    by_id = {row.op_id: row for row in rows}
    listed = [
        _a_covered_write(rows),
        by_id[_K8S_READ],
        by_id["vault.kv.read"],
        by_id["vault.kv.list"],
        by_id["vault.token.create"],
    ]
    service = RunApprovalService()
    approval_id = await _pending_before_upgrade(listed)

    shown = await service.get(_TENANT, approval_id)
    notes = shown.entries_that_wait or []
    assert [(n.entry, n.op_id) for n in notes] == [
        (1, _K8S_READ),
        (2, "vault.kv.read"),
        (4, "vault.token.create"),
    ]
    assert notes[0].reason == (
        f"op '{_K8S_READ}' reads, writes or makes a credential. A run approval never "
        "covers it: each call waits for a person"
    )
    assert notes[1].reason == (
        "op 'vault.kv.read' reads a credential. A run approval never covers it: "
        "each call with reveal_secret waits for a person"
    )
    approver = _operator("person-approver", PrincipalKind.USER, approver=True, self_approver=True)
    approved = await service.approve(approver, approval_id)
    assert approved.status == "approved"
    assert approved.entries_that_wait == notes
    (in_list,) = await service.list_(_TENANT)
    assert in_list.entries_that_wait is None


@pytest.mark.asyncio
async def test_a_request_without_a_credential_entry_has_no_entry_that_waits() -> None:
    rows = await _register_all()
    write = _a_covered_write(rows)
    read = await RunApprovalService().request(
        _operator(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=connector_id_of(write), op_id=write.op_id)],
            reason="one write",
        ),
    )
    assert read.entries_that_wait == []


def _ingested_row(op_id: str, **fields: object) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="example",
        version="1.0",
        impl_id="example-rest",
        op_id=op_id,
        method="GET",
        source_kind="ingested",
        safety_level="caution",
        requires_approval=True,
        tenant_id=None,
        **fields,
    )


async def _seed_grants(rows: list[EndpointDescriptor], principal_sub: str = _PRINCIPAL) -> None:
    """A live standing grant per row."""
    async with get_sessionmaker()() as session, session.begin():
        for row in rows:
            session.add(
                ServicePrincipalGrant(
                    tenant_id=_TENANT,
                    principal_sub=principal_sub,
                    op_id=row.op_id,
                    connector_id=connector_id_of(row),
                    target_id=None,
                    reason="test: every operation",
                    created_by_sub="test-admin",
                )
            )


async def _seed_grant_and_run_approval(rows: list[EndpointDescriptor]) -> None:
    """A live standing grant per row, and one live run approval that lists every row."""
    await _seed_grants(rows)
    await _approved_before_upgrade(rows)


async def _seed_ingested_rows() -> None:
    """One row that stores the ``credential_read`` class, one with a caller-address field."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            _ingested_row(
                "GET:/v1/example/tokens/{name}", op_class="credential_read", parameter_schema={}
            )
        )
        session.add(
            _ingested_row(
                "GET:/v1/example/probe",
                parameter_schema={
                    "type": "object",
                    "properties": {"host": {"type": "string", "x-meho-caller-address": True}},
                },
            )
        )


@pytest.fixture(params=[False, True], ids=["classification-off", "classification-on"])
def classification(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[bool]:
    """The ``POLICY_CLASSIFICATION`` switch, set by the test and not by the environment.

    The policy operations register only with the switch on, and the suite
    keeps the registered rows of its first run. So the kept rows are
    dropped before and after the test.
    """
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true" if request.param else "false")
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    yield bool(request.param)
    clear_typed_op_snapshot()


async def _decide(row: EndpointDescriptor, principal_sub: str) -> tuple[PermissionVerdict, str]:
    """The answer of the policy gate for one call of *row* under the run :data:`_RUN`."""
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    token = work_ref_var.set(_RUN)
    try:
        verdict, reason = await policy_gate(
            operator=_operator(principal_sub),
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
            decide_only=True,
        )
    finally:
        work_ref_var.reset(token)
    return verdict, reason or ""


@pytest.mark.asyncio
async def test_no_grant_and_no_run_approval_clears_an_operation_that_is_never_covered(
    classification: bool,
) -> None:
    """Over every operation: the gate equals the shadow evaluator, and clears no never-covered op.

    Two ingested rows join the registry: one that stores the
    ``credential_read`` class and one with a caller-address field. A run
    approval also never covers an operation that runs a command as root.

    The one known difference: the gate refuses a call that the operation's
    own requester check refuses for a service login (``tools.sign_out``,
    plus ``policy.apply`` and ``policy.classify``, registered only with the
    classification switch on). The shadow evaluator has no requester check.
    """
    await _seed_ingested_rows()
    rows = await _register_all()
    await _seed_grant_and_run_approval(rows)
    kinds_seen: set[str] = set()
    requester_denied: set[str] = set()

    token = work_ref_var.set(_RUN)
    try:
        for row in rows:
            descriptor = await lookup_descriptor(
                tenant_id=_TENANT,
                product=row.product,
                version=row.version,
                impl_id=row.impl_id,
                op_id=row.op_id,
            )
            assert descriptor is not None, row.op_id
            connector_id = connector_id_of(descriptor)
            today, reason = await policy_gate(
                operator=_operator(),
                descriptor=descriptor,
                target=None,
                connector_id=connector_id,
                decide_only=True,
            )
            shadow = await evaluate_call(
                operator=_operator(),
                descriptor=descriptor,
                target=None,
                connector_id=connector_id,
                decide_only=True,
            )
            denial = requester_check_denial(descriptor.op_id, _operator(), {})
            if denial is not None:
                assert (today, reason) == (PermissionVerdict.DENY, denial), row.op_id
                requester_denied.add(row.op_id)
            else:
                assert shadow.verdict is _FROM_TODAY[today], (row.op_id, today, shadow.trace)
            never = never_covered_reason(descriptor)
            if never is not None:
                kinds_seen.add(never[0])
                assert "auto-granted" not in (reason or ""), (row.op_id, reason)
            if never is not None or is_root_shell(descriptor.op_id):
                assert "by run approval" not in (reason or ""), (row.op_id, reason)
    finally:
        work_ref_var.reset(token)
    assert {"credential", "caller_address", "delete_shaped"} <= kinds_seen
    expected_denied = {"tools.sign_out"}
    if classification:
        expected_denied |= {"policy.apply", "policy.classify"}
    assert requester_denied == expected_denied


@pytest.mark.asyncio
async def test_a_standing_grant_and_a_run_approval_cover_the_same_operations(
    classification: bool,
) -> None:
    """One login holds only standing grants, another only a run approval, for every operation.

    Each operation is covered under both or under neither. The one
    exception: a standing grant may cover an operation that runs a command
    as root on the host, a run approval never does. So a check of what is
    never covered that is added to only one of the two paths fails here.
    """
    del classification
    await _seed_ingested_rows()
    rows = await _register_all()
    await _seed_grants(rows, _ONLY_GRANT)
    await _approved_before_upgrade(rows, _ONLY_RUN_APPROVAL)
    granted_count = 0

    for row in rows:
        _, grant_reason = await _decide(row, _ONLY_GRANT)
        _, run_reason = await _decide(row, _ONLY_RUN_APPROVAL)
        granted = grant_reason.startswith("auto-granted by ")
        run_approved = run_reason.startswith("auto-approved by run approval ")
        granted_count += granted
        if is_root_shell(row.op_id):
            assert not run_approved, (row.op_id, run_reason)
        else:
            assert granted == run_approved, (row.op_id, grant_reason, run_reason)
    assert granted_count > 10, "too few operations that a standing grant covers"


@pytest.mark.asyncio
async def test_the_wait_of_a_listed_credential_read_says_why_in_its_audit_row() -> None:
    """A service call of ``vault.kv.read`` waits; the audit row of the wait says why."""
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import clear_registry, register_connector_v2
    from meho_backplane.operations import (
        dispatch,
        register_typed_operation,
        reset_dispatcher_caches,
    )

    class _Vault(Connector):
        product = "vault"
        version = "1.x"
        impl_id = "vault"

        async def fingerprint(self, target: object, operator: object = None) -> None:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: object) -> None:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: object, op_id: str, params: object) -> None:  # type: ignore[override]
            raise NotImplementedError

    reset_dispatcher_caches()
    clear_registry()
    try:
        register_connector_v2(product="vault", version="", impl_id="", cls=_Vault)
        embedding = AsyncMock()
        embedding.encode_one.return_value = [0.1] * 384
        embedding.dimension = 384
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id="vault.kv.read",
            handler=_never_runs,
            summary="Read a secret.",
            description="Read a secret.",
            parameter_schema={"type": "object"},
            safety_level="caution",
            when_to_use=None,
            embedding_service=embedding,
        )
        (row,) = await _all_rows()
        await _seed_grant_and_run_approval([row])

        token = work_ref_var.set(_RUN)
        try:
            parked = await dispatch(
                operator=_operator(),
                connector_id="vault-1.x",
                op_id="vault.kv.read",
                target=None,
                params={},
            )
        finally:
            work_ref_var.reset(token)
    finally:
        reset_dispatcher_caches()
        clear_registry()

    assert parked.status == "awaiting_approval", parked.error
    async with get_sessionmaker()() as session:
        (request_row,) = (
            await session.scalars(select(AuditLog).where(AuditLog.path == "approval.request"))
        ).all()
    assert request_row.payload["wait_reason"].endswith(CREDENTIAL_WAIT_NOTE)


async def _never_runs(**_: object) -> dict[str, object]:
    raise AssertionError("a call that waits for a person never runs")


async def _all_rows() -> list[EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(EndpointDescriptor))).scalars())
        session.expunge_all()
    return rows
