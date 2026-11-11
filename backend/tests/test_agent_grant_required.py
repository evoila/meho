# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The switch ``AGENT_GRANT_REQUIRED``: no grant, no rights for agent logins (#6189).

* ``off`` (default): an agent with no matching grant gets the risk-level
  default, as before.
* ``observe``: the same verdict, plus one ``agent_no_grant_would_deny``
  log line for each call that ``on`` would refuse.
* ``on``: ``deny`` on every risk level, with the code ``agent_no_grant``,
  on every path that reads agent grants: the dispatcher, the preview, a
  direct hosted-agent run and the docs tools' target check.
* A matching row works as before. People and service accounts are not
  touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth import permissions
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.permissions import agent_no_grant_log_scope, resolve_verdict
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    AuditLog,
    PermissionVerdict,
    Target,
    Tenant,
)
from meho_backplane.docs_search.target_scope import TargetDeniedError, _check_target_access
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._errors import result_denied
from meho_backplane.operations._park_check import _PARK_CHECKS
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.deny_reasons import (
    AGENT_GRANT_DENIED,
    AGENT_NO_GRANT,
    deny_reason_code,
)
from meho_backplane.operations.governed_subops import (
    register_governed_subops,
    reset_governed_subop_registry,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.test_agent_invocation import _make_operator as _make_person_for_run
from tests.test_agent_invocation import _seed_definition, _seed_echo_op
from tests.test_agent_refusal_audit_reason import _refusal, _run_call
from tests.test_destructive_preview_binding import _CONNECTOR_ID as _DELETE_CONNECTOR
from tests.test_destructive_preview_binding import _OP_ID as _DELETE_OP
from tests.test_destructive_preview_binding import _PARAMS as _DELETE_PARAMS
from tests.test_destructive_preview_binding import (
    _blast_radius_builder,
    _insert_destructive_descriptor,
    _register_recording_connector,
    _seed_target,
)
from tests.test_destructive_preview_binding import _operator as _delete_operator
from tests.test_operations_dispatcher import (
    _FakeTarget,
    _module_handler_returning_dict,
    _NoOpVaultConnector,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
_AGENT = "agent:grant-test"
_AGENT_SUB = "99999999-9999-9999-9999-999999999999"
_LEVELS = ("safe", "caution", "dangerous", "destructive")
_DEFAULT = {
    "safe": PermissionVerdict.AUTO_EXECUTE,
    "caution": PermissionVerdict.NEEDS_APPROVAL,
    "dangerous": PermissionVerdict.NEEDS_APPROVAL,
    "destructive": PermissionVerdict.NEEDS_APPROVAL,
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("AGENT_GRANT_REQUIRED", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    reset_governed_subop_registry()
    yield
    clear_registry()
    reset_dispatcher_caches()
    reset_governed_subop_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", mode)
    get_settings.cache_clear()


def _operator(
    kind: PrincipalKind = PrincipalKind.AGENT, role: TenantRole = TenantRole.OPERATOR
) -> Operator:
    agent = kind is PrincipalKind.AGENT
    return Operator(
        sub=_AGENT_SUB if agent else "person-1",
        name="Grant Test",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        client_id=_AGENT if agent else None,
    )


async def _grant(
    op_pattern: str,
    verdict: str,
    target_scope: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="t-grant", name="Tenant grant"))
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern=op_pattern,
                verdict=verdict,
                target_scope=target_scope,
                created_by_sub="admin",
                expires_at=expires_at,
            )
        )
        await session.commit()


async def _resolve(
    safety_level: str,
    *,
    operator: Operator | None = None,
    op_id: str = "vault.kv.read",
    target_id: Any = None,
) -> tuple[PermissionVerdict, str]:
    async with get_sessionmaker()() as session:
        return await resolve_verdict(
            session=session,
            operator=operator or _operator(),
            op_id=op_id,
            safety_level=safety_level,
            target_id=target_id,
        )


# --- Settings -----------------------------------------------------------------


@pytest.mark.parametrize(("raw", "mode"), [(None, "off"), ("", "off"), (" ON ", "on")])
def test_switch_values(monkeypatch: pytest.MonkeyPatch, raw: str | None, mode: str) -> None:
    if raw is not None:
        _switch(monkeypatch, raw)
    assert get_settings().agent_grant_required == mode


def test_unknown_switch_value_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "strict")
    with pytest.raises(ValueError, match="AGENT_GRANT_REQUIRED must be one of"):
        get_settings()


# --- Resolver: three states x four risk levels, no matching row ------------------


