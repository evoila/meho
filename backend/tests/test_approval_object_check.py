# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved run acts only on the object the approver saw.

This suite pins the object check every approved run makes before it
starts (``operations/approval_object_check.py``):

* The approval card names the object by id or key, or by its fields when
  the system has no id. Counts and yes/no flags do not count.
* Right before the run, MEHO builds the card again against the live
  system. A live object with another identity refuses the run and nothing
  is sent. The same object runs. A gone object runs, so the handler can
  answer "already absent" itself. A card that does not build refuses the
  run: an unreadable object is never taken for the approved one. A card
  that said the object was absent refuses the run when an object is there
  now.
* All three run paths check: the approve surfaces, the agent's own resume
  and the re-run after a failure.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet

import meho_backplane.operations._request_preview as preview_module
import meho_backplane.operations.approval_object_check as object_check_module
import meho_backplane.operations.dispatcher as dispatcher_module
import meho_backplane.operations.meta_tools as meta_tools_module
from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_object_check import (
    REASON_APPEARED,
    REASON_CHANGED,
    REASON_UNREADABLE,
    card_object,
    live_card,
    object_identity,
)
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approval_rerun import (
    RerunNeedsNewApprovalError,
    RerunRefusal,
    rerun_failed_request,
)
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000b0b1")
_OP_ID = "objcheck.user.delete"
_CONNECTOR_ID = "objcheck-1.x"
_PARAMS: dict[str, Any] = {"username": "bob"}
_PREVIEW: dict[str, Any] = {
    "status": "ok",
    "connector_id": _CONNECTOR_ID,
    "op_id": _OP_ID,
    "method": "COMPOSITE",
    "resolved_path": _OP_ID,
    "query": None,
    "redacted_body": _PARAMS,
}
_PREVIEW_HASH = preview_module.compute_preview_hash(_PREVIEW)

#: The card of a user found by name: the approver saw this id.
_USER_CARD: dict[str, Any] = {
    "kind": "user",
    "username": "bob",
    "id": "1111-first",
    "present": True,
    "enabled": True,
    "groups": ["/ops"],
}
#: The card of a user that was not there: the approver saw "nothing to delete".
_ABSENT_USER_CARD: dict[str, Any] = {
    "kind": "user",
    "username": "bob",
    "id": None,
    "present": False,
}
#: A card of a DNS record set: the fields stay the same with or without records.
_DNS_CARD: dict[str, Any] = {
    "kind": "dns_record",
    "zone": "example.test",
    "name": "web.example.test.",
    "type": "A",
    "view": None,
}
#: A card of an object whose system has no id (a firewall alias).
_ALIAS_CARD: dict[str, Any] = {
    "kind": "alias",
    "name": "web_servers",
    "type": "host",
    "address": "10.0.0.0/24",
    "descr": None,
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(sub: str = "agent-object-check") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


def _card(obj: dict[str, Any] | None) -> dict[str, Any]:
    """A stored card the way the dispatcher stores it: the block at the top level."""
    effect: dict[str, Any] = {"op_id": _OP_ID, "connector_id": _CONNECTOR_ID}
    if obj is not None:
        effect["blast_radius"] = {"object": obj, "children": [], "irreversibility": "permanent"}
    return effect


async def _persist_target(name: str) -> uuid.UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=name,
                product="objcheck",
                host="first.invalid",
                port=443,
                secret_ref="kv/objcheck/first",
                aliases=[],
                extras={},
            )
        )
        await s.commit()
    return target_id


async def _park_approved(
    target_id: uuid.UUID | None,
    *,
    card: dict[str, Any] | None = _USER_CARD,
    preview_hash: str | None = _PREVIEW_HASH,
    failed_run: bool = False,
) -> ApprovalRequest:
    """Park one request with its approval card, then approve it.

    With *failed_run* the request also stores a failed run, the state the
    re-run path needs.
    """
    target = SimpleNamespace(id=target_id, fqdn=None) if target_id is not None else None
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params=dict(_PARAMS),
            params_hash=compute_params_hash(_PARAMS),
            run_id=uuid.uuid4(),
            proposed_effect=_card(card),
            preview_hash=preview_hash,
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.decided_at = datetime.now(UTC)
        request.reviewed_by = "op-approver"
        if failed_run:
            request.resumed_at = datetime.now(UTC)
            request.resume_result = {
                "status": "error",
                "op_id": _OP_ID,
                "error": "connector_error: timeout",
            }
        await s.commit()
    return request


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return row


