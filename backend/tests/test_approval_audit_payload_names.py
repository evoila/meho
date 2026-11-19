# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each id in the payload of an approval audit row stands next to the same person's name.

The payload has two pairs: ``principal_sub`` / ``principal_name`` (the
person who acted on the row, the row's ``operator_sub``) and
``requester_sub`` / ``requester_name`` (the person who asked). A decision
row adds ``reviewed_by`` / ``reviewed_by_name``. The readers show a row in
the older form (requester id in ``principal_sub``, actor name in
``principal_name``) with the row's ``operator_sub`` as ``principal_sub``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.audit_query.approval_payload import approval_payload_view
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    EXPIRY_OPERATOR_SUB,
    ResumeRequester,
    approve_request,
    create_pending_request,
    expire_stale_requests,
    expiry_operator,
    reject_request,
    withdraw_request,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.audit.routes import _build_drawer_context

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000a11d")
_REQUESTER_SUB = "requester-sub"
_REQUESTER_NAME = "Rita Requester"
_REVIEWER_SUB = "reviewer-sub"
_REVIEWER_NAME = "Victor Reviewer"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _person(sub: str, name: str) -> Operator:
    return Operator(
        sub=sub,
        name=name,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        auth_time=int(time.time()),
        sid=str(uuid.uuid4()),
        azp="meho-cli-approve",
        acr="1",
    )


async def _park(session: AsyncSession, **kwargs: Any) -> uuid.UUID:
    request = await create_pending_request(
        session,
        operator=_person(_REQUESTER_SUB, _REQUESTER_NAME),
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
        **kwargs,
    )
    await session.commit()
    return request.id


async def _row(path: str) -> AuditLog:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == path))).scalars().all()
    assert len(rows) == 1
    return rows[0]


def _assert_pairs(
    row: AuditLog, *, actor_sub: str, actor_name: str | None, reviewer: bool = False
) -> None:
    payload = row.payload
    assert row.operator_sub == actor_sub
    assert payload["principal_sub"] == actor_sub
    assert payload.get("principal_name") == actor_name
    assert payload["requester_sub"] == _REQUESTER_SUB
    assert payload["requester_name"] == _REQUESTER_NAME
    if reviewer:
        assert payload["reviewed_by"] == actor_sub
        assert payload["reviewed_by_name"] == actor_name


@pytest.mark.asyncio
async def test_request_row_names_the_requester_in_both_pairs(session: AsyncSession) -> None:
    await _park(session)

    row = await _row("approval.request")
    _assert_pairs(row, actor_sub=_REQUESTER_SUB, actor_name=_REQUESTER_NAME)


@pytest.mark.asyncio
async def test_approve_row_by_another_person_keeps_each_id_next_to_its_name(
    session: AsyncSession,
) -> None:
    request_id = await _park(session)

    async with get_sessionmaker()() as s2:
        await approve_request(
            s2, request_id, operator=_person(_REVIEWER_SUB, _REVIEWER_NAME), params={}
        )
        await s2.commit()

    row = await _row("approval.decision")
    assert row.payload["decision"] == "approved"
    assert row.payload["self_approved"] is False
    _assert_pairs(row, actor_sub=_REVIEWER_SUB, actor_name=_REVIEWER_NAME, reviewer=True)


@pytest.mark.asyncio
async def test_reject_row_by_another_person_keeps_each_id_next_to_its_name(
    session: AsyncSession,
) -> None:
    request_id = await _park(session)

    async with get_sessionmaker()() as s2:
        await reject_request(s2, request_id, operator=_person(_REVIEWER_SUB, _REVIEWER_NAME))
        await s2.commit()

    row = await _row("approval.decision")
    assert row.payload["decision"] == "rejected"
    _assert_pairs(row, actor_sub=_REVIEWER_SUB, actor_name=_REVIEWER_NAME, reviewer=True)


