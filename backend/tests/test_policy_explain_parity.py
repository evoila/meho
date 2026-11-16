# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho policy explain`` gives the answer of the real call.

A matrix of callers and operations runs through explain and through the
real call. The vendor is never called: the step that would send the call
is replaced, so a call that gets there counts as ``run``. Only the
multi-step operation really runs: its step check decides if it waits.
Explain and the call must agree, except for a check that explain names in
``not_checked``.

Sandbox mode is on, so the service login's delete grant lets the
``destructive`` operation run at once. A call without a preview hash is
then refused, and explain must name that check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import structlog

from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.broadcast.announce_gate import reset_announce_gate_cache_for_testing
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.keycloak.connector import KeycloakConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    EndpointDescriptor,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatcher as _dispatcher
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.governed_subops import (
    register_governed_subops,
    reset_governed_subop_registry,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.policy.explain_checks import (
    CONNECTOR_RESOLUTION,
    DESTRUCTIVE_PREVIEW_BINDING,
    IDEMPOTENCY_KEY,
    OPERATION_PARAM_CHECK,
    PERMISSION_PREFLIGHT,
)
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import seed_delete_grant, turn_sandbox_mode_on

_TENANT = UUID("00000000-0000-0000-0000-000000006540")
_CLAIM_LOOKUP = "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim"
_SEND = "meho_backplane.operations.dispatcher._execute_and_audit"

#: The refusal code of a call, and the ``not_checked`` name that covers it.
_COVERED_BY: dict[str, str] = {
    "no_connector": CONNECTOR_RESOLUTION,
    "ambiguous_connector": CONNECTOR_RESOLUTION,
    "target_required": CONNECTOR_RESOLUTION,
    "refused_before_approval": OPERATION_PARAM_CHECK,
    "preview_binding_required": DESTRUCTIVE_PREVIEW_BINDING,
    "write_capability_denied": PERMISSION_PREFLIGHT,
    "idempotency_key_conflict": IDEMPOTENCY_KEY,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_announce_gate_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    reset_governed_subop_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    reset_governed_subop_registry()
    reset_broadcast_client_for_testing()
    reset_announce_gate_cache_for_testing()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Caller:
    name: str
    operator: Operator
    hosted_run: bool = False


def _operator(
    sub: str, kind: PrincipalKind, *, web: bool = False, client_id: str | None = None
) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        web_session=web,
        client_id=client_id,
    )


_CALLERS = (
    _Caller("person in the console", _operator("person-a", PrincipalKind.USER, web=True)),
    _Caller("person with a token", _operator("person-b", PrincipalKind.USER)),
    _Caller("service account with a grant", _operator("svc-a", PrincipalKind.SERVICE)),
    _Caller(
        "agent with a grant",
        _operator("agent-a", PrincipalKind.AGENT, client_id="agent:granted"),
    ),
    _Caller(
        "agent without a grant",
        _operator("agent-b", PrincipalKind.AGENT, client_id="agent:plain"),
    ),
    _Caller(
        "person's login in a hosted agent run",
        _operator("person-c", PrincipalKind.USER),
        hosted_run=True,
    ),
)


@contextmanager
def _as(caller: _Caller) -> Iterator[None]:
    if not caller.hosted_run:
        yield
        return
    token = current_agent_run_id_var.set(uuid.uuid4())
    try:
        with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: "agent:hosted"}):
            yield
    finally:
        current_agent_run_id_var.reset(token)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Op:
    name: str
    connector_id: str
    op_id: str
    target: str | None = None


class _NoOpConnector(Connector):
    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


