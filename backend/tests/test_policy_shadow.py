# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shadow mode of the policy evaluator (``POLICY_ENGINE_SHADOW``).

* With the switch off the evaluator does not run.
* With the switch on, a forced difference writes one
  ``policy_shadow_mismatch`` line, counts the metric once, and does not
  change today's answer. A failure inside the shadow does not either.
* The shadow uses no grant: a standing grant is recorded as used once
  (by today's gate), not twice.
* On a table of caller kind x risk level x approval flag x delete sign x
  grant x gate, in and out of sandbox mode, the evaluator gives today's
  verdict in every cell (no mismatch line).
* The approval side: a forced difference writes one line and the answer
  stays today's.
* The gate keeps the rule the evaluator matched, marked as not deciding,
  for the audit row and the approval request (#6044). With the switch off,
  or when the shadow fails, it keeps none, and no old rule is left behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    PermissionVerdict,
    SandboxDeleteGrant,
    ServicePrincipalGrant,
)
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.policy import shadow
from meho_backplane.policy.evaluate import Decision, RuleRef, Verdict
from meho_backplane.policy.stamp import RuleStamp, keep_rule, take_rule
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import (
    seed_grant_row,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006042")
_CONNECTOR = "vmware-rest-9.0"

_KINDS = ("person", "person_web", "agent", "hosted_run", "agent_hosted_run", "service", "runner")
_LEVELS = ("safe", "caution", "dangerous", "destructive")
_SIGNS = ("none", "verb", "tag", "name")
_GRANTS = ("none", "ordinary", "delete")
_GATES = ("direct", "step")
_OP_IDS = {
    "none": "POST:/vcenter/vm/{vm}/plain",
    "verb": "DELETE:/vcenter/vm/{vm}",
    "tag": "vmware.vm.wipe",
    "name": "vmware.vm.delete",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "false")
    get_settings.cache_clear()


def _principal_kind(kind: str) -> PrincipalKind:
    if kind.startswith("agent"):
        return PrincipalKind.AGENT
    return {
        "service": PrincipalKind.SERVICE,
        "runner": PrincipalKind.RUNNER,
    }.get(kind, PrincipalKind.USER)


def _operator(
    kind: str, grant: str = "none", *, role: TenantRole = TenantRole.OPERATOR
) -> Operator:
    return Operator(
        sub=f"{kind}:{grant}",
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=_principal_kind(kind),
        web_session=kind == "person_web",
    )


def _descriptor(
    level: str, requires_approval: bool = False, sign: str = "none", gate: str = "direct"
) -> EndpointDescriptor:
    fields: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "op_id": _OP_IDS[sign],
        "safety_level": level,
        "requires_approval": requires_approval,
        "parameter_schema": {},
    }
    if gate == "step":
        fields["source_kind"] = "composite"
    else:
        fields["source_kind"] = "typed" if sign in ("tag", "name") else "ingested"
        fields["method"] = "DELETE" if sign == "verb" else "POST"
        fields["tags"] = ["write", "destructive"] if sign == "tag" else ["write"]
    return EndpointDescriptor(**fields)


async def _gate(kind: str, operator: Operator, descriptor: EndpointDescriptor) -> PermissionVerdict:
    if kind.endswith("hosted_run"):
        with actor_delegation("agent:hosted-run"):
            verdict, _ = await policy_gate(
                operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
            )
        return verdict
    verdict, _ = await policy_gate(
        operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
    )
    return verdict


def _mismatches(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == "policy_shadow_mismatch"]


def _metric(kind: str) -> float:
    return shadow.POLICY_SHADOW_MISMATCH_TOTAL.labels(kind=kind)._value.get()


def _forced(verdict: Verdict) -> Any:
    def fake(*_args: Any, **_kwargs: Any) -> Decision:
        return Decision(verdict=verdict, rule=RuleRef(source="default", rule_id="forced", text="x"))

    return fake


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_the_evaluator_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)
    calls: list[Any] = []

    async def boom(**kwargs: Any) -> None:
        calls.append(kwargs)
        raise AssertionError("the shadow ran with the switch off")

    monkeypatch.setattr(shadow, "evaluate_call", boom)
    monkeypatch.setattr(shadow, "evaluate", _forced(Verdict.DENY))
    with capture_logs() as logs:
        verdict = await _gate("person", _operator("person"), _descriptor("safe"))
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert calls == []
    assert _mismatches(logs) == []


