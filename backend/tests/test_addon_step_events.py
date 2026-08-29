# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the add-on step-event push contract (#3027).

Covers the two acceptance criteria directly:

* **Durable delivery with resume** — :meth:`AddonStepEventService.record_if_owned`
  appends monotonic ``seq`` rows; :meth:`list_for_pairing` reads strictly
  forward from an ``after`` cursor, so an add-on that persists its last
  ``seq`` resumes without gaps or repeats across a restart.
* **Scoping enforced** — a step event is written only when its owner
  principal matches a pairing's ``service_account_sub``; one pairing's
  subscription never returns another pairing's events, even when both
  carry the same ``work_ref``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AddonPairing, AddonStepEvent, Tenant
from meho_backplane.operations.addon_pairing_contract import BACKPLANE_CONTRACT_VERSION
from meho_backplane.operations.addon_step_events import AddonStepEventService
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OTHER_TENANT = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_SUB_A = "svc-account-A"
_SUB_B = "svc-account-B"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _seed_tenant(tenant_id: uuid.UUID = _TENANT, slug: str = "tenant-a") -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=slug))
            await session.commit()


async def _seed_pairing(
    *,
    tenant_id: uuid.UUID = _TENANT,
    name: str,
    service_account_sub: str | None,
) -> uuid.UUID:
    """Insert a pairing row directly (no Keycloak) and return its id."""
    pairing_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AddonPairing(
                id=pairing_id,
                tenant_id=tenant_id,
                name=name,
                keycloak_client_id=f"addon:{name}",
                keycloak_internal_id=f"kc-{name}",
                service_account_sub=service_account_sub,
                owner_sub="op-admin",
                contract_version=BACKPLANE_CONTRACT_VERSION,
                addon_contract_version=BACKPLANE_CONTRACT_VERSION,
                addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
                created_by_sub="op-admin",
            )
        )
        await session.commit()
    return pairing_id


async def _record(
    service: AddonStepEventService,
    *,
    tenant_id: uuid.UUID = _TENANT,
    owner_principal_sub: str | None,
    event_kind: str = "approval.approved",
    work_ref: str | None = "gh:evoila/meho#1",
    payload: dict[str, object] | None = None,
) -> AddonStepEvent | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await service.record_if_owned(
            session,
            tenant_id=tenant_id,
            owner_principal_sub=owner_principal_sub,
            event_kind=event_kind,
            work_ref=work_ref,
            audit_id=None,
            payload=payload or {"decision": "approved"},
        )
        await session.commit()
        return row


@pytest.mark.asyncio
async def test_record_if_owned_appends_for_a_paired_principal() -> None:
    """A produced event whose owner matches a pairing's sub is recorded."""
    await _seed_tenant()
    pairing_id = await _seed_pairing(name="automation", service_account_sub=_SUB_A)
    service = AddonStepEventService()

    row = await _record(service, owner_principal_sub=_SUB_A)

    assert row is not None
    assert row.pairing_id == pairing_id
    assert row.event_kind == "approval.approved"
    assert row.seq >= 1