class _VaultConnector(_NoOpConnector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"


class _OpenConnector(_NoOpConnector):
    product = "widget"
    version = "1.x"
    impl_id = "widget"
    accepts_ingested_operations = True


_KC_CONNECTOR = f"{KeycloakConnector.impl_id}-{KeycloakConnector.version}"
_OPS = (
    _Op("safe read", "vault-1.x", "vault.parity.read"),
    _Op("caution write", "vault-1.x", "vault.parity.write"),
    _Op("dangerous write", "vault-1.x", "vault.parity.delete"),
    _Op("ingested op, target accepts it", "widget-1.x", "POST:/items", "<widget-target>"),
    _Op("ingested op, target refuses it", _KC_CONNECTOR, "POST:/items", "<kc-target>"),
    _Op("multi-step caution write", "vault-1.x", "vault.parity.multi"),
    _Op("destructive delete", "vault-1.x", "vault.parity.destroy"),
)
_MULTI = _OPS[5]
_DESTROY = _OPS[6]
#: The write step of the multi-step operation. No caller has a grant on it.
_STEP = "POST:/parity/steps"


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _multi_handler(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> Any:
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_MULTI.connector_id,
        op_id=_STEP,
        safety_level="dangerous",
        requires_approval=False,
        target=target,
        params={},
    )
    return gated if gated is not None else {"done": True}


async def _seed(embedding: AsyncMock, *, announce_rule: bool) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_VaultConnector)
    register_connector_v2(product="widget", version="", impl_id="", cls=_OpenConnector)
    register_connector_v2(
        product=KeycloakConnector.product,
        version=KeycloakConnector.version,
        impl_id=KeycloakConnector.impl_id,
        cls=KeycloakConnector,
    )
    for op, level in zip(_OPS[:3], ("safe", "caution", "dangerous"), strict=True):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op.op_id,
            handler=_handler,
            summary="Test op.",
            description="Test op.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=embedding,
            safety_level=level,
        )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_DESTROY.op_id,
        handler=_handler,
        summary="Test op.",
        description="Test op.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="destructive",
        requires_approval=True,
    )
    await register_composite_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_MULTI.op_id,
        handler=_multi_handler,
        summary="Test op.",
        description="Test op.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="caution",
        requires_approval=False,
    )
    register_governed_subops(
        composite_op_id=_MULTI.op_id, connector_id=_MULTI.connector_id, sub_op_ids=(_STEP,)
    )
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(id=_TENANT, slug="t-6540", name="Tenant", announce_gate_enabled=announce_rule)
        )
        for name, product, version in (
            ("<widget-target>", "widget", "1.0"),
            ("<kc-target>", KeycloakConnector.product, "26.0"),
        ):
            session.add(
                TargetORM(
                    tenant_id=_TENANT,
                    name=name,
                    aliases=[],
                    product=product,
                    host=f"{product}.example.test",
                    port=443,
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={},
                    fingerprint={"version": version},
                )
            )
        for product, version, impl_id in (
            ("widget", "1.x", "widget"),
            (KeycloakConnector.product, KeycloakConnector.version, KeycloakConnector.impl_id),
        ):
            session.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    op_id="POST:/items",
                    source_kind="ingested",
                    method="POST",
                    path="/items",
                    summary="Add an item",
                    description="Ingested write.",
                    tags=[],
                    parameter_schema={"type": "object"},
                    safety_level="caution",
                    requires_approval=False,
                    is_enabled=True,
                    embedding=[0.1] * 384,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub="agent:granted",
                op_pattern="*",
                verdict="auto-execute",
                created_by_sub="seed-admin",
            )
        )
        for op in (*_OPS[1:4], _MULTI):
            session.add(
                ServicePrincipalGrant(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT,
                    principal_sub="svc-a",
                    op_id=op.op_id,
                    connector_id=op.connector_id,
                    target_id=None,
                    reason="test: standing grant",
                    created_by_sub="seed-admin",
                )
            )
        await session.commit()
    await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub="svc-a",
        op_id=_DESTROY.op_id,
        connector_id=_DESTROY.connector_id,
    )
    reset_announce_gate_cache_for_testing()


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


async def _real_call(caller: _Caller, op: _Op, send: AsyncMock) -> tuple[str, str | None]:
    """The answer of the real call (``run``, ``wait`` or ``deny``) and its refusal code."""
    send.reset_mock()
    body: dict[str, Any] = {"connector_id": op.connector_id, "op_id": op.op_id, "params": {}}
    if op.target is not None:
        body["target"] = op.target
    result = await call_operation(caller.operator, body)
    code = (result.get("extras") or {}).get("error_code")
    if code == "awaiting_approval":
        return "wait", None
    if send.await_count and result.get("status") == "ok":
        return "run", None
    return "deny", code if isinstance(code, str) else None


async def _mismatches(embedding: AsyncMock, *, announce_rule: bool) -> tuple[list[str], set[str]]:
    """The cases where explain and the call disagree, and every answer the calls gave."""
    await _seed(embedding, announce_rule=announce_rule)
    sent = OperationResult(status="ok", op_id="sent", duration_ms=0.0)
    really_send = _dispatcher._execute_and_audit

    async def _send(**kwargs: Any) -> OperationResult:
        if kwargs["op_id"] == _MULTI.op_id:
            return await really_send(**kwargs)
        return sent

    found: list[str] = []
    answers: set[str] = set()
    with ExitStack() as stack:
        send = stack.enter_context(patch(_SEND, new=AsyncMock(side_effect=_send)))
        stack.enter_context(patch(_CLAIM_LOOKUP, new=AsyncMock(return_value=False)))
        for caller in _CALLERS:
            for op in _OPS:
                with _as(caller):
                    answer = await explain_call(
                        CallQuestion(
                            asker=caller.operator,
                            me=True,
                            connector_id=op.connector_id,
                            op_id=op.op_id,
                            target=op.target,
                        )
                    )
                    call, code = await _real_call(caller, op, send)
                answers.add(call)
                named = _COVERED_BY.get(code or "")
                if answer.answer == call or (named and named in (answer.not_checked or [])):
                    continue
                found.append(
                    f"{caller.name} / {op.name}: explain {answer.answer} "
                    f"({answer.today.reason if answer.today else answer.error}), "
                    f"call {call} ({code})"
                )
    return found, answers


@pytest.mark.parametrize("announce_rule", [True, False], ids=["announce-on", "announce-off"])
async def test_explain_agrees_with_the_real_call(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, announce_rule: bool
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    mismatches, answers = await _mismatches(embedding, announce_rule=announce_rule)
    assert mismatches == [], "\n".join(mismatches)
    # The matrix reaches every answer, so it checks each path of the call.
    assert answers == {"run", "wait", "deny"}