@pytest.mark.asyncio
async def test_a_forced_difference_writes_one_line_and_keeps_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shadow, "evaluate", _forced(Verdict.DENY))
    before = _metric("call")
    with capture_logs() as logs:
        verdict = await _gate("person", _operator("person"), _descriptor("safe"))
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    lines = _mismatches(logs)
    assert len(lines) == 1
    assert lines[0]["today_verdict"] == "auto-execute"
    assert lines[0]["shadow_verdict"] == "deny"
    assert lines[0]["shadow_rule_id"] == "forced"
    assert lines[0]["op_id"] == _OP_IDS["none"]
    assert _metric("call") == before + 1


@pytest.mark.asyncio
async def test_the_same_answer_writes_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow, "evaluate", _forced(Verdict.RUN))
    before = _metric("call")
    with capture_logs() as logs:
        verdict = await _gate("person", _operator("person"), _descriptor("safe"))
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert _mismatches(logs) == []
    assert _metric("call") == before


@pytest.mark.asyncio
async def test_a_failure_in_the_shadow_keeps_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Decision:
        raise RuntimeError("evaluator broke")

    monkeypatch.setattr(shadow, "evaluate", broken)
    with capture_logs() as logs:
        verdict = await _gate("person", _operator("person"), _descriptor("dangerous"))
    assert verdict is PermissionVerdict.NEEDS_APPROVAL
    assert _mismatches(logs) == []
    assert any(e.get("event") == "policy_shadow_failed" for e in logs)


# ---------------------------------------------------------------------------
# The kept rule (#6044)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_keeps_the_rule_once_marked_as_not_deciding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shadow, "evaluate", _forced(Verdict.DENY))
    verdict = await _gate("person", _operator("person"), _descriptor("safe"))
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    stamp = take_rule()
    assert stamp == RuleStamp(source="default", rule_id="forced", text="x", decided=False)
    assert stamp.label() == "default:forced (shadow)"
    assert stamp.as_json() == {
        "source": "default",
        "rule_id": "forced",
        "text": "x",
        "decided": False,
    }
    assert take_rule() is None


@pytest.mark.asyncio
async def test_switch_off_keeps_no_rule_and_drops_an_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    keep_rule(RuleStamp(source="default", rule_id="old", text="from an earlier gate"))
    await _gate("person", _operator("person"), _descriptor("safe"))
    assert take_rule() is None


@pytest.mark.asyncio
async def test_a_failure_in_the_shadow_keeps_no_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Decision:
        raise RuntimeError("evaluator broke")

    monkeypatch.setattr(shadow, "evaluate", broken)
    keep_rule(RuleStamp(source="default", rule_id="old", text="from an earlier gate"))
    await _gate("person", _operator("person"), _descriptor("safe"))
    assert take_rule() is None


def test_a_rule_that_decided_has_no_shadow_mark() -> None:
    stamp = RuleStamp(source="tenant_rule", rule_id="r1", text="t", policy_version=4, decided=True)
    assert stamp.label() == "tenant_rule:r1"
    assert stamp.as_json()["decided"] is True


@pytest.mark.asyncio
async def test_the_shadow_uses_no_grant() -> None:
    """Today's gate records the grant use once; the shadow records nothing."""
    grant_id = await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=_TENANT,
        principal_sub="service:ordinary",
        op_id=_OP_IDS["none"],
        connector_id=_CONNECTOR,
    )
    with capture_logs() as logs:
        verdict = await _gate("service", _operator("service", "ordinary"), _descriptor("dangerous"))
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert _mismatches(logs) == []
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    uses = [r for r in rows if r.payload.get("grant_id") == str(grant_id)]
    assert len(uses) == 1


# ---------------------------------------------------------------------------
# Today's verdict in every cell of the table
# ---------------------------------------------------------------------------


