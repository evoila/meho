# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Idempotent park creation, binding identity, and run phase on list/show (#3486).

A durable client (for example an add-on that drives a two-person governed
delete) must be able to retry a call after a restart without creating a
second approval request. This suite pins:

* **Retry returns the same request.** Two ``call_operation`` submits with
  the same ``idempotency_key`` give one approval request; the second gets
  the first one's ``approval_request_id`` back.
* **The key is scoped.** A different principal, a different tenant, or no
  key at all still gets its own request. The same key with a different call
  is refused (``idempotency_key_conflict``). A rejected request frees the key.
* **A race cannot create two.** The partial unique index (migration 0105)
  makes the losing insert return the winner's row.
* **List/show carry ``preview_hash`` and ``result_state``.** A client can
  match a request to the row it previewed, and tell "approved, not run yet"
  from "approved and run" without polling ``/result``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import func, select

import meho_backplane.audit as _audit_module
import meho_backplane.operations.approval_queue as approval_queue_module
from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.main import app
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, register_preview_builder
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    IdempotencyKeyConflictError,
    approval_result_state,
    approve_request,
    create_pending_request,
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.test_destructive_preview_binding import (
    _CONNECTOR_ID,
    _OP_ID,
    _PARAMS,
    _TENANT,
    _approval_row_count,
    _blast_radius_builder,
    _insert_destructive_descriptor,
    _operator,
    _register_recording_connector,
    _seed_target,
)

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_KEY = "addon-run-7:row-42"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")

    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_audit_module, "publish_event", _noop)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    builders_snapshot = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders_snapshot)


@pytest.fixture
def embedding() -> list[float]:
    return [0.1] * 384


async def _request_audit_count() -> int:
    async with get_sessionmaker()() as s:
        stmt = select(func.count()).select_from(AuditLog).where(AuditLog.path == "approval.request")
        return int((await s.execute(stmt)).scalar_one())


async def _park_direct(
    *,
    sub: str = "op-requester",
    tenant_id: uuid.UUID = _TENANT,
    key: str | None = _KEY,
    params: dict[str, Any] | None = None,
) -> ApprovalRequest:
    """Park a non-destructive request straight through ``create_pending_request``."""
    operator = _operator(sub=sub)
    operator = operator.model_copy(update={"tenant_id": tenant_id})
    call_params = params if params is not None else {"name": "web-01"}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=operator,
            connector_id="gh-rest-3",
            op_id="POST:/vms",
            target=None,
            params=call_params,
            params_hash=compute_params_hash(call_params),
            idempotency_key=key,
        )
        await s.commit()
    return request


# ---------------------------------------------------------------------------
# Conformance: a retried destructive call yields one park
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retried_destructive_call_returns_the_same_park(embedding: list[float]) -> None:
    """Two submits with the same key -> one request; the retry gets its id back."""
    connector = _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"], "idempotency_key": _KEY}

    first = await call_operation(requester, bound)
    assert first["status"] == "awaiting_approval", first
    assert "approval_request_reused" not in first["extras"]

    # The client "crashes" before it stores the id, then resubmits on restart.
    second = await call_operation(requester, bound)
    assert second["status"] == "awaiting_approval", second
    assert second["extras"]["approval_request_id"] == first["extras"]["approval_request_id"]
    assert second["extras"]["approval_request_reused"] is True

    assert await _approval_row_count() == 1
    assert await _request_audit_count() == 1
    assert connector.calls == []


@pytest.mark.asyncio
async def test_destructive_retry_still_needs_a_matching_preview_hash(
    embedding: list[float],
) -> None:
    """The retry path runs the destructive gate first: a wrong hash is refused."""
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    first = await call_operation(
        requester, {**args, "preview_hash": preview["preview_hash"], "idempotency_key": _KEY}
    )
    assert first["status"] == "awaiting_approval"

    retry = await call_operation(
        requester, {**args, "preview_hash": "0" * 64, "idempotency_key": _KEY}
    )
    assert retry["status"] == "denied"
    assert retry["extras"]["error_code"] == "preview_hash_mismatch"
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
async def test_call_with_a_key_held_by_another_call_is_refused(embedding: list[float]) -> None:
    """Same key, different ``work_ref``: refused, audited, nothing new parked."""
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"], "idempotency_key": _KEY}
    first = await call_operation(requester, {**bound, "work_ref": "gh:example/repo#1"})
    assert first["status"] == "awaiting_approval"

    other = await call_operation(requester, {**bound, "work_ref": "gh:example/repo#2"})
    assert other["status"] == "denied", other
    assert other["extras"]["error_code"] == "idempotency_key_conflict"
    assert other["extras"]["approval_request_id"] == first["extras"]["approval_request_id"]
    assert await _approval_row_count() == 1

    async with get_sessionmaker()() as s:
        denied_rows = (
            (await s.execute(select(AuditLog).where(AuditLog.work_ref == "gh:example/repo#2")))
            .scalars()
            .all()
        )
    assert [row.status_code for row in denied_rows] == [403]


# ---------------------------------------------------------------------------
# Scope of the key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_key_each_submit_parks_again() -> None:
    """No key keeps the old behaviour: same params, two requests."""
    a = await _park_direct(key=None)
    b = await _park_direct(key=None)
    assert a.id != b.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
async def test_same_key_from_another_principal_gets_its_own_park() -> None:
    """Same params and key, different requester -> two requests (never shared)."""
    a = await _park_direct(sub="addon-a")
    b = await _park_direct(sub="addon-b")
    assert a.id != b.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
