# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The fixed rules: one list in the code, asked before any grant.

``policy/fixed_rules.py`` holds the rules that no policy, no tenant admin
and no platform admin can lower. These tests check:

* which rule :func:`fixed_rule_for` names, for each rule and each kind of
  caller (a person in the web console, a person's token, an agent login,
  a service login and a hosted agent run);
* that the call gate gives the same answer as before the module, for each
  rule and each kind of caller, also when the caller holds a grant;
* that the rule ids of ``policy/floors.py`` come from the module;
* that over the approval table every row with a fixed rule gets one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from meho_backplane import op_classes
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    EndpointDescriptor,
    PermissionVerdict,
    RunApproval,
    ServicePrincipalGrant,
)
from meho_backplane.operations import grant_approval
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.policy import fixed_rules
from meho_backplane.policy.fixed_rules import FIXED_RULES, fixed_rule_for
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006958")
_CONNECTOR = "fixed-rules-rest-1.0"
_WORK_REF = "run-fixed-rules"
_RIGHTS_OP = "fixedrules.login.grant_role"
_RIGHTS_OP_NO_MARK = "fixedrules.login.grant_group"
_HIDDEN_READ = "fixedrules.secret.list"

_CALLERS = ("person_web", "person_token", "agent", "service", "hosted_run")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "false")
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "off")
    monkeypatch.setenv("SANDBOX_MODE", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _marked_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test operations with the marks their built-in kind has.

    Two changes of rights: one with the per-call mark (every registered
    change of rights has it today), one without. One credential read
    marked as returning no secret value (like ``vault.kv.list``).
    """
    rights = set(grant_approval._RIGHTS_CHANGE_OPS) | {_RIGHTS_OP, _RIGHTS_OP_NO_MARK}
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", rights)
    monkeypatch.setattr(
        op_classes, "_PER_CALL_APPROVAL", set(op_classes._PER_CALL_APPROVAL) | {_RIGHTS_OP}
    )
    monkeypatch.setattr(
        op_classes, "_NO_SECRET_VALUE", set(op_classes._NO_SECRET_VALUE) | {_HIDDEN_READ}
    )


# ---------------------------------------------------------------------------
# What is asked: one operation for each fixed rule, and two without one
# ---------------------------------------------------------------------------


def _descriptor(
    op_id: str,
    level: str,
    *,
    method: str = "POST",
    op_class: str | None = None,
    requires_approval: bool = False,
    caller_address: bool = False,
) -> EndpointDescriptor:
    schema: dict[str, Any] = {}
    if caller_address:
        schema = {
            "type": "object",
            "properties": {"address": {"type": "string", CALLER_ADDRESS_MARKER: True}},
        }
    return EndpointDescriptor(
        product="fixedrules",
        version="1.0",
        impl_id="fixedrules-rest",
        op_id=op_id,
        source_kind="ingested",
        method=method,
        tags=[],
        safety_level=level,
        requires_approval=requires_approval,
        op_class=op_class,
        parameter_schema=schema,
    )


#: One operation for each case, shaped like the built-in operations of its kind.
_OPS: dict[str, EndpointDescriptor] = {
    "policy_change": _descriptor("policy.apply", "dangerous", requires_approval=True),
    "rights_change": _descriptor(_RIGHTS_OP, "caution", requires_approval=True),
    "credential_write": _descriptor(
        "fixedrules.secret.put", "caution", op_class="credential_write"
    ),
    "credential_read": _descriptor(
        "fixedrules.secret.get", "safe", method="GET", op_class="credential_read"
    ),
    "credential_hidden_read": _descriptor(
        _HIDDEN_READ, "safe", method="GET", op_class="credential_read"
    ),
    "caller_address": _descriptor(
        "fixedrules.http.head", "caution", requires_approval=True, caller_address=True
    ),
    "destructive": _descriptor("fixedrules.vm.destroy", "destructive", method="DELETE"),
    "delete": _descriptor("fixedrules.vm.delete", "dangerous", method="DELETE"),
    "plain_write": _descriptor("fixedrules.vm.rename", "caution"),
    "plain_read": _descriptor("fixedrules.vm.list", "safe", method="GET"),
}

#: The rule :func:`fixed_rule_for` names for each case and each kind of caller.
_RULE_BY_CASE: dict[str, dict[str, str | None]] = {
    "policy_change": {
        "person_web": None,
        "person_token": "policy_change_waits",
        "agent": "policy_change_waits",
        "service": "policy_change_waits",
        "hosted_run": "policy_change_waits",
    },
    "rights_change": {
        "person_web": None,
        "person_token": "rights_change_waits",
        "agent": "rights_change_waits",
        "service": "rights_change_waits",
        "hosted_run": "rights_change_waits",
    },
    "credential_write": dict.fromkeys(_CALLERS, "credential_waits"),
    "credential_read": dict.fromkeys(_CALLERS, "credential_waits"),
    "credential_hidden_read": dict.fromkeys(_CALLERS, "credential_waits"),
    "caller_address": dict.fromkeys(_CALLERS, "caller_address_waits"),
    "destructive": dict.fromkeys(_CALLERS, "destructive_waits"),
    "delete": dict.fromkeys(_CALLERS, "delete_waits"),
    "plain_write": dict.fromkeys(_CALLERS),
    "plain_read": dict.fromkeys(_CALLERS),
}

_RUN = PermissionVerdict.AUTO_EXECUTE
_WAIT = PermissionVerdict.NEEDS_APPROVAL

#: The gate's answer before this module existed, for each case and each kind
#: of caller. Every caller holds a grant for every operation: the person and
#: the service a standing grant, the service a run approval too, the agent
#: and the hosted agent an agent grant that runs every operation.
_DENY = PermissionVerdict.DENY
_GATE_BY_CASE: dict[str, dict[str, PermissionVerdict]] = {
    # Only a person may ask for a policy import (its requester check).
    "policy_change": {
        "person_web": _WAIT,
        "person_token": _WAIT,
        "agent": _DENY,
        "service": _DENY,
        "hosted_run": _DENY,
    },
    "rights_change": dict.fromkeys(_CALLERS, _WAIT),
    "credential_write": {
        "person_web": _RUN,
        "person_token": _WAIT,
        "agent": _WAIT,
        "service": _WAIT,
        "hosted_run": _WAIT,
    },
    "credential_read": {
        "person_web": _RUN,
        "person_token": _RUN,
        "agent": _WAIT,
        "service": _WAIT,
        "hosted_run": _WAIT,
    },
    # A person's own read of a hidden-value list runs at once, as today.
    "credential_hidden_read": dict.fromkeys(_CALLERS, _RUN),
    "caller_address": dict.fromkeys(_CALLERS, _WAIT),
    "destructive": dict.fromkeys(_CALLERS, _WAIT),
    "delete": dict.fromkeys(_CALLERS, _WAIT),
    "plain_write": {
        "person_web": _RUN,
        "person_token": _WAIT,
        "agent": _WAIT,
        "service": _RUN,
        "hosted_run": _WAIT,
    },
    "plain_read": dict.fromkeys(_CALLERS, _RUN),
}


# ---------------------------------------------------------------------------
# Who asks
# ---------------------------------------------------------------------------


def _sub(caller: str) -> str:
    return f"fixed-rules:{caller}"


def _actor(caller: str) -> str | None:
    return "hosted-agent:fixed-rules" if caller == "hosted_run" else None


def _operator(caller: str) -> Operator:
    kind = {"agent": PrincipalKind.AGENT, "service": PrincipalKind.SERVICE}.get(
        caller, PrincipalKind.USER
    )
    role = TenantRole.OPERATOR if kind is PrincipalKind.AGENT else TenantRole.TENANT_ADMIN
    return Operator(
        sub=_sub(caller),
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        principal_kind=kind,
        tenant_role=role,
        web_session=caller == "person_web",
    )


async def _seed_grants() -> None:
    """A grant for every operation, for every caller that can hold one."""
    now = datetime.now(UTC)
    rows: list[Any] = [
        AgentPermission(
            tenant_id=_TENANT,
            principal_sub=sub,
            op_pattern="*",
            verdict=PermissionVerdict.AUTO_EXECUTE,
            created_by_sub="test-admin",
        )
        for sub in (_sub("agent"), _actor("hosted_run"))
    ]
    for caller in ("person_web", "person_token", "service"):
        rows += [
            ServicePrincipalGrant(
                tenant_id=_TENANT,
                principal_sub=_sub(caller),
                op_id=descriptor.op_id,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="test: every operation",
                created_by_sub="test-admin",
            )
            for descriptor in _OPS.values()
        ]
    rows.append(
        RunApproval(
            tenant_id=_TENANT,
            principal_sub=_sub("service"),
            work_ref=_WORK_REF,
            entries=[{"connector_id": _CONNECTOR, "op_id": d.op_id} for d in _OPS.values()],
            reason="test: every operation",
            requested_at=now,
            expires_at=now + timedelta(hours=1),
            approved_at=now,
            approved_by_sub="test-approver",
        )
    )
    async with get_sessionmaker()() as session, session.begin():
        session.add_all(rows)


def _named_rule(case: str, caller: str) -> str | None:
    with actor_delegation(actor) if (actor := _actor(caller)) else nullcontext():
        return fixed_rule_for(_OPS[case], _operator(caller))


async def _gate(case: str, caller: str) -> tuple[PermissionVerdict, str | None]:
    work_ref = work_ref_var.set(_WORK_REF)
    try:
        with actor_delegation(actor) if (actor := _actor(caller)) else nullcontext():
            return await policy_gate(
                operator=_operator(caller),
                descriptor=_OPS[case],
                target=None,
                connector_id=_CONNECTOR,
            )
    finally:
        work_ref_var.reset(work_ref)


# ---------------------------------------------------------------------------
# The rule for each case and each kind of caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("case", sorted(_OPS))
def test_fixed_rule_for_names_the_rule(case: str, caller: str) -> None:
    assert _named_rule(case, caller) == _RULE_BY_CASE[case][caller]


def test_every_named_rule_is_in_the_list() -> None:
    named = {rule for by_caller in _RULE_BY_CASE.values() for rule in by_caller.values()}
    assert named - {None} <= set(FIXED_RULES)
    # An agent never approves: a rule of the approve path, never of a call.
    assert set(FIXED_RULES) - named == {"agent_never_approves"}


def test_each_rule_has_one_plain_sentence() -> None:
    for rule_id, sentence in FIXED_RULES.items():
        assert rule_id.endswith(("_waits", "_approves")), rule_id
        assert sentence.endswith(".") and sentence.count(". ") <= 1, rule_id


def test_a_rights_change_in_the_console_with_a_bound_agent_names_the_rule() -> None:
    """A hosted agent run from the web console is not the person's own click."""
    operator = _operator("person_web")
    with actor_delegation("hosted-agent:fixed-rules"):
        assert fixed_rule_for(_OPS["rights_change"], operator) == "rights_change_waits"


def test_a_service_login_in_the_console_names_the_rights_rule() -> None:
    operator = _operator("service").model_copy(update={"web_session": True})
    assert fixed_rule_for(_OPS["rights_change"], operator) == "rights_change_waits"


def test_a_credential_call_by_its_params_names_the_credential_rule() -> None:
    descriptor = _descriptor("fixedrules.vm.rename", "caution")
    descriptor.credential_call = True  # type: ignore[attr-defined]
    assert fixed_rule_for(descriptor, _operator("agent")) == "credential_waits"


def test_a_delete_name_alone_names_the_delete_rule() -> None:
    descriptor = _descriptor("POST:/VirtualMachine/{moId}/Destroy_Task", "dangerous")
    assert fixed_rule_for(descriptor, _operator("service")) == "delete_waits"


# ---------------------------------------------------------------------------
# The gate's answer is the same as before, for each rule and each caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_answer_is_unchanged_for_each_rule_and_caller() -> None:
    await _seed_grants()
    differences = []
    for case in sorted(_OPS):
        for caller in _CALLERS:
            verdict, reason = await _gate(case, caller)
            expected = _GATE_BY_CASE[case][caller]
            if verdict is not expected:
                differences.append(f"{case}/{caller}: {verdict.value} ({reason})")
    assert not differences, "\n".join(differences)


@pytest.mark.asyncio
async def test_no_grant_clears_a_fixed_rule_for_a_service_login() -> None:
    await _seed_grants()
    for case, by_caller in _RULE_BY_CASE.items():
        if by_caller["service"] is None or _GATE_BY_CASE[case]["service"] is not _WAIT:
            continue
        verdict, reason = await _gate(case, "service")
        assert verdict is _WAIT, case
        assert "auto-granted" not in (reason or ""), case
        assert "auto-approved" not in (reason or ""), case


@pytest.mark.asyncio
async def test_a_standing_grant_never_clears_a_change_of_rights() -> None:
    """Every registered change of rights has the per-call mark today, so no
    grant clears it. The fixed rule holds also for one without the mark."""
    descriptor = _descriptor(_RIGHTS_OP_NO_MARK, "caution")
    operator = _operator("service")
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                tenant_id=_TENANT,
                principal_sub=operator.sub,
                op_id=descriptor.op_id,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="test: a change of rights",
                created_by_sub="test-admin",
            )
        )
    assert fixed_rule_for(descriptor, operator) == "rights_change_waits"
    verdict, reason = await policy_gate(
        operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
    )
    assert verdict is _WAIT
    assert "auto-granted" not in (reason or "")