async def _seed_grants() -> None:
    for kind in _KINDS:
        for op_id in _OP_IDS.values():
            if kind.startswith("agent"):
                async with get_sessionmaker()() as session:
                    session.add(
                        AgentPermission(
                            tenant_id=_TENANT,
                            principal_sub=f"{kind}:ordinary",
                            op_pattern=op_id,
                            verdict=PermissionVerdict.AUTO_EXECUTE.value,
                            created_by_sub="op-admin",
                        )
                    )
                    await session.commit()
                continue
            await seed_grant_row(
                model=ServicePrincipalGrant,
                tenant_id=_TENANT,
                principal_sub=f"{kind}:ordinary",
                op_id=op_id,
                connector_id=_CONNECTOR,
            )
            await seed_grant_row(
                model=SandboxDeleteGrant,
                tenant_id=_TENANT,
                principal_sub=f"{kind}:delete",
                op_id=op_id,
                connector_id=_CONNECTOR,
                expires_at=soon(),
            )


async def _table_mismatches() -> tuple[int, list[dict[str, Any]]]:
    cells = 0
    with capture_logs() as logs:
        for kind in _KINDS:
            for level in (*_LEVELS, "unknown"):
                for ra in (False, True):
                    for sign in _SIGNS:
                        for grant in _GRANTS:
                            for gate in _GATES:
                                await _gate(
                                    kind,
                                    _operator(kind, grant),
                                    _descriptor(level, ra, sign, gate),
                                )
                                cells += 1
    return cells, _mismatches(logs)


@pytest.mark.asyncio
async def test_the_evaluator_gives_todays_verdict_in_every_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_grants()
    turn_sandbox_mode_off(monkeypatch)
    cells, off = await _table_mismatches()
    assert cells == 7 * 5 * 2 * 4 * 3 * 2
    assert off == [], off[:3]
    turn_sandbox_mode_on(monkeypatch)
    _, on = await _table_mismatches()
    assert on == [], on[:3]


@pytest.mark.asyncio
async def test_agent_grant_required_and_read_only_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent with no grant under ``AGENT_GRANT_REQUIRED=on``, and a read_only agent."""
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "on")
    get_settings.cache_clear()
    with capture_logs() as logs:
        for level in _LEVELS:
            for role in (TenantRole.OPERATOR, TenantRole.READ_ONLY):
                operator = _operator("agent", "none", role=role)
                await _gate("agent", operator, _descriptor(level))
                await _gate("agent_hosted_run", operator, _descriptor(level))
                await _gate("hosted_run", _operator("person", role=role), _descriptor(level))
    assert _mismatches(logs) == []


# ---------------------------------------------------------------------------
# The approval side
# ---------------------------------------------------------------------------


def _request(*, requester: str, level: str) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        principal_sub=requester,
        op_id=_OP_IDS["none"],
        status="pending",
        proposed_effect={"safety_level": level},
    )


def test_approval_answers_agree() -> None:
    approver = _operator("person", "approver")
    with capture_logs() as logs:
        for level in _LEVELS:
            for requester in ("someone-else", approver.sub):
                for kind in ("person", "agent", "service"):
                    approve_right(
                        _operator(kind, "approver"), _request(requester=requester, level=level)
                    )
                approve_right(
                    _operator("person", "approver", role=TenantRole.TENANT_ADMIN),
                    _request(requester=requester, level=level),
                )
    assert _mismatches(logs) == []


def test_a_forced_approval_difference_writes_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shadow,
        "may_approve",
        lambda *_a, **_k: (False, RuleRef(source="default", rule_id="forced", text="x")),
    )
    before = _metric("approval")
    with capture_logs() as logs:
        right = approve_right(_operator("person"), _request(requester="other", level="caution"))
    assert right.may_approve is True
    lines = _mismatches(logs)
    assert len(lines) == 1
    assert lines[0]["kind"] == "approval"
    assert lines[0]["today_may_approve"] is True
    assert lines[0]["shadow_may_approve"] is False
    assert _metric("approval") == before + 1


def test_approval_switch_off_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("ran with the switch off")

    monkeypatch.setattr(shadow, "may_approve", boom)
    with capture_logs() as logs:
        right = approve_right(_operator("person"), _request(requester="other", level="caution"))
    assert right.may_approve is True
    assert _mismatches(logs) == []
