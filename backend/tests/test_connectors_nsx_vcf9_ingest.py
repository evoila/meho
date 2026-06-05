# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF-9 NSX 9.x ingest + dispatch + curation tests (#1530).

NSX-T 4.x was renumbered onto the VCF train at VCF 9.0 — a live
appliance reports NSX 9.0.x and the vendor spec carries ``info.version``
in the 9.x scheme (observed ``9.1.0.0``). Before #1530 the
:class:`~meho_backplane.connectors.nsx.NsxConnector` advertised
``supported_version_range=">=4.0,<5.0"``, so a VCF-9 NSX appliance was
un-ingestable into a *dispatchable* connector under any label: labelling
``version=9.1.0.0`` cleared the spec/label gate but the class
version-range pre-flight rejected it; labelling ``version=4.2`` cleared
the range but failed the spec/label match.

This module pins the three legs of the fix:

* **Ingest version-range pre-flight** —
  :func:`check_version_covered_by_registered_class` accepts a VCF-9 9.x
  label against the registered :class:`NsxConnector` and still rejects
  a 10.x label that falls outside the widened band.
* **Runtime dispatch** —
  :func:`~meho_backplane.connectors.resolver.resolve_connector` binds a
  9.x-fingerprinted NSX target to :class:`NsxConnector`.
* **Core-op curation** —
  :func:`~meho_backplane.connectors.nsx.apply_nsx_core_curation` resolves
  the connector_id the ingest actually landed under (e.g.
  ``nsx-rest-9.1.0.0`` from an operator-supplied 9.x label), not the
  hard-coded 4.2 slug it used before.

