# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run approvals, the stored run result and the in-order check read the stored class.

* A run approval never covers an operation whose row stores
  ``credential_mint`` or ``credential_write`` (with no preview builder): the
  live gate and a new run approval request give the same answer as the
  standing grants.
* After an approved run of a stored ``credential_mint``, the new credential
  is only in the answer to the caller: not on the request row, not in the
  audit rows, not in the trace, not in the result store.
* The in-order check counts a reveal as a read only when the op id and the
  stored class both say read.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    PermissionVerdict,
    RunApproval,
    Tenant,
)
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatcher, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.approval_queue import _dispatch_resume_with_bound_context
from meho_backplane.operations.approved_reveal import is_write_reveal
from meho_backplane.operations.ingest.safety_floors import _FLOORS
from meho_backplane.operations.reducer import SECRET_REVEAL_CONTEXT_KEY
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.settings import get_settings
from tests._stored_op_class_harness import (
    GET_CLIENTS,
    MADE_UP_VALUE,
    Clients,
    Target,
    call,
    ingest,
    operator,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000065a1")
_PRINCIPAL = "svc-sub-stored-class"
_RUN = "run-stored-class"
_CONNECTOR = "vmware-rest-9.0"
_OP = "POST:/vcenter/keys"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    Clients.calls = []
    Clients.answer = [{"name": "a"}]
    Clients.failure = None
    yield
    _FLOORS.clear()
    reset_dispatcher_caches()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def run_ref() -> Iterator[str]:
    token = work_ref_var.set(_RUN)
    yield _RUN
    work_ref_var.reset(token)


def _service() -> Operator:
    return Operator(
        sub=_PRINCIPAL,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


def _descriptor(stored: str | None) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=_OP,
        method="POST",
        source_kind="ingested",
        safety_level="caution",
        requires_approval=False,
        parameter_schema={},
        op_class=stored,
        tenant_id=None,
    )


async def _seed_descriptor(stored: str | None) -> None:
    async with get_sessionmaker()() as session:
        session.add(_descriptor(stored))
        await session.commit()


async def _seed_live_run_approval() -> uuid.UUID:
    """A run approval that lists the operation, written straight to the table.

    It stands for a row approved before the connector floor stored the class.
    """
    approval_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            RunApproval(
                id=approval_id,
                tenant_id=_TENANT,
                principal_sub=_PRINCIPAL,
                work_ref=_RUN,
                entries=[{"connector_id": _CONNECTOR, "op_id": _OP}],
                reason="make a key",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
        await session.commit()
    return approval_id


# -- a run approval never covers a stored credential_mint or credential_write ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "verdict"),
    [
        ("credential_mint", PermissionVerdict.NEEDS_APPROVAL),
        ("credential_write", PermissionVerdict.NEEDS_APPROVAL),
        (None, PermissionVerdict.AUTO_EXECUTE),
    ],
)
async def test_live_run_approval_never_covers_a_stored_secret_class(
    run_ref: str, stored: str | None, verdict: PermissionVerdict
) -> None:
    await _seed_live_run_approval()

    got, reason = await policy_gate(
        operator=_service(),
        descriptor=_descriptor(stored),
        target=None,
        connector_id=_CONNECTOR,
    )

    assert got is verdict, reason


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["credential_mint", "credential_write"])
async def test_run_approval_request_with_a_stored_secret_class_says_each_call_waits(
    stored: str,
) -> None:
    await _seed_descriptor(stored)

    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_OP)],
                reason="make a key",
            ),
        )

    assert "entries[0]" in refused.value.message
    assert "a person approves each call" in refused.value.message


@pytest.mark.asyncio
async def test_run_approval_request_without_a_stored_class_is_accepted() -> None:
    await _seed_descriptor(None)

    read = await RunApprovalService().request(
        _service(),
        RunApprovalCreate(
            work_ref=_RUN,
            entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_OP)],
            reason="make a key",
        ),
    )

    assert read.status == "pending"


# -- an approved run of a stored credential_mint keeps no copy of the answer --------------


class _RecordingReducer:
    """A reducer that keeps the context it is given and returns the answer whole."""

    contexts: ClassVar[list[dict[str, Any]]] = []

    async def reduce(self, payload: Any, schema: Any, context: Any = None) -> tuple[Any, None]:
        type(self).contexts.append(dict(context or {}))
        return payload, None


async def _tenant_with_recorder_on() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"stored-{tenant_id.hex[:8]}",
                name="stored class",
                flight_recorder_enabled=True,
            )
        )
        await session.commit()
    return tenant_id