@pytest.mark.asyncio
async def test_in_sandbox_mode_a_delete_grant_may_still_cover_a_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox mode keeps its delete exception: the gate reads the grant tables."""
    from meho_backplane.operations import _validate

    monkeypatch.setattr(_validate, "sandbox_mode_active", lambda: True)
    assert not _validate._fixed_rule_blocks_grants(_operator("service"), _OPS["delete"])
    monkeypatch.setattr(_validate, "sandbox_mode_active", lambda: False)
    assert _validate._fixed_rule_blocks_grants(_operator("service"), _OPS["delete"])
    assert not _validate._fixed_rule_blocks_grants(_operator("service"), _OPS["plain_write"])


def test_the_policy_change_ops_are_the_policy_operations() -> None:
    from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
    from meho_backplane.connectors.policy.classify import POLICY_CLASSIFY_OP_ID
    from meho_backplane.connectors.policy.op_edit import (
        POLICY_OP_EDIT_OP_ID,
        POLICY_OP_REMOVE_OVERRIDE_OP_ID,
    )

    assert {
        POLICY_APPLY_OP_ID,
        POLICY_CLASSIFY_OP_ID,
        POLICY_OP_EDIT_OP_ID,
        POLICY_OP_REMOVE_OVERRIDE_OP_ID,
    } == fixed_rules.POLICY_CHANGE_OPS


# ---------------------------------------------------------------------------
# The floors and the approval table
# ---------------------------------------------------------------------------


def test_the_floors_take_their_rule_ids_from_the_module() -> None:
    from meho_backplane.policy import floors

    assert floors.DESTRUCTIVE_WAITS is fixed_rules.DESTRUCTIVE_WAITS
    assert floors.CREDENTIAL_WAITS is fixed_rules.CREDENTIAL_WAITS


#: Operations a person approves on each call for a reason that is not a
#: fixed rule (the connector's per-call mark). They keep that rule as it is.
_PER_CALL_ONLY = frozenset({"secret.move", "msad.group.add-member", "msad.group.remove-member"})


@pytest.mark.asyncio
async def test_each_approval_table_row_with_a_fixed_rule_gets_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row gets a rule id exactly when no grant covers it, it is destructive,
    or it is a change of rights (``tests/snapshots/approval_table.json``)."""
    from tests.test_approval_table_snapshot import _use_built_in_settings

    _use_built_in_settings(monkeypatch)
    clear_typed_op_snapshot()
    try:
        descriptors = [d for d in await register_all_descriptors() if d.tenant_id is None]
    finally:
        clear_typed_op_snapshot()
    table = json.loads(
        (Path(__file__).parent / "snapshots" / "approval_table.json").read_text("utf-8")
    )
    by_op = {(row["op_id"], row["connector_id"]): row for row in table}
    agent = _operator("agent")
    wrong = []
    for descriptor in descriptors:
        row = by_op[(descriptor.op_id, connector_id_of(descriptor))]
        rule = fixed_rule_for(descriptor, agent)
        expected = (
            row["never_covered"] is not None and descriptor.op_id not in _PER_CALL_ONLY
        ) or (
            row["risk_level"] == "destructive"
            or grant_approval.is_rights_change_op(descriptor.op_id)
        )
        if (rule is not None) != expected:
            wrong.append(f"{descriptor.op_id}: rule={rule} row={row}")
    assert len(descriptors) == len(table)
    assert not wrong, "\n".join(wrong)
    per_call = {row["op_id"] for row in table if row["never_covered"] == "per_call_approval"}
    assert per_call >= _PER_CALL_ONLY
    # No grant covered a change of rights before the module either.
    rights = [row for row in table if grant_approval.is_rights_change_op(row["op_id"])]
    assert rights
    assert all(row["never_covered"] is not None for row in rights)


