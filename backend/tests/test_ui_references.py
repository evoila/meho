# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit + DB tests for the shared drawer reference resolver (internal#236).

:func:`meho_backplane.ui.references.resolve_audit_references` is the shared
helper both console detail drawers use to turn an ``audit_log`` row's
reference GUIDs into named, linked substance. These tests exercise it
directly (no HTTP layer): the pure status / relative-time mappers, the
happy-path resolution against every store, and the graceful-degradation
fallbacks (missing / cross-tenant / nameless references) that must never
raise and must keep the raw id reachable.

Leak hygiene: every fixture uses synthetic tenant ids, target names,
operator subs, and op ids -- no real lab hostnames / realms / principals.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    RunbookRun,
    Target,
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.references import (
    humanize_relative,
    resolve_audit_references,
    resolve_status,
)

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_BASE = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide the mandatory chassis settings so ``get_settings`` constructs.

    The conftest supplies ``DATABASE_URL`` + schema; the Settings model still
    requires the keycloak / vault / backplane fields (no defaults). The DB
    engine reads them through ``get_settings`` when the resolver acquires a
    session.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Pure mappers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "label", "tone"),
    [
        (200, "OK", "success"),
        (202, "Awaiting approval", "warning"),
        (401, "Denied", "error"),
        (403, "Denied", "error"),
        (404, "Client error", "error"),
        (500, "Server error", "error"),
        # A 3xx redirect follows the audit substrate's partition (< 400 is OK).
        (302, "OK", "success"),
        # A 1xx informational code is the neutral fallthrough.
        (100, "HTTP 100", "neutral"),
    ],
)
def test_resolve_status_mapping(code: int, label: str, tone: str) -> None:
    """Each status band maps to an operator-facing label + DaisyUI tone."""
    status = resolve_status(code)
    assert status.code == code
    assert status.label == label
    assert status.tone == tone


def test_humanize_relative_buckets() -> None:
    """Coarse "X ago" buckets; naive + aware instants both resolve."""
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    assert humanize_relative(now - timedelta(seconds=5), now=now) == "just now"
    assert humanize_relative(now - timedelta(minutes=3), now=now) == "3 min ago"
    assert humanize_relative(now - timedelta(hours=2), now=now) == "2 h ago"
    assert humanize_relative(now - timedelta(days=4), now=now) == "4 d ago"
    # Older than a week collapses to the absolute date.
    assert humanize_relative(now - timedelta(days=40), now=now) == "2026-04-04"
    # A naive ``occurred_at`` (SQLite reads back tz-naive) is treated as UTC
    # rather than raising on the aware-minus-naive subtraction.
    naive = datetime(2026, 5, 14, 11, 58, 0)
    assert humanize_relative(naive, now=now) == "2 min ago"
    # Future timestamp (clock skew) collapses to "just now", never negative.
    assert humanize_relative(now + timedelta(minutes=5), now=now) == "just now"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_target(*, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    target_id = uuid.uuid4()

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                Target(
                    id=target_id,
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product="synthetic-product",
                    host=f"{name}.example.test",
                )
            )

    asyncio.run(_do())
    return target_id


def _seed_operation(
    *, op_id: str, summary: str, group_name: str, tenant_id: uuid.UUID | None = None
) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            group_id = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product="synthetic-product",
                    version="1.0",
                    impl_id="synthetic-impl",
                    group_key="grp",
                    name=group_name,
                    when_to_use="when testing",
                )
            )
            session.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    product="synthetic-product",
                    version="1.0",
                    impl_id="synthetic-impl",
                    op_id=op_id,
                    source_kind="typed",
                    summary=summary,
                    group_id=group_id,
                )
            )

    asyncio.run(_do())


def _seed_run(*, tenant_id: uuid.UUID, template: str, state: str) -> uuid.UUID:
    run_id = uuid.uuid4()

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                RunbookRun(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    template_slug=template,
                    template_version=1,
                    assigned_to="op-synthetic",
                    target="synthetic-target",
                    started_by="op-synthetic",
                    state=state,
                )
            )

    asyncio.run(_do())
    return run_id


