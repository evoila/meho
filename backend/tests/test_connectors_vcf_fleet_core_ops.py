# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the VCF Fleet read-only v0.5 core-ops curation.

Covers :mod:`meho_backplane.connectors.vcf_fleet.core_ops`:

* :func:`classify_fleet_op` — path classifier rules for the Fleet
  (vRSLCM-derived) LCM REST surface. Validates that the most-specific
  prefix wins for nested datacenter/vcenter and environment/product
  paths, and that the request surface (under ``/lcm/request/``) is
  classified independently of the LCM-ops surface (under
  ``/lcm/lcops/``).
* Read-only invariants — asserts that every :data:`FLEET_CORE_OPS`
  entry is a ``GET`` (the v0.5 core is read-only), and that the
  curated data carries all the metadata the agent reads
  (``when_to_call`` / ``output_shape`` / ``next_step`` per op,
  ``name`` + ``when_to_use`` per group).
* :func:`apply_fleet_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 6
  curated groups, lands ``llm_instructions`` on the 8 curated ops,
  and explicitly disables non-core ops via the audit-log-driven
  operator-override exclusion. The load-bearing assertion is "8 ops
  dispatchable, every other op in curated groups stays
  ``is_enabled=False``".

Test harness mirrors :mod:`tests.test_connectors_harbor_core_ops`:
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
from meho_backplane.connectors.vcf_fleet import (
    FLEET_CORE_GROUPS,
    FLEET_CORE_OPS,
    FLEET_IMPL_ID,
    FLEET_PRODUCT,
    FLEET_VERSION,
    apply_fleet_core_curation,
    classify_fleet_op,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env vars Operator construction reads transitively."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FAKE_JWT = "header.payload.signature"


def _make_operator(*, tenant_id: uuid.UUID) -> Operator:
    """Build a tenant-admin :class:`Operator` for the curation tests."""
    return Operator(
        sub=f"fleet-core-ops-test-{uuid.uuid4()}",
        name="Fleet Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# classify_fleet_op — path classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected_group",
    [
        ("GET:/lcm/lcops/api/v2/about", "fleet-about"),
        ("GET:/lcm/lcops/api/v2/datacenters", "fleet-datacenter"),
        (
            "GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters",
            "fleet-vcenter",
        ),
        ("GET:/lcm/lcops/api/v2/environments", "fleet-environment"),
        (
            "GET:/lcm/lcops/api/v2/environments/{environmentId}",
            "fleet-environment",
        ),
        (
            "GET:/lcm/lcops/api/v2/environments/{environmentId}/products",
            "fleet-product",
        ),
        ("GET:/lcm/request/api/v2/requests", "fleet-request"),
        ("GET:/lcm/request/api/v2/requests/{requestId}", "fleet-request"),
        # Non-curated paths → "none"
        ("GET:/lcm/locker/api/v2/passwords", "none"),
        ("GET:/lcm/authzn/api/v2/users", "none"),
        # Non-GET → "none" (v0.5 core is read-only)
        ("POST:/lcm/lcops/api/v2/environments", "none"),
        ("DELETE:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}", "none"),
        # Malformed op_id → "none"
        ("bad-op-id-no-colon", "none"),
    ],
    ids=str,
)
def test_classify_fleet_op_returns_correct_group(op_id: str, expected_group: str) -> None:
    """classify_fleet_op returns the curated group_key for all 8 core ops."""
    assert classify_fleet_op(op_id) == expected_group


def test_classify_fleet_op_vcenters_precedes_datacenters_in_hierarchy() -> None:
    """vCenter paths classify as fleet-vcenter, not fleet-datacenter.

    The vCenter list path
    ``/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters`` starts
    with the datacenter prefix; rule ordering must keep the deeper
    prefix listed first so it wins the ``startswith`` race.
    """
    vcenters = "GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters"
    assert classify_fleet_op(vcenters) == "fleet-vcenter"


def test_classify_fleet_op_products_precedes_environments_in_hierarchy() -> None:
    """Product paths classify as fleet-product, not fleet-environment.

    The products path under an environment starts with the
    environments prefix; rule ordering must keep the deeper prefix
    listed first.
    """
    products = "GET:/lcm/lcops/api/v2/environments/{environmentId}/products"
    assert classify_fleet_op(products) == "fleet-product"


def test_classify_fleet_op_all_core_ops_are_classified() -> None:
    """Every op in FLEET_CORE_OPS classifies to a non-'none' group."""
    for op in FLEET_CORE_OPS:
        group = classify_fleet_op(op.op_id)
        assert group != "none", (
            f"FLEET_CORE_OPS entry {op.op_id!r} classified as 'none' — "
            "check FLEET_PATH_RULES ordering"
        )
        assert group == op.group_key, (
            f"FLEET_CORE_OPS entry {op.op_id!r} classified as {group!r} "
            f"but declared group_key={op.group_key!r}"
        )


# ---------------------------------------------------------------------------
# Read-only + metadata invariants
# ---------------------------------------------------------------------------


def test_fleet_core_ops_has_exactly_eight_ops() -> None:
    """FLEET_CORE_OPS carries exactly 8 entries — the v0.5 read core per #835."""
    assert len(FLEET_CORE_OPS) == 8, (
        f"v0.5 Fleet read core must have exactly 8 ops; "
        f"got {len(FLEET_CORE_OPS)}: {[op.op_id for op in FLEET_CORE_OPS]}"
    )


def test_fleet_core_groups_has_exactly_six_groups() -> None:
    """FLEET_CORE_GROUPS carries exactly 6 entries spanning the v0.5 core.

    The 8 ops distribute across 6 groups: fleet-about, fleet-datacenter,
    fleet-vcenter, fleet-environment (carries env.list + env.get),
    fleet-product, fleet-request (carries req.list + req.get).
    """
    assert len(FLEET_CORE_GROUPS) == 6, (
        f"v0.5 Fleet read core must have exactly 6 groups; "
        f"got {len(FLEET_CORE_GROUPS)}: "
        f"{[g.group_key for g in FLEET_CORE_GROUPS]}"
    )


def test_fleet_core_ops_are_all_get() -> None:
    """Every op in FLEET_CORE_OPS is a GET — the v0.5 core is read-only."""
    for op in FLEET_CORE_OPS:
        assert op.op_id.startswith("GET:"), (
            f"FLEET_CORE_OPS entry {op.op_id!r} is not a GET; "
            "v0.5 Fleet core is read-only — write ops stay staged"
        )


def test_fleet_core_ops_op_ids_are_unique() -> None:
    """No duplicate op_ids in FLEET_CORE_OPS."""
    op_ids = [op.op_id for op in FLEET_CORE_OPS]
    assert len(op_ids) == len(set(op_ids)), f"duplicate op_ids in FLEET_CORE_OPS: {op_ids}"


def test_fleet_core_groups_group_keys_are_unique() -> None:
    """No duplicate group_keys in FLEET_CORE_GROUPS."""
    keys = [g.group_key for g in FLEET_CORE_GROUPS]
    assert len(keys) == len(set(keys)), f"duplicate group_keys in FLEET_CORE_GROUPS: {keys}"


def test_fleet_core_ops_group_keys_resolve_to_a_known_group() -> None:
    """Every op's group_key matches an entry in FLEET_CORE_GROUPS."""
    known_keys = {g.group_key for g in FLEET_CORE_GROUPS}
    for op in FLEET_CORE_OPS:
        assert op.group_key in known_keys, (
            f"FLEET_CORE_OPS entry {op.op_id!r} references unknown "
            f"group_key={op.group_key!r}; known: {sorted(known_keys)}"
        )


def test_fleet_core_ops_llm_instructions_all_populated() -> None:
    """Every op in FLEET_CORE_OPS has non-empty llm_instructions with canonical keys."""
    required_keys = {"when_to_call", "output_shape", "next_step"}
    for op in FLEET_CORE_OPS:
        assert op.llm_instructions, f"op {op.op_id!r} has empty llm_instructions"
        missing = required_keys - set(op.llm_instructions)
        assert not missing, f"op {op.op_id!r} llm_instructions missing keys: {missing}"
        for key in required_keys:
            val = op.llm_instructions[key]
            assert isinstance(val, str) and val.strip(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] must be a non-empty string"
            )


def test_fleet_core_groups_when_to_use_all_populated() -> None:
    """Every group in FLEET_CORE_GROUPS has non-empty when_to_use + name."""
    for group in FLEET_CORE_GROUPS:
        assert group.when_to_use and group.when_to_use.strip(), (
            f"group {group.group_key!r} has empty when_to_use"
        )
        assert group.name and group.name.strip(), f"group {group.group_key!r} has empty name"


def test_fleet_core_groups_when_to_use_is_not_placeholder() -> None:
    """No group's when_to_use is a placeholder/auto-derived blurb.

    Asserts the hints are reviewed prose, not the
    ``"Operations grouped under '<key>' for <product>"`` auto-derive
    template the LLM-grouping pass falls back to when an operator
    skipped curation.
    """
    for group in FLEET_CORE_GROUPS:
        assert "Operations grouped under" not in group.when_to_use, (
            f"group {group.group_key!r} carries the auto-derive "
            f"placeholder when_to_use — curation incomplete"
        )
        assert "Placeholder" not in group.when_to_use, (
            f"group {group.group_key!r} carries a 'Placeholder' sentinel in when_to_use"
        )


def test_fleet_about_llm_instructions_flag_the_9_0_regression() -> None:
    """fleet.about's llm_instructions must warn the agent about the 9.0 HTTP 500.

    The wrapper-verified known issue is that ``/lcm/lcops/api/v2/about``
    returns HTTP 500 in VCF 9.0 builds — the connector's probe falls
    back to ``/lcm/lcops/api/v2/datacenters``, and the agent needs the
    same fallback guidance in its reasoning context.
    """
    about_ops = [op for op in FLEET_CORE_OPS if op.op_id.endswith(":/lcm/lcops/api/v2/about")]
    assert len(about_ops) == 1, (
        f"expected exactly 1 fleet.about op; got {[op.op_id for op in about_ops]}"
    )
    about_op = about_ops[0]
    combined = " ".join(str(v) for v in about_op.llm_instructions.values()).lower()
    assert "500" in combined, (
        f"fleet.about llm_instructions must mention the HTTP 500 regression in VCF 9.0; "
        f"got: {about_op.llm_instructions!r}"
    )
    assert "datacenter" in combined, (
        f"fleet.about llm_instructions must point the agent at "
        f"fleet.datacenter.list as the fallback; got: {about_op.llm_instructions!r}"
    )


# ---------------------------------------------------------------------------
# apply_fleet_core_curation — substrate integration (SQLite)
# ---------------------------------------------------------------------------


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`FLEET_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``). For each :data:`FLEET_CORE_OPS`
    entry, inserts the curated op (``is_enabled=False`` to match the
    G0.7 ingest default for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops
    per curated group with deterministic op_ids so the test can
    assert :func:`apply_fleet_core_curation` correctly disables them.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}

    async with sessionmaker() as session:
        for group in FLEET_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=tenant_id,
                product=FLEET_PRODUCT,
                version=FLEET_VERSION,
                impl_id=FLEET_IMPL_ID,
                group_key=group.group_key,
                name=f"Placeholder name for {group.group_key}",
                when_to_use="Placeholder when_to_use.",
                review_status="staged",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in FLEET_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=FLEET_PRODUCT,
                    version=FLEET_VERSION,
                    impl_id=FLEET_IMPL_ID,
                    op_id=op.op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    handler_ref=None,
                    group_id=group_ids[op.group_key],
                    summary=f"Placeholder summary for {op.op_id}.",
                    description=f"Placeholder description for {op.op_id}.",
                    parameter_schema={"type": "object", "properties": {}},
                    response_schema={"type": "object"},
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=False,
                    tags=["spec:vcf-fleet-9.0/lcm-api.yaml"],
                )
            )

        for group in FLEET_CORE_GROUPS:
            for i in range(extra_ops_per_group):
                extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=FLEET_PRODUCT,
                        version=FLEET_VERSION,
                        impl_id=FLEET_IMPL_ID,
                        op_id=extra_op_id,
                        source_kind="ingested",
                        method="GET",
                        path=f"/canary-extra/{group.group_key}/{i}",
                        handler_ref=None,
                        group_id=group_ids[group.group_key],
                        summary="Extra non-curated op.",
                        description="Extra non-curated op.",
                        parameter_schema={"type": "object", "properties": {}},
                        response_schema={"type": "object"},
                        llm_instructions=None,
                        safety_level="safe",
                        requires_approval=False,
                        is_enabled=False,
                        tags=[],
                    )
                )

        await session.commit()

    return group_ids


