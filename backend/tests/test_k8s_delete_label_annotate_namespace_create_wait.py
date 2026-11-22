# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Kubernetes delete, label, annotate and namespace create always wait for a person.

Every call of ``k8s.delete``, ``k8s.label``, ``k8s.annotate`` and
``k8s.namespace.create`` waits for a person. No standing grant, no agent
grant, no run approval and no approval override covers it, for any login,
as a direct call and as a step of a multi-step run.

In sandbox mode a live delete grant still covers ``k8s.delete``, and only
that one of the four. A delete grant covers no other call that always
waits. ``k8s.scale``, ``k8s.rollout.restart``, ``k8s.cordon`` and
``k8s.cronjob.run`` keep their rules: a standing grant and a run approval
still cover them. ``k8s.secret.delete`` and ``k8s.finalizer.remove`` keep
their rules too.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.api.v1.approvals import _proposed_effect_for
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationPolicyOverride,
    PermissionVerdict,
    RunApproval,
    SandboxDeleteGrant,
    ServicePrincipalGrant,
)
from meho_backplane.mcp.tools.approvals import _row_to_dict
from meho_backplane.op_classes import (
    _DECLARED,
    _SANDBOX_DELETE_GRANT,
    call_always_waits,
    declare_op_classes,
    declare_sandbox_delete_grant,
    sandbox_delete_grant_allowed,
)
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.governed_subops import classify_subop_grantability
from meho_backplane.operations.op_policy import _with_floors, effective_policy
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    CREDENTIAL_CARD_LINE,
    CREDENTIAL_CLASSES,
    DeleteGrantConsoleOnlyError,
    GrantValidationError,
    ServicePrincipalGrantService,
    credential_call_waits,
    credential_grant_reason,
)
from meho_backplane.operations.wait_when_set import raise_descriptor_for_params
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.policy.shadow import evaluate_call
from meho_backplane.policy.store import connector_id_of
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    rows_in,
    seed_delete_grant,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)
from tests.test_credential_calls_always_wait import (
    _RUN,
    _SERVICE,
    _TENANT,
    _agent,
    _agent_grant,
    _name_in_every_grant,
    _register_all,
    _service,
)
from tests.test_credential_capable_calls_wait import _person, _stored_request
from tests.test_every_k8s_apply_and_api_write_waits import (
    _assert_waits_for_every_login,
    _step_waits,
)
from tests.test_guest_reads_and_pod_applies_wait import _ingested, _row, _settings_env

__all__ = ["_settings_env"]

_DELETE = "k8s.delete"
_POD = {"kind": "pod", "name": "web-1", "namespace": "apps"}

#: The four operations and valid params for each.
_FOUR: dict[str, dict[str, Any]] = {
    "k8s.delete": _POD,
    "k8s.label": {"kind": "namespace", "name": "apps", "labels": {"team": "a"}},
    "k8s.annotate": {"kind": "namespace", "name": "apps", "annotations": {"note": "a"}},
    "k8s.namespace.create": {"name": "apps"},
}

#: Label and annotate on a Namespace and on a Deployment.
_CALLS: list[tuple[str, dict[str, Any]]] = [
    *_FOUR.items(),
    (
        "k8s.label",
        {"kind": "deployment", "name": "web", "namespace": "apps", "labels": {"team": None}},
    ),
    (
        "k8s.annotate",
        {"kind": "deployment", "name": "web", "namespace": "apps", "annotations": {"n": "b"}},
    ),
]
_CALL_IDS = [
    "delete",
    "label-namespace",
    "annotate-namespace",
    "namespace-create",
    "label-deployment",
    "annotate-deployment",
]

#: Operations that keep their rules, and valid params for each.
_KEEP: list[tuple[str, dict[str, Any]]] = [
    ("k8s.scale", {"name": "web", "namespace": "apps", "replicas": 2}),
    ("k8s.rollout.restart", {"name": "web", "namespace": "apps"}),
    ("k8s.cordon", {"name": "node-1"}),
    ("k8s.cordon", {"name": "node-1", "uncordon": True}),
    ("k8s.cronjob.run", {"cronjob": "nightly", "namespace": "apps"}),
]
_KEEP_IDS = ["scale", "rollout-restart", "cordon", "uncordon", "cronjob-run"]