async def test_same_key_in_another_tenant_gets_its_own_park() -> None:
    """The key is tenant-scoped: another tenant never sees this tenant's request."""
    other_tenant = uuid.UUID("00000000-0000-0000-0000-000000003487")
    a = await _park_direct()
    b = await _park_direct(tenant_id=other_tenant)
    assert a.id != b.id
    assert b.tenant_id == other_tenant


@pytest.mark.asyncio
async def test_same_key_and_call_returns_the_existing_row() -> None:
    a = await _park_direct()
    b = await _park_direct()
    assert a.id == b.id
    assert getattr(b, "_reused", False) is True
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
async def test_same_key_with_different_params_is_refused() -> None:
    """A key held by a different call is refused; nothing new is parked."""
    first = await _park_direct()
    with pytest.raises(IdempotencyKeyConflictError) as exc_info:
        await _park_direct(params={"name": "web-02"})
    assert exc_info.value.request_id == first.id
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
async def test_same_key_with_different_work_ref_is_refused() -> None:
    """Two runs under different change tickets never share one request."""
    token = work_ref_var.set("gh:example/repo#1")
    try:
        await _park_direct()
    finally:
        work_ref_var.reset(token)
    token = work_ref_var.set("gh:example/repo#2")
    try:
        with pytest.raises(IdempotencyKeyConflictError):
            await _park_direct()
    finally:
        work_ref_var.reset(token)
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
async def test_rejected_request_frees_the_key() -> None:
    """After a rejection, the same key parks a new request."""
    first = await _park_direct()
    async with get_sessionmaker()() as s:
        await reject_request(s, first.id, operator=_operator(sub="op-approver"), reason="no")
        await s.commit()
    second = await _park_direct()
    assert second.id != first.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
async def test_racing_insert_returns_the_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The losing insert of a race returns the row the winner committed.

    The first lookup is forced to miss, as if the other request committed
    between this request's lookup and its insert. The partial unique index
    then refuses the insert, and the winner's row comes back.
    """
    winner = await _park_direct()

    real_find = approval_queue_module._find_idempotent_request
    calls = {"n": 0}

    async def _miss_once(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_find(*args, **kwargs)

    monkeypatch.setattr(approval_queue_module, "_find_idempotent_request", _miss_once)
    loser = await _park_direct()

    assert loser.id == winner.id
    assert calls["n"] == 2
    assert await _approval_row_count() == 1
    assert await _request_audit_count() == 1


# ---------------------------------------------------------------------------
# result_state on the row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_state_follows_the_run_phase() -> None:
    row = await _park_direct(key=None)
    assert approval_result_state(row) is None

    row.status = ApprovalRequestStatus.APPROVED.value
    assert approval_result_state(row) is ApprovalResultState.NOT_STARTED

    row.resumed_at = datetime.now(UTC)
    assert approval_result_state(row) is ApprovalResultState.STARTED

    row.resume_result = {"status": "ok", "op_id": "POST:/vms"}
    assert approval_result_state(row) is ApprovalResultState.EXECUTED

    row.resume_result = {"status": "error", "op_id": "POST:/vms"}
    assert approval_result_state(row) is ApprovalResultState.FAILED

    row.status = ApprovalRequestStatus.REJECTED.value
    assert approval_result_state(row) is None


# ---------------------------------------------------------------------------
# List/show surface: preview_hash + result_state, end to end
# ---------------------------------------------------------------------------


def _headers(key: Any, *, sub: str) -> dict[str, str]:
    token = mint_token(key, sub=sub, tenant_role=TenantRole.OPERATOR.value, tenant_id=str(_TENANT))
    return {"Authorization": f"Bearer {token}"}


async def _get_json(path: str, headers: dict[str, str], key: Any) -> Any:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            response = await ac.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_list_and_show_tell_approved_from_executed(embedding: list[float]) -> None:
    """``preview_hash`` and ``result_state`` ride the REST list and show views."""
    connector = _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound_hash = preview["preview_hash"]
    call = await call_operation(
        requester, {**args, "preview_hash": bound_hash, "idempotency_key": _KEY}
    )
    request_id = call["extras"]["approval_request_id"]

    key = make_rsa_keypair("kid-3486")
    headers = _headers(key, sub="op-approver")

    pending = await _get_json(f"/api/v1/approvals/{request_id}", headers, key)
    assert pending["preview_hash"] == bound_hash
    assert pending["result_state"] is None

    approver = _operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, uuid.UUID(request_id), operator=approver, params=None)
        await s.commit()

    listed = await _get_json("/api/v1/approvals?status=approved", headers, key)
    (row_view,) = [v for v in listed if v["id"] == request_id]
    assert row_view["preview_hash"] == bound_hash
    assert row_view["result_state"] == "not_started"
    assert connector.calls == []

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, uuid.UUID(request_id))
        assert row is not None
        result = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert result.status == "ok"

    shown = await _get_json(f"/api/v1/approvals/{request_id}", headers, key)
    assert shown["status"] == "approved"
    assert shown["result_state"] == "executed"
    assert connector.calls == [{"verb": "DELETE", "path": "/vms/vm-42"}]

    # A retry after the run returns the same, already-run request: no second delete.
    retry = await call_operation(
        requester, {**args, "preview_hash": bound_hash, "idempotency_key": _KEY}
    )
    assert retry["extras"]["approval_request_id"] == request_id
    assert retry["extras"]["approval_request_reused"] is True
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
async def test_view_preview_hash_is_none_for_non_destructive_row() -> None:
    row = await _park_direct(key=None)
    view = _view(row)
    assert view.preview_hash is None
    assert view.result_state is None
