# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho policy explain`` asks the checks that run after the policy gate.

* With the announce rule of a tenant on, an agent's ``caution`` write that
  the gate lets run is ``deny``, as the call is. With the rule off it runs.
  A person is never asked.
* An ingested operation on a target whose connector does not take ingested
  operations is ``deny``, as the call is.
* Every answer about a call names what explain does not check.
* A call the gate denies is not asked again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.broadcast.announce_gate import reset_announce_gate_cache_for_testing
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.keycloak.connector import KeycloakConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._ingested_owner import INGESTED_OPS_NOT_ACCEPTED
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.policy.explain import CallQuestion, explain_call, without_policy
from meho_backplane.policy.explain_checks import NOT_CHECKED, refusal_after_gate
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-000000006536")
_WRITE_CONNECTOR = "vault-1.x"
_WRITE_OP = "vault.kv.delete"
_KC_CONNECTOR = f"{KeycloakConnector.impl_id}-{KeycloakConnector.version}"
_INGESTED_OP = "DELETE:/admin/items/{item}"
_TARGET_NAME = "<kc-target>"
_CLAIM_LOOKUP = "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim"


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
    yield
    reset_dispatcher_caches()
    clear_registry()
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


def _person(*, web: bool = True, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub="person-a",
        name="person-a",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
        web_session=web,
    )


@contextmanager
def _hosted_agent_run() -> Iterator[None]:
    """A call inside a hosted agent run: an agent, also with a person's login."""
    token = current_agent_run_id_var.set(uuid.uuid4())
    try:
        yield
    finally:
        current_agent_run_id_var.reset(token)


async def _seed_tenant(*, announce_rule: bool) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(id=_TENANT, slug="t-6536", name="Tenant", announce_gate_enabled=announce_rule)
        )
        await session.commit()
    reset_announce_gate_cache_for_testing()


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register_caution_write(embedding: AsyncMock) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_WRITE_OP,
        handler=_handler,
        summary="Test op.",
        description="Test op.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="caution",
    )


def _write_question(asker: Operator) -> CallQuestion:
    return CallQuestion(asker=asker, me=True, connector_id=_WRITE_CONNECTOR, op_id=_WRITE_OP)


# ---------------------------------------------------------------------------
# The announce rule of a tenant
# ---------------------------------------------------------------------------


async def test_announce_rule_on_denies_an_agents_caution_write(embedding: AsyncMock) -> None:
    await _seed_tenant(announce_rule=True)
    await _register_caution_write(embedding)
    with patch(_CLAIM_LOOKUP, new=AsyncMock(return_value=False)), _hosted_agent_run():
        answer = await explain_call(_write_question(_person()))
        call = await dispatch(
            operator=_person(),
            connector_id=_WRITE_CONNECTOR,
            op_id=_WRITE_OP,
            target=None,
            params={},
        )
    # The call is refused, and explain says so with the call's reason.
    assert call.extras["error_code"] == "announce_required", call
    assert answer.answer == "deny", answer
    assert answer.today is not None
    assert answer.today.verdict == "deny"
    assert answer.today.policy_verdict == "deny"
    assert answer.today.reason is not None
    assert answer.today.reason.startswith("announce_required: ")
    assert "meho_broadcast_announce" in answer.today.reason
    assert answer.approvers is None
    assert answer.not_checked == NOT_CHECKED


async def test_announce_rule_off_the_agents_caution_write_runs(embedding: AsyncMock) -> None:
    await _seed_tenant(announce_rule=False)
    await _register_caution_write(embedding)
    with patch(_CLAIM_LOOKUP, new=AsyncMock(return_value=False)), _hosted_agent_run():
        answer = await explain_call(_write_question(_person()))
    assert answer.answer == "run", answer
    assert answer.today is not None
    assert answer.today.reason is None


async def test_announce_rule_never_asks_a_person(embedding: AsyncMock) -> None:
    await _seed_tenant(announce_rule=True)
    await _register_caution_write(embedding)
    claim = AsyncMock(return_value=False)
    with patch(_CLAIM_LOOKUP, new=claim):
        answer = await explain_call(_write_question(_person()))
    assert answer.answer == "run", answer
    claim.assert_not_awaited()


