# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the VCFA dual-plane read-only v0.5 core-ops curation.

Covers :mod:`meho_backplane.connectors.vcf_automation.core_ops`:

* :func:`classify_vcfa_op` — path-prefix classifier rules across both
  the provider plane (``/cloudapi/1.0.0/*``) and the tenant plane
  (``/iaas/api/*``).
* :func:`apply_vcfa_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 8
  curated groups, lands ``llm_instructions`` on the 11 curated ops
  (6 provider + 5 tenant), and explicitly disables non-core ops in
  curated groups via the audit-log-driven operator-override
  exclusion so the :meth:`enable_group` cascade skips them. The
  load-bearing assertion is "11 ops dispatchable, every other op in
  curated groups stays ``is_enabled=False``".
* The dual-plane invariant: every enabled op's path resolves through
  :func:`plane_for_path` to the same plane its group declares. A
  drift would mean the dispatcher routes the op through the wrong
  auth plane and surfaces HTTP 401 in production.
* The ``spec_source`` tag: every persisted row of an ingested VCFA
  op carries ``spec:cloudapi`` or ``spec:iaas`` in its ``tags``
  column, so the operator can distinguish planes in
  ``meho connector review`` output.

Test harness mirrors :mod:`tests.test_connectors_nsx_core_ops`:
SQLite via the autouse ``_default_database_url`` fixture, hand-rolled
operator + connector seeding, audit-row counting from the chassis
``audit_log`` table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vcf_automation import (
    VCFA_CORE_GROUPS,
    VCFA_CORE_OPS,
    VCFA_IMPL_ID,
    VCFA_PATH_RULES,
    VCFA_PRODUCT,
    VCFA_VERSION,
    apply_vcfa_core_curation,
    classify_vcfa_op,
)
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction reads transitively.

    Mirrors :mod:`tests.test_connectors_nsx_core_ops` — the chassis
    :func:`get_settings` is cached, so each test rotates the cache
    before and after to keep cross-test leakage out.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FAKE_JWT = "header.payload.signature"

#: Stable tenant id for the curation tests. ``None`` would be closer
#: to the production VCFA shape (built-in scope) but tests that
#: exercise the audit-row + cascade path are easier to keep isolated
#: under a fresh tenant UUID per test.
_TEST_TENANT: uuid.UUID = uuid.UUID("22222222-3333-4444-5555-666666666666")


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"vcfa-core-ops-test-{uuid.uuid4()}",
        name="VCFA Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _spec_source_for_op(op_id: str) -> str:
    """Return the ``spec_source`` tag the dual-plane ingest writes per op.

    The provider plane is ingested from ``cloudapi.yaml`` and the
    tenant plane from ``iaas.yaml``. Both feed
    :func:`register_ingested_operations` once each with the per-spec
    ``spec_source`` argument, which appends ``f"spec:{spec_source}"``
    to every row's ``tags`` column. The seed helper below replicates
    that contract without going through the real ingest pipeline.
    """
    if op_id.startswith("GET:/iaas/api/"):
        return "iaas"
    return "cloudapi"


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`VCFA_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``, name + when_to_use placeholders so
    :func:`apply_vcfa_core_curation`'s ``edit_group`` step has
    something to overwrite). For each :data:`VCFA_CORE_OPS` entry,
    inserts the curated op (``is_enabled=False`` to match the G0.7
    ingest default for ``source_kind='ingested'`` rows). Each op's
    ``tags`` carry the plane-appropriate ``spec:<source>`` marker
    the dual-spec ingest contract emits.

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops
    per curated group with deterministic op_ids
    (``f"GET:/canary-extra/{group_key}/{i}"``) so the test can
    assert :func:`apply_vcfa_core_curation` correctly disables them.
    The extra ops carry the curated group's plane's spec_source
    tag — a non-core ``/cloudapi/...`` op sits under a provider
    group, a non-core ``/iaas/api/...`` op sits under a tenant
    group.

    Returns ``{group_key: group_id}`` for follow-up assertions.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}
    group_planes = {group.group_key: group.plane for group in VCFA_CORE_GROUPS}
    async with sessionmaker() as session:
        for group in VCFA_CORE_GROUPS:
            group_id = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=VCFA_PRODUCT,
                    version=VCFA_VERSION,
                    impl_id=VCFA_IMPL_ID,
                    group_key=group.group_key,
                    name=f"PLACEHOLDER NAME for {group.group_key}",
                    when_to_use=f"PLACEHOLDER when_to_use for {group.group_key}",
                    review_status="staged",
                ),
            )
            group_ids[group.group_key] = group_id

        for op in VCFA_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            spec_source = _spec_source_for_op(op.op_id)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=VCFA_PRODUCT,
                    version=VCFA_VERSION,
                    impl_id=VCFA_IMPL_ID,
                    op_id=op.op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    group_id=group_ids[op.group_key],
                    summary=f"ingested op {op.op_id}",
                    tags=[f"spec:{spec_source}"],
                    is_enabled=False,
                ),
            )

        if extra_ops_per_group:
            for group in VCFA_CORE_GROUPS:
                # Plane-appropriate non-core paths: provider groups
                # get a ``/cloudapi/...`` path, tenant groups get a
                # ``/iaas/api/...`` path, so the seeded extras are
                # internally consistent with their group's plane.
                if group_planes[group.group_key] == "provider":
                    extra_prefix = f"/cloudapi/1.0.0/_extra/{group.group_key}"
                    extra_spec_source = "cloudapi"
                else:
                    extra_prefix = f"/iaas/api/_extra/{group.group_key}"
                    extra_spec_source = "iaas"
                for i in range(extra_ops_per_group):
                    extra_op_id = f"GET:{extra_prefix}/{i}"
                    session.add(
                        EndpointDescriptor(
                            tenant_id=tenant_id,
                            product=VCFA_PRODUCT,
                            version=VCFA_VERSION,
                            impl_id=VCFA_IMPL_ID,
                            op_id=extra_op_id,
                            source_kind="ingested",
                            method="GET",
                            path=f"{extra_prefix}/{i}",
                            group_id=group_ids[group.group_key],
                            summary=f"non-core op in {group.group_key}",
                            tags=[f"spec:{extra_spec_source}"],
                            is_enabled=False,
                        ),
                    )
        await session.commit()
    return group_ids


async def _ops_state(
    *,
    tenant_id: uuid.UUID,
) -> dict[str, dict[str, Any]]:
    """Return ``{op_id: {is_enabled, llm_instructions, tags}}`` for every VCFA op."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id == tenant_id,
            EndpointDescriptor.product == VCFA_PRODUCT,
            EndpointDescriptor.version == VCFA_VERSION,
            EndpointDescriptor.impl_id == VCFA_IMPL_ID,
        )
        rows = (await session.execute(stmt)).scalars().all()
    return {
        row.op_id: {
            "is_enabled": row.is_enabled,
            "llm_instructions": row.llm_instructions,
            "tags": list(row.tags or ()),
        }
        for row in rows
    }


async def _group_statuses(*, tenant_id: uuid.UUID) -> dict[str, str]:
    """Return ``{group_key: review_status}`` for every VCFA group."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.tenant_id == tenant_id,
            OperationGroup.product == VCFA_PRODUCT,
            OperationGroup.version == VCFA_VERSION,
            OperationGroup.impl_id == VCFA_IMPL_ID,
        )
        rows = (await session.execute(stmt)).scalars().all()
    return {row.group_key: row.review_status for row in rows}


# ---------------------------------------------------------------------------
# classify_vcfa_op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected",
    [
        # Provider plane families.
        ("GET:/cloudapi/1.0.0/site", "provider-site"),
        ("GET:/cloudapi/1.0.0/orgs", "provider-orgs"),
        ("GET:/cloudapi/1.0.0/orgs/abc-123", "provider-orgs"),
        ("GET:/cloudapi/1.0.0/regions", "provider-regions"),
        ("GET:/cloudapi/1.0.0/regions/def-456", "provider-regions"),
        ("GET:/cloudapi/1.0.0/users", "provider-users"),
        # Tenant plane families.
        ("GET:/iaas/api/about", "tenant-about"),
        ("GET:/iaas/api/projects", "tenant-projects"),
        ("GET:/iaas/api/deployments", "tenant-deployments"),
        ("GET:/iaas/api/deployments/abc-789", "tenant-deployments"),
        ("GET:/iaas/api/blueprints", "tenant-blueprints"),
        # Outside both curated families.
        ("GET:/cloudapi/1.0.0/cells", "none"),
        ("GET:/iaas/api/machines", "none"),
        ("GET:/api/versions", "none"),
        # Non-GET verbs are not in the read core.
        ("POST:/cloudapi/1.0.0/orgs", "none"),
        ("DELETE:/iaas/api/deployments/abc", "none"),
        # Malformed op_ids.
        ("malformed_op_id_no_colon", "none"),
    ],
)
def test_classify_vcfa_op_maps_paths_to_expected_group_keys(
    op_id: str,
    expected: str,
) -> None:
    """Path-prefix classifier returns the canonical ``group_key`` per path."""
    assert classify_vcfa_op(op_id) == expected


# ---------------------------------------------------------------------------
# Module invariants
# ---------------------------------------------------------------------------


def test_vcfa_core_ops_has_eleven_entries_six_provider_five_tenant() -> None:
    """The v0.5 read core is 11 ops total: 6 provider + 5 tenant (per #369 DoD).

    The exact count is contractually fixed in #836's acceptance criteria
    and the parent Initiative #369's definition of done. A drift here
    (e.g. someone adds a 12th op) would silently widen the surface
    beyond the operator-reviewed scope; this assertion catches it.
    """
    provider_count = sum(1 for op in VCFA_CORE_OPS if op.plane == "provider")
    tenant_count = sum(1 for op in VCFA_CORE_OPS if op.plane == "tenant")
    assert len(VCFA_CORE_OPS) == 11, f"expected 11 core ops, got {len(VCFA_CORE_OPS)}"
    assert provider_count == 6, f"expected 6 provider ops, got {provider_count}"
    assert tenant_count == 5, f"expected 5 tenant ops, got {tenant_count}"


def test_every_curated_op_plane_matches_path_classification() -> None:
    """Each :data:`VCFA_CORE_OPS` plane field matches :func:`plane_for_path`.

    A drift between the declared plane and the path-derived plane
    would mean the dispatcher routes an op through the wrong auth
    plane and surfaces HTTP 401 in production. The module-import
    ``_validate_module_invariants()`` already enforces this; this
    test pins it as an explicit acceptance check.
    """
    for op in VCFA_CORE_OPS:
        _, path = op.op_id.split(":", 1)
        derived = plane_for_path(path)
        assert derived == op.plane, (
            f"op {op.op_id!r} declares plane={op.plane!r} but plane_for_path returns {derived!r}"
        )


def test_every_curated_group_has_plane_named_in_when_to_use() -> None:
    """Each group's ``when_to_use`` mentions its plane explicitly.

    Plane awareness in the agent-facing group hint is load-bearing
    for dual-plane routing — the AC requires every enabled group's
    ``when_to_use`` to name its plane. Match is case-insensitive
    and accepts the markdown-bold form (``**provider plane**`` /
    ``**tenant plane**``) the curated strings use.
    """
    for group in VCFA_CORE_GROUPS:
        body = group.when_to_use.lower()
        assert group.plane in body, (
            f"group {group.group_key!r} declares plane={group.plane!r} but "
            f"its when_to_use string does not name the plane: {group.when_to_use!r}"
        )


def test_path_rules_cover_every_curated_group_key() -> None:
    """:data:`VCFA_PATH_RULES` resolves to every :data:`VCFA_CORE_GROUPS` key.

    A new group entry without a matching path rule would leave
    :func:`classify_vcfa_op` returning ``"none"`` for that group's
    ops — silently un-curated.
    """
    rule_group_keys = {group_key for _, group_key in VCFA_PATH_RULES}
    curated_group_keys = {group.group_key for group in VCFA_CORE_GROUPS}
    assert curated_group_keys.issubset(rule_group_keys), (
        f"groups missing path rules: {curated_group_keys - rule_group_keys}"
    )


# ---------------------------------------------------------------------------
# apply_vcfa_core_curation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_curation_enables_groups_and_lands_llm_instructions() -> None:
    """All 8 curated groups end ``enabled``; all 11 core ops carry the curated blob."""
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_vcfa_core_curation(ReviewService(operator), tenant_id=tenant_id)

    statuses = await _group_statuses(tenant_id=tenant_id)
    assert all(s == "enabled" for s in statuses.values()), statuses
    assert set(statuses) == {g.group_key for g in VCFA_CORE_GROUPS}

    state = await _ops_state(tenant_id=tenant_id)
    for op in VCFA_CORE_OPS:
        row = state[op.op_id]
        assert row["is_enabled"] is True, op.op_id
        assert row["llm_instructions"] == op.llm_instructions, op.op_id


@pytest.mark.asyncio
async def test_apply_curation_keeps_non_core_ops_disabled_via_override_path() -> None:
    """Non-core ops in curated groups stay ``is_enabled=False`` after enable_group cascade.

    The audit-log-driven operator-override exclusion (see
    :func:`meho_backplane.operations.ingest._internals.operator_disabled_op_ids`)
    is the substrate that makes per-op exclusivity work inside a
    group-level enable. This test seeds 2 extra non-core ops per
    curated group (16 extras total across 8 groups), runs the
    helper, and asserts only the 11 curated ops flip to
    ``is_enabled=True`` while the 16 non-core ops stay ``False``.
    """
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_vcfa_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    core_op_ids = {op.op_id for op in VCFA_CORE_OPS}
    enabled_count = 0
    disabled_count = 0
    for op_id, row in state.items():
        if op_id in core_op_ids:
            assert row["is_enabled"] is True, (
                f"curated core op {op_id} should be enabled after curation"
            )
            enabled_count += 1
        else:
            assert row["is_enabled"] is False, (
                f"non-core op {op_id} should stay disabled after curation; "
                f"the operator-override path was the safeguard"
            )
            disabled_count += 1
    assert enabled_count == len(VCFA_CORE_OPS) == 11
    assert disabled_count == len(VCFA_CORE_GROUPS) * 2 == 16


@pytest.mark.asyncio
async def test_every_enabled_op_carries_spec_source_tag_naming_its_plane() -> None:
    """Each enabled op's ``tags`` carry the ``spec:<cloudapi|iaas>`` marker.

    The dual-plane ingest writes ``spec:cloudapi`` on provider-plane
    rows (sourced from ``vcf-automation-9.0/cloudapi.yaml``) and
    ``spec:iaas`` on tenant-plane rows (sourced from
    ``vcf-automation-9.0/iaas.yaml``). Operators distinguish the
    two planes via these tags in ``meho connector review`` output;
    this test pins that the curated 11-op core preserves the tag
    after curation runs (curation must not strip the
    spec_source marker).
    """
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_vcfa_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    for op in VCFA_CORE_OPS:
        row = state[op.op_id]
        expected_tag = "spec:iaas" if op.plane == "tenant" else "spec:cloudapi"
        assert expected_tag in row["tags"], (
            f"op {op.op_id!r} (plane={op.plane!r}) should carry tag "
            f"{expected_tag!r}; got tags={row['tags']!r}"
        )


@pytest.mark.asyncio
async def test_apply_curation_writes_expected_audit_rows() -> None:
    """The curation step emits the expected audit-row breakdown.

    Per group: 1 ``meho.connector.edit_group`` row (always) + at
    most 1 ``meho.connector.enable_group`` row (suppressed if the
    group was already enabled). Per non-core op in a curated group:
    1 ``meho.connector.edit_op`` row carrying
    ``is_enabled_set_to=False``. Per core op: 1
    ``meho.connector.edit_op`` row carrying
    ``fields_updated=['llm_instructions']``.

    The audit-row counts are the canonical proof the helper
    actually walked the audit path it claims to walk; a future
    refactor that silently bypasses ``ReviewService`` would fail
    this assertion.
    """
    tenant_id = _TEST_TENANT
    extras_per_group = 1
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=extras_per_group)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_vcfa_core_curation(ReviewService(operator), tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()

    by_path: dict[str, list[AuditLog]] = {}
    for row in rows:
        by_path.setdefault(row.path, []).append(row)

    group_count = len(VCFA_CORE_GROUPS)
    op_count = len(VCFA_CORE_OPS)
    non_core_count = group_count * extras_per_group

    assert len(by_path.get("meho.connector.edit_group", [])) == group_count, (
        f"one edit_group row per curated group; got "
        f"{len(by_path.get('meho.connector.edit_group', []))}"
    )
    assert len(by_path.get("meho.connector.enable_group", [])) == group_count, (
        f"one enable_group row per curated group (none were enabled "
        f"before this run); got {len(by_path.get('meho.connector.enable_group', []))}"
    )
    edit_op_rows = by_path.get("meho.connector.edit_op", [])
    assert len(edit_op_rows) == non_core_count + op_count, (
        f"expected {non_core_count + op_count} edit_op rows "
        f"({non_core_count} non-core disables + {op_count} curated "
        f"llm_instructions); got {len(edit_op_rows)}"
    )
    disable_rows = [r for r in edit_op_rows if r.payload.get("is_enabled_set_to") is False]
    assert len(disable_rows) == non_core_count, (
        f"expected {non_core_count} disable rows; got {len(disable_rows)}"
    )
    llm_rows = [r for r in edit_op_rows if r.payload.get("fields_updated") == ["llm_instructions"]]
    assert len(llm_rows) == op_count, (
        f"expected {op_count} llm_instructions rows; got {len(llm_rows)}"
    )


@pytest.mark.asyncio
async def test_apply_curation_is_safe_to_rerun() -> None:
    """Re-running the helper on an already-curated connector keeps state correct.

    The audit layer isn't idempotent (each ``edit_group`` /
    ``edit_op`` writes a fresh row even on no-op values), but the
    end-state assertions hold: 11 ops enabled with the curated
    blob; non-core ops still disabled; all curated groups still
    enabled.
    """
    tenant_id = _TEST_TENANT
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=1)
    operator = _make_operator(tenant_id=tenant_id)

    await apply_vcfa_core_curation(ReviewService(operator), tenant_id=tenant_id)
    # Second pass — the substrate must not regress any state.
    await apply_vcfa_core_curation(ReviewService(operator), tenant_id=tenant_id)

    state = await _ops_state(tenant_id=tenant_id)
    core_op_ids = {op.op_id for op in VCFA_CORE_OPS}
    for op_id, row in state.items():
        if op_id in core_op_ids:
            assert row["is_enabled"] is True, op_id
        else:
            assert row["is_enabled"] is False, op_id
    statuses = await _group_statuses(tenant_id=tenant_id)
    assert all(s == "enabled" for s in statuses.values()), statuses
