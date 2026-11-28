# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, a delivery call that deletes waits for a person.

The seven delivery calls of the GitOps and cluster connectors carry no
delete sign on the whole operation. With the switch on, a sync or a
rollback with ``prune`` ``true`` has a delete sign of its own, read from
its params: it waits for a person, under a standing grant and under a run
approval. A sync or a rollback without ``prune`` runs under the grant. A
refresh has no check. The app set, the cluster apply, the job create and
the command run wait for a person on every call, with the switch on or off.

Each call goes the whole path: ``preview_operation`` and ``call_operation``,
then the dispatcher and the real gate. Each operation is a stand-in on a
made-up connector, with the op id, the risk level and the approval flag of
the built-in operation (read from ``tests/snapshots/approval_table.json``),
so no call reaches a real system.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.argocd.op_classes import (
    ROLLBACK_PRUNE_LINE,
    SYNC_PRUNE_LINE,
    rollback_prunes,
    sync_prunes,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, ServicePrincipalGrant, Target
from meho_backplane.op_classes import (
    UNREADABLE_DELETE_REASON,
    declare_delete_check,
    params_delete_reason,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from tests._hard_to_undo_helpers import (
    RUNS,
    SERVICE,
    TENANT,
    add_connector,
    custom_policy,
    finish,
    register_op,
    seed_run_approval,
    seed_target,
    start,
)
from tests._op_class_state import kept_op_class_state
from tests._sandbox_mode_helpers import seed_grant_row

_PRODUCT = "deliver"
_CONNECTOR = "deliver-api-1.0"
_TARGET = "deliver-app-01"
_RUN = "run-deliver-app"
_SNAPSHOT = Path(__file__).parent / "snapshots" / "approval_table.json"

_SYNC = "argocd.app.sync"
_ROLLBACK = "argocd.app.rollback"
_REFRESH = "argocd.app.refresh"
#: The calls that wait for a person on every call, with the switch on or off.
_ALWAYS = ("argocd.app.set", "k8s.apply", "k8s.job.create", "k8s.exec")
_SEVEN = (_SYNC, _ROLLBACK, _REFRESH, *_ALWAYS)

#: (op id, params of the form that deletes, the card line).
_DELETING = [
    (_SYNC, {"name": "web", "prune": True}, SYNC_PRUNE_LINE),
    (_ROLLBACK, {"name": "web", "id": 3, "prune": True}, ROLLBACK_PRUNE_LINE),
]
_DELETING_IDS = ["sync-prune", "rollback-prune"]

#: (op id, params of a plain form) of the calls a grant covers on main.
_PLAIN = [
    (_SYNC, {"name": "web"}),
    (_SYNC, {"name": "web", "prune": False}),
    (_ROLLBACK, {"name": "web", "id": 3}),
    (_ROLLBACK, {"name": "web", "id": 3, "prune": False}),
    (_REFRESH, {"name": "web", "hard": True}),
]
_PLAIN_IDS = ["sync", "sync-prune-false", "rollback", "rollback-prune-false", "refresh"]

#: (op id, params) of the calls that wait on every call on main.
_ALWAYS_CALLS = [
    ("argocd.app.set", {"name": "web", "spec": {"syncPolicy": {"automated": {"prune": True}}}}),
    ("k8s.apply", {"manifest": "kind: ConfigMap\nmetadata:\n  name: web\n"}),
    ("k8s.job.create", {"name": "once", "namespace": "apps", "spec": {}}),
    ("k8s.exec", {"pod": "web-0", "namespace": "apps", "command": ["rm", "-rf", "/data"]}),
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    add_connector(_PRODUCT, "1.0", "deliver-api")
    yield
    finish()


def _built_in_rows() -> dict[str, dict[str, Any]]:
    rows = json.loads(_SNAPSHOT.read_text())
    return {row["op_id"]: row for row in rows if row["op_id"] in _SEVEN}


async def _register_the_seven() -> None:
    built_in = _built_in_rows()
    assert set(built_in) == set(_SEVEN)
    for op_id, row in built_in.items():
        assert row["needs_approval"] is True, op_id
        await register_op(
            op_id,
            None,
            product=_PRODUCT,
            impl_id="deliver-api",
            safety_level=row["risk_level"],
        )
    await seed_target(_TARGET, product=_PRODUCT)


def _service() -> Operator:
    return Operator(
        sub=SERVICE,
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


def _person() -> Operator:
    return Operator(
        sub="op:deliver-person",
        name="Person",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _agent() -> Operator:
    return Operator(
        sub=str(uuid.uuid5(uuid.NAMESPACE_URL, "agent:deliver")),
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id="agent:deliver",
    )


async def _seed_grants() -> None:
    """A standing grant of the service login for each of the seven calls."""
    for op_id in _SEVEN:
        await seed_grant_row(
            model=ServicePrincipalGrant,
            tenant_id=TENANT,
            principal_sub=SERVICE,
            op_id=op_id,
            connector_id=_CONNECTOR,
            target_product=_PRODUCT,
        )


async def _seed_run_approval() -> None:
    async with get_sessionmaker()() as session:
        target = (await session.execute(select(Target).where(Target.name == _TARGET))).scalar_one()
    await seed_run_approval(target.id, work_ref=_RUN, connector_id=_CONNECTOR, op_ids=_SEVEN)


def _args(op_id: str, params: dict[str, Any], work_ref: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "connector_id": _CONNECTOR,
        "op_id": op_id,
        "target": _TARGET,
        "params": params,
    }
    if work_ref is not None:
        args["work_ref"] = work_ref
    return args


async def _answer(
    operator: Operator, op_id: str, params: dict[str, Any], work_ref: str | None = None
) -> tuple[str, str]:
    """Preview, then call. Return (the preview's verdict, the call's status)."""
    RUNS.clear()
    args = _args(op_id, params, work_ref)
    preview = await preview_operation(operator, args)
    result = await call_operation(operator, args)
    status = result["status"]
    if status == "ok":
        assert [params] == RUNS, (op_id, RUNS)
    else:
        assert RUNS == [], (op_id, RUNS)
    return preview["policy_verdict"], status


def _never_runs(answer: tuple[str, str]) -> bool:
    """The preview says the call waits, and the call does not run.

    The call waits for a person, or, for the cluster apply, is refused
    before it waits: its card needs a dry run on a real cluster, which the
    stand-in does not have. Both are the same with the switch on and off.
    """
    verdict, status = answer
    return verdict == "needs-approval" and status in {"awaiting_approval", "denied"}


async def _cards() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(ApprovalRequest))).scalars().all()
        return [dict(row.proposed_effect or {}) for row in rows]


async def _grant_use_rows() -> int:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    return len(rows)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "line"),
    [
        ({"prune": True}, SYNC_PRUNE_LINE),
        ({"prune": False}, None),
        ({"prune": None}, None),
        ({}, None),
    ],
)
def test_a_sync_or_a_rollback_deletes_only_with_prune(
    params: dict[str, Any], line: str | None
) -> None:
    assert sync_prunes(params) == line
    assert rollback_prunes(params) == (ROLLBACK_PRUNE_LINE if line else None)
    assert params_delete_reason(_SYNC, params) == line


def test_a_refresh_and_the_calls_that_always_wait_have_no_check() -> None:
    for op_id, params in [(_REFRESH, {"name": "web"}), *_ALWAYS_CALLS]:
        assert params_delete_reason(op_id, params) is None, op_id


def test_a_check_that_fails_counts_as_a_delete() -> None:
    def _broken(params: Any) -> str | None:
        raise KeyError("prune")

    def _not_a_text(params: Any) -> str | None:
        return True  # type: ignore[return-value]

    with kept_op_class_state():
        declare_delete_check("deliver.broken", _broken)
        declare_delete_check("deliver.not_a_text", _not_a_text)
        assert params_delete_reason("deliver.broken", {}) == UNREADABLE_DELETE_REASON
        assert params_delete_reason("deliver.not_a_text", {}) == UNREADABLE_DELETE_REASON


# ---------------------------------------------------------------------------
# Switch on: the whole path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params", "line"), _DELETING, ids=_DELETING_IDS)
async def test_switch_on_a_call_that_deletes_waits_under_a_standing_grant(
    monkeypatch: pytest.MonkeyPatch, op_id: str, params: dict[str, Any], line: str
) -> None:
    await _register_the_seven()
    await _seed_grants()
    custom_policy(monkeypatch, on=True)

    assert await _answer(_service(), op_id, params) == ("needs-approval", "awaiting_approval")
    assert await _grant_use_rows() == 0
    (card,) = await _cards()
    assert card["delete_wait"] == line


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params", "line"), _DELETING, ids=_DELETING_IDS)
async def test_switch_on_a_call_that_deletes_waits_under_a_run_approval(
    monkeypatch: pytest.MonkeyPatch, op_id: str, params: dict[str, Any], line: str
) -> None:
    await _register_the_seven()
    await _seed_run_approval()
    custom_policy(monkeypatch, on=True)

    assert await _answer(_service(), op_id, params, work_ref=_RUN) == (
        "needs-approval",
        "awaiting_approval",
    )
    assert await _grant_use_rows() == 0
    (card,) = await _cards()
    assert card["delete_wait"] == line


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _PLAIN, ids=_PLAIN_IDS)
async def test_switch_on_a_plain_call_runs_under_the_grant_and_the_run_approval(
    monkeypatch: pytest.MonkeyPatch, op_id: str, params: dict[str, Any]
) -> None:
    await _register_the_seven()
    await _seed_grants()
    await _seed_run_approval()
    custom_policy(monkeypatch, on=True)

    assert await _answer(_service(), op_id, params) == ("auto-execute", "ok")
    assert await _answer(_service(), op_id, params, work_ref=_RUN) == ("auto-execute", "ok")
    assert await _cards() == []