SQLite via the autouse ``_default_database_url`` conftest fixture; no
``pg_engine`` required. Runs in the ``meho-runners-ci`` lane alongside the
other ``backend/tests/test_connectors_nsx_*.py`` files; no Docker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.nsx import (
    NSX_CONNECTOR_ID,
    NSX_CORE_GROUPS,
    NSX_CORE_OPS,
    NSX_IMPL_ID,
    NSX_PRODUCT,
    NsxConnector,
    apply_nsx_core_curation,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import (
    ReviewService,
    UncoveredVersionLabel,
    check_version_covered_by_registered_class,
)
from meho_backplane.settings import get_settings

#: The ``info.version`` a VCF-9 NSX appliance's spec carries (per the
#: #1530 origin report, RDC ci-01). An operator labels the ingest with
#: this value so it matches the spec, then dispatches under the derived
#: ``connector_id="nsx-rest-9.1.0.0"``.
_VCF9_LABEL = "9.1.0.0"
_VCF9_CONNECTOR_ID = f"{NSX_IMPL_ID}-{_VCF9_LABEL}"

_TEST_TENANT = uuid.UUID("9f000000-0000-0000-0000-0000000009f0")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction reads transitively."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_operator() -> Operator:
    """Build a tenant-admin operator for the curation leg."""
    return Operator(
        sub=f"nsx-vcf9-test-{uuid.uuid4()}",
        name="NSX VCF9 Ingest Test",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TEST_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


class _FingerprintStub:
    """Minimal ``target.fingerprint`` shape the resolver reads."""

    def __init__(self, version: str) -> None:
        self.version = version


class _TargetStub:
    """Duck-typed target carrying ``.product`` + ``.fingerprint.version``."""

    def __init__(self, *, product: str, version: str) -> None:
        self.product = product
        self.fingerprint = _FingerprintStub(version)


# ---------------------------------------------------------------------------
# Leg 1 — ingest version-range pre-flight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["9.0", "9.0.2", "9.1.0.0", "9.9", "4.2"])
def test_ingest_preflight_accepts_nsx_9x_and_4x_labels(label: str) -> None:
    """The ingest range pre-flight accepts VCF-9 9.x (and still 4.x) labels.

    Before #1530 the only registered ``(nsx, nsx-rest)`` class advertised
    ``>=4.0,<5.0``, so a 9.x label raised :exc:`UncoveredVersionLabel`
    here. The widened ``>=4.0,<10.0`` advertisement covers the whole
    VCF-aligned band; the call returns ``None`` (no raise).
    """
    # Returns None on success; an uncovered label would raise.
    check_version_covered_by_registered_class(
        product=NSX_PRODUCT,
        version=label,
        impl_id=NSX_IMPL_ID,
    )


@pytest.mark.parametrize("label", ["10.0", "10.1", "3.9"])
def test_ingest_preflight_rejects_out_of_band_labels(label: str) -> None:
    """Labels outside ``>=4.0,<10.0`` still raise — the widen is bounded.

    The fix widens the band to the VCF-aligned 9.x line, not to "any
    version". A 10.x label (a hypothetical future NSX line that would
    need its own class / range decision) and a pre-4.0 label both stay
    uncovered so the operator gets the actionable
    :exc:`UncoveredVersionLabel` 422 rather than a silent dead-letter
    ingest.
    """
    with pytest.raises(UncoveredVersionLabel) as exc_info:
        check_version_covered_by_registered_class(
            product=NSX_PRODUCT,
            version=label,
            impl_id=NSX_IMPL_ID,
        )
    assert exc_info.value.product == NSX_PRODUCT
    assert exc_info.value.version == label


# ---------------------------------------------------------------------------
# Leg 2 — runtime dispatch resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["9.0.2", "9.1.0.0", "4.2"])
def test_resolver_dispatches_nsx_9x_target_to_nsx_connector(version: str) -> None:
    """A 9.x-fingerprinted NSX target resolves to :class:`NsxConnector`.

    The resolver keys on the class's PEP 440 ``supported_version_range``
    (a :class:`packaging.specifiers.SpecifierSet`), not the class-pinned
    ``version`` attribute — so widening the range is sufficient for
    dispatch (#1530). The 4.x case proves the standalone NSX-T line still
    binds through the same class.
    """
    target = _TargetStub(product=NSX_PRODUCT, version=version)
    assert resolve_connector(target) is NsxConnector


# ---------------------------------------------------------------------------
# Leg 3 — core-op curation resolves the ingest's connector_id
# ---------------------------------------------------------------------------


async def _seed_groups_and_ops(*, connector_version: str) -> None:
    """Seed the 9 curated NSX ops + groups under a given version label.

    Mirrors the G0.7 ingest default: ``review_status='staged'`` groups,
    ``is_enabled=False`` / ``source_kind='ingested'`` ops, scoped to the
    operator tenant so the curation's audit-log path stays isolated.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}
    async with sessionmaker() as session:
        for group in NSX_CORE_GROUPS:
            group_id = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=_TEST_TENANT,
                    product=NSX_PRODUCT,
                    version=connector_version,
                    impl_id=NSX_IMPL_ID,
                    group_key=group.group_key,
                    name=f"PLACEHOLDER {group.group_key}",
                    when_to_use=f"PLACEHOLDER when_to_use {group.group_key}",
                    review_status="staged",
                ),
            )
            group_ids[group.group_key] = group_id

        for op in NSX_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=_TEST_TENANT,
                    product=NSX_PRODUCT,
                    version=connector_version,
                    impl_id=NSX_IMPL_ID,
                    op_id=op.op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    group_id=group_ids[op.group_key],
                    summary=f"ingested op {op.op_id}",
                    is_enabled=False,
                ),
            )
        await session.commit()


async def _enabled_state(*, connector_version: str) -> dict[str, bool]:
    """Return ``{op_id: is_enabled}`` for the seeded connector version."""
    from sqlalchemy import select

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.tenant_id == _TEST_TENANT,
                        EndpointDescriptor.product == NSX_PRODUCT,
                        EndpointDescriptor.version == connector_version,
                        EndpointDescriptor.impl_id == NSX_IMPL_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
    return {row.op_id: row.is_enabled for row in rows}


@pytest.mark.asyncio
async def test_curation_resolves_operator_supplied_9x_connector_id() -> None:
    """``apply_nsx_core_curation`` curates ops under the ingest's 9.x id.

    Ingested rows land under the **operator-supplied** ``version`` label,
    so a VCF-9 spec ingested as ``version=9.1.0.0`` produces
    ``connector_id="nsx-rest-9.1.0.0"`` — not the class pin's
    ``nsx-rest-9.0``. Passing that id to the parameterised helper (#1530)
    enables exactly the 9 curated ops the ingest landed.
    """
    await _seed_groups_and_ops(connector_version=_VCF9_LABEL)

    await apply_nsx_core_curation(
        ReviewService(_make_operator()),
        tenant_id=_TEST_TENANT,
        connector_id=_VCF9_CONNECTOR_ID,
    )

    state = await _enabled_state(connector_version=_VCF9_LABEL)
    core_op_ids = {op.op_id for op in NSX_CORE_OPS}
    assert state, "expected the seeded 9.x ops to be present"
    assert all(state[op_id] for op_id in core_op_ids), (
        f"every curated 9.x core op should be enabled after curation; got {state}"
    )


def test_default_connector_id_tracks_the_vcf9_class_pin() -> None:
    """The curation helper's default ``connector_id`` is ``nsx-rest-9.0``.

    The default mirrors the registered class pin (#1530). An operator who
    ingested under the exact ``9.0`` label can call the helper without an
    override; finer 9.x labels supply the override.
    """
    assert NSX_CONNECTOR_ID == "nsx-rest-9.0"
