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
  is refused (``idempotency_key_conflict``). A rejected request frees the key,
  and so does a pending one past its deadline, even before the sweeper runs.
* **A race cannot create two.** The partial unique index (migration 0105)
  makes the losing insert return the winner's row. A retry that loses the
  race to expire an overdue request gets the replacement, not the expired
  row (the real two-connection race runs on Postgres in
  ``tests/integration/test_approval_idempotent_retry_pg.py``).
* **List/show carry ``preview_hash`` and ``result_state``.** A client can
  match a request to the row it previewed, and tell "approved, not run yet"
  from "approved and run" without polling ``/result``. Both resume paths
  store the outcome: the operator surface and the in-process agent waiter.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import func, select

import meho_backplane.operations.approval_queue as approval_queue_module
import meho_backplane.operations.meta_tools as meta_tools_module
from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.main import app
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._audit import agent_session_id_var, policy_rule_var, work_ref_var
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, register_preview_builder
from meho_backplane.operations._validate import compute_audit_params_hash, compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    IdempotencyKeyConflictError,
    ResumeRequester,
    SelfApprovalForbiddenError,
    UnknownRequesterApprovalError,
    approval_result_state,
    approve_request,
    create_pending_request,
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approved_reveal import fetch_approved_reveal
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.policy.stamp import RuleStamp
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

from . import _reveal_fetch_helpers as reveal_helpers
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
            proposed_effect={"safety_level": "caution"},
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
    # A retry of a request that still waits keeps the waiting text.
    assert "requires approval before execution" in second["error"]
    assert "already approved" not in second["error"]

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
    assert other["audit_id"] == str(denied_rows[0].id)


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
async def test_overdue_pending_request_is_expired_and_a_new_one_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after the deadline, before the sweeper runs, gets a new request.

    The old row can no longer be approved, so handing it back would leave
    the client stuck. The retry expires it (with a decision audit row) and
    parks a new request with the same key.
    """
    first = await _park_direct()
    assert first.expires_at is not None
    late = first.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(approval_queue_module, "_now", lambda: late)

    second = await _park_direct()

    assert second.id != first.id
    assert getattr(second, "_reused", False) is False
    assert second._expired_request.id == first.id  # type: ignore[attr-defined]
    assert await _approval_row_count() == 2
    async with get_sessionmaker()() as s:
        old = await s.get(ApprovalRequest, first.id)
        assert old is not None
        assert old.status == ApprovalRequestStatus.EXPIRED.value
        decisions = (
            (await s.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert [row.payload["decision"] for row in decisions] == ["expired"]
    # Same identity the sweeper uses, not the requester.
    assert decisions[0].operator_sub == approval_queue_module.EXPIRY_OPERATOR_SUB

    # The new request is the one that can still be decided.
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, second.id, operator=_operator(sub="op-approver"))
        await s.commit()
    assert approved.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_expiry_during_a_retry_does_not_take_the_retry_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expiry row of the old request names no policy rule (#6044).

    The retry writes that row while its own rule is bound. Only the retry's
    new request and its "request" row name the rule.
    """
    first = await _park_direct()
    assert first.expires_at is not None
    late = first.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(approval_queue_module, "_now", lambda: late)

    stamp = RuleStamp(source="default", rule_id="person.write", text="a write waits")
    token = policy_rule_var.set(stamp)
    try:
        second = await _park_direct()
    finally:
        policy_rule_var.reset(token)

    assert second._expired_request.id == first.id  # type: ignore[attr-defined]
    async with get_sessionmaker()() as s:
        expiry = (
            await s.execute(select(AuditLog).where(AuditLog.path == "approval.decision"))
        ).scalar_one()
        requests = {
            row.payload["approval_request_id"]: row
            for row in (
                await s.execute(select(AuditLog).where(AuditLog.path == "approval.request"))
            ).scalars()
        }
        new_request = await s.get(ApprovalRequest, second.id)
    assert expiry.payload["decision"] == "expired"
    assert (expiry.policy_rule, expiry.policy_version) == (None, None)
    assert requests[str(first.id)].policy_rule is None
    assert requests[str(second.id)].policy_rule == "default:person.write (shadow)"
    assert new_request is not None
    assert new_request.policy_rule is not None
    assert new_request.policy_rule["rule_id"] == "person.write"