async def test_apply_fleet_core_curation_enables_exactly_8_ops() -> None:
    """apply_fleet_core_curation enables exactly the 8 core ops."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_fleet_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == FLEET_PRODUCT,
                EndpointDescriptor.version == FLEET_VERSION,
                EndpointDescriptor.impl_id == FLEET_IMPL_ID,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        enabled_ops = result.scalars().all()

    enabled_op_ids = {op.op_id for op in enabled_ops}
    expected_op_ids = {op.op_id for op in FLEET_CORE_OPS}
    assert enabled_op_ids == expected_op_ids, (
        f"enabled ops don't match FLEET_CORE_OPS; "
        f"extra={enabled_op_ids - expected_op_ids!r}, "
        f"missing={expected_op_ids - enabled_op_ids!r}"
    )


async def test_apply_fleet_core_curation_disables_non_core_ops_in_curated_groups() -> None:
    """Extra ops seeded in curated groups stay is_enabled=False after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)

    review_service = ReviewService(operator=operator)
    await apply_fleet_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == FLEET_PRODUCT,
                EndpointDescriptor.version == FLEET_VERSION,
                EndpointDescriptor.impl_id == FLEET_IMPL_ID,
                EndpointDescriptor.op_id.like("GET:/canary-extra/%"),
            )
        )
        extra_ops = result.scalars().all()

    assert extra_ops, "expected extra non-curated ops to exist"
    for op in extra_ops:
        assert op.is_enabled is False, (
            f"non-core op {op.op_id!r} should be disabled after curation; "
            f"got is_enabled={op.is_enabled!r}"
        )