#: Calls that always wait and that a delete grant never covers, in sandbox mode too.
_NOT_FOR_A_DELETE_GRANT: list[tuple[str, dict[str, Any]]] = [
    ("k8s.label", _FOUR["k8s.label"]),
    ("k8s.annotate", _FOUR["k8s.annotate"]),
    ("k8s.namespace.create", _FOUR["k8s.namespace.create"]),
    (
        "k8s.apply",
        {"manifest": yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {}})},
    ),
    ("k8s.exec", {"pod": "web-1", "namespace": "apps", "command": ["id"]}),
    ("k8s.secret.create", {"name": "db", "namespace": "apps"}),
]
_NOT_FOR_A_DELETE_GRANT_IDS = ["label", "annotate", "namespace-create", "apply", "exec", "secret"]
_INGESTED_POD_DELETE = "DELETE:/api/v1/namespaces/{namespace}/pods/{name}"

#: Deletes whose rules do not change, and valid params for each.
_OTHER_DELETES: list[tuple[str, dict[str, Any]]] = [
    ("k8s.secret.delete", {"name": "db", "namespace": "apps"}),
    (
        "k8s.finalizer.remove",
        {"kind": "Widget", "api_version": "example.test/v1", "name": "a", "namespace": "apps"},
    ),
]
_OTHER_DELETE_IDS = ["secret-delete", "finalizer-remove"]


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    turn_sandbox_mode_on(monkeypatch)
    yield
    turn_sandbox_mode_off(monkeypatch)


async def _gate(
    operator: Operator,
    row: EndpointDescriptor,
    params: dict[str, Any],
    *,
    decide_only: bool = True,
) -> tuple[PermissionVerdict, str]:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    raise_descriptor_for_params(descriptor, params)
    token = work_ref_var.set(_RUN)
    try:
        verdict, reason = await policy_gate(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
            params=params,
            decide_only=decide_only,
        )
    finally:
        work_ref_var.reset(token)
    return verdict, reason or ""


async def _explained(row: EndpointDescriptor, params: dict[str, Any]) -> Verdict:
    """What the policy explain gives for a service login's call of *row*."""
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    raise_descriptor_for_params(descriptor, params)
    token = work_ref_var.set(_RUN)
    try:
        decision = await evaluate_call(
            operator=_service(),
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
            decide_only=True,
            params=params,
        )
    finally:
        work_ref_var.reset(token)
    return decision.verdict


async def _step(operator: Operator, op_id: str, params: dict[str, Any]) -> Any:
    """Gate *op_id* as a step of a multi-step run that no person approved."""
    token = work_ref_var.set(_RUN)
    try:
        return await enforce_subop_policy(
            operator=operator,
            connector_id="k8s-1.x",
            op_id=op_id,
            safety_level="caution",
            requires_approval=False,
            target=None,
            params=params,
        )
    finally:
        work_ref_var.reset(token)


async def _delete_grant_for(row: EndpointDescriptor) -> uuid.UUID:
    return await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_SERVICE,
        op_id=row.op_id,
        connector_id=connector_id_of(row),
    )


async def _seed_standing_grant(row: EndpointDescriptor) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                op_id=row.op_id,
                connector_id=connector_id_of(row),
                target_id=None,
                reason="test: a plain write",
                created_by_sub="test-admin",
            )
        )


async def _seed_run_approval(row: EndpointDescriptor) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                work_ref=_RUN,
                entries=[{"connector_id": connector_id_of(row), "op_id": row.op_id}],
                reason="test: a plain write",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )


def _create(op_id: str, connector_id: str, *, expires_at: datetime | None = None) -> Any:
    return ServiceGrantCreate(
        principal_sub=_SERVICE,
        op_id=op_id,
        connector_id=connector_id,
        reason="unattended work",
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Outside sandbox mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "params"), _CALLS, ids=_CALL_IDS)
def test_each_call_always_waits(op_id: str, params: dict[str, Any]) -> None:
    assert call_always_waits(op_id, params) is True
    assert credential_grant_reason(op_id) is not None
    assert credential_call_waits(op_id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _CALLS, ids=_CALL_IDS)