@pytest.mark.asyncio
async def test_call_after_the_deadline_parks_again_and_records_the_expiry(
    embedding: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through ``call_operation``: a late retry gets a new id, not a stuck one."""
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)
    recorded: list[tuple[str, uuid.UUID]] = []

    async def _record(**kwargs: Any) -> None:
        recorded.append((kwargs["decision"], kwargs["request"].id))

    monkeypatch.setattr(approval_queue_module, "record_approval_step_event", _record)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"], "idempotency_key": _KEY}
    first = await call_operation(requester, bound)
    first_id = uuid.UUID(first["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, first_id)
        assert row is not None and row.expires_at is not None
        late = row.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(approval_queue_module, "_now", lambda: late)

    second = await call_operation(requester, bound)
    assert second["status"] == "awaiting_approval", second
    second_id = uuid.UUID(second["extras"]["approval_request_id"])
    assert second_id != first_id
    assert "approval_request_reused" not in second["extras"]
    assert recorded == [
        ("pending", first_id),
        ("expired", first_id),
        ("pending", second_id),
    ]


@pytest.mark.asyncio
async def test_pending_request_within_its_deadline_is_still_reused() -> None:
    first = await _park_direct()
    second = await _park_direct()
    assert second.id == first.id
    assert getattr(second, "_expired_request", None) is None


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


@pytest.mark.asyncio
async def test_retry_that_loses_the_expiry_race_gets_the_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that read an overdue row another retry already expired gets the new row.

    Sequence: two retries read the same overdue ``pending`` row. The first
    locks it, expires it, parks a replacement and commits. The second then
    finds no ``pending`` row under the lock. It must look the key up again
    and return the replacement, never the expired row the client could no
    longer get approved.
    """
    first = await _park_direct()
    assert first.expires_at is not None
    monkeypatch.setattr(
        approval_queue_module, "_now", lambda: first.expires_at + timedelta(seconds=1)
    )
    replacement = await _park_direct()
    assert replacement.id != first.id

    # ``first`` is detached and still reads ``pending``: exactly what the
    # losing retry saw before the winner committed.
    real_find = approval_queue_module._find_idempotent_request
    calls = {"n": 0}

    async def _stale_first_read(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return first
        return await real_find(*args, **kwargs)

    monkeypatch.setattr(approval_queue_module, "_find_idempotent_request", _stale_first_read)
    loser = await _park_direct()

    assert loser.id == replacement.id
    assert getattr(loser, "_reused", False) is True
    assert calls["n"] == 2
    assert await _approval_row_count() == 2
    assert await _request_audit_count() == 2
    async with get_sessionmaker()() as s:
        decisions = (
            (await s.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert len(decisions) == 1


@pytest.mark.asyncio
async def test_late_retry_that_waits_on_an_approval_gets_the_approved_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late retry that waited for an approve must return the approved row.

    Sequence: the retry reads its ``pending`` row past the deadline. Before
    it can lock the row, an approve (started before the deadline) commits.
    The locked re-read then finds no ``pending`` row. The retry looks the
    key up again and must see the row as ``approved``, not the ``pending``
    state its session cached on the first read. With the stale state it
    would try to expire the row again, miss again, and never return.
    """
    first = await _park_direct()
    assert first.expires_at is not None
    late: datetime = first.expires_at + timedelta(seconds=1)
    real_now = approval_queue_module._now
    clock = {"now": real_now}
    monkeypatch.setattr(approval_queue_module, "_now", lambda: clock["now"]())

    real_lock = approval_queue_module._lock_and_expire_pending
    approved = {"n": 0}

    async def _approve_then_lock(*args: Any, **kwargs: Any) -> ApprovalRequest | None:
        if approved["n"] == 0:
            approved["n"] += 1
            clock["now"] = real_now
            async with get_sessionmaker()() as s:
                await approve_request(s, first.id, operator=_operator(sub="op-reviewer"))
                await s.commit()
            clock["now"] = lambda: late
        return await real_lock(*args, **kwargs)

    monkeypatch.setattr(approval_queue_module, "_lock_and_expire_pending", _approve_then_lock)
    clock["now"] = lambda: late

    retry = await asyncio.wait_for(_park_direct(), timeout=10)

    assert retry.id == first.id
    assert retry.status == ApprovalRequestStatus.APPROVED.value
    assert getattr(retry, "_reused", False) is True
    assert getattr(retry, "_expired_request", None) is None
    assert approved["n"] == 1
    assert await _approval_row_count() == 1
    assert await _request_audit_count() == 1
    async with get_sessionmaker()() as s:
        decisions = (
            (await s.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    assert [d.payload.get("decision") for d in decisions] == ["approved"]


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

    row.resume_result = {
        "status": "error",
        "op_id": "POST:/vms",
        "extras": {"error_code": "audit_commit_failed", "execution_outcome": "uncertain"},
    }
    assert approval_result_state(row) is ApprovalResultState.UNCERTAIN

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

    # A MEHO-SA-Operator may approve another person's high-risk change (#3328).
    approver = _operator(sub="op-approver").model_copy(update={"self_approver": True})
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
    assert retry["error"] == (
        f"awaiting_approval: the approval request {request_id} is already approved"
    )
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        ({"status": "ok", "op_id": "POST:/vms", "result": {"id": "vm-7"}}, "executed"),
        ({"status": "error", "op_id": "POST:/vms", "error": "upstream 500"}, "failed"),
    ],
)
async def test_agent_resume_moves_list_and_show_off_started(
    monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any], expected: str
) -> None:
    """The in-process agent resume stores its outcome like the operator resume does.

    Before, only ``resume_dispatch_after_approval`` stored the result, so a
    request the agent waiter resumed stayed ``started`` forever and a client
    could not tell it from one still running.
    """
    row = await _park_direct(key=None)
    async with get_sessionmaker()() as s:
        await approve_request(s, row.id, operator=_operator(sub="op-approver"))
        await s.commit()

    async def _redispatch(
        _operator: Any, _arguments: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        return envelope

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _redispatch)
    resumed = await _resume_approved_or_already_resumed(
        operator=_operator(sub="op-requester"),
        call_arguments={
            "connector_id": "gh-rest-3",
            "op_id": "POST:/vms",
            "params": {"name": "web-01"},
            "target": None,
        },
        awaiting_envelope={"op_id": "POST:/vms", "extras": {}},
        approval_request_id=row.id,
    )
    assert resumed == envelope

    key = make_rsa_keypair("kid-3486-agent")
    headers = _headers(key, sub="op-approver")
    shown = await _get_json(f"/api/v1/approvals/{row.id}", headers, key)
    assert shown["status"] == "approved"
    assert shown["result_state"] == expected
    listed = await _get_json("/api/v1/approvals?status=approved", headers, key)
    (row_view,) = [v for v in listed if v["id"] == str(row.id)]
    assert row_view["result_state"] == expected


@pytest.mark.asyncio
async def test_view_preview_hash_is_none_for_non_destructive_row() -> None:
    row = await _park_direct(key=None)
    person = Operator(
        sub="view-reader",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=row.tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    view = _view(row, operator=person)
    assert view.preview_hash is None
    assert view.result_state is None


# ---------------------------------------------------------------------------
# The same call from the same login, with no key (APPROVAL_SAME_CALL_REUSE)
# ---------------------------------------------------------------------------


@pytest.fixture
def same_call_reuse_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APPROVAL_SAME_CALL_REUSE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _park_same_call(
    *,
    sub: str = "op-requester",
    tenant_id: uuid.UUID = _TENANT,
    key: str | None = None,
    params: dict[str, Any] | None = None,
    target_id: uuid.UUID | None = None,
    preview_hash: str | None = None,
    run_id: uuid.UUID | None = None,
    resume_parent: dict[str, Any] | None = None,
    op_id: str = "POST:/vms",
    connector_id: str = "gh-rest-3",
    reveal_secret: bool = False,
    requester: ResumeRequester | None = None,
) -> ApprovalRequest:
    operator = _operator(sub=sub).model_copy(update={"tenant_id": tenant_id})
    call_params = params if params is not None else {"name": "web-01"}
    target = SimpleNamespace(id=target_id) if target_id is not None else None
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=call_params,
            params_hash=compute_params_hash(call_params),
            idempotency_key=key,
            preview_hash=preview_hash,
            run_id=run_id,
            resume_parent=resume_parent,
            reveal_secret=reveal_secret,
            requester=requester,
            proposed_effect={"safety_level": "caution"},
        )
        await s.commit()
    return request


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_same_call_without_a_key_gets_the_waiting_request() -> None:
    a = await _park_same_call()
    b = await _park_same_call()
    assert b.id == a.id
    assert getattr(b, "_reused", False) is True
    assert await _approval_row_count() == 1
    assert await _request_audit_count() == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_same_call_with_another_key_gets_the_waiting_request() -> None:
    a = await _park_same_call(key="key-a")
    b = await _park_same_call(key="key-b")
    assert b.id == a.id
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_retry_with_the_second_key_gets_the_request_after_approval() -> None:
    a = await _park_same_call(key="key-a")
    b = await _park_same_call(key="key-b")
    assert b.id == a.id
    async with get_sessionmaker()() as s:
        await approve_request(s, a.id, operator=_operator(sub="op-approver"))
        await s.commit()

    retry_b = await _park_same_call(key="key-b")
    retry_a = await _park_same_call(key="key-a")

    assert retry_b.id == a.id
    assert getattr(retry_b, "_reused", False) is True
    assert retry_a.id == a.id
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_the_second_key_still_refuses_a_different_call() -> None:
    a = await _park_same_call(key="key-a")
    await _park_same_call(key="key-b")
    with pytest.raises(IdempotencyKeyConflictError) as exc:
        await _park_same_call(key="key-b", params={"name": "web-02"})
    assert exc.value.request_id == a.id


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_the_second_key_of_another_login_gets_its_own_request() -> None:
    a = await _park_same_call(sub="login-a", key="key-a")
    await _park_same_call(sub="login-a", key="key-b")
    other = await _park_same_call(sub="login-b", key="key-b")
    assert other.id != a.id
    assert getattr(other, "_reused", False) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
@pytest.mark.parametrize(
    "stored",
    [
        {"run_id": uuid.UUID("00000000-0000-0000-0000-000000006797")},
        {"resume_parent": {"op_id": "composite.op", "params": {}}},
    ],
)
async def test_a_direct_call_never_gets_a_stored_run_or_multi_step_request(
    stored: dict[str, Any],
) -> None:
    first = await _park_same_call(**stored)
    direct = await _park_same_call()
    assert direct.id != first.id
    assert getattr(direct, "_reused", False) is False
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
@pytest.mark.parametrize(
    "change",
    [
        {"params": {"name": "web-02"}},
        {"target_id": uuid.UUID("00000000-0000-0000-0000-000000006794")},
        {"preview_hash": "sha256:other"},
    ],
)
async def test_a_different_call_gets_its_own_request(change: dict[str, Any]) -> None:
    a = await _park_same_call()
    b = await _park_same_call(**change)
    assert b.id != a.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_different_work_ref_gets_its_own_request() -> None:
    token = work_ref_var.set("gh:example/repo#1")
    try:
        a = await _park_same_call()
    finally:
        work_ref_var.reset(token)
    token = work_ref_var.set("gh:example/repo#2")
    try:
        b = await _park_same_call()
    finally:
        work_ref_var.reset(token)
    assert b.id != a.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_another_login_in_the_same_tenant_gets_its_own_request() -> None:
    a = await _park_same_call(sub="login-a")
    b = await _park_same_call(sub="login-b")
    assert b.id != a.id
    assert getattr(b, "_reused", False) is False
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_another_tenant_gets_its_own_request() -> None:
    other_tenant = uuid.UUID("00000000-0000-0000-0000-000000006795")
    a = await _park_same_call()
    b = await _park_same_call(tenant_id=other_tenant)
    assert b.id != a.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
@pytest.mark.parametrize("closing", ["rejected", "withdrawn", "expired", "overdue", "approved"])
async def test_a_request_that_no_longer_waits_is_not_reused(
    closing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await _park_same_call()
    async with get_sessionmaker()() as s:
        if closing == "rejected":
            await reject_request(s, first.id, operator=_operator(sub="op-approver"), reason="no")
        elif closing == "withdrawn":
            await approval_queue_module.withdraw_request(
                s, first.id, operator=_operator(sub="op-requester")
            )
        elif closing == "approved":
            await approve_request(s, first.id, operator=_operator(sub="op-approver"))
        elif closing == "expired":
            row = await s.get(ApprovalRequest, first.id)
            assert row is not None
            row.status = ApprovalRequestStatus.EXPIRED.value
        await s.commit()
    if closing == "overdue":
        assert first.expires_at is not None
        late = first.expires_at + timedelta(seconds=1)
        monkeypatch.setattr(approval_queue_module, "_now", lambda: late)

    second = await _park_same_call()

    assert second.id != first.id
    assert getattr(second, "_reused", False) is False
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_call_in_an_agent_run_or_a_multi_step_step_parks_again() -> None:
    first = await _park_same_call()
    in_run = await _park_same_call(run_id=uuid.uuid4())
    step = await _park_same_call(resume_parent={"op_id": "composite.op", "params": {}})
    assert len({first.id, in_run.id, step.id}) == 3
    assert await _approval_row_count() == 3


def _person_behind(sub: str) -> list[dict[str, Any]]:
    return [{"sub": sub, "name": None, "why": "agent_owner"}]


async def _park_machine_call(
    monkeypatch: pytest.MonkeyPatch, people_behind: list[dict[str, Any]]
) -> ApprovalRequest:
    """Park the same call of one agent login, with *people_behind* behind it."""

    async def _find(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return people_behind

    monkeypatch.setattr(approval_queue_module, "find_people_behind", _find)
    operator = _operator(sub="agent-login", principal_kind=PrincipalKind.AGENT)
    call_params = {"name": "web-01"}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=operator,
            connector_id="gh-rest-3",
            op_id="POST:/vms",
            target=None,
            params=call_params,
            params_hash=compute_params_hash(call_params),
            proposed_effect={"safety_level": "caution"},
        )
        await s.commit()
    return request


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_call_with_other_people_behind_the_login_gets_its_own_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _park_machine_call(monkeypatch, _person_behind("person-a"))
    second = await _park_machine_call(monkeypatch, _person_behind("person-b"))

    assert second.id != first.id
    assert getattr(second, "_reused", False) is False
    assert await _approval_row_count() == 2
    async with get_sessionmaker()() as s:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s, second.id, operator=_operator(sub="person-b"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_call_with_the_same_people_behind_the_login_gets_the_waiting_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _park_machine_call(monkeypatch, _person_behind("Person-A"))
    second = await _park_machine_call(monkeypatch, _person_behind("person-a"))

    assert second.id == first.id
    assert getattr(second, "_reused", False) is True
    assert await _approval_row_count() == 1


async def _park_as_kind(monkeypatch: pytest.MonkeyPatch, kind: PrincipalKind) -> ApprovalRequest:
    """Park the same call of one login id, signed in as *kind*.

    A person has no people behind (``None``). An agent login has nobody known
    behind it (``[]``).
    """

    async def _find(*_args: Any, **kwargs: Any) -> list[dict[str, Any]] | None:
        return None if kwargs["kind"] == PrincipalKind.USER.value else []

    monkeypatch.setattr(approval_queue_module, "find_people_behind", _find)
    operator = _operator(sub="shared-login", principal_kind=kind)
    call_params = {"name": "web-01"}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=operator,
            connector_id="gh-rest-3",
            op_id="POST:/vms",
            target=None,
            params=call_params,
            params_hash=compute_params_hash(call_params),
            proposed_effect={"safety_level": "caution"},
        )
        await s.commit()
    return request


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_the_same_login_as_another_kind_gets_its_own_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    as_person = await _park_as_kind(monkeypatch, PrincipalKind.USER)
    as_agent = await _park_as_kind(monkeypatch, PrincipalKind.AGENT)

    assert as_agent.id != as_person.id
    assert getattr(as_agent, "_reused", False) is False
    assert as_agent.principal_kind == PrincipalKind.AGENT.value
    assert await _approval_row_count() == 2
    async with get_sessionmaker()() as s:
        with pytest.raises(UnknownRequesterApprovalError):
            await approve_request(s, as_agent.id, operator=_operator(sub="other-person"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.AGENT])
async def test_the_same_login_as_the_same_kind_gets_the_waiting_request(
    monkeypatch: pytest.MonkeyPatch, kind: PrincipalKind
) -> None:
    first = await _park_as_kind(monkeypatch, kind)
    second = await _park_as_kind(monkeypatch, kind)

    assert second.id == first.id
    assert getattr(second, "_reused", False) is True
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
async def test_same_call_without_a_key_parks_again_with_the_switch_off() -> None:
    a = await _park_same_call()
    b = await _park_same_call()
    assert a.id != b.id
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
@pytest.mark.parametrize(
    "change",
    [
        {"reveal_secret": True},
        {"op_id": "POST:/vms/other"},
        {"connector_id": "gh-rest-4"},
    ],
)
async def test_the_same_params_with_another_choice_or_operation_get_their_own_request(
    change: dict[str, Any],
) -> None:
    a = await _park_same_call()
    b = await _park_same_call(**change)
    assert b.id != a.id
    assert getattr(b, "_reused", False) is False
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_rerun_for_an_earlier_requester_gets_its_own_request() -> None:
    a = await _park_same_call()
    rerun = await _park_same_call(
        requester=ResumeRequester(sub="op-requester", act=None, name=None)
    )
    assert rerun.id != a.id
    assert getattr(rerun, "_reused", False) is False
    assert await _approval_row_count() == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_the_second_key_of_the_same_login_in_another_tenant_gets_its_own_request() -> None:
    other_tenant = uuid.UUID("00000000-0000-0000-0000-000000006796")
    a = await _park_same_call(key="key-a")
    assert (await _park_same_call(key="key-b")).id == a.id

    other = await _park_same_call(tenant_id=other_tenant, key="key-b")

    assert other.id != a.id
    assert other.tenant_id == other_tenant
    assert getattr(other, "_reused", False) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_the_second_key_does_not_get_a_rejected_request() -> None:
    a = await _park_same_call(key="key-a")
    assert (await _park_same_call(key="key-b")).id == a.id
    async with get_sessionmaker()() as s:
        await reject_request(s, a.id, operator=_operator(sub="op-approver"), reason="no")
        await s.commit()

    retry_b = await _park_same_call(key="key-b")

    assert retry_b.id != a.id
    assert retry_b.status == ApprovalRequestStatus.PENDING.value
    assert getattr(retry_b, "_reused", False) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_call_that_gets_the_waiting_request_writes_its_own_audit_row(
    embedding: list[float],
) -> None:
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)
    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"]}
    first_session, second_session = uuid.uuid4(), uuid.uuid4()

    token = agent_session_id_var.set(first_session)
    try:
        first = await call_operation(requester, bound)
    finally:
        agent_session_id_var.reset(token)
    token = agent_session_id_var.set(second_session)
    try:
        second = await call_operation(requester, bound)
    finally:
        agent_session_id_var.reset(token)

    request_id = first["extras"]["approval_request_id"]
    assert second["extras"]["approval_request_id"] == request_id
    assert second["extras"]["approval_request_reused"] is True
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.method == "APPROVAL")
                    .order_by(AuditLog.occurred_at)
                )
            )
            .scalars()
            .all()
        )
    assert [row.path for row in rows] == ["approval.request", "approval.reuse"]
    request_row, reuse_row = rows
    assert first["audit_id"] == str(request_row.id)
    assert second["audit_id"] == str(reuse_row.id)
    assert reuse_row.payload["approval_request_id"] == request_id
    assert reuse_row.payload["params_hash"] == compute_audit_params_hash(_PARAMS)
    assert request_row.agent_session_id == first_session
    assert reuse_row.agent_session_id == second_session
    assert reuse_row.parent_audit_id == request_row.id
    assert reuse_row.status_code == 202
    assert await _approval_row_count() == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("same_call_reuse_on")
async def test_a_late_retry_that_gets_another_waiting_request_sends_the_old_expiry(
    embedding: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key A passes its deadline, key B makes B, a retry with key A expires A and gets B."""
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)
    recorded: list[tuple[str, uuid.UUID]] = []

    async def _record(**kwargs: Any) -> None:
        recorded.append((kwargs["decision"], kwargs["request"].id))

    monkeypatch.setattr(approval_queue_module, "record_approval_step_event", _record)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"]}
    first = await call_operation(requester, {**bound, "idempotency_key": "key-a"})
    first_id = uuid.UUID(first["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, first_id)
        assert row is not None and row.expires_at is not None
        late = row.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(approval_queue_module, "_now", lambda: late)

    second = await call_operation(requester, {**bound, "idempotency_key": "key-b"})
    second_id = uuid.UUID(second["extras"]["approval_request_id"])
    assert second_id != first_id

    retry = await call_operation(requester, {**bound, "idempotency_key": "key-a"})
    assert retry["status"] == "awaiting_approval", retry
    assert uuid.UUID(retry["extras"]["approval_request_id"]) == second_id
    assert retry["extras"]["approval_request_reused"] is True
    assert recorded == [
        ("pending", first_id),
        ("pending", second_id),
        ("expired", first_id),
    ]
    async with get_sessionmaker()() as s:
        expired = await s.get(ApprovalRequest, first_id)
        assert expired is not None
        assert expired.status == ApprovalRequestStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_retry_of_an_approved_request_says_it_is_approved(embedding: list[float]) -> None:
    """Approved, not yet run: the retry gets the same request and a true text."""
    connector = _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)

    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"], "idempotency_key": _KEY}
    first = await call_operation(requester, bound)
    request_id = first["extras"]["approval_request_id"]

    approver = _operator(sub="op-approver").model_copy(update={"self_approver": True})
    async with get_sessionmaker()() as s:
        await approve_request(s, uuid.UUID(request_id), operator=approver, params=None)
        await s.commit()

    retry = await call_operation(requester, bound)
    assert retry["status"] == "awaiting_approval", retry
    assert retry["extras"]["approval_request_id"] == request_id
    assert retry["extras"]["approval_request_reused"] is True
    assert set(retry["extras"]) == set(first["extras"]) | {"approval_request_reused"}
    assert retry["error"] == (
        f"awaiting_approval: the approval request {request_id} is already approved"
    )
    assert await _approval_row_count() == 1
    assert connector.calls == []


#: The reads of the secret value, made by :func:`_counting_read`.
_secret_reads: list[dict[str, Any]] = []
_plain_read = reveal_helpers._read_handler


async def _counting_read(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    _secret_reads.append(params)
    return await _plain_read(operator, target, params)


@pytest.mark.asyncio
async def test_retry_of_an_approved_secret_request_says_only_it_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approved request to show a secret value: the retry runs nothing, the value is still there."""
    _secret_reads.clear()
    monkeypatch.setattr(reveal_helpers, "_read_handler", _counting_read)
    await reveal_helpers.register_vault_read()

    requester = _operator(sub="op-requester")
    call = {
        "operator": requester,
        "connector_id": reveal_helpers.CONNECTOR_ID,
        "op_id": reveal_helpers.READ_OP,
        "target": None,
        "params": {"path": "app/db", "reveal_secret": True},
        "idempotency_key": _KEY,
    }
    first = await dispatch(**call)
    assert first.status == "awaiting_approval", first.error
    request_id = uuid.UUID(first.extras["approval_request_id"])

    reviewer = requester.model_copy(
        update={"sub": "reviewer-6793", "tenant_role": TenantRole.TENANT_ADMIN}
    )
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=reviewer)
        await s.commit()

    retry = await dispatch(**call)
    assert retry.status == "awaiting_approval", retry.error
    assert retry.extras["approval_request_id"] == str(request_id)
    assert retry.extras["approval_request_reused"] is True
    assert (
        retry.error == f"awaiting_approval: the approval request {request_id} is already approved"
    )
    assert reveal_helpers.SECRET_VALUE not in retry.model_dump_json()
    assert await _approval_row_count() == 1
    assert _secret_reads == []

    fetched = await fetch_approved_reveal(requester, request_id)
    assert fetched.status == "ok", fetched.error
    assert reveal_helpers.SECRET_VALUE in fetched.model_dump_json()
    assert len(_secret_reads) == 1


# ---------------------------------------------------------------------------
# A retry with the same key writes its own audit row (APPROVAL_RETRY_OWN_AUDIT_ROW)
# ---------------------------------------------------------------------------


@pytest.fixture
def retry_own_row_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APPROVAL_RETRY_OWN_AUDIT_ROW", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _approval_audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.method == "APPROVAL")
                    .order_by(AuditLog.occurred_at)
                )
            )
            .scalars()
            .all()
        )


async def _call_twice_with_one_key(
    embedding: list[float], monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], dict[str, Any], list[str], tuple[uuid.UUID, uuid.UUID]]:
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)
    events: list[str] = []

    async def _record(**kwargs: Any) -> None:
        events.append(kwargs["decision"])

    monkeypatch.setattr(approval_queue_module, "record_approval_step_event", _record)
    requester = _operator(sub="op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "gh-prod", "params": _PARAMS}
    preview = await preview_operation(requester, args)
    bound = {**args, "preview_hash": preview["preview_hash"], "idempotency_key": "retry-key"}
    sessions = (uuid.uuid4(), uuid.uuid4())
    answers = []
    for session_id in sessions:
        token = agent_session_id_var.set(session_id)
        try:
            answers.append(await call_operation(requester, bound))
        finally:
            agent_session_id_var.reset(token)
    return answers[0], answers[1], events, sessions


@pytest.mark.asyncio
@pytest.mark.usefixtures("retry_own_row_on")
async def test_a_retry_with_the_same_key_writes_its_own_audit_row(
    embedding: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, events, (first_session, second_session) = await _call_twice_with_one_key(
        embedding, monkeypatch
    )

    request_id = first["extras"]["approval_request_id"]
    assert second["extras"]["approval_request_id"] == request_id
    assert second["extras"]["approval_request_reused"] is True
    rows = await _approval_audit_rows()
    assert [row.path for row in rows] == ["approval.request", "approval.reuse"]
    request_row, reuse_row = rows
    assert first["audit_id"] == str(request_row.id)
    assert second["audit_id"] == str(reuse_row.id)
    assert reuse_row.payload["approval_request_id"] == request_id
    assert reuse_row.payload["params_hash"] == compute_audit_params_hash(_PARAMS)
    assert request_row.agent_session_id == first_session
    assert reuse_row.agent_session_id == second_session
    assert await _approval_row_count() == 1
    assert events == ["pending"]


@pytest.mark.asyncio
async def test_a_retry_with_the_same_key_writes_no_row_with_the_switch_off(
    embedding: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, events, _ = await _call_twice_with_one_key(embedding, monkeypatch)

    assert second["extras"]["approval_request_id"] == first["extras"]["approval_request_id"]
    rows = await _approval_audit_rows()
    assert [row.path for row in rows] == ["approval.request"]
    assert second["audit_id"] == first["audit_id"] == str(rows[0].id)
    assert events == ["pending"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("retry_own_row_on")
async def test_a_retry_of_an_approved_request_writes_its_own_audit_row() -> None:
    a = await _park_same_call(key="key-a")
    async with get_sessionmaker()() as s:
        await approve_request(s, a.id, operator=_operator(sub="op-approver"))
        await s.commit()

    retry = await _park_same_call(key="key-a")

    assert retry.id == a.id
    reuse_rows = [r for r in await _approval_audit_rows() if r.path == "approval.reuse"]
    assert len(reuse_rows) == 1
    assert reuse_rows[0].payload["params_hash"] == compute_audit_params_hash({"name": "web-01"})
    assert getattr(retry, "_reuse_audit_id", None) == reuse_rows[0].id


@pytest.mark.asyncio
@pytest.mark.usefixtures("retry_own_row_on")
async def test_the_retry_row_hashes_the_params_with_secret_values_masked() -> None:
    params = {"name": "web-01", "password": "first-test-value"}
    first = await _park_same_call(key="key-secret", params=params)
    retry = await _park_same_call(key="key-secret", params=params)

    assert retry.id == first.id
    reuse_rows = [r for r in await _approval_audit_rows() if r.path == "approval.reuse"]
    assert len(reuse_rows) == 1
    stored = reuse_rows[0].payload["params_hash"]
    assert stored == compute_audit_params_hash({"name": "web-01", "password": "other-test-value"})
    assert stored != compute_params_hash(params)
    assert "first-test-value" not in str(reuse_rows[0].payload)
