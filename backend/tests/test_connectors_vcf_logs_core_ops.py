# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the vRLI read-only v0.5 core-ops curation.

Covers :mod:`meho_backplane.connectors.vcf_logs.core_ops`:

* :func:`classify_vrli_op` — path-prefix classifier rules. Validates
  that ``GET``-method ops under each curated family slot into the
  expected group and that non-``GET`` methods (write ops) classify
  as ``"none"`` even when their path matches a curated family.
* :data:`VRLI_CORE_OPS` integrity — every entry classifies to its
  declared ``group_key``, every entry's ``op_id`` begins with
  ``GET:`` (read-only invariant), and every entry's
  ``llm_instructions`` blob carries the canonical three keys with
  non-empty string values.
* :func:`apply_vrli_core_curation` — the operator-review-time
  substrate call that flips ``review_status='enabled'`` on the 5
  curated groups, lands ``llm_instructions`` on the 7 curated ops,
  and explicitly disables non-core ops via the audit-log-driven
  operator-override exclusion. The load-bearing assertion is "7
  ops dispatchable, every other op in curated groups stays
  ``is_enabled=False``".

Test harness mirrors :mod:`tests.test_connectors_harbor_core_ops`
and :mod:`tests.test_connectors_nsx_core_ops`: SQLite via the
autouse ``_default_database_url`` fixture, hand-rolled operator +
connector seeding, audit-row counting from the chassis
``audit_log`` table.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vcf_logs import (
    VRLI_CORE_GROUPS,
    VRLI_CORE_OPS,
    VRLI_IMPL_ID,
    VRLI_PRODUCT,
    VRLI_VERSION,
    apply_vrli_core_curation,
    classify_vrli_op,
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
        sub=f"vrli-core-ops-test-{uuid.uuid4()}",
        name="vRLI Core Ops Test",
        email=None,
        raw_jwt=_FAKE_JWT,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# classify_vrli_op — path classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id,expected_group",
    [
        ("GET:/api/v2/version", "vrli-system"),
        ("GET:/api/v2/fields", "vrli-system"),
        ("GET:/api/v2/events/{constraints}", "vrli-events"),
        ("GET:/api/v2/aggregated-events/{constraints}", "vrli-events"),
        ("GET:/api/v2/hosts", "vrli-inventory"),
        ("GET:/api/v2/content/contentpack/list", "vrli-content"),
        ("GET:/api/v2/alerts", "vrli-alerts"),
        # Non-curated paths → "none"
        ("GET:/api/v2/sessions", "none"),
        ("GET:/api/v2/sessions/current", "none"),
        ("GET:/api/v2/notification/webhook", "none"),
        # Write ops on curated paths → "none" (read-only invariant)
        ("POST:/api/v2/alerts", "none"),
        ("DELETE:/api/v2/alerts/{alertId}", "none"),
        ("POST:/api/v2/events/ingest/{uuid}", "none"),
        # Malformed op_id → "none"
        ("bad-op-id-no-colon", "none"),
    ],
    ids=str,
)
def test_classify_vrli_op_returns_correct_group(op_id: str, expected_group: str) -> None:
    """classify_vrli_op returns the curated group_key for all 7 core ops."""
    assert classify_vrli_op(op_id) == expected_group


def test_classify_vrli_op_all_core_ops_are_classified() -> None:
    """Every op in VRLI_CORE_OPS classifies to its declared group_key."""
    for op in VRLI_CORE_OPS:
        group = classify_vrli_op(op.op_id)
        assert group != "none", (
            f"VRLI_CORE_OPS entry {op.op_id!r} classified as 'none' — "
            "check VRLI_PATH_RULES ordering"
        )
        assert group == op.group_key, (
            f"VRLI_CORE_OPS entry {op.op_id!r} classified as {group!r} "
            f"but declared group_key={op.group_key!r}"
        )


def test_classify_vrli_op_rejects_write_methods_on_curated_paths() -> None:
    """Non-GET methods under curated paths classify as 'none'.

    Curated read-only invariant: even if the path matches a curated
    family (e.g. ``/api/v2/alerts``), a non-GET op never lands under
    the group. v0.5 stays read-only; write ops keep
    ``is_enabled=False``.
    """
    write_method_op_ids = [
        "POST:/api/v2/alerts",
        "PUT:/api/v2/alerts/{alertId}",
        "DELETE:/api/v2/alerts/{alertId}",
        "POST:/api/v2/content/contentpack/import",
        "DELETE:/api/v2/hosts/{hostId}",
    ]
    for op_id in write_method_op_ids:
        assert classify_vrli_op(op_id) == "none", (
            f"non-GET op {op_id!r} should classify as 'none' "
            "(read-only invariant) but matched a curated group"
        )


# ---------------------------------------------------------------------------
# VRLI_CORE_OPS structural invariants
# ---------------------------------------------------------------------------


