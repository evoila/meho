# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approved-dispatch result surface — capture + principal-scoped read (#3209).

Task #3209. A paired, out-of-process consumer that parks a **non-idempotent**
governed op for approval (the concrete case: VCF
``installer.sddc.bringup.start``) cannot resume it in place after a human
approves unless it can retrieve the result the backplane produced when it
re-dispatched the approved op — otherwise its only option is a second submit,
which starts a second bring-up.

This suite pins the two halves of the fix:

* **Capture** — the exactly-one-resumer winning path
  (:func:`~meho_backplane.operations.approval_queue.resume_dispatch_after_approval`)
  persists the reduced / handle-shaped result envelope onto
  ``approval_request.resume_result``.
* **Read** — :func:`~meho_backplane.operations.approval_queue.read_approval_result`
  and ``GET /api/v1/approvals/{id}/result`` surface it **only** to the
  originating principal (the request owner), write a synchronous
  ``approval.result`` audit row, and refuse every other principal (operator
  role or not) with 403.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.audit as _audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus, AuditLog
from meho_backplane.main import app
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ResultAccessForbiddenError,
    create_pending_request,
    get_request,
    read_approval_result,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000039a9")
_OTHER_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000039b9")
_OWNER_SUB = "addon:vcf-bringup"

_BRINGUP_OP = "installer.sddc.bringup.start"
_BRINGUP_CONNECTOR = "vcf-installer-9.x"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires + silence the broadcast."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)

    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_audit_module, "publish_event", _noop)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Open a session against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = _OWNER_SUB,
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: uuid.UUID = _TENANT_ID,
    principal_kind: PrincipalKind = PrincipalKind.SERVICE,
) -> Operator:
    return Operator(
        sub=sub,
        name="Test Principal",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=principal_kind,
    )