def _spy_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the dispatcher; every call stands for a call to the vendor."""
    calls: list[dict[str, Any]] = []

    async def _fake_dispatch(**kwargs: Any) -> OperationResult:
        calls.append(kwargs)
        return OperationResult(status="ok", op_id=kwargs["op_id"], data={}, duration_ms=1.0)

    monkeypatch.setattr(dispatcher_module, "dispatch", _fake_dispatch)
    return calls


def _spy_agent_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the agent's approved re-dispatch; record every call."""
    seen: list[dict[str, Any]] = []

    async def _spy(
        _operator: Operator, arguments: dict[str, Any], *, resolved_target: Any = None
    ) -> dict[str, Any]:
        seen.append({"arguments": arguments, "target_id": getattr(resolved_target, "id", None)})
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    return seen


def _same_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview hash check passes; only the object check decides."""

    async def _preview(**_kwargs: Any) -> dict[str, Any]:
        return _PREVIEW

    monkeypatch.setattr(preview_module, "preview_dispatch", _preview)


def _live_object(
    monkeypatch: pytest.MonkeyPatch,
    obj: dict[str, Any] | None,
    *,
    builds: bool = True,
    unavailable: bool = False,
) -> list[dict[str, Any]]:
    """The card MEHO builds right before the run names *obj*.

    ``builds=False`` means no card at all (the op did not resolve, or the
    build raised). ``unavailable=True`` means the card carries the
    ``preview_unavailable`` marker a builder that raised leaves behind.
    """
    calls: list[dict[str, Any]] = []

    async def _fake_live_card(operator: Operator, **kwargs: Any) -> dict[str, Any] | None:
        calls.append(kwargs)
        if not builds:
            return None
        if unavailable:
            return {**_card(None), "preview_unavailable": True, "preview_error": "read failed"}
        return _card(obj)

    monkeypatch.setattr(object_check_module, "live_card", _fake_live_card)
    return calls


async def _agent_resume(request: ApprovalRequest, target_name: str) -> dict[str, Any]:
    return await _resume_approved_or_already_resumed(
        operator=_operator(),
        call_arguments={
            "connector_id": _CONNECTOR_ID,
            "op_id": _OP_ID,
            "params": dict(_PARAMS),
            "target": {"name": target_name},
        },
        awaiting_envelope={"op_id": _OP_ID, "extras": {}},
        approval_request_id=request.id,
    )


# ---------------------------------------------------------------------------
# What names the object
# ---------------------------------------------------------------------------


async def test_identity_is_the_id_when_the_card_has_one() -> None:
    assert object_identity(_USER_CARD) == {"id": "1111-first"}


async def test_identity_is_the_key_when_the_card_has_one() -> None:
    assert object_identity({"kind": "drs_rule", "name": "web-apart", "key": 14}) == {"key": 14}


@pytest.mark.parametrize(
    "obj",
    [
        {**_USER_CARD, "present": False, "id": None},
        {**_USER_CARD, "id": None},
        {**_USER_CARD, "id": ""},
        {"kind": "realm", "realm": "r", "present": False},
    ],
)
async def test_identity_is_none_when_the_card_says_the_object_is_absent(
    obj: dict[str, Any],
) -> None:
    assert object_identity(obj) is None


async def test_identity_without_id_is_the_card_fields_without_counts_and_flags() -> None:
    obj = {
        "kind": "realm",
        "realm": "customers",
        "present": True,
        "enabled": True,
        "users": 12,
        "clients": 3,
        "children_names": ["a"],
    }

    assert object_identity(obj) == {
        "kind": "realm",
        "realm": "customers",
        "children_names": ["a"],
    }


async def test_identity_reads_like_the_stored_card() -> None:
    """The stored card is JSON; a live tuple must equal its stored list."""
    live = {"kind": "dns_record", "zone": "example.test", "values": ("a", "b")}
    stored = {"kind": "dns_record", "zone": "example.test", "values": ["a", "b"]}

    assert object_identity(live) == object_identity(stored)
    assert object_identity({"kind": "x", "key": (1, 2)}) == {"key": [1, 2]}


async def test_card_object_is_read_from_the_top_level_or_the_preview_body() -> None:
    top = {"blast_radius": {"object": _ALIAS_CARD}}
    nested = {"preview": {"blast_radius": {"object": _ALIAS_CARD}}}

    assert card_object(top) == _ALIAS_CARD
    assert card_object(nested) == _ALIAS_CARD
    assert card_object({"op_id": _OP_ID}) is None
    assert card_object({"blast_radius": {"object": "not-a-block"}}) is None
    assert card_object(None) is None


@pytest.mark.parametrize(
    ("block", "absent"),
    [
        pytest.param({"object": _DNS_CARD, "children": [], "match_count": 0}, True, id="zero"),
        pytest.param(
            {"object": _DNS_CARD, "children": [{"rdata": "192.0.2.1"}], "match_count": 1},
            False,
            id="one",
        ),
        pytest.param({"object": _DNS_CARD, "children": []}, False, id="no-count"),
        pytest.param({"object": _DNS_CARD, "match_count": "0"}, False, id="not-a-number"),
        pytest.param({"object": _DNS_CARD, "match_count": False}, False, id="a-flag"),
    ],
)
async def test_card_object_keeps_the_absence_a_zero_count_states(
    block: dict[str, Any], absent: bool
) -> None:
    """A DNS card says "nothing there" only through its count; the object must keep that."""
    obj = card_object({"blast_radius": block})

    assert obj is not None
    if absent:
        assert obj == {**_DNS_CARD, "present": False}
        assert object_identity(obj) is None
    else:
        assert obj == _DNS_CARD
        assert object_identity(obj) == _DNS_CARD


async def test_live_card_is_none_when_the_op_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _miss(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "error", "error": "unknown_op"}

    monkeypatch.setattr(preview_module, "_resolve_descriptor", _miss)

    assert (
        await live_card(
            _operator(), connector_id=_CONNECTOR_ID, op_id=_OP_ID, target=None, params=_PARAMS
        )
        is None
    )


# ---------------------------------------------------------------------------
# The approve surfaces (REST, console and MCP share this function)
# ---------------------------------------------------------------------------


async def test_operator_resume_is_refused_when_a_new_object_has_the_approved_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-replaced")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, {**_USER_CARD, "id": "2222-second", "groups": ["/admins"]})
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == [], "the vendor must get no call"
    assert result.status == "denied"
    assert result.extras["error_code"] == "object_changed_since_approval"
    assert result.extras["reason"] == REASON_CHANGED
    stored = await _row(request.id)
    assert stored.resumed_at is not None, "the request can never run later"
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "object_changed_since_approval"


async def test_operator_resume_runs_when_the_object_is_the_approved_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-same")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    # Only the id counts: a new group membership does not stop the run.
    _live_object(monkeypatch, {**_USER_CARD, "groups": ["/ops", "/billing"], "enabled": False})
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok"
    assert len(calls) == 1
    assert calls[0]["params"] == _PARAMS


async def test_operator_resume_runs_when_the_object_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-gone")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, {**_USER_CARD, "id": None, "present": False})
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok", "the handler answers 'already absent' itself"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "live_card_kwargs",
    [
        pytest.param({"builds": False}, id="no-card"),
        pytest.param({"unavailable": True}, id="builder-raised"),
    ],
)
async def test_operator_resume_is_refused_when_the_card_does_not_build(
    monkeypatch: pytest.MonkeyPatch, live_card_kwargs: dict[str, Any]
) -> None:
    """An unreadable object is never taken for the approved one.

    The op does not resolve, the builder declines, or it finds the object
    and then fails while reading the rest of the card (a user's groups):
    the card has no object, and the run must not look the name up again.
    """
    target_id = await _persist_target(f"kc-no-card-{uuid.uuid4().hex[:6]}")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, None, **live_card_kwargs)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == [], "the vendor must get no call"
    assert result.status == "denied"
    assert result.extras["error_code"] == "object_changed_since_approval"
    assert result.extras["reason"] == REASON_UNREADABLE
    stored = await _row(request.id)
    assert stored.resumed_at is not None, "the request can never run later"
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["reason"] == REASON_UNREADABLE


async def test_operator_resume_is_refused_when_an_object_appeared_under_an_absent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approver saw "nothing to delete"; the run must not delete what came later."""
    target_id = await _persist_target("kc-appeared")
    request = await _park_approved(target_id, card=_ABSENT_USER_CARD)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, {**_USER_CARD, "id": "3333-new"})
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == [], "the vendor must get no call"
    assert result.status == "denied"
    assert result.extras["error_code"] == "object_changed_since_approval"
    assert result.extras["reason"] == REASON_APPEARED
    stored = await _row(request.id)
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["reason"] == REASON_APPEARED