def test_vrli_core_ops_has_seven_entries() -> None:
    """The read-only v0.5 core enables exactly 7 ops per #369 DoD."""
    assert len(VRLI_CORE_OPS) == 7, (
        f"VRLI_CORE_OPS should carry 7 entries (v0.5 read core); got {len(VRLI_CORE_OPS)}"
    )


def test_vrli_core_ops_all_are_read_only_get() -> None:
    """Every op in VRLI_CORE_OPS is a GET (no POST/PUT/DELETE)."""
    for op in VRLI_CORE_OPS:
        assert op.op_id.startswith("GET:"), (
            f"VRLI_CORE_OPS entry {op.op_id!r} is not a GET; v0.5 core is read-only"
        )


def test_vrli_core_ops_llm_instructions_all_populated() -> None:
    """Every op in VRLI_CORE_OPS has non-empty llm_instructions with canonical keys."""
    required_keys = {"when_to_call", "output_shape", "next_step"}
    for op in VRLI_CORE_OPS:
        assert op.llm_instructions, f"op {op.op_id!r} has empty llm_instructions"
        missing = required_keys - set(op.llm_instructions)
        assert not missing, f"op {op.op_id!r} llm_instructions missing keys: {missing}"
        for key in required_keys:
            val = op.llm_instructions[key]
            assert isinstance(val, str) and val.strip(), (
                f"op {op.op_id!r} llm_instructions[{key!r}] must be a non-empty string"
            )


def test_vrli_core_groups_when_to_use_all_populated() -> None:
    """Every group in VRLI_CORE_GROUPS has a non-empty when_to_use hint."""
    for group in VRLI_CORE_GROUPS:
        assert group.when_to_use and group.when_to_use.strip(), (
            f"group {group.group_key!r} has empty when_to_use"
        )
        assert group.name and group.name.strip(), f"group {group.group_key!r} has empty name"


def test_vrli_core_groups_event_query_op_is_jsonflux_handle_shaped() -> None:
    """The event-query op's llm_instructions advertise the JSONFlux handle shape.

    Per #834 acceptance criteria, ``vrli.event.query`` returns a
    JSONFlux result handle (event result sets are large by nature).
    The agent learns this from the op's ``output_shape`` /
    ``next_step`` text — assert the operator-reviewed copy mentions
    the handle so the LLM knows to drill via
    ``result_describe`` / ``result_query`` rather than expecting an
    inline payload.
    """
    event_query_op = next(
        op for op in VRLI_CORE_OPS if op.op_id == "GET:/api/v2/events/{constraints}"
    )
    output_shape = str(event_query_op.llm_instructions["output_shape"]).lower()
    next_step = str(event_query_op.llm_instructions["next_step"]).lower()
    combined = f"{output_shape} {next_step}"
    assert "handle" in combined or "jsonflux" in combined, (
        f"vrli.event.query llm_instructions should mention the JSONFlux handle "
        f"so the agent reads results via result_describe / result_query; "
        f"got output_shape={event_query_op.llm_instructions['output_shape']!r}, "
        f"next_step={event_query_op.llm_instructions['next_step']!r}"
    )


def test_vrli_core_groups_cover_every_core_op_group_key() -> None:
    """Every op.group_key appears in VRLI_CORE_GROUPS."""
    group_keys = {g.group_key for g in VRLI_CORE_GROUPS}
    for op in VRLI_CORE_OPS:
        assert op.group_key in group_keys, (
            f"op {op.op_id!r} group_key={op.group_key!r} has no matching VRLI_CORE_GROUPS entry"
        )


# ---------------------------------------------------------------------------
# apply_vrli_core_curation — substrate integration (SQLite)
# ---------------------------------------------------------------------------