@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("mode", ["off", "observe"])
async def test_no_grant_keeps_the_default_when_off_or_observe(
    monkeypatch: pytest.MonkeyPatch, mode: str, level: str
) -> None:
    _switch(monkeypatch, mode)
    with capture_logs() as logs:
        verdict, reason = await _resolve(level)
    assert verdict is _DEFAULT[level]
    assert "safety_level default" in reason
    would_deny = [e for e in logs if e["event"] == "agent_no_grant_would_deny"]
    if mode == "off":
        assert would_deny == []
    else:
        assert would_deny == [
            {
                "event": "agent_no_grant_would_deny",
                "log_level": "info",
                "agent": _AGENT,
                "tenant_id": str(_TENANT),
                "op_id": "vault.kv.read",
                "target_id": None,
                "safety_level": level,
            }
        ]


@pytest.mark.parametrize("level", [*_LEVELS, "unknown-level"])
async def test_no_grant_is_denied_on_every_level_when_on(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    _switch(monkeypatch, "on")
    verdict, reason = await _resolve(level)
    assert verdict is PermissionVerdict.DENY
    assert reason.startswith("agent_no_grant: ")
    assert _AGENT in reason
    assert "'vault.kv.read'" in reason
    assert f"meho agent grant create --principal {_AGENT}" in reason
    assert "console under Agents" in reason


@pytest.mark.parametrize("level", _LEVELS)
async def test_matching_row_works_as_before_when_on(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    _switch(monkeypatch, "on")
    await _grant("*", "auto-execute")
    with capture_logs() as logs:
        verdict, reason = await _resolve(level)
    # The risk-level ceiling still applies: only ``safe`` runs at once.
    assert verdict is _DEFAULT[level]
    assert "permission row" in reason
    assert not [e for e in logs if e["event"] == "agent_no_grant_would_deny"]


async def test_deny_row_stays_a_grant_refusal_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "on")
    await _grant("vault.kv.*", "deny")
    verdict, reason = await _resolve("safe")
    assert verdict is PermissionVerdict.DENY
    assert deny_reason_code(reason) == AGENT_GRANT_DENIED


async def test_role_ceiling_still_applies_to_a_matching_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    await _grant("*", "auto-execute")
    verdict, reason = await _resolve("safe", operator=_operator(role=TenantRole.READ_ONLY))
    assert verdict is PermissionVerdict.NEEDS_APPROVAL
    assert "role ceiling" in reason


async def test_row_for_another_op_or_target_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    await _grant("nsx.*", "auto-execute")
    await _grant("vault.kv.*", "auto-execute", target_scope=str(uuid.uuid4()))
    verdict, reason = await _resolve("safe", target_id=uuid.uuid4())
    assert verdict is PermissionVerdict.DENY
    assert reason.startswith("agent_no_grant: ")


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.SERVICE])
async def test_other_logins_are_not_touched(
    monkeypatch: pytest.MonkeyPatch, kind: PrincipalKind
) -> None:
    _switch(monkeypatch, "on")
    verdict, _ = await _resolve("safe", operator=_operator(kind))
    assert verdict is PermissionVerdict.AUTO_EXECUTE


# --- Codes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "agent_no_grant: agent agent:a has no grant",
        "agent agent:a acting for person-1: agent_no_grant: agent agent:a has no grant",
    ],
)
def test_refusal_maps_to_agent_no_grant(reason: str) -> None:
    assert deny_reason_code(reason) == AGENT_NO_GRANT
    result = result_denied("vault.kv.read", reason, 1.0)
    assert result.extras["error_code"] == AGENT_NO_GRANT
    assert result.extras["reason_code"] == AGENT_NO_GRANT


def test_other_refusals_keep_their_codes() -> None:
    result = result_denied("vault.kv.read", "verdict=deny; source=permission row", 1.0)
    assert result.extras["error_code"] == "denied"
    assert result.extras["reason_code"] == AGENT_GRANT_DENIED


# --- Dispatcher and preview -----------------------------------------------------


async def _register_op(
    stub: AsyncMock, op_id: str = "vault.kv.get", *, requires_approval: bool = False
) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_module_handler_returning_dict,
        summary="Read op.",
        description="A safe read.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=requires_approval,
        when_to_use=None,
        embedding_service=stub,
    )


async def _dispatch(operator: Operator, op_id: str = "vault.kv.get") -> Any:
    return await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id=op_id,
        target=_FakeTarget(product="vault"),
        params={},
    )


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(result.scalars())


async def test_dispatch_refuses_an_agent_with_no_grant(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_op(stub_embedding_service)

    result = await _dispatch(_operator())

    assert result.status == "denied"
    assert result.extras["error_code"] == AGENT_NO_GRANT
    assert result.extras["reason_code"] == AGENT_NO_GRANT
    (row,) = await _audit_rows("vault.kv.get")
    assert row.policy_decision == "deny"
    assert row.payload["error"]["reason_code"] == AGENT_NO_GRANT


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.SERVICE])
async def test_dispatch_lets_people_and_services_run_as_before(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, kind: PrincipalKind
) -> None:
    _switch(monkeypatch, "on")
    await _register_op(stub_embedding_service)

    result = await _dispatch(_operator(kind))

    assert result.status == "ok", result.error