async def test_operator_resume_runs_when_the_object_is_still_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-still-absent")
    request = await _park_approved(target_id, card=_ABSENT_USER_CARD)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, dict(_ABSENT_USER_CARD))
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok", "the handler answers 'already absent' itself"
    assert len(calls) == 1


async def test_operator_resume_is_refused_when_the_absent_card_does_not_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card that said "absent" still needs the live card to build."""
    target_id = await _persist_target("kc-absent-no-card")
    request = await _park_approved(target_id, card=_ABSENT_USER_CARD)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, None, builds=False)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == []
    assert result.extras["reason"] == REASON_UNREADABLE


@pytest.mark.parametrize(
    ("live", "runs"),
    [
        (dict(_ALIAS_CARD), True),
        ({**_ALIAS_CARD, "address": "10.9.0.0/24"}, False),
        ({**_ALIAS_CARD, "type": "network"}, False),
        ({**_ALIAS_CARD, "descr": "new owner"}, False),
    ],
)
async def test_object_without_id_is_compared_by_its_fields(
    monkeypatch: pytest.MonkeyPatch, live: dict[str, Any], runs: bool
) -> None:
    target_id = await _persist_target(f"fw-{uuid.uuid4().hex[:6]}")
    request = await _park_approved(target_id, card=_ALIAS_CARD)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, live)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    if runs:
        assert result.status == "ok"
        assert len(calls) == 1
    else:
        assert calls == []
        assert result.extras["error_code"] == "object_changed_since_approval"


@pytest.mark.parametrize(
    ("approved_count", "live_count", "runs", "reason"),
    [
        pytest.param(0, 2, False, REASON_APPEARED, id="appeared"),
        pytest.param(0, 0, True, None, id="still-absent"),
        pytest.param(2, 0, True, None, id="gone"),
        pytest.param(2, 1, True, None, id="fewer-values"),
    ],
)
async def test_record_set_card_is_compared_by_its_count_as_well(
    monkeypatch: pytest.MonkeyPatch,
    approved_count: int,
    live_count: int,
    runs: bool,
    reason: str | None,
) -> None:
    """The record set keeps its name; only the count says whether records are there."""
    target_id = await _persist_target(f"dns-{uuid.uuid4().hex[:6]}")
    request = await _park_approved(target_id, card=_DNS_CARD)
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request.id)
        assert row is not None
        effect = dict(row.proposed_effect)
        effect["blast_radius"] = {**effect["blast_radius"], "match_count": approved_count}
        row.proposed_effect = effect
        await s.commit()
    _same_preview(monkeypatch)
    live_block = {"object": _DNS_CARD, "children": [], "match_count": live_count}
    _live_object(monkeypatch, None)

    async def _fake_live_card(operator: Operator, **_kwargs: Any) -> dict[str, Any]:
        return {**_card(None), "blast_radius": live_block}

    monkeypatch.setattr(object_check_module, "live_card", _fake_live_card)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    if runs:
        assert result.status == "ok"
        assert len(calls) == 1
    else:
        assert calls == [], "the vendor must get no call"
        assert result.extras["error_code"] == "object_changed_since_approval"
        assert result.extras["reason"] == reason


async def test_request_without_preview_hash_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-no-hash")
    request = await _park_approved(target_id, preview_hash=None)
    live_calls = _live_object(monkeypatch, {**_USER_CARD, "id": "2222-second"})
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok"
    assert len(calls) == 1
    assert live_calls == [], "a request without a preview hash builds no card"


async def test_card_without_object_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    target_id = await _persist_target("kc-no-object")
    request = await _park_approved(target_id, card=None)
    _same_preview(monkeypatch)
    live_calls = _live_object(monkeypatch, _USER_CARD)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok"
    assert len(calls) == 1
    assert live_calls == []


async def test_live_card_is_built_for_the_saved_target_and_stored_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-args")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    live_calls = _live_object(monkeypatch, _USER_CARD)
    _spy_dispatch(monkeypatch)

    await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert len(live_calls) == 1
    assert live_calls[0]["connector_id"] == _CONNECTOR_ID
    assert live_calls[0]["op_id"] == _OP_ID
    assert live_calls[0]["params"] == _PARAMS
    assert live_calls[0]["target"].id == target_id


# ---------------------------------------------------------------------------
# The agent's own resume
# ---------------------------------------------------------------------------


async def test_agent_resume_is_refused_when_a_new_object_has_the_approved_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-agent-replaced")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, {**_USER_CARD, "id": "2222-second"})
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, "kc-agent-replaced")

    assert seen == []
    assert resumed["status"] == "denied"
    assert resumed["extras"]["error_code"] == "object_changed_since_approval"
    stored = await _row(request.id)
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "object_changed_since_approval"


@pytest.mark.parametrize(
    ("card", "live", "reason"),
    [
        pytest.param(_USER_CARD, None, REASON_UNREADABLE, id="unreadable"),
        pytest.param(
            _ABSENT_USER_CARD, {**_USER_CARD, "id": "3333-new"}, REASON_APPEARED, id="appeared"
        ),
    ],
)
async def test_agent_resume_is_refused_when_the_object_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    card: dict[str, Any],
    live: dict[str, Any] | None,
    reason: str,
) -> None:
    name = f"kc-agent-{uuid.uuid4().hex[:6]}"
    target_id = await _persist_target(name)
    request = await _park_approved(target_id, card=card)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, live, builds=live is not None)
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, name)

    assert seen == []
    assert resumed["status"] == "denied"
    assert resumed["extras"]["reason"] == reason


async def test_agent_resume_runs_when_the_object_is_the_approved_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("kc-agent-same")
    request = await _park_approved(target_id)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, dict(_USER_CARD))
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, "kc-agent-same")

    assert resumed == {"status": "ok"}
    assert len(seen) == 1
    assert seen[0]["target_id"] == target_id


# ---------------------------------------------------------------------------
# The re-run after a failed run
# ---------------------------------------------------------------------------


async def test_rerun_is_refused_when_a_new_object_has_the_approved_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _park_approved(None, failed_run=True)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, {**_USER_CARD, "id": "2222-second"})
    calls = _spy_dispatch(monkeypatch)

    with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request.id)

    assert exc_info.value.reason is RerunRefusal.OBJECT_CHANGED
    assert calls == []
    assert (await _row(request.id)).rerun_count == 0


@pytest.mark.parametrize(
    ("card", "live"),
    [
        pytest.param(_USER_CARD, None, id="unreadable"),
        pytest.param(_ABSENT_USER_CARD, {**_USER_CARD, "id": "3333-new"}, id="appeared"),
    ],
)
async def test_rerun_is_refused_when_the_object_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch, card: dict[str, Any], live: dict[str, Any] | None
) -> None:
    request = await _park_approved(None, card=card, failed_run=True)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, live, builds=live is not None)
    calls = _spy_dispatch(monkeypatch)

    with pytest.raises(RerunNeedsNewApprovalError) as exc_info:
        await rerun_failed_request(operator=_operator(), request_id=request.id)

    assert exc_info.value.reason is RerunRefusal.OBJECT_CHANGED
    assert calls == []


async def test_rerun_runs_when_the_object_is_the_approved_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _park_approved(None, failed_run=True)
    _same_preview(monkeypatch)
    _live_object(monkeypatch, dict(_USER_CARD))
    calls = _spy_dispatch(monkeypatch)

    outcome = await rerun_failed_request(operator=_operator(), request_id=request.id)

    assert outcome.result.status == "ok"
    assert len(calls) == 1