@pytest.mark.asyncio
async def test_self_approval_row_names_one_person_in_every_pair(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    request_id = await _park(session)

    async with get_sessionmaker()() as s2:
        await approve_request(
            s2, request_id, operator=_person(_REQUESTER_SUB, _REQUESTER_NAME), params={}
        )
        await s2.commit()

    row = await _row("approval.decision")
    assert row.payload["self_approved"] is True
    _assert_pairs(row, actor_sub=_REQUESTER_SUB, actor_name=_REQUESTER_NAME, reviewer=True)


@pytest.mark.asyncio
async def test_withdraw_row_names_the_requester_in_both_pairs(session: AsyncSession) -> None:
    request_id = await _park(session)

    async with get_sessionmaker()() as s2:
        await withdraw_request(s2, request_id, operator=_person(_REQUESTER_SUB, _REQUESTER_NAME))
        await s2.commit()

    row = await _row("approval.withdraw")
    assert row.payload["withdrawn_by"] == _REQUESTER_SUB
    _assert_pairs(row, actor_sub=_REQUESTER_SUB, actor_name=_REQUESTER_NAME)


@pytest.mark.asyncio
async def test_expiry_row_names_the_expiry_actor_apart_from_the_requester(
    session: AsyncSession,
) -> None:
    await _park(session, expires_at=datetime.now(UTC) - timedelta(minutes=5))

    async with get_sessionmaker()() as s2:
        await expire_stale_requests(s2, operator=expiry_operator(_TENANT_ID))
        await s2.commit()

    row = await _row("approval.decision")
    assert row.payload["decision"] == "expired"
    assert "reviewed_by_name" not in row.payload
    _assert_pairs(row, actor_sub=EXPIRY_OPERATOR_SUB, actor_name=row.payload.get("principal_name"))
    assert row.payload.get("principal_name") != _REQUESTER_NAME


@pytest.mark.asyncio
async def test_repark_of_a_multi_step_run_names_reviewer_and_requester_apart(
    session: AsyncSession,
) -> None:
    """A later step that waits again is saved for the requester, written by the reviewer."""
    request = await create_pending_request(
        session,
        operator=_person(_REVIEWER_SUB, _REVIEWER_NAME),
        connector_id="vault-1.x",
        op_id="vault.kv.write",
        target=None,
        params={},
        params_hash=compute_params_hash({}),
        requester=ResumeRequester(sub=_REQUESTER_SUB, act=None, name=_REQUESTER_NAME),
    )
    await session.commit()

    assert request.principal_sub == _REQUESTER_SUB
    row = await _row("approval.request")
    _assert_pairs(row, actor_sub=_REVIEWER_SUB, actor_name=_REVIEWER_NAME)


# ---------------------------------------------------------------------------
# Rows in the older form
# ---------------------------------------------------------------------------


def _old_form_payload() -> dict[str, Any]:
    return {
        "approval_request_id": str(uuid.uuid4()),
        "op_id": "vault.kv.write",
        "connector_id": "vault-1.x",
        "principal_sub": _REQUESTER_SUB,
        "principal_name": _REVIEWER_NAME,
        "result_status": "decision",
        "decision": "approved",
        "reviewed_by": _REVIEWER_SUB,
    }


async def _insert_old_form_row() -> AuditLog:
    row = AuditLog(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        operator_sub=_REVIEWER_SUB,
        tenant_id=_TENANT_ID,
        event_class=AuditEventClass.APPROVAL_DECISION,
        method="APPROVAL",
        path="approval.decision",
        status_code=200,
        duration_ms=Decimal("0.00"),
        payload=_old_form_payload(),
    )
    async with get_sessionmaker()() as s:
        s.add(row)
        await s.commit()
    return row


def test_view_moves_the_requester_of_an_old_form_row() -> None:
    view = approval_payload_view(
        method="APPROVAL", operator_sub=_REVIEWER_SUB, payload=_old_form_payload()
    )

    assert view["principal_sub"] == _REVIEWER_SUB
    assert view["principal_name"] == _REVIEWER_NAME
    assert view["requester_sub"] == _REQUESTER_SUB


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        # Not an approval row.
        ("DISPATCH", {"principal_sub": "someone-else", "principal_name": "X"}),
        # A row in the new form.
        (
            "APPROVAL",
            {"principal_sub": _REVIEWER_SUB, "requester_sub": _REQUESTER_SUB},
        ),
        # An old-form row of one person: nothing is mixed.
        ("APPROVAL", {"principal_sub": _REVIEWER_SUB, "principal_name": _REVIEWER_NAME}),
        # No id, or an id that is not a string.
        ("APPROVAL", {"principal_name": _REVIEWER_NAME}),
        ("APPROVAL", {"principal_sub": None}),
        ("APPROVAL", {}),
    ],
)
def test_view_leaves_other_rows_unchanged(method: str, payload: dict[str, Any]) -> None:
    view = approval_payload_view(method=method, operator_sub=_REVIEWER_SUB, payload=payload)

    assert view == payload
    assert view is not payload


@pytest.mark.asyncio
async def test_audit_query_shows_an_old_form_row_with_the_main_columns() -> None:
    row = await _insert_old_form_row()

    async with get_sessionmaker()() as s:
        result = await query_audit(AuditQueryFilters(), tenant_id=_TENANT_ID, session=s)

    (entry,) = [e for e in result.rows if e.id == row.id]
    assert entry.principal_sub == _REVIEWER_SUB
    assert entry.principal_name == _REVIEWER_NAME
    assert entry.payload["principal_sub"] == _REVIEWER_SUB
    assert entry.payload["requester_sub"] == _REQUESTER_SUB


@pytest.mark.asyncio
async def test_console_drawer_shows_an_old_form_row_with_the_main_columns() -> None:
    row = await _insert_old_form_row()

    async with get_sessionmaker()() as s:
        stored = await s.get(AuditLog, row.id)
        assert stored is not None
        context = await _build_drawer_context(s, stored, is_admin=False)

    shown = context["request_payload"]
    assert isinstance(shown, dict)
    assert shown["principal_sub"] == _REVIEWER_SUB
    assert shown["principal_name"] == _REVIEWER_NAME
    assert shown["requester_sub"] == _REQUESTER_SUB
    # The stored row keeps its form: the audit log is append-only.
    assert stored.payload["principal_sub"] == _REQUESTER_SUB