async def test_requires_approval_does_not_turn_the_refusal_into_a_wait(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_op(stub_embedding_service, "vault.kv.flagged", requires_approval=True)

    result = await _dispatch(_operator(), "vault.kv.flagged")

    assert result.status == "denied"
    assert result.extras["error_code"] == AGENT_NO_GRANT


async def test_preview_says_deny_with_the_same_code(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_op(stub_embedding_service)

    envelope = await preview_dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.get",
        target=_FakeTarget(product="vault"),
        params={},
    )

    assert envelope["policy_verdict"] == "deny", envelope
    assert envelope["policy_reason"].startswith("agent_no_grant: ")


# --- Multi-step operations -------------------------------------------------------

_MULTI_OP = "vault.kv.multi"
_WRITE_STEP = "POST:/v1/secret/data/{path}"
#: The calls of :func:`_multi_step_handler`; a refused call adds none.
_MULTI_RUNS: list[dict[str, Any]] = []


async def _multi_step_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """A multi-step op's handler; it records that it ran."""
    del operator, target
    _MULTI_RUNS.append(params)
    return {"ran": True}


async def _register_multi_step_op(stub: AsyncMock) -> None:
    """A safe op that lists one read step and one write step."""
    _MULTI_RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_MULTI_OP,
        handler=_multi_step_handler,
        summary="Multi-step op.",
        description="Reads, then writes.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=False,
        when_to_use=None,
        embedding_service=stub,
    )
    register_governed_subops(
        composite_op_id=_MULTI_OP,
        connector_id="vault-1.x",
        sub_op_ids=("GET:/v1/secret/data/{path}", _WRITE_STEP),
    )


async def _seed_vault_target() -> Target:
    """A stored target, so an approved call can load it again when it resumes."""
    async with get_sessionmaker()() as session:
        target = Target(
            tenant_id=_TENANT,
            name="vault-grant-test",
            aliases=[],
            product="vault",
            host="vault.test",
            port=8200,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "1.15"},
            notes="seeded by test_agent_grant_required",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


async def _approval_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


@pytest.mark.parametrize("op_verdict", ["auto-execute", "needs-approval"])
async def test_multi_step_op_with_an_uncovered_write_step_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, op_verdict: str
) -> None:
    """The op has a grant, its write step has none: refused before it waits or runs."""
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, op_verdict)

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "denied", result
    assert result.extras["error_code"] == AGENT_NO_GRANT
    assert result.extras["reason_code"] == AGENT_NO_GRANT
    assert _WRITE_STEP in result.error
    assert _MULTI_RUNS == []
    assert await _approval_requests() == []
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id=_MULTI_OP,
        target=_FakeTarget(product="vault"),
        params={},
    )
    if op_verdict == "auto-execute":
        # The preview checks the same step list and gives the same answer.
        assert preview["policy_verdict"] == "deny", preview
        assert _WRITE_STEP in preview["policy_reason"]


async def test_multi_step_op_runs_when_every_write_step_has_a_grant(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "auto-execute")
    await _grant(_WRITE_STEP, "auto-execute")

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1


async def test_multi_step_op_with_the_switch_off_is_not_checked_up_front(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "auto-execute")

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "ok", result.error


async def test_expired_grant_does_not_count_when_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_op(stub_embedding_service)
    await _grant("vault.kv.get", "auto-execute", expires_at=datetime.now(UTC) - timedelta(hours=1))

    result = await _dispatch(_operator())

    assert result.status == "denied"
    assert result.extras["error_code"] == AGENT_NO_GRANT


async def test_expired_step_grant_does_not_count_when_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "auto-execute")
    await _grant(_WRITE_STEP, "auto-execute", expires_at=datetime.now(UTC) - timedelta(hours=1))

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "denied"
    assert result.extras["error_code"] == AGENT_NO_GRANT
    assert _MULTI_RUNS == []


async def test_waiting_call_runs_after_a_person_approves_when_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """A needs-approval grant makes the call wait; after the approval it runs."""
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    target = await _seed_vault_target()

    parked = await dispatch(
        operator=_operator(), connector_id="vault-1.x", op_id=_MULTI_OP, target=target, params={}
    )

    assert parked.status == "awaiting_approval", parked
    assert _MULTI_RUNS == []
    approver = _operator(PrincipalKind.USER)
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver)
        await session.commit()
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        result = await resume_dispatch_after_approval(operator=approver, request=request)

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1


# --- Direct hosted-agent run ----------------------------------------------------


