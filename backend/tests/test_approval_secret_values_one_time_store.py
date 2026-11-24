# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A waiting request keeps secret values only in the encrypted one-time store.

A secret value is a value under a secret name, at any depth, or a value in
a field that a connector marked with ``declare_secret_field``. The row of
the request shows the marker for each of them. The approved run sends the
real values to the target, once. The row, the audit rows and every answer
that shows the request hold no part of a secret, while it waits, after it
runs and after it ends without a run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane import op_classes
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_handoff import (
    ApprovalExecutionPayload,
    ApprovalHandoffKeyError,
)
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    expire_stale_requests,
    expiry_operator,
    get_request,
    reject_request,
    resume_dispatch_after_approval,
    withdraw_request,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000561")
_REQUESTER_SUB = "agent:requester"
_REVIEWER_SUB = "op:reviewer"
_CONNECTOR_ID = "notes-1.x"
_NAMED_OP = "notes.config.apply"
_MARKED_OP = "notes.record.update"
_SECRET = "made-up-pw-value-4711"
_MARKER = "[REDACTED:param_name]"
_CONFIG_TEXT = f"mode = fast\nadmin_password = {_SECRET}\n"
_NAMED_PARAMS: dict[str, Any] = {
    "name": "web",
    "spec": {"hosts": [{"name": "a", "credentials": {"password": _SECRET}}]},
}
_MARKED_PARAMS: dict[str, Any] = {
    "name": "web",
    "config": {"text": _CONFIG_TEXT},
    "note": f"set it to {_SECRET}",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def marked_fields() -> Iterator[None]:
    op_classes.declare_secret_field(_MARKED_OP, "config", "text")
    op_classes.declare_secret_field(_MARKED_OP, "note")
    yield
    op_classes._SECRET_FIELDS.pop(_MARKED_OP, None)


class _FakeDispatch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> OperationResult:
        self.calls.append(kwargs)
        return OperationResult(
            status="ok", op_id=kwargs["op_id"], result={"done": True}, duration_ms=1.0
        )


@pytest.fixture
def fake_dispatch(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatch:
    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    return fake


def _operator(sub: str, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


def _requester() -> Operator:
    return _operator(_REQUESTER_SUB, PrincipalKind.AGENT)


def _reviewer() -> Operator:
    return _operator(_REVIEWER_SUB, PrincipalKind.USER)


async def _park(
    params: dict[str, Any],
    *,
    op_id: str = _NAMED_OP,
    approved: bool = False,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_requester(),
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        if approved:
            request.status = ApprovalRequestStatus.APPROVED.value
            request.reviewed_by = _REVIEWER_SUB
            request.decided_at = datetime.now(UTC)
        if expires_at is not None:
            request.expires_at = expires_at
        await s.commit()
        return request.id


async def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        return await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)


async def _everything_stored(request_id: uuid.UUID) -> str:
    """The request row and every audit row of the tenant, as one text."""
    row = await _load(request_id)
    async with get_sessionmaker()() as s:
        audits = (await s.execute(select(AuditLog))).scalars().all()
    columns = [c.name for c in ApprovalRequest.__table__.columns]
    parts = [json.dumps({c: str(getattr(row, c)) for c in columns})]
    audit_columns = [c.name for c in AuditLog.__table__.columns]
    parts += [json.dumps({c: str(getattr(a, c)) for c in audit_columns}) for a in audits]
    return "\n".join(parts)


async def _handoff_rows(request_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as s:
        rows = await s.scalars(
            select(ApprovalExecutionPayload).where(
                ApprovalExecutionPayload.request_id == request_id
            )
        )
        return len(list(rows))


@pytest.mark.asyncio
async def test_waiting_call_with_a_secret_named_value_keeps_it_only_in_the_one_time_store() -> None:
    request_id = await _park(_NAMED_PARAMS)

    row = await _load(request_id)
    assert row.execution_handle is not None
    assert await _handoff_rows(request_id) == 1
    assert row.params == {
        "name": "web",
        "spec": {"hosts": [{"name": "a", "credentials": _MARKER}]},
    }
    assert _SECRET not in await _everything_stored(request_id)


@pytest.mark.asyncio
async def test_approved_run_sends_the_real_values_once_and_the_row_stays_clean(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park(_NAMED_PARAMS, approved=True)

    first = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )
    second = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert first.status == "ok"
    assert second.extras.get("error_code") != "ok"
    assert len(fake_dispatch.calls) == 1
    assert fake_dispatch.calls[0]["params"] == _NAMED_PARAMS
    assert await _handoff_rows(request_id) == 0
    assert _SECRET not in await _everything_stored(request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["reject", "withdraw", "expire"])
async def test_a_request_that_ends_without_a_run_keeps_no_secret(how: str) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    request_id = await _park(_NAMED_PARAMS, expires_at=past if how == "expire" else None)
    async with get_sessionmaker()() as s:
        if how == "reject":
            await reject_request(s, request_id, operator=_reviewer(), reason="no")
        elif how == "withdraw":
            await withdraw_request(s, request_id, operator=_requester(), reason="no")
        else:
            await expire_stale_requests(s, operator=expiry_operator(_TENANT_ID))
        await s.commit()

    assert await _handoff_rows(request_id) == 0
    assert _SECRET not in await _everything_stored(request_id)


@pytest.mark.asyncio
async def test_marked_fields_keep_their_text_only_in_the_one_time_store(
    marked_fields: None, fake_dispatch: _FakeDispatch
) -> None:
    request_id = await _park(_MARKED_PARAMS, op_id=_MARKED_OP, approved=True)

    row = await _load(request_id)
    assert row.params == {"name": "web", "config": {"text": _MARKER}, "note": _MARKER}
    assert row.execution_handle is not None
    assert _SECRET not in await _everything_stored(request_id)

    result = await resume_dispatch_after_approval(operator=_requester(), request=row)

    assert result.status == "ok"
    assert fake_dispatch.calls[0]["params"] == _MARKED_PARAMS
    assert _SECRET not in await _everything_stored(request_id)


@pytest.mark.asyncio
async def test_a_marked_field_that_is_not_set_keeps_the_call_as_it_is(
    marked_fields: None,
) -> None:
    params = {"name": "web", "note": None}
    request_id = await _park(params, op_id=_MARKED_OP)

    row = await _load(request_id)
    assert row.params == params
    assert row.execution_handle is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"a": {"b": {"password": _SECRET}}},
        {"items": [{"x": 1}, {"apiToken": _SECRET}]},
        {"PASSWORD": _SECRET},
        {"password": _SECRET * 5000},
        {"password": "Pässwörd-пароль-密码-" + _SECRET},
        {"password": 4711_4711},
    ],
    ids=["two_deep", "in_list", "upper_case", "long", "non_english", "number"],
)
async def test_odd_shapes_of_a_secret_stay_out_of_every_row(
    params: dict[str, Any], fake_dispatch: _FakeDispatch
) -> None:
    request_id = await _park(params, approved=True)

    text = await _everything_stored(request_id)
    assert _SECRET not in text
    assert "4711" not in text.replace(_SECRET, "")
    assert (await _load(request_id)).execution_handle is not None

    await resume_dispatch_after_approval(operator=_requester(), request=await _load(request_id))
    assert fake_dispatch.calls[0]["params"] == params


@pytest.mark.asyncio
async def test_a_call_without_a_secret_is_stored_and_runs_as_before(
    fake_dispatch: _FakeDispatch,
) -> None:
    params = {"vm": "vm-42", "state": "off"}
    request_id = await _park(params, approved=True)

    row = await _load(request_id)
    assert row.params == params
    assert row.execution_handle is None
    await resume_dispatch_after_approval(operator=_requester(), request=row)
    assert fake_dispatch.calls[0]["params"] == params


@pytest.mark.asyncio
async def test_without_the_handoff_key_a_call_with_a_secret_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(ApprovalHandoffKeyError):
        await _park(_NAMED_PARAMS)
    assert await _park({"vm": "vm-42"}) is not None


@pytest.mark.asyncio
async def test_a_row_with_plain_params_from_an_older_release_still_runs(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park({"vm": "vm-42"}, approved=True)
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        row.params = _NAMED_PARAMS
        row.params_hash = compute_params_hash(_NAMED_PARAMS)
        await s.commit()

    result = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert result.status == "ok"
    assert fake_dispatch.calls[0]["params"] == _NAMED_PARAMS


@pytest.mark.asyncio
async def test_stored_params_with_another_mark_text_never_run(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park({"vm": "vm-42"}, approved=True)
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        row.params = {"vm": "vm-42", "state": "***REDACTED***"}
        await s.commit()

    await resume_dispatch_after_approval(operator=_requester(), request=await _load(request_id))

    assert fake_dispatch.calls == []


def test_a_secret_field_needs_a_path() -> None:
    with pytest.raises(ValueError):
        op_classes.declare_secret_field("notes.x")


def test_the_connector_kit_exports_the_secret_field_mark() -> None:
    import meho_connector_kit as kit

    assert "declare_secret_field" in kit.__all__
    assert kit.declare_secret_field is op_classes.declare_secret_field