async def _stored_rows(tenant_id: uuid.UUID) -> tuple[ApprovalRequest, str, str, int]:
    """The request row, the audit rows and the trace of the run, as text."""
    async with get_sessionmaker()() as session:
        (request,) = (await session.execute(select(ApprovalRequest))).scalars().all()
        audits = (await session.execute(select(AuditLog))).scalars().all()
        traces = (
            (
                await session.execute(
                    select(DispatchTrace).where(DispatchTrace.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        spans = (await session.execute(select(DispatchTraceSpan))).scalars().all()

    def text(rows: Any) -> str:
        return json.dumps(
            [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows], default=str
        )

    return request, text([request]) + text(audits), text(traces) + text(spans), len(traces)


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "kept"), [("credential_mint", False), (None, True)])
async def test_approved_run_of_a_stored_credential_mint_keeps_no_copy(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    stored: str | None,
    kept: bool,
) -> None:
    """The new credential is in the caller's answer once, and in no stored place."""
    tenant_id = await _tenant_with_recorder_on()
    await ingest(stub_embedding_service, {GET_CLIENTS: stored} if stored else {}, approval=True)
    parked = await call(operator(tenant_id=tenant_id), GET_CLIENTS, {})
    assert parked.status == "awaiting_approval", (parked.status, parked.error)
    async with get_sessionmaker()() as session:
        (request,) = (await session.execute(select(ApprovalRequest))).scalars().all()
    Clients.answer = [{"name": "client-1", "token": MADE_UP_VALUE}]
    _RecordingReducer.contexts = []
    monkeypatch.setattr(dispatcher, "_DEFAULT_REDUCER", _RecordingReducer())

    result = await _dispatch_resume_with_bound_context(
        operator=operator(tenant_id=tenant_id),
        request=request,
        resolved_target=Target(),
        effective_params={},
    )

    assert result.status == "ok", (result.status, result.error)
    assert json.dumps(result.result).count(MADE_UP_VALUE) == (0 if kept else 1)
    # The result store: the reducer is told to store nothing.
    (context,) = _RecordingReducer.contexts
    assert (context.get(SECRET_REVEAL_CONTEXT_KEY) is True) is not kept
    row, request_and_audit, trace, trace_count = await _stored_rows(tenant_id)
    assert trace_count == 1
    assert MADE_UP_VALUE not in request_and_audit
    assert MADE_UP_VALUE not in trace
    # The request row keeps the answer of a plain run, and none of a mint.
    assert (row.resume_result["result"] is not None) is kept
    assert row.resume_result["extras"].get("secret_value_withheld", False) is not kept


@pytest.mark.asyncio
async def test_stored_result_of_a_credential_mint_from_the_op_id_has_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``credential_mint`` from the op id is stored without its answer too."""
    import meho_backplane.db.engine as engine
    from meho_backplane.operations.approval_queue import persist_resume_result

    written: dict[str, Any] = {}

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def execute(self, statement: Any) -> None:
            written.update(statement.compile().params)

        async def commit(self) -> None:
            return None

    monkeypatch.setattr(engine, "get_sessionmaker", lambda: _Session)
    envelope = {"status": "ok", "op_id": "x", "result": {"token": MADE_UP_VALUE}, "extras": {}}

    await persist_resume_result(
        uuid.uuid4(), envelope, parked_op_id="x", parked_op_class="credential_mint"
    )

    assert MADE_UP_VALUE not in json.dumps(written, default=str)
    assert written["resume_result"]["result"] is None
    assert written["resume_result"]["extras"]["secret_value_withheld"] is True


# -- approve in order: a reveal is a read only when both classes say so ------------------


@pytest.mark.parametrize(
    ("op_id", "stored", "changes"),
    [
        ("rabbitmq.request", "read", True),
        ("postgres.query", "read", True),
        ("rabbitmq.request", None, True),
        (GET_CLIENTS, "read", False),
        (GET_CLIENTS, "credential_read", False),
        (GET_CLIENTS, "write", True),
    ],
)
def test_reveal_is_a_read_only_when_the_op_id_and_the_stored_class_say_so(
    op_id: str, stored: str | None, changes: bool
) -> None:
    request = SimpleNamespace(
        op_id=op_id,
        reveal_secret=True,
        proposed_effect={"op_class": stored} if stored else {},
    )

    assert is_write_reveal(request) is changes  # type: ignore[arg-type]