async def test_direct_run_is_refused_when_the_agent_has_no_grant(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """The person may run the op; the run's agent has no grant, so the call is refused."""
    _switch(monkeypatch, "on")
    await _seed_definition()
    await _seed_echo_op(stub_embedding_service)

    row = _refusal(await _run_call())

    assert row.policy_decision == "deny"
    assert row.actor_sub == "agent:reader"
    assert row.payload["error"]["reason_code"] == AGENT_NO_GRANT
    assert row.payload["error"]["error_code"] == AGENT_NO_GRANT
    assert "agent_no_grant: agent agent:reader" in row.payload["error"]["reason"]


async def test_direct_run_person_alone_runs_the_op(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """Control: the same person calling the op outside a run is not refused."""
    _switch(monkeypatch, "on")
    await _seed_echo_op(stub_embedding_service)

    result = await dispatch(
        operator=_make_person_for_run(),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=_FakeTarget(product="vault"),
        params={},
    )

    assert result.status == "ok", result.error


# --- Docs tools' target check ---------------------------------------------------


async def test_docs_target_check_refuses_an_agent_with_no_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    row: Any = SimpleNamespace(id=uuid.uuid4(), name="docs-target")

    async with get_sessionmaker()() as session:
        with pytest.raises(TargetDeniedError) as caught:
            await _check_target_access(
                session, _operator(), row, op_id="meho.docs.search", query="docs-target"
            )
        # A person is not checked against agent grants.
        await _check_target_access(
            session,
            _operator(PrincipalKind.USER),
            row,
            op_id="meho.docs.search",
            query="docs-target",
        )

    detail: Any = caught.value.detail
    assert detail["reason"].startswith("agent_no_grant: ")
    assert deny_reason_code(detail["reason"]) == AGENT_NO_GRANT


async def test_docs_target_check_allows_with_the_switch_off() -> None:
    row: Any = SimpleNamespace(id=uuid.uuid4(), name="docs-target")
    async with get_sessionmaker()() as session:
        await _check_target_access(
            session, _operator(), row, op_id="meho.docs.search", query="docs-target"
        )


# --- observe: one log line per call ---------------------------------------------


def _would_deny_spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    spy = MagicMock()
    monkeypatch.setattr(permissions, "_log", spy)
    return spy


def _would_deny_ops(spy: MagicMock) -> list[str]:
    return [
        c.kwargs["op_id"]
        for c in spy.info.call_args_list
        if c.args == ("agent_no_grant_would_deny",)
    ]


async def test_observe_logs_once_per_call_inside_a_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "observe")
    spy = _would_deny_spy(monkeypatch)

    with agent_no_grant_log_scope():
        await _resolve("caution")
        await _resolve("caution")
        await _resolve("caution", op_id="vault.kv.list")
        with agent_no_grant_log_scope():  # a preview built inside the call
            await _resolve("caution")
    assert _would_deny_ops(spy) == ["vault.kv.read", "vault.kv.list"]

    with agent_no_grant_log_scope(), agent_no_grant_log_scope(new=True):  # a new top-level call
        await _resolve("caution")
    await _resolve("caution")  # outside a call each check logs
    assert _would_deny_ops(spy) == [
        "vault.kv.read",
        "vault.kv.list",
        "vault.kv.read",
        "vault.kv.read",
    ]


async def _passes(ctx: PreviewContext) -> str | None:
    return None


async def test_observe_preview_with_a_park_check_logs_once(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """The park check and the governance fields both run the gate; one line."""
    _switch(monkeypatch, "observe")
    await _register_op(stub_embedding_service, "vault.kv.flagged", requires_approval=True)
    monkeypatch.setitem(_PARK_CHECKS, "vault.kv.flagged", AsyncMock(side_effect=_passes))
    spy = _would_deny_spy(monkeypatch)

    envelope = await preview_dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.flagged",
        target=_FakeTarget(product="vault"),
        params={},
    )

    assert envelope["policy_verdict"] == "needs-approval", envelope
    _PARK_CHECKS["vault.kv.flagged"].assert_awaited_once()
    assert _would_deny_ops(spy) == ["vault.kv.flagged"]


async def test_observe_destructive_call_logs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The call builds its preview again to check the hash; still one line."""
    _switch(monkeypatch, "observe")
    _register_recording_connector()
    monkeypatch.setitem(_PREVIEW_BUILDERS, _DELETE_OP, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=[0.1] * 384)
    agent = _delete_operator(sub="agent:deleter", principal_kind=PrincipalKind.AGENT)
    args = {
        "connector_id": _DELETE_CONNECTOR,
        "op_id": _DELETE_OP,
        "target": "gh-prod",
        "params": _DELETE_PARAMS,
    }
    spy = _would_deny_spy(monkeypatch)

    preview = await preview_operation(agent, args)
    assert preview["status"] == "ok", preview
    assert _would_deny_ops(spy) == [_DELETE_OP]

    call = await call_operation(agent, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    assert _would_deny_ops(spy) == [_DELETE_OP, _DELETE_OP]