@pytest.mark.asyncio
async def test_record_if_owned_noop_when_owner_absent_or_unpaired() -> None:
    """No pairing match (or no owner) -> no row written, cheap no-op."""
    await _seed_tenant()
    await _seed_pairing(name="automation", service_account_sub=_SUB_A)
    service = AddonStepEventService()

    assert await _record(service, owner_principal_sub=None) is None
    assert await _record(service, owner_principal_sub="some-human-sub") is None

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        count = (
            await session.execute(select(func.count()).select_from(AddonStepEvent))
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_list_for_pairing_resumes_forward_from_cursor() -> None:
    """Durable resume: reading with after=<last seq> yields only newer events."""
    await _seed_tenant()
    pairing_id = await _seed_pairing(name="automation", service_account_sub=_SUB_A)
    service = AddonStepEventService()

    for i in range(3):
        await _record(service, owner_principal_sub=_SUB_A, event_kind=f"approval.step{i}")

    first_page = await service.list_for_pairing(pairing_id=pairing_id, after_seq=0, limit=2)
    assert [e.event_kind for e in first_page.items] == ["approval.step0", "approval.step1"]
    assert first_page.next_cursor is not None

    # Resume strictly past the cursor — no repeats, the third event only.
    resumed = await service.list_for_pairing(
        pairing_id=pairing_id,
        after_seq=int(first_page.next_cursor),
        limit=100,
    )
    assert [e.event_kind for e in resumed.items] == ["approval.step2"]

    # A cursor at the head returns an empty page (nothing missed, nothing new).
    tail = await service.list_for_pairing(
        pairing_id=pairing_id,
        after_seq=int(resumed.next_cursor),
        limit=100,
    )
    assert tail.items == []
    assert tail.next_cursor is None


@pytest.mark.asyncio
async def test_scoping_never_delivers_another_pairings_events() -> None:
    """Acceptance: events outside the principal's lineage are never delivered.

    Two paired add-ons in the same tenant, each with an event carrying the
    **same** work_ref. Each pairing's subscription returns only its own
    event — the other's is never visible.
    """
    await _seed_tenant()
    pairing_a = await _seed_pairing(name="automation", service_account_sub=_SUB_A)
    pairing_b = await _seed_pairing(name="ssp", service_account_sub=_SUB_B)
    service = AddonStepEventService()

    shared_work_ref = "gh:evoila/meho#42"
    await _record(
        service,
        owner_principal_sub=_SUB_A,
        event_kind="approval.approved",
        work_ref=shared_work_ref,
    )
    await _record(
        service,
        owner_principal_sub=_SUB_B,
        event_kind="approval.rejected",
        work_ref=shared_work_ref,
    )

    a_events = await service.list_for_pairing(pairing_id=pairing_a, after_seq=0, limit=100)
    b_events = await service.list_for_pairing(pairing_id=pairing_b, after_seq=0, limit=100)

    assert [e.event_kind for e in a_events.items] == ["approval.approved"]
    assert [e.event_kind for e in b_events.items] == ["approval.rejected"]
    # Cross-pairing isolation is total, work_ref collision notwithstanding.
    assert all(e.work_ref == shared_work_ref for e in a_events.items + b_events.items)


@pytest.mark.asyncio
async def test_resolve_pairing_for_sub_binds_by_token_sub() -> None:
    """The subscription bind: a caller's token sub maps to its own pairing."""
    await _seed_tenant()
    pairing_id = await _seed_pairing(name="automation", service_account_sub=_SUB_A)
    service = AddonStepEventService()

    bound = await service.resolve_pairing_for_sub(tenant_id=_TENANT, service_account_sub=_SUB_A)
    assert bound is not None
    assert bound.id == pairing_id
    assert bound.name == "automation"

    # An unknown sub binds to nothing (a non-add-on service principal).
    assert (
        await service.resolve_pairing_for_sub(tenant_id=_TENANT, service_account_sub="nobody")
        is None
    )


@pytest.mark.asyncio
async def test_record_scoped_to_tenant() -> None:
    """A sub that matches a pairing in another tenant is not attributed here."""
    await _seed_tenant()
    await _seed_tenant(tenant_id=_OTHER_TENANT, slug="tenant-b")
    await _seed_pairing(tenant_id=_OTHER_TENANT, name="automation", service_account_sub=_SUB_A)
    service = AddonStepEventService()

    # Same sub, but recorded under _TENANT where no pairing owns it.
    row = await _record(service, tenant_id=_TENANT, owner_principal_sub=_SUB_A)
    assert row is None


@pytest.mark.asyncio
async def test_record_if_owned_committed_persists_and_is_fail_open() -> None:
    """The committed variant writes a durable row and swallows failures."""
    await _seed_tenant()
    pairing_id = await _seed_pairing(name="automation", service_account_sub=_SUB_A)
    service = AddonStepEventService()

    await service.record_if_owned_committed(
        tenant_id=_TENANT,
        owner_principal_sub=_SUB_A,
        event_kind="approval.approved",
        work_ref="gh:evoila/meho#7",
        audit_id=uuid.uuid4(),
        payload={"decision": "approved"},
    )

    page = await service.list_for_pairing(pairing_id=pairing_id, after_seq=0, limit=100)
    assert [e.event_kind for e in page.items] == ["approval.approved"]

    # Fail-open: an unpaired owner is a silent no-op (no raise, no row).
    await service.record_if_owned_committed(
        tenant_id=_TENANT,
        owner_principal_sub="not-an-addon",
        event_kind="approval.approved",
        work_ref=None,
        audit_id=None,
        payload={},
    )
    page2 = await service.list_for_pairing(pairing_id=pairing_id, after_seq=0, limit=100)
    assert len(page2.items) == 1