def _rights_changes_without_the_per_call_mark(op_ids: list[str]) -> list[str]:
    return [
        op_id
        for op_id in op_ids
        if grant_approval.is_rights_change_op(op_id)
        and not op_classes.needs_per_call_approval(op_id)
    ]


@pytest.mark.asyncio
async def test_each_built_in_change_of_rights_carries_the_per_call_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every built-in operation that counts as a change of rights needs a
    person to approve each call, so no grant and no run approval covers it."""
    from tests.test_approval_table_snapshot import _use_built_in_settings

    _use_built_in_settings(monkeypatch)
    clear_typed_op_snapshot()
    try:
        descriptors = [d for d in await register_all_descriptors() if d.tenant_id is None]
    finally:
        clear_typed_op_snapshot()
    op_ids = sorted({descriptor.op_id for descriptor in descriptors})
    rights = [op_id for op_id in op_ids if grant_approval.is_rights_change_op(op_id)]
    assert "keycloak.group.member.add" in rights
    assert _rights_changes_without_the_per_call_mark(op_ids) == []

    # The scan finds an operation whose mark is missing.
    monkeypatch.setattr(
        op_classes,
        "_PER_CALL_APPROVAL",
        op_classes._PER_CALL_APPROVAL - {"keycloak.group.member.add"},
    )
    assert _rights_changes_without_the_per_call_mark(op_ids) == ["keycloak.group.member.add"]