async def test_each_call_waits_for_every_login_under_every_grant(
    op_id: str, params: dict[str, Any]
) -> None:
    rows = await _register_all()
    await _assert_waits_for_every_login(_row(rows, op_id), params)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", list(_FOUR))
async def test_grant_create_refuses_each_operation(op_id: str) -> None:
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            _create(op_id, "k8s-1.x"),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", list(_FOUR))
async def test_agent_grant_create_refuses_each_operation(op_id: str) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(op_id, PermissionVerdict.AUTO_EXECUTE)
        )
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", list(_FOUR))
async def test_run_approval_create_refuses_each_operation(op_id: str) -> None:
    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id="k8s-1.x", op_id=op_id)],
                reason="one run",
            ),
        )
    assert refused.value.message == f"entries[0]: {credential_grant_reason(op_id)} Leave it out."


@pytest.mark.parametrize("op_id", list(_FOUR))
def test_an_override_does_not_turn_off_the_approval(op_id: str) -> None:
    row = EndpointDescriptor(op_id=op_id, safety_level="caution", requires_approval=True)
    off = effective_policy("caution", True, [OperationPolicyOverride(requires_approval=False)])
    assert off.requires_approval is False
    assert _with_floors(row, off).requires_approval is True


@pytest.mark.asyncio
async def test_a_delete_grant_row_covers_nothing_outside_sandbox_mode() -> None:
    row = _row(await _register_all(), _DELETE)
    await _delete_grant_for(row)
    verdict, reason = await _gate(_service(), row, dict(_POD))
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert "auto-granted" not in reason
    assert await _explained(row, dict(_POD)) is Verdict.WAIT
    assert await _step_waits(_service(), _DELETE, dict(_POD))


# ---------------------------------------------------------------------------
# Sandbox mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_a_delete_grant_covers_k8s_delete_in_sandbox_mode() -> None:
    row = _row(await _register_all(), _DELETE)
    grant_id = await _delete_grant_for(row)

    verdict, reason = await _gate(_service(), row, dict(_POD))
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    assert reason == f"auto-granted by standing grant {grant_id}"
    assert await grant_use_rows(grant_id) == []

    assert await _explained(row, dict(_POD)) is Verdict.RUN

    verdict, reason = await _gate(_service(), row, dict(_POD), decide_only=False)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    assert str(grant_id) in reason
    assert len(await grant_use_rows(grant_id)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_a_delete_grant_covers_k8s_delete_as_a_step_in_sandbox_mode() -> None:
    row = _row(await _register_all(), _DELETE)
    grant_id = await _delete_grant_for(row)
    result = await _step(_service(), _DELETE, dict(_POD))
    assert result is None
    assert len(await grant_use_rows(grant_id)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_delete_grant_create_and_renewal_accept_k8s_delete_in_sandbox_mode() -> None:
    row = _row(await _register_all(), _DELETE)
    service = ServicePrincipalGrantService()
    entry = await service.create(
        _TENANT,
        "op-admin",
        _create(_DELETE, connector_id_of(row), expires_at=soon()),
        from_console=True,
        creator_role=TenantRole.TENANT_ADMIN,
    )
    assert entry.delete_grant is True
    assert await rows_in(SandboxDeleteGrant) == 1
    assert await rows_in(ServicePrincipalGrant) == 0

    renewed = await service.renew_delete_grants(
        _TENANT,
        _SERVICE,
        "op-admin",
        expires_at=soon(14),
        from_console=True,
        creator_role=TenantRole.TENANT_ADMIN,
    )
    assert [new.op_id for new in renewed] == [_DELETE]
    assert all(new.delete_grant for new in renewed)


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_no_other_grant_covers_k8s_delete_in_sandbox_mode() -> None:
    row = _row(await _register_all(), _DELETE)
    await _assert_waits_for_every_login(row, dict(_POD))
    for operator in (_service(), _agent()):
        result = await _step(operator, _DELETE, dict(_POD))
        assert result is not None and result.status == "awaiting_approval"


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_only_a_delete_grant_can_be_made_for_k8s_delete_in_sandbox_mode() -> None:
    row = _row(await _register_all(), _DELETE)
    with pytest.raises(DeleteGrantConsoleOnlyError):
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            _create(_DELETE, connector_id_of(row), expires_at=soon()),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert await rows_in(ServicePrincipalGrant) == 0
    assert await rows_in(SandboxDeleteGrant) == 0
    with pytest.raises(AgentGrantValidationError):
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(_DELETE, PermissionVerdict.AUTO_EXECUTE)
        )
    with pytest.raises(RunApprovalValidationError):
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id=connector_id_of(row), op_id=_DELETE)],
                reason="one run",
            ),
        )