def _seed_audit(
    *,
    tenant_id: uuid.UUID | None,
    op_id: str = "acme.vm.list",
    operator_sub: str = "sub-1234",
    status_code: int = 200,
    payload_extra: dict[str, Any] | None = None,
    target_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    parent_audit_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    actor_sub: str | None = None,
    work_ref: str | None = None,
    second: int = 0,
) -> AuditLog:
    row_id = uuid.uuid4()
    payload: dict[str, Any] = {"op_id": op_id}
    if payload_extra:
        payload.update(payload_extra)

    async def _do() -> AuditLog:
        async with get_sessionmaker()() as session, session.begin():
            row = AuditLog(
                id=row_id,
                occurred_at=_BASE + timedelta(seconds=second),
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                target_id=target_id,
                run_id=run_id,
                parent_audit_id=parent_audit_id,
                agent_session_id=agent_session_id,
                actor_sub=actor_sub,
                work_ref=work_ref,
                method="POST",
                path="/mcp",
                status_code=status_code,
                request_id=uuid.uuid4(),
                duration_ms=Decimal("1.0"),
                payload=payload,
            )
            session.add(row)
        # Re-fetch detached so the resolver reads a plain row (no lazy session).
        async with get_sessionmaker()() as session:
            fetched = await session.get(AuditLog, row_id)
            assert fetched is not None
            return fetched

    return asyncio.run(_do())


def _resolve(row: AuditLog, *, op_id: str = "acme.vm.list", is_admin: bool = False) -> Any:
    async def _do() -> Any:
        async with get_sessionmaker()() as session:
            return await resolve_audit_references(
                session, row, op_id=op_id, op_class="read", is_admin=is_admin, now=_BASE
            )

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# Happy path: every reference resolves to named, linked substance
# ---------------------------------------------------------------------------


def test_resolve_references_happy_path() -> None:
    """Principal, target, op, run, parent, session, work_ref all resolve."""
    _seed_tenant(_TENANT_A, "tenant-a")
    target_id = _seed_target(tenant_id=_TENANT_A, name="edge-gateway-01")
    _seed_operation(op_id="acme.vm.list", summary="List virtual machines", group_name="Inventory")
    run_id = _seed_run(tenant_id=_TENANT_A, template="nightly-drain", state="in_progress")
    parent = _seed_audit(tenant_id=_TENANT_A, op_id="acme.vm.create", second=0)
    session_uuid = uuid.uuid4()
    row = _seed_audit(
        tenant_id=_TENANT_A,
        op_id="acme.vm.list",
        operator_sub="sub-9999",
        payload_extra={"principal_name": "Ada Lovelace", "principal_email": "ada@example.test"},
        target_id=target_id,
        run_id=run_id,
        parent_audit_id=parent.id,
        agent_session_id=session_uuid,
        work_ref="gh:acme/widgets#7",
        second=1,
    )

    refs = _resolve(row, op_id="acme.vm.list", is_admin=True)

    # Principal: display name + service marker (agent session present).
    assert refs.principal.resolved is True
    assert refs.principal.name == "Ada Lovelace"
    assert refs.principal.email == "ada@example.test"
    assert refs.principal.is_service is True
    assert refs.principal.display == "Ada Lovelace"

    # Target: name + detail link.
    assert refs.target.resolved is True
    assert refs.target.name == "edge-gateway-01"
    assert refs.target.href == "/ui/connectors/edge-gateway-01"

    # Operation: human summary + group.
    assert refs.operation.resolved is True
    assert refs.operation.summary == "List virtual machines"
    assert refs.operation.group_name == "Inventory"

    # Run: state + template + link.
    assert refs.run.resolved is True
    assert refs.run.state == "in_progress"
    assert refs.run.template == "nightly-drain"
    assert refs.run.href == f"/ui/runbooks/runs/{run_id}"

    # Parent: labelled lineage line + drawer link.
    assert refs.parent.resolved is True
    assert refs.parent.summary_line == "acme.vm.create"
    assert refs.parent.href == f"/ui/audit/show/{parent.id}"

    # Session: replay link enabled for the admin lift.
    assert refs.session.present is True
    assert refs.session.replay_enabled is True
    assert refs.session.replay_href == f"/ui/audit/sessions/{session_uuid}/replay"

    # work_ref: gh shorthand linked out.
    assert refs.work_ref.resolved is True
    assert refs.work_ref.href == "https://github.com/acme/widgets/issues/7"
    assert refs.work_ref.label == "acme/widgets#7"

    # Plain-language "what happened" uses names, not GUIDs.
    assert "Ada Lovelace" in refs.what_happened
    assert "List virtual machines" in refs.what_happened
    assert "edge-gateway-01" in refs.what_happened
    assert "OK" in refs.what_happened
    assert refs.audit_href == f"/ui/audit?audit_id={row.id}"