async def test_apply_fleet_core_curation_sets_llm_instructions_on_all_core_ops() -> None:
    """llm_instructions is populated on all 8 core ops after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_fleet_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    expected_op_ids = {op.op_id for op in FLEET_CORE_OPS}
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == FLEET_PRODUCT,
                EndpointDescriptor.op_id.in_(list(expected_op_ids)),
            )
        )
        curated_ops = result.scalars().all()

    assert len(curated_ops) == len(FLEET_CORE_OPS)
    for op in curated_ops:
        assert op.llm_instructions is not None and op.llm_instructions, (
            f"llm_instructions not set on core op {op.op_id!r} after curation"
        )


async def test_apply_fleet_core_curation_enables_all_6_curated_groups() -> None:
    """All 6 curated groups land review_status='enabled' after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_fleet_core_curation(review_service, tenant_id=tenant_id)

    expected_group_keys = {g.group_key for g in FLEET_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == FLEET_PRODUCT,
                OperationGroup.group_key.in_(list(expected_group_keys)),
            )
        )
        groups = result.scalars().all()

    assert len(groups) == len(FLEET_CORE_GROUPS)
    for group in groups:
        assert group.review_status == "enabled", (
            f"group {group.group_key!r} should be 'enabled' after curation; "
            f"got review_status={group.review_status!r}"
        )


async def test_apply_fleet_core_curation_writes_operator_reviewed_when_to_use() -> None:
    """Curated groups carry the operator-reviewed when_to_use after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_fleet_core_curation(review_service, tenant_id=tenant_id)

    expected: dict[str, Any] = {g.group_key: g.when_to_use for g in FLEET_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == FLEET_PRODUCT,
            )
        )
        groups = result.scalars().all()

    for group in groups:
        if group.group_key in expected:
            assert group.when_to_use == expected[group.group_key], (
                f"group {group.group_key!r} when_to_use mismatch after curation"
            )


async def test_apply_fleet_core_curation_emits_audit_rows() -> None:
    """apply_fleet_core_curation writes at least one audit row per curated group."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_fleet_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
            )
        )
        audit_rows = result.scalars().all()

    assert len(audit_rows) >= len(FLEET_CORE_GROUPS), (
        f"expected ≥{len(FLEET_CORE_GROUPS)} audit rows from curation; got {len(audit_rows)}"
    )