@pytest.mark.usefixtures("sandbox")
def test_an_override_does_not_turn_off_the_approval_of_k8s_delete_in_sandbox_mode() -> None:
    row = EndpointDescriptor(op_id=_DELETE, safety_level="dangerous", requires_approval=True)
    off = effective_policy("dangerous", True, [OperationPolicyOverride(requires_approval=False)])
    assert _with_floors(row, off).requires_approval is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
@pytest.mark.parametrize(
    ("op_id", "params"), _NOT_FOR_A_DELETE_GRANT, ids=_NOT_FOR_A_DELETE_GRANT_IDS
)
async def test_a_delete_grant_covers_no_other_call_that_always_waits(
    op_id: str, params: dict[str, Any]
) -> None:
    row = _row(await _register_all(), op_id)
    await _delete_grant_for(row)
    verdict, reason = await _gate(_service(), row, params)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert "auto-granted" not in reason
    assert await _explained(row, params) is Verdict.WAIT
    assert await _step_waits(_service(), op_id, params)
    result = await _step(_service(), op_id, params)
    assert result is not None and result.status == "awaiting_approval"
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            _create(op_id, connector_id_of(row), expires_at=soon()),
            from_console=True,
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
async def test_step_discovery_says_k8s_delete_is_not_grantable_outside_sandbox_mode() -> None:
    await _register_all()
    step = classify_subop_grantability(_DELETE)
    assert step.grantable is False
    assert step.ungrantable_reason == credential_grant_reason(_DELETE)


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_step_discovery_says_k8s_delete_is_grantable_in_sandbox_mode() -> None:
    await _register_all()
    step = classify_subop_grantability(_DELETE)
    assert step.grantable is True
    assert step.ungrantable_reason is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
@pytest.mark.parametrize(
    ("op_id", "params"), _NOT_FOR_A_DELETE_GRANT, ids=_NOT_FOR_A_DELETE_GRANT_IDS
)
async def test_step_discovery_keeps_other_calls_that_always_wait_not_grantable(
    op_id: str, params: dict[str, Any]
) -> None:
    await _register_all()
    step = classify_subop_grantability(op_id)
    assert step.grantable is False
    assert step.ungrantable_reason == credential_grant_reason(op_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_a_delete_grant_does_not_cover_an_ingested_pod_delete() -> None:
    row = _ingested(_INGESTED_POD_DELETE)
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    await _delete_grant_for(row)
    params = {"namespace": "apps", "name": "web-1"}
    verdict, reason = await _gate(_service(), row, params)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert "auto-granted" not in reason
    with pytest.raises(GrantValidationError):
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            _create(_INGESTED_POD_DELETE, connector_id_of(row), expires_at=soon()),
            from_console=True,
            creator_role=TenantRole.TENANT_ADMIN,
        )


def test_the_sandbox_delete_mark_refuses_a_credential_operation() -> None:
    with pytest.raises(ValueError, match="credential"):
        declare_sandbox_delete_grant(("k8s.secret.create",))
    assert sandbox_delete_grant_allowed("k8s.secret.create") is False
    assert sandbox_delete_grant_allowed(_DELETE) is True
    for op_id in ("k8s.label", "k8s.annotate", "k8s.namespace.create", "k8s.apply"):
        assert sandbox_delete_grant_allowed(op_id) is False