async def test_announce_rule_passes_an_agent_with_a_claim(embedding: AsyncMock) -> None:
    await _seed_tenant(announce_rule=True)
    await _register_caution_write(embedding)
    with patch(_CLAIM_LOOKUP, new=AsyncMock(return_value=True)), _hosted_agent_run():
        answer = await explain_call(_write_question(_person()))
    assert answer.answer == "run", answer


# ---------------------------------------------------------------------------
# An ingested operation on a connector that does not take them
# ---------------------------------------------------------------------------


async def _seed_ingested_delete() -> None:
    register_connector_v2(
        product=KeycloakConnector.product,
        version=KeycloakConnector.version,
        impl_id=KeycloakConnector.impl_id,
        cls=KeycloakConnector,
    )
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT,
                name=_TARGET_NAME,
                aliases=[],
                product=KeycloakConnector.product,
                host="keycloak.example.test",
                port=443,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "26.0"},
            )
        )
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                product=KeycloakConnector.product,
                version=KeycloakConnector.version,
                impl_id=KeycloakConnector.impl_id,
                op_id=_INGESTED_OP,
                source_kind="ingested",
                method="DELETE",
                path="/admin/items/{item}",
                summary="Delete the item",
                description="Ingested delete.",
                tags=[],
                parameter_schema={
                    "type": "object",
                    "properties": {"item": {"type": "string", "x-meho-param-loc": "path"}},
                    "required": ["item"],
                },
                safety_level="dangerous",
                requires_approval=True,
                is_enabled=True,
                embedding=[0.1] * 384,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


async def test_an_ingested_op_on_a_connector_that_refuses_them_is_denied() -> None:
    await _seed_ingested_delete()
    asker = _person(web=False, role=TenantRole.TENANT_ADMIN)
    answer = await explain_call(
        CallQuestion(
            asker=asker,
            me=True,
            connector_id=_KC_CONNECTOR,
            op_id=_INGESTED_OP,
            target=_TARGET_NAME,
        )
    )
    call = await call_operation(
        asker,
        {
            "connector_id": _KC_CONNECTOR,
            "op_id": _INGESTED_OP,
            "target": _TARGET_NAME,
            "params": {"item": "<item-id>"},
        },
    )
    # The call is refused before it waits; explain gives the same reason.
    assert call["extras"]["error_code"] == INGESTED_OPS_NOT_ACCEPTED, call
    assert answer.status == "ok", answer
    assert answer.answer == "deny", answer
    assert answer.today is not None
    assert answer.today.reason == call["error"]
    assert answer.approvers is None
    assert answer.not_checked == NOT_CHECKED
    async with get_sessionmaker()() as session:
        count = (await session.execute(select(func.count()).select_from(ApprovalRequest))).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# What explain does not check, and a call the gate denies
# ---------------------------------------------------------------------------


async def test_the_not_checked_line_stays_for_a_caller_who_is_not_an_admin(
    embedding: AsyncMock,
) -> None:
    await _seed_tenant(announce_rule=False)
    await _register_caution_write(embedding)
    answer = await explain_call(_write_question(_person()))
    assert answer.not_checked == NOT_CHECKED
    assert without_policy(answer).not_checked == NOT_CHECKED
    assert NOT_CHECKED.startswith("Not checked: ")
    assert "\n" not in NOT_CHECKED


async def test_a_call_the_gate_denies_is_not_asked_again() -> None:
    boom = AsyncMock(side_effect=AssertionError("must not be asked"))
    descriptor = EndpointDescriptor(op_id=_WRITE_OP, safety_level="caution", source_kind="typed")
    with patch("meho_backplane.policy.explain_checks.announce_gate_blocks", new=boom):
        refusal = await refusal_after_gate(
            operator=_person(), descriptor=descriptor, target=None, answer="deny"
        )
    assert refusal is None
    boom.assert_not_awaited()


async def test_a_call_that_waits_is_not_asked_the_announce_rule() -> None:
    gate = AsyncMock(return_value="refused")
    descriptor = EndpointDescriptor(op_id=_WRITE_OP, safety_level="caution", source_kind="typed")
    with patch("meho_backplane.policy.explain_checks.announce_gate_blocks", new=gate):
        refusal = await refusal_after_gate(
            operator=_person(), descriptor=descriptor, target=None, answer="wait"
        )
    assert refusal is None
    gate.assert_not_awaited()
