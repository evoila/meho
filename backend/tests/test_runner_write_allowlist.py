# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-runner remote-write capability allowlist service (#3190, mechanism 2).

Service-level (no HTTP): grant / list / the mint-side ``load_runner_allowlist``
reader against the real ``runner_write_allowlist`` + ``runner_principal``
tables. The security contracts the acceptance criteria pin: enrollment grants
no write capability at birth, a write capability requires the separate human
``grant`` step (recording the operator), and a revoked/unknown runner gets
none.
"""

from __future__ import annotations

import uuid

import pytest

from meho_backplane.auth.runner_principals import RunnerPrincipalNotFoundError
from meho_backplane.auth.runner_write_allowlist import (
    RemoteWriteCapabilityGrant,
    RunnerWriteAllowlistService,
    load_runner_allowlist,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, Tenant
from meho_backplane.runner.satellite_tier import RemoteWriteAllowEntry
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_RUNNER = "runner-edge-1"
_OPERATOR = "operator-sub"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-b", name="tenant-b"))
        await session.commit()


async def _seed_runner(*, name: str = _RUNNER, revoked: bool = False) -> uuid.UUID:
    """Seed one runner principal (as enrollment would) and return its id."""
    runner_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            RunnerPrincipal(
                id=runner_id,
                tenant_id=_TENANT,
                name=name,
                keycloak_client_id=f"runner:{name}",
                keycloak_internal_id=f"kc-{name}",
                owner_sub="owner-sub",
                created_by_sub="creator-sub",
                revoked=revoked,
            )
        )
        await session.commit()
    return runner_id


def _grant(op_pattern: str, target_scope: str = "*") -> RemoteWriteCapabilityGrant:
    return RemoteWriteCapabilityGrant(op_pattern=op_pattern, target_scope=target_scope)


async def test_grant_creates_entry_bound_to_the_operator() -> None:
    await _seed_tenant()
    runner_id = await _seed_runner()

    entry = await RunnerWriteAllowlistService().grant(
        _TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.tag_set", "tgt-1")
    )

    assert entry.runner_principal_id == runner_id
    assert entry.op_pattern == "vmware.vm.tag_set"
    assert entry.target_scope == "tgt-1"
    # Bound at issuance: the granting human is recorded (never a runner).
    assert entry.created_by_sub == _OPERATOR


async def test_enrollment_grants_no_write_capability_at_birth() -> None:
    # A freshly-enrolled runner (principal seeded, no grant) has an EMPTY
    # allowlist — programmatic enrollment can never grant write capability at
    # birth (T7); a capability appears only after the separate human grant step.
    await _seed_tenant()
    await _seed_runner()

    entries = await RunnerWriteAllowlistService().list_(_TENANT, _RUNNER)
    assert entries == []

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        loaded = await load_runner_allowlist(session, tenant_id=_TENANT, runner_name=_RUNNER)
    assert loaded == ()


async def test_grant_is_idempotent_on_the_same_capability() -> None:
    await _seed_tenant()
    await _seed_runner()
    service = RunnerWriteAllowlistService()

    first = await service.grant(_TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.tag_set", "*"))
    second = await service.grant(
        _TENANT, _RUNNER, "other-operator", _grant("vmware.vm.tag_set", "*")
    )

    # Same fully-scoped capability → the existing row, not a duplicate.
    assert second.id == first.id
    entries = await service.list_(_TENANT, _RUNNER)
    assert len(entries) == 1


async def test_list_returns_granted_capabilities_sorted() -> None:
    await _seed_tenant()
    await _seed_runner()
    service = RunnerWriteAllowlistService()
    await service.grant(_TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.tag_set"))
    await service.grant(_TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.annotation_set"))

    entries = await service.list_(_TENANT, _RUNNER)

    assert [e.op_pattern for e in entries] == [
        "vmware.vm.annotation_set",
        "vmware.vm.tag_set",
    ]


async def test_grant_unknown_runner_raises() -> None:
    await _seed_tenant()
    with pytest.raises(RunnerPrincipalNotFoundError):
        await RunnerWriteAllowlistService().grant(
            _TENANT, "no-such-runner", _OPERATOR, _grant("vmware.vm.tag_set")
        )


async def test_grant_revoked_runner_raises() -> None:
    # A revoked runner gets no new write capability (the resolve step filters
    # ``revoked=False``), so a granting attempt fails as not-found.
    await _seed_tenant()
    await _seed_runner(revoked=True)
    with pytest.raises(RunnerPrincipalNotFoundError):
        await RunnerWriteAllowlistService().grant(
            _TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.tag_set")
        )


async def test_load_runner_allowlist_projects_entries_for_the_mint() -> None:
    await _seed_tenant()
    await _seed_runner()
    service = RunnerWriteAllowlistService()
    await service.grant(_TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.tag_set", "tgt-1"))
    await service.grant(_TENANT, _RUNNER, _OPERATOR, _grant("vmware.vm.annotation_set", "*"))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        loaded = await load_runner_allowlist(session, tenant_id=_TENANT, runner_name=_RUNNER)

    assert set(loaded) == {
        RemoteWriteAllowEntry("vmware.vm.tag_set", "tgt-1"),
        RemoteWriteAllowEntry("vmware.vm.annotation_set", "*"),
    }


async def test_load_runner_allowlist_unknown_runner_is_empty() -> None:
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        loaded = await load_runner_allowlist(session, tenant_id=_TENANT, runner_name="ghost")
    assert loaded == ()