def test_a_credential_class_declared_after_the_mark_ends_the_exception() -> None:
    op_id = "example.thing.delete"
    declare_sandbox_delete_grant((op_id,))
    try:
        assert sandbox_delete_grant_allowed(op_id) is True
        declare_op_classes(credential_write=(op_id,))
        assert sandbox_delete_grant_allowed(op_id) is False
    finally:
        _SANDBOX_DELETE_GRANT.discard(op_id)
        _DECLARED.pop(op_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _OTHER_DELETES, ids=_OTHER_DELETE_IDS)
async def test_other_kubernetes_deletes_keep_their_verdict(
    monkeypatch: pytest.MonkeyPatch, op_id: str, params: dict[str, Any]
) -> None:
    row = _row(await _register_all(), op_id)
    grant_id = await _delete_grant_for(row)
    await _seed_standing_grant(row)

    verdict, reason = await _gate(_service(), row, params)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert "auto-granted" not in reason

    turn_sandbox_mode_on(monkeypatch)
    try:
        verdict, reason = await _gate(_service(), row, params)
    finally:
        turn_sandbox_mode_off(monkeypatch)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    assert reason == (
        f"auto-granted by delete grant {grant_id}, when the call sends the "
        "preview_hash of this preview"
    )


# ---------------------------------------------------------------------------
# Operations that keep their rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _KEEP, ids=_KEEP_IDS)
async def test_a_standing_grant_still_covers_the_daily_writes(
    op_id: str, params: dict[str, Any]
) -> None:
    assert call_always_waits(op_id, params) is False
    row = _row(await _register_all(), op_id)
    await _seed_standing_grant(row)
    verdict, reason = await _gate(_service(), row, params)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    assert reason.startswith("auto-granted by standing grant "), reason


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), _KEEP, ids=_KEEP_IDS)
async def test_a_run_approval_still_covers_the_daily_writes(
    op_id: str, params: dict[str, Any]
) -> None:
    row = _row(await _register_all(), op_id)
    await _seed_run_approval(row)
    verdict, reason = await _gate(_service(), row, params)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    assert reason.startswith("auto-approved by run approval "), reason


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted({op_id for op_id, _ in _KEEP}))
async def test_grant_create_still_accepts_the_daily_writes(op_id: str) -> None:
    row = _row(await _register_all(), op_id)
    entry = await ServicePrincipalGrantService().create(
        _TENANT,
        "op-admin",
        _create(op_id, connector_id_of(row)),
        creator_role=TenantRole.TENANT_ADMIN,
    )
    assert entry.delete_grant is False


# ---------------------------------------------------------------------------
# What an agent reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_four_operations_keep_their_rows() -> None:
    rows = await _register_all()
    for op_id in _FOUR:
        row = _row(rows, op_id)
        assert row.op_class not in CREDENTIAL_CLASSES, op_id
        assert row.requires_approval is True, op_id
        expected = "dangerous" if op_id == _DELETE else "caution"
        assert row.safety_level == expected, op_id
    async with get_sessionmaker()() as session:
        stored = (
            await session.execute(
                select(EndpointDescriptor.op_id).where(EndpointDescriptor.op_id.in_(list(_FOUR)))
            )
        ).all()
    assert {op_id for (op_id,) in stored} == set(_FOUR)


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params"), list(_FOUR.items()), ids=list(_FOUR))
async def test_the_card_line_is_for_a_person_only(op_id: str, params: dict[str, Any]) -> None:
    row = await _stored_request(op_id, params)
    stored = dict(row.proposed_effect)
    assert stored["credential_wait"] == CREDENTIAL_CARD_LINE
    for view in (
        _row_to_dict(row)["proposed_effect"],
        _proposed_effect_for(_service(), row),
        _proposed_effect_for(_agent(), row),
    ):
        assert "credential_wait" not in view
    assert _proposed_effect_for(_person(), row) == stored


@pytest.mark.asyncio
async def test_an_agent_with_an_auto_execute_row_waits_for_each_operation() -> None:
    registered = await _register_all()
    rows = [_row(registered, op_id) for op_id in _FOUR]
    await _name_in_every_grant(rows)
    for row in rows:
        verdict, reason = await _gate(_agent(), row, _FOUR[row.op_id])
        assert verdict is PermissionVerdict.NEEDS_APPROVAL, (row.op_id, reason)