async def _seed_curated_groups_and_ops(
    *,
    tenant_id: uuid.UUID,
    extra_ops_per_group: int = 0,
) -> dict[str, uuid.UUID]:
    """Seed every :data:`VRLI_CORE_GROUPS` entry + its child ops.

    Inserts one :class:`OperationGroup` per curated group_key
    (``review_status='staged'``). For each :data:`VRLI_CORE_OPS`
    entry, inserts the curated op (``is_enabled=False`` to match
    the G0.7 ingest default for ``source_kind='ingested'`` rows).

    When *extra_ops_per_group* > 0, also seeds *N* non-curated ops
    per curated group with deterministic op_ids so the test can
    assert :func:`apply_vrli_core_curation` correctly disables
    them.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, uuid.UUID] = {}

    async with sessionmaker() as session:
        for group in VRLI_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=tenant_id,
                product=VRLI_PRODUCT,
                version=VRLI_VERSION,
                impl_id=VRLI_IMPL_ID,
                group_key=group.group_key,
                name=f"Placeholder name for {group.group_key}",
                when_to_use="Placeholder when_to_use.",
                review_status="staged",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in VRLI_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=VRLI_PRODUCT,
                    version=VRLI_VERSION,
                    impl_id=VRLI_IMPL_ID,
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
                    tags=["spec:vcf-logs-9.0/api-v2.yaml"],
                )
            )

        for group in VRLI_CORE_GROUPS:
            for i in range(extra_ops_per_group):
                extra_op_id = f"GET:/canary-extra/{group.group_key}/{i}"
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=VRLI_PRODUCT,
                        version=VRLI_VERSION,
                        impl_id=VRLI_IMPL_ID,
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


async def test_apply_vrli_core_curation_enables_exactly_7_ops() -> None:
    """apply_vrli_core_curation enables exactly the 7 core ops."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrli_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == VRLI_PRODUCT,
                EndpointDescriptor.version == VRLI_VERSION,
                EndpointDescriptor.impl_id == VRLI_IMPL_ID,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        enabled_ops = result.scalars().all()

    enabled_op_ids = {op.op_id for op in enabled_ops}
    expected_op_ids = {op.op_id for op in VRLI_CORE_OPS}
    assert enabled_op_ids == expected_op_ids, (
        f"enabled ops don't match VRLI_CORE_OPS; "
        f"extra={enabled_op_ids - expected_op_ids!r}, "
        f"missing={expected_op_ids - enabled_op_ids!r}"
    )


async def test_apply_vrli_core_curation_disables_non_core_ops_in_curated_groups() -> None:
    """Extra ops seeded in curated groups stay is_enabled=False after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id, extra_ops_per_group=2)

    review_service = ReviewService(operator=operator)
    await apply_vrli_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == VRLI_PRODUCT,
                EndpointDescriptor.version == VRLI_VERSION,
                EndpointDescriptor.impl_id == VRLI_IMPL_ID,
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


async def test_apply_vrli_core_curation_sets_llm_instructions_on_all_core_ops() -> None:
    """llm_instructions is populated on all 7 core ops after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrli_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    expected_op_ids = {op.op_id for op in VRLI_CORE_OPS}
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == VRLI_PRODUCT,
                EndpointDescriptor.op_id.in_(list(expected_op_ids)),
            )
        )
        curated_ops = result.scalars().all()

    assert len(curated_ops) == len(VRLI_CORE_OPS)
    for op in curated_ops:
        assert op.llm_instructions is not None and op.llm_instructions, (
            f"llm_instructions not set on core op {op.op_id!r} after curation"
        )


async def test_apply_vrli_core_curation_enables_all_5_curated_groups() -> None:
    """All 5 curated groups land review_status='enabled' after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrli_core_curation(review_service, tenant_id=tenant_id)

    expected_group_keys = {g.group_key for g in VRLI_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == VRLI_PRODUCT,
                OperationGroup.group_key.in_(list(expected_group_keys)),
            )
        )
        groups = result.scalars().all()

    assert len(groups) == len(VRLI_CORE_GROUPS)
    for group in groups:
        assert group.review_status == "enabled", (
            f"group {group.group_key!r} should be 'enabled' after curation; "
            f"got review_status={group.review_status!r}"
        )


async def test_apply_vrli_core_curation_writes_operator_reviewed_when_to_use() -> None:
    """Curated groups carry the operator-reviewed when_to_use after curation."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrli_core_curation(review_service, tenant_id=tenant_id)

    expected: dict[str, Any] = {g.group_key: g.when_to_use for g in VRLI_CORE_GROUPS}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(OperationGroup).where(
                OperationGroup.tenant_id == tenant_id,
                OperationGroup.product == VRLI_PRODUCT,
            )
        )
        groups = result.scalars().all()

    for group in groups:
        if group.group_key in expected:
            assert group.when_to_use == expected[group.group_key], (
                f"group {group.group_key!r} when_to_use mismatch after curation"
            )
            # Reviewed when_to_use must not match the placeholder seeded
            # by _seed_curated_groups_and_ops — the acceptance criterion
            # is "non-placeholder reviewed text".
            assert "Placeholder" not in group.when_to_use, (
                f"group {group.group_key!r} still carries placeholder when_to_use "
                "after curation — apply_vrli_core_curation did not write the "
                "reviewed text"
            )


async def test_apply_vrli_core_curation_emits_audit_rows() -> None:
    """apply_vrli_core_curation writes at least one audit row per curated group."""
    tenant_id = uuid.uuid4()
    operator = _make_operator(tenant_id=tenant_id)
    await _seed_curated_groups_and_ops(tenant_id=tenant_id)

    review_service = ReviewService(operator=operator)
    await apply_vrli_core_curation(review_service, tenant_id=tenant_id)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
            )
        )
        audit_rows = result.scalars().all()

    assert len(audit_rows) >= len(VRLI_CORE_GROUPS), (
        f"expected ≥{len(VRLI_CORE_GROUPS)} audit rows from curation; got {len(audit_rows)}"
    )
