# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Policy gate for direct hosted-agent runs: the agent's grants apply (#4237).

A person who starts a hosted agent run directly stays the subject of each
call the run makes, and the run's agent is bound as the actor
(:func:`~meho_backplane.auth.delegation.actor_delegation`). The gate works
out the agent's verdict and the person's verdict and returns the stricter
one. These tests drive :func:`~meho_backplane.operations._validate.policy_gate`
directly with and without the actor bound.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, PermissionVerdict, Tenant
from meho_backplane.operations import _validate
from meho_backplane.operations._validate import policy_gate
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT = uuid.UUID("42370000-0000-0000-0000-000000000001")
_AGENT = "agent:reader"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _person(*, principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="alice",
        name="Alice",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


def _descriptor(
    op_id: str = "vault.kv.read",
    *,
    safety_level: str = "safe",
    requires_approval: bool = False,
) -> Any:
    return SimpleNamespace(
        op_id=op_id, safety_level=safety_level, requires_approval=requires_approval
    )


async def _seed_grant(op_pattern: str, verdict: str, principal_sub: str = _AGENT) -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="t-4237", name="Tenant 4237"))
            await session.flush()
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=principal_sub,
                op_pattern=op_pattern,
                verdict=verdict,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()


async def test_without_an_actor_the_person_verdict_is_unchanged() -> None:
    """No run bound: a person's caution op still runs (default allow)."""
    await _seed_grant("*", "deny")
    verdict, reason = await policy_gate(
        operator=_person(), descriptor=_descriptor(safety_level="caution"), target=None
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason is None


async def test_agent_deny_grant_denies_an_op_the_person_may_run() -> None:
    """The agent's deny grant wins over the person's default allow."""
    await _seed_grant("*", "deny")
    with actor_delegation(_AGENT):
        verdict, reason = await policy_gate(
            operator=_person(), descriptor=_descriptor(), target=None
        )
    assert verdict is PermissionVerdict.DENY
    assert reason is not None
    assert "agent agent:reader acting for alice" in reason


async def test_agent_safety_default_applies_without_a_grant() -> None:
    """With no grant, a caution op waits for approval, as for the agent alone."""
    with actor_delegation(_AGENT):
        verdict, _reason = await policy_gate(
            operator=_person(), descriptor=_descriptor(safety_level="caution"), target=None
        )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL


async def test_agent_auto_execute_grant_runs_a_safe_op() -> None:
    """When both may run the op, it runs."""
    await _seed_grant("vault.kv.*", "auto-execute")
    with actor_delegation(_AGENT):
        verdict, reason = await policy_gate(
            operator=_person(), descriptor=_descriptor(), target=None
        )
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason is None


async def test_person_requires_approval_still_applies_when_the_agent_may_run() -> None:
    """The person's own approval rule still holds inside a direct run."""
    await _seed_grant("vault.kv.*", "auto-execute")
    with actor_delegation(_AGENT):
        verdict, reason = await policy_gate(
            operator=_person(),
            descriptor=_descriptor(requires_approval=True),
            target=None,
        )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL
    assert reason is not None
    assert "requires_approval" in reason


async def test_grant_for_another_agent_does_not_apply() -> None:
    """Only the run's own agent grants count."""
    await _seed_grant("*", "deny", principal_sub="agent:other")
    with actor_delegation(_AGENT):
        verdict, _reason = await policy_gate(
            operator=_person(), descriptor=_descriptor(), target=None
        )
    assert verdict is PermissionVerdict.AUTO_EXECUTE


async def test_agent_denial_skips_the_person_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied call never reaches the person's gate, so no standing grant is used."""
    await _seed_grant("*", "deny")

    async def _must_not_run(**_kwargs: Any) -> Any:
        raise AssertionError("the person's verdict must not be worked out after a denial")

    monkeypatch.setattr(_validate, "_non_agent_verdict", _must_not_run)
    with actor_delegation(_AGENT):
        verdict, _reason = await policy_gate(
            operator=_person(principal_kind=PrincipalKind.SERVICE),
            descriptor=_descriptor(safety_level="caution"),
            target=None,
            connector_id="vault-1.x",
        )
    assert verdict is PermissionVerdict.DENY