async def _park_approved(
    *,
    sub: str = _OWNER_SUB,
    tenant_id: uuid.UUID = _TENANT_ID,
    resume_result: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert a parked → approved request row; return its id.

    ``resume_result`` seeds the captured envelope directly for read-side
    tests; the capture-side test leaves it ``None`` and drives the real
    resume path instead.
    """
    operator = _make_operator(sub=sub, tenant_id=tenant_id)
    params = {"sddc_spec_ref": "spec-42"}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=operator,
            connector_id=_BRINGUP_CONNECTOR,
            op_id=_BRINGUP_OP,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        if resume_result is not None:
            request.resume_result = resume_result
        await s.commit()
        return request.id


# ---------------------------------------------------------------------------
# Capture: the winning resume path persists the reduced envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_capture_persists_reduced_result_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The winning resumer stores the re-dispatch's reduced envelope on the row.

    Non-idempotent bring-up case: the consumer needs the resulting task id back,
    not a re-submit. The captured envelope carries it, and ``resumed_at`` is
    stamped (the exactly-one-resumer claim was won).
    """
    request_id = await _park_approved()

    fake = OperationResult(
        status="ok",
        op_id=_BRINGUP_OP,
        result={"task_id": "sddc-bringup-42", "state": "IN_PROGRESS"},
        duration_ms=12.0,
    )

    import meho_backplane.operations.dispatcher as dispatcher_module

    async def _fake_dispatch(**_kwargs: object) -> OperationResult:
        return fake

    monkeypatch.setattr(dispatcher_module, "dispatch", _fake_dispatch)

    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    operator = _make_operator()
    result = await resume_dispatch_after_approval(operator=operator, request=row)
    assert result.status == "ok"

    async with get_sessionmaker()() as s:
        stored = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    assert stored.resume_result is not None, "resume result must be captured on the row"
    assert stored.resume_result["status"] == "ok"
    assert stored.resume_result["result"]["task_id"] == "sddc-bringup-42"
    assert stored.resumed_at is not None


@pytest.mark.asyncio
async def test_resume_capture_skipped_when_claim_already_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumer that loses the claim does not overwrite the captured result.

    Only the exactly-one-resumer winner reaches the capture point, so a benign
    ``already_resumed`` no-op leaves ``resume_result`` untouched (the winner
    owns it).
    """
    seeded = {"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "winner-task"}}
    request_id = await _park_approved(resume_result=seeded)

    # Pre-claim the row so this resumer loses (resumed_at already set).
    from meho_backplane.operations.approval_queue import claim_resume

    assert await claim_resume(request_id) is True

    import meho_backplane.operations.dispatcher as dispatcher_module

    async def _fail_dispatch(**_kwargs: object) -> OperationResult:  # pragma: no cover
        raise AssertionError("a losing resumer must not re-dispatch")

    monkeypatch.setattr(dispatcher_module, "dispatch", _fail_dispatch)

    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    result = await resume_dispatch_after_approval(operator=_make_operator(), request=row)
    assert result.status == "already_resumed"

    async with get_sessionmaker()() as s:
        stored = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    assert stored.resume_result == seeded, "loser must not clobber the winner's result"


# ---------------------------------------------------------------------------
# Read: principal scope + audit row (service layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_approval_result_returns_row_for_owner(session: AsyncSession) -> None:
    """The originating principal reads its result back."""
    seeded = {"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "t-1"}}
    request_id = await _park_approved(resume_result=seeded)

    row = await read_approval_result(
        session, operator=_make_operator(sub=_OWNER_SUB), request_id=request_id
    )
    await session.commit()
    assert row.resume_result == seeded
    assert row.principal_sub == _OWNER_SUB


@pytest.mark.asyncio
async def test_read_approval_result_writes_synchronous_audit_row(
    session: AsyncSession,
) -> None:
    """A result read leaves one ``approval.result`` audit row (v0.1-spec §6)."""
    request_id = await _park_approved(
        resume_result={"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "t-2"}}
    )
    await read_approval_result(
        session, operator=_make_operator(sub=_OWNER_SUB), request_id=request_id
    )
    await session.commit()

    async with get_sessionmaker()() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.result")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    audit = rows[0]
    assert audit.method == "APPROVAL"
    assert audit.status_code == 200
    assert audit.operator_sub == _OWNER_SUB
    assert audit.payload["approval_request_id"] == str(request_id)
    assert audit.payload["resume_result_present"] is True


@pytest.mark.asyncio
async def test_read_approval_result_forbidden_for_non_owner(session: AsyncSession) -> None:
    """A principal that is not the request owner is refused (→ 403)."""
    request_id = await _park_approved(
        resume_result={"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "t-3"}}
    )
    # Same tenant, operator role, but a different sub — must still be refused.
    with pytest.raises(ResultAccessForbiddenError):
        await read_approval_result(
            session,
            operator=_make_operator(sub="operator:someone-else", role=TenantRole.OPERATOR),
            request_id=request_id,
        )


@pytest.mark.asyncio
async def test_read_approval_result_forbidden_write_leaves_no_audit_row(
    session: AsyncSession,
) -> None:
    """A refused (non-owner) read writes no ``approval.result`` audit row."""
    request_id = await _park_approved(
        resume_result={"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "t-4"}}
    )
    with pytest.raises(ResultAccessForbiddenError):
        await read_approval_result(
            session,
            operator=_make_operator(sub="operator:intruder"),
            request_id=request_id,
        )
    await session.rollback()

    async with get_sessionmaker()() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.result")))
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_read_approval_result_cross_tenant_is_not_found(session: AsyncSession) -> None:
    """A request in another tenant is a 404, never reaching the owner guard."""
    request_id = await _park_approved(tenant_id=_OTHER_TENANT_ID, sub=_OWNER_SUB)
    with pytest.raises(ApprovalNotFoundError):
        await read_approval_result(
            session,
            operator=_make_operator(sub=_OWNER_SUB, tenant_id=_TENANT_ID),
            request_id=request_id,
        )


# ---------------------------------------------------------------------------
# Read: REST surface (route wiring + auth dependency)
# ---------------------------------------------------------------------------


def _token(key: Any, *, role: TenantRole, sub: str) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(_TENANT_ID))


@pytest.mark.asyncio
async def test_result_endpoint_owner_reads_result_non_operator_role() -> None:
    """The owner reads its result at 200 — even with a non-operator role.

    Proves the ``/result`` route is **principal-scoped**, not operator-gated:
    a ``read_only`` owner (a paired add-on's service principal) reads its own
    result; the payload rides through unchanged.
    """
    seeded = {"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "sddc-99"}}
    request_id = await _park_approved(resume_result=seeded)

    key = make_rsa_keypair("kid-owner")
    headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY, sub=_OWNER_SUB)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
        ) as ac:
            response = await ac.get(f"/api/v1/approvals/{request_id}/result", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_request_id"] == str(request_id)
    assert body["status"] == "approved"
    assert body["result"] == seeded


@pytest.mark.asyncio
async def test_result_endpoint_non_owner_is_forbidden() -> None:
    """A non-owner principal (even operator role) gets 403 ``not_request_owner``."""
    request_id = await _park_approved(
        resume_result={"status": "ok", "op_id": _BRINGUP_OP, "result": {"task_id": "x"}}
    )
    key = make_rsa_keypair("kid-intruder")
    headers = {
        "Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR, sub='op:intruder')}"
    }
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
        ) as ac:
            response = await ac.get(f"/api/v1/approvals/{request_id}/result", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_request_owner"}