@pytest.mark.asyncio
async def test_switch_on_the_calls_that_always_wait_keep_the_answer_of_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_the_seven()
    await _seed_grants()
    await _seed_run_approval()
    callers = [(_service(), None), (_service(), _RUN), (_person(), None), (_agent(), None)]

    custom_policy(monkeypatch, on=False)
    off = [await _answer(o, op, p, w) for op, p in _ALWAYS_CALLS for o, w in callers]
    custom_policy(monkeypatch, on=True)
    on = [await _answer(o, op, p, w) for op, p in _ALWAYS_CALLS for o, w in callers]

    assert on == off
    assert all(_never_runs(answer) for answer in on)
    assert all("delete_wait" not in card for card in await _cards())


# ---------------------------------------------------------------------------
# Switch off: every answer equals main
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_every_answer_is_the_answer_of_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_the_seven()
    await _seed_grants()
    await _seed_run_approval()
    custom_policy(monkeypatch, on=False)
    calls = [(op, p) for op, p, _ in _DELETING] + _PLAIN + _ALWAYS_CALLS
    runs = ("auto-execute", "ok")

    for op_id, params in calls:
        service = [
            await _answer(_service(), op_id, params),
            await _answer(_service(), op_id, params, work_ref=_RUN),
        ]
        if op_id in _ALWAYS:
            assert all(_never_runs(answer) for answer in service), op_id
        else:
            assert service == [runs, runs], op_id
        assert _never_runs(await _answer(_person(), op_id, params)), op_id
        assert _never_runs(await _answer(_agent(), op_id, params)), op_id

    assert all("delete_wait" not in card for card in await _cards())
