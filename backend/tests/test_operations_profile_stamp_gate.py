# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Review-gate interlock for ExecutionProfile stamping (G0.28-T5 #1971).

Security-load-bearing acceptance criteria for the parent Initiative
#1965: stamping an :class:`ExecutionProfile` onto an ingested connector
makes it *dispatchable* but must NEVER auto-enable dispatch. The
``is_enabled=False`` / ``review_status='staged'`` review gate stays the
load-bearing interlock — a profiled-but-unreviewed op is blocked at
dispatch exactly as a staged bare-shim op is, until an operator clears
the gate per-op.

These tests pin the three issue acceptance criteria:

1. Stamping a profile leaves ops ``is_enabled=False`` /
   ``review_status='staged'`` (it does not touch the review gate).
2. Dispatch against an unreviewed profiled connector is blocked just
   like a staged ingested op (``lookup_descriptor`` hard-filters
   ``is_enabled = TRUE``, so the staged op is invisible to dispatch).
3. An audit event (``meho.connector.profile_stamp``) is emitted on the
   first stamp, and a re-stamp is idempotent (no duplicate row).

Plus the enable-time advisory (``profiled_but_unreviewed``) that surfaces
the gate-clearance at the moment an operator flips ``is_enabled=True`` on
a profiled connector's op.

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`; each test owns
its own seed data and clears the process-global v2 registry around itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.registry import all_connectors_v2, clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations._lookup import descriptor_exists_any_state, lookup_descriptor
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.ingest._internals import (
    OP_PROFILE_STAMP,
    enable_time_auto_shim_warnings,
)
from meho_backplane.operations.ingest.connector_registration import (
    ensure_connector_class_registered,
    resolved_profiled_connector_class,
)
from meho_backplane.settings import get_settings

_PRODUCT = "acme"
_VERSION = "1.2"
_IMPL_ID = "acme-rest"
_CONNECTOR_ID = "acme-rest-1.2"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Operator construction depends on transitively."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


class _AcmeProfiled(ProfiledRestConnector):
    """A profiled connector for the acme triple (carries a vetted profile in T3+)."""

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1.2,<2.0"


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _seed_staged_connector(*, tenant_id: uuid.UUID, ops_per_group: int = 3) -> None:
    """Insert one staged group with *ops_per_group* staged (disabled) ops."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="group-0",
                name="Group 0",
                when_to_use="Use group 0.",
                review_status="staged",
            )
        )
        for i in range(ops_per_group):
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=f"GET:/api/v1/group-0/{i}",
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/v1/group-0/{i}",
                    group_id=group_id,
                    summary=f"Operation {i}",
                    is_enabled=False,
                )
            )
        await session.commit()


async def _ops_state(*, tenant_id: uuid.UUID) -> dict[str, bool]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.tenant_id == tenant_id,
                        EndpointDescriptor.product == _PRODUCT,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {r.op_id: r.is_enabled for r in rows}


async def _group_states(*, tenant_id: uuid.UUID) -> list[str]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(OperationGroup).where(
                        OperationGroup.tenant_id == tenant_id,
                        OperationGroup.product == _PRODUCT,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [r.review_status for r in rows]


async def _count_audit_rows(*, op_id: str) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
        return len(list(rows))


async def _latest_audit_row(*, op_id: str) -> AuditLog:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.path == op_id)
                    .order_by(AuditLog.occurred_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert row is not None, f"expected an audit row with path={op_id}"
        return row


# ---------------------------------------------------------------------------
# AC1 — stamping a profile leaves ops staged / disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_leaves_ops_staged_and_disabled() -> None:
    """AC1: stamping a profile does not flip any op's is_enabled / review_status."""
    tenant_id = uuid.uuid4()
    await _seed_staged_connector(tenant_id=tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    first = await service.record_profile_stamp(
        _CONNECTOR_ID, tenant_id=tenant_id, connector_class=_AcmeProfiled
    )

    assert first is True
    # The connector is now dispatchable (registered) ...
    assert all_connectors_v2()[(_PRODUCT, _VERSION, _IMPL_ID)] is _AcmeProfiled
    # ... but every op stays disabled and every group stays staged.
    assert all(enabled is False for enabled in (await _ops_state(tenant_id=tenant_id)).values())
    assert await _group_states(tenant_id=tenant_id) == ["staged"]


# ---------------------------------------------------------------------------
# AC2 — dispatch against an unreviewed profiled connector is blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_blocked_for_unreviewed_profiled_op_like_staged_op() -> None:
    """AC2: a staged profiled op is invisible to the dispatch lookup.

    ``lookup_descriptor`` hard-filters ``is_enabled = TRUE``, so a staged
    op never resolves to a connector at all — the profiled connector being
    registered (dispatchable) makes no difference. The op is still present
    in any state (``descriptor_exists_any_state``), proving it is the
    review gate, not absence, that blocks dispatch.
    """
    tenant_id = uuid.uuid4()
    await _seed_staged_connector(tenant_id=tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    await service.record_profile_stamp(
        _CONNECTOR_ID, tenant_id=tenant_id, connector_class=_AcmeProfiled
    )

    op_id = "GET:/api/v1/group-0/0"
    # Dispatch lookup is blocked (staged → is_enabled=False).
    assert (
        await lookup_descriptor(
            tenant_id=tenant_id,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
        )
        is None
    )
    # But the row exists — it is the gate, not absence, that blocks it.
    assert (
        await descriptor_exists_any_state(
            tenant_id=tenant_id,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
        )
        is True
    )

    # After the operator clears the gate, the same lookup resolves.
    await service.edit_op(_CONNECTOR_ID, op_id, tenant_id=tenant_id, is_enabled=True)
    assert (
        await lookup_descriptor(
            tenant_id=tenant_id,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
        )
    ) is not None


# ---------------------------------------------------------------------------
# AC3 — an audit event fires on first stamp; re-stamp is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_stamp_emits_audit_event() -> None:
    """AC3: the first stamp writes one ``meho.connector.profile_stamp`` audit row."""
    tenant_id = uuid.uuid4()
    await _seed_staged_connector(tenant_id=tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    await service.record_profile_stamp(
        _CONNECTOR_ID, tenant_id=tenant_id, connector_class=_AcmeProfiled
    )

    assert await _count_audit_rows(op_id=OP_PROFILE_STAMP) == 1
    row = await _latest_audit_row(op_id=OP_PROFILE_STAMP)
    assert row.payload["connector_id"] == _CONNECTOR_ID
    assert row.payload["product"] == _PRODUCT
    assert row.payload["version"] == _VERSION
    assert row.payload["impl_id"] == _IMPL_ID
    assert row.payload["connector_class"] == "_AcmeProfiled"


@pytest.mark.asyncio
async def test_restamp_is_idempotent_no_duplicate_audit() -> None:
    """A second stamp of an already-stamped connector is a no-op (no audit row)."""
    tenant_id = uuid.uuid4()
    await _seed_staged_connector(tenant_id=tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))

    assert (
        await service.record_profile_stamp(
            _CONNECTOR_ID, tenant_id=tenant_id, connector_class=_AcmeProfiled
        )
        is True
    )
    assert (
        await service.record_profile_stamp(
            _CONNECTOR_ID, tenant_id=tenant_id, connector_class=_AcmeProfiled
        )
        is False
    )
    assert await _count_audit_rows(op_id=OP_PROFILE_STAMP) == 1


@pytest.mark.asyncio
async def test_stamp_rejects_non_profiled_class() -> None:
    """Only a ProfiledRestConnector carries a profile to stamp; a bare shim raises."""
    tenant_id = uuid.uuid4()
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    ensure_connector_class_registered(
        product="other", version="1.0", impl_id="other-rest", base_url=None
    )
    bare = all_connectors_v2()[("other", "1.0", "other-rest")]

    with pytest.raises(TypeError, match="ProfiledRestConnector"):
        await service.record_profile_stamp(
            "other-rest-1.0", tenant_id=tenant_id, connector_class=bare
        )


# ---------------------------------------------------------------------------
# Enable-time advisory — profiled_but_unreviewed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_on_profiled_connector_returns_profiled_advisory() -> None:
    """Enabling an op on a profiled connector surfaces the gate-clearance advisory.

    The connector IS dispatchable, so this is not the bare-shim dead end —
    the advisory confirms the enable (not the earlier stamp) is what made
    the op callable. The write still lands (warnings never block it).
    """
    tenant_id = uuid.uuid4()
    await _seed_staged_connector(tenant_id=tenant_id)
    service = ReviewService(_make_operator(tenant_id=tenant_id))
    await service.record_profile_stamp(
        _CONNECTOR_ID, tenant_id=tenant_id, connector_class=_AcmeProfiled
    )

    warnings = await service.edit_op(
        _CONNECTOR_ID, "GET:/api/v1/group-0/0", tenant_id=tenant_id, is_enabled=True
    )

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.code == "profiled_but_unreviewed"
    assert warning.connector_class == "_AcmeProfiled"
    assert "review gate" in warning.message
    assert "did NOT" in warning.message
    # The write still landed.
    assert (await _ops_state(tenant_id=tenant_id))["GET:/api/v1/group-0/0"] is True


def test_resolved_profiled_connector_class_probe() -> None:
    """The enable-time probe returns the profiled class name, ``None`` for a bare shim."""
    from meho_backplane.connectors.registry import register_connector_v2

    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_AcmeProfiled)
    assert resolved_profiled_connector_class(product=_PRODUCT, version=_VERSION) == "_AcmeProfiled"

    clear_registry()
    ensure_connector_class_registered(
        product="bare", version="3.0", impl_id="bare-rest", base_url=None
    )
    assert resolved_profiled_connector_class(product="bare", version="3.0") is None


def test_enable_time_warnings_empty_for_no_connector() -> None:
    """No registered connector for the line → no advisory (resolver miss is fail-soft)."""
    from meho_backplane.operations.ingest._internals import ConnectorScope

    scope = ConnectorScope(product="ghost", version="9.9", impl_id="ghost-rest", tenant_id=None)
    assert enable_time_auto_shim_warnings("ghost-rest-9.9", "GET:/x", scope) == []