def test_resolve_references_parent_line_includes_target() -> None:
    """A parent row bound to a target renders "op on target"."""
    _seed_tenant(_TENANT_A, "tenant-a")
    parent_target = _seed_target(tenant_id=_TENANT_A, name="core-switch-9")
    parent = _seed_audit(
        tenant_id=_TENANT_A, op_id="acme.net.patch", target_id=parent_target, second=0
    )
    row = _seed_audit(tenant_id=_TENANT_A, parent_audit_id=parent.id, second=1)

    refs = _resolve(row)
    assert refs.parent.summary_line == "acme.net.patch on core-switch-9"


# ---------------------------------------------------------------------------
# Graceful degradation: unresolved references fall back to raw ids
# ---------------------------------------------------------------------------


def test_resolve_references_unresolved_fallbacks() -> None:
    """Missing target / run / parent / non-gh work_ref degrade, never raise."""
    _seed_tenant(_TENANT_A, "tenant-a")
    missing_target = uuid.uuid4()
    missing_run = uuid.uuid4()
    missing_parent = uuid.uuid4()
    row = _seed_audit(
        tenant_id=_TENANT_A,
        op_id="acme.unknown.op",
        operator_sub="sub-nameless",
        target_id=missing_target,
        run_id=missing_run,
        parent_audit_id=missing_parent,
        work_ref="CR-2026-0042",
    )

    refs = _resolve(row, op_id="acme.unknown.op")

    # Principal: no payload name -> falls back to the raw sub.
    assert refs.principal.resolved is False
    assert refs.principal.display == "sub-nameless"
    assert refs.principal.is_service is False

    # Target: present (id bound) but unresolved -> raw id kept.
    assert refs.target.present is True
    assert refs.target.resolved is False
    assert refs.target.id == str(missing_target)
    assert refs.target.name is None

    # Operation: no descriptor -> no summary, but op id kept.
    assert refs.operation.resolved is False
    assert refs.operation.summary is None
    assert refs.operation.op_id == "acme.unknown.op"

    # Run + parent: present but unresolved, raw ids + links still reachable.
    assert refs.run.present is True
    assert refs.run.resolved is False
    assert refs.run.href == f"/ui/runbooks/runs/{missing_run}"
    assert refs.parent.present is True
    assert refs.parent.resolved is False
    assert refs.parent.href == f"/ui/audit/show/{missing_parent}"

    # work_ref: opaque ref kept raw (never guessed into a URL).
    assert refs.work_ref.present is True
    assert refs.work_ref.resolved is False
    assert refs.work_ref.raw == "CR-2026-0042"
    assert refs.work_ref.href is None


def test_resolve_references_cross_tenant_target_not_leaked() -> None:
    """A target_id belonging to another tenant resolves to no name."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    foreign_target = _seed_target(tenant_id=_TENANT_B, name="tenant-b-secret-host")
    row = _seed_audit(tenant_id=_TENANT_A, target_id=foreign_target)

    refs = _resolve(row)
    assert refs.target.present is True
    assert refs.target.resolved is False
    assert refs.target.name is None
    # The foreign name is never surfaced.
    assert refs.what_happened.find("tenant-b-secret-host") == -1


def test_resolve_references_no_target_is_not_unresolved() -> None:
    """A row that touched no target reports target absent (not unresolved)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row = _seed_audit(tenant_id=_TENANT_A, target_id=None)

    refs = _resolve(row)
    assert refs.target.present is False
    assert refs.target.resolved is False
    assert refs.target.id is None


def test_resolve_references_service_marker_from_actor_sub() -> None:
    """A delegated actor (no agent session) still flags the service marker."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row = _seed_audit(tenant_id=_TENANT_A, actor_sub="agent-sub-77", agent_session_id=None)

    refs = _resolve(row)
    assert refs.principal.is_service is True
    assert refs.principal.actor_sub == "agent-sub-77"


def test_resolve_references_replay_disabled_for_non_admin() -> None:
    """The replay deep-link stays disabled for a non-admin lift."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row = _seed_audit(tenant_id=_TENANT_A, agent_session_id=uuid.uuid4())

    refs = _resolve(row, is_admin=False)
    assert refs.session.present is True
    assert refs.session.replay_enabled is False
