# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the reviewer-context resolver (#3353).

:func:`~meho_backplane.operations.approval_context.resolve_reviewer_context`
turns a parked :class:`~meho_backplane.db.models.ApprovalRequest`'s
machine-truthful coordinates (a placeholder ``op_id``, a ``target_id``
GUID, a ``params_hash``) into the single redacted, human-legible contract
both the console modal and ``meho approvals show`` render. These tests
pin:

* the resolved fields -- target name + product/version, the subject of a
  path-variable op, the parent composite op walked off the #3348 lineage,
  work_ref / run_id, blast radius, and the "what will happen" summary;
* the **secret-hygiene** hard constraint -- a credential-class op gets no
  subject echo, a secret-keyed param never surfaces, and a secret-shaped
  identity value is dropped by the redaction engine (no secret reaches the
  summary);
* fail-open -- a resolution miss yields an empty context, never raises;
* the shared-contract parity -- the REST projection and the console both
  expose the same resolved values from the one resolver.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    EndpointDescriptor,
    Target,
)
from meho_backplane.operations.approval_context import resolve_reviewer_context
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _seed_target(
    session: AsyncSession,
    *,
    target_id: uuid.UUID,
    tenant_id: uuid.UUID = _TENANT,
    name: str = "lab-vcenter",
    product: str = "vmware",
    version: str | None = "9.0",
) -> None:
    session.add(
        Target(
            id=target_id,
            tenant_id=tenant_id,
            name=name,
            product=product,
            version=version,
            host="vcenter.lab.example",
        )
    )
    await session.flush()


async def _seed_descriptor(
    session: AsyncSession,
    *,
    op_id: str,
    summary: str,
    product: str = "vmware",
    version: str = "9.0",
) -> None:
    session.add(
        EndpointDescriptor(
            product=product,
            version=version,
            impl_id="vmware-rest",
            op_id=op_id,
            source_kind="ingested",
            safety_level="dangerous",
            requires_approval=False,
            parameter_schema={},
            summary=summary,
            tenant_id=None,
        )
    )
    await session.flush()


async def _seed_request(
    session: AsyncSession,
    *,
    op_id: str = "POST:/vcenter/vm/{vm}/power?action=start",
    connector_id: str = "vmware-rest-9.0",
    tenant_id: uuid.UUID = _TENANT,
    target_id: uuid.UUID | None = None,
    params: dict[str, object] | None = None,
    proposed_effect: dict[str, object] | None = None,
    work_ref: str | None = None,
    run_id: uuid.UUID | None = None,
    request_audit_id: uuid.UUID | None = None,
) -> ApprovalRequest:
    row = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        principal_sub="svc-blueprint",
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        params_hash="0" * 64,
        params=params if params is not None else {},
        proposed_effect=proposed_effect or {"op_id": op_id, "connector_id": connector_id},
        status=ApprovalRequestStatus.PENDING.value,
        created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        work_ref=work_ref,
        request_audit_id=request_audit_id,
    )
    session.add(row)
    await session.flush()
    return row


async def _seed_audit(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    path: str,
    tenant_id: uuid.UUID = _TENANT,
    parent_audit_id: uuid.UUID | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditLog(
            id=audit_id,
            operator_sub="svc-blueprint",
            method="POST",
            path=path,
            status_code=202,
            tenant_id=tenant_id,
            parent_audit_id=parent_audit_id,
            payload=payload or {},
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolves_target_name_product_version(session: AsyncSession) -> None:
    tid = uuid.uuid4()
    await _seed_target(session, target_id=tid)
    request = await _seed_request(session, target_id=tid)

    ctx = await resolve_reviewer_context(session, request)

    assert ctx.target_name == "lab-vcenter"
    assert ctx.target_product == "vmware"
    assert ctx.target_version == "9.0"


@pytest.mark.asyncio
async def test_target_cross_tenant_not_leaked(session: AsyncSession) -> None:
    """A target_id belonging to another tenant resolves to no name."""
    tid = uuid.uuid4()
    await _seed_target(session, target_id=tid, tenant_id=_OTHER_TENANT)
    request = await _seed_request(session, target_id=tid, tenant_id=_TENANT)

    ctx = await resolve_reviewer_context(session, request)

    assert ctx.target_name is None
    assert ctx.target_product is None


@pytest.mark.asyncio
async def test_no_target_resolves_to_none(session: AsyncSession) -> None:
    request = await _seed_request(session, target_id=None)
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.target_name is None


# ---------------------------------------------------------------------------
# Subject resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolves_subject_from_path_var_and_name(session: AsyncSession) -> None:
    request = await _seed_request(
        session,
        op_id="POST:/vcenter/vm/{vm}/power?action=start",
        params={"vm": "vm-1042", "name": "web-01"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject == "web-01 (vm-1042)"


@pytest.mark.asyncio
async def test_subject_from_name_only(session: AsyncSession) -> None:
    request = await _seed_request(
        session,
        op_id="POST:/vcenter/vm/{vm}/power?action=start",
        params={"name": "web-01"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject == "web-01"


@pytest.mark.asyncio
async def test_subject_from_moid_only(session: AsyncSession) -> None:
    request = await _seed_request(
        session,
        op_id="POST:/VirtualMachine/{moId}/ReconfigVM_Task",
        params={"moId": "vm-1042"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject == "vm-1042"


@pytest.mark.asyncio
async def test_subject_none_without_identity_params(session: AsyncSession) -> None:
    request = await _seed_request(
        session,
        op_id="POST:/vcenter/vm/{vm}/power?action=start",
        params={"flavor": "small"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject is None


# ---------------------------------------------------------------------------
# Secret hygiene (the hard constraint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_class_op_gets_no_subject(session: AsyncSession) -> None:
    """A credential-class op echoes no subject even with a ``name`` param."""
    request = await _seed_request(
        session,
        op_id="vault.kv.put",
        connector_id="vault-1.x",
        params={"name": "app-db", "path": "secret/data/app"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject is None


@pytest.mark.asyncio
async def test_secret_keyed_param_never_surfaces(session: AsyncSession) -> None:
    """A secret-keyed param (password / token) is not in the identity allowlist."""
    request = await _seed_request(
        session,
        op_id="POST:/vcenter/vm/{vm}/power?action=start",
        params={"password": "hunter2", "token": "sk-abcdef0123456789"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject is None
    # And nothing secret-shaped rides the summary either.
    assert ctx.summary is None or "hunter2" not in ctx.summary


@pytest.mark.asyncio
async def test_secret_shaped_identity_value_is_dropped(session: AsyncSession) -> None:
    """A ``name`` whose value is secret-shaped is dropped by the redaction engine."""
    jwt_shaped = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456"
    request = await _seed_request(
        session,
        op_id="POST:/vcenter/vm/{vm}/power?action=start",
        params={"name": jwt_shaped},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.subject is None
    # The secret-shaped value never reaches the summary.
    assert ctx.summary is None or jwt_shaped not in ctx.summary


# ---------------------------------------------------------------------------
# Parent composite lineage (#3348)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolves_parent_composite_op_via_lineage(session: AsyncSession) -> None:
    """A child park walks request_audit_id -> parent_audit_id -> composite op id."""
    composite_audit_id = uuid.uuid4()
    child_audit_id = uuid.uuid4()
    # A dispatch audit row carries the op id both on ``path`` and in
    # ``payload["op_id"]`` (see operations/_audit.py); the resolver reads
    # the payload op id first.
    await _seed_audit(
        session,
        audit_id=composite_audit_id,
        path="vmware.composite.vm.power",
        payload={"op_id": "vmware.composite.vm.power"},
    )
    await _seed_audit(
        session,
        audit_id=child_audit_id,
        path="approval.request",
        parent_audit_id=composite_audit_id,
        payload={"op_id": "POST:/vcenter/vm/{vm}/power?action=start"},
    )
    request = await _seed_request(session, request_audit_id=child_audit_id)

    ctx = await resolve_reviewer_context(session, request)
    assert ctx.parent_composite_op_id == "vmware.composite.vm.power"


@pytest.mark.asyncio
async def test_no_parent_composite_for_top_level_park(session: AsyncSession) -> None:
    """A top-level park's audit row has no parent -> no composite op."""
    child_audit_id = uuid.uuid4()
    await _seed_audit(
        session, audit_id=child_audit_id, path="approval.request", parent_audit_id=None
    )
    request = await _seed_request(session, request_audit_id=child_audit_id)

    ctx = await resolve_reviewer_context(session, request)
    assert ctx.parent_composite_op_id is None


@pytest.mark.asyncio
async def test_parent_composite_cross_tenant_not_leaked(session: AsyncSession) -> None:
    """A parent audit row in another tenant does not resolve."""
    composite_audit_id = uuid.uuid4()
    child_audit_id = uuid.uuid4()
    await _seed_audit(
        session,
        audit_id=composite_audit_id,
        path="vmware.composite.vm.power",
        tenant_id=_OTHER_TENANT,
    )
    await _seed_audit(
        session,
        audit_id=child_audit_id,
        path="approval.request",
        tenant_id=_TENANT,
        parent_audit_id=composite_audit_id,
    )
    request = await _seed_request(session, request_audit_id=child_audit_id, tenant_id=_TENANT)

    ctx = await resolve_reviewer_context(session, request)
    assert ctx.parent_composite_op_id is None


# ---------------------------------------------------------------------------
# Run context + blast radius
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surfaces_work_ref_and_run_id(session: AsyncSession) -> None:
    run_id = uuid.uuid4()
    request = await _seed_request(session, work_ref="gh:evoila/meho#42", run_id=run_id)
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.work_ref == "gh:evoila/meho#42"
    assert ctx.run_id == str(run_id)


@pytest.mark.asyncio
async def test_surfaces_blast_radius_from_proposed_effect(session: AsyncSession) -> None:
    blast = {"object": "vm-1042", "children": ["disk-1"], "irreversibility": "hard"}
    request = await _seed_request(
        session,
        proposed_effect={"op_id": "x", "safety_level": "destructive", "blast_radius": blast},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.blast_radius == blast


@pytest.mark.asyncio
async def test_blast_radius_none_when_absent(session: AsyncSession) -> None:
    request = await _seed_request(session, proposed_effect={"op_id": "x", "connector_id": "y"})
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.blast_radius is None


# ---------------------------------------------------------------------------
# Summary sentence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builds_summary_from_descriptor(session: AsyncSession) -> None:
    op_id = "POST:/vcenter/vm/{vm}/power?action=start"
    tid = uuid.uuid4()
    await _seed_target(session, target_id=tid)
    await _seed_descriptor(session, op_id=op_id, summary="Power on a virtual machine")
    request = await _seed_request(
        session,
        op_id=op_id,
        target_id=tid,
        params={"vm": "vm-1042", "name": "web-01"},
    )
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.summary is not None
    assert "Power on a virtual machine" in ctx.summary
    assert "web-01 (vm-1042)" in ctx.summary
    assert "lab-vcenter" in ctx.summary
    assert "vmware 9.0" in ctx.summary


@pytest.mark.asyncio
async def test_summary_none_when_only_raw_op_id(session: AsyncSession) -> None:
    """No descriptor, no subject, no target -> nothing legible to add."""
    request = await _seed_request(session, op_id="POST:/opaque/thing", params={})
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.summary is None


# ---------------------------------------------------------------------------
# Fail-open + shared-contract parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_open_empty_context(session: AsyncSession) -> None:
    """A request with nothing to resolve yields an empty context, no raise."""
    request = await _seed_request(session, target_id=None, params={}, op_id="POST:/opaque/thing")
    ctx = await resolve_reviewer_context(session, request)
    assert ctx.is_empty


@pytest.mark.asyncio
async def test_console_and_rest_render_same_resolved_context(session: AsyncSession) -> None:
    """The REST projection and the console both expose the one resolver's values.

    Parity regression (#3353): ``meho approvals show`` (via
    ``_reviewer_context_view`` on the REST view) and the console modal
    (which receives the raw :class:`ReviewerContext` dataclass) must render
    the *same* resolved substance, since both derive from
    :func:`resolve_reviewer_context`. This asserts the REST projection does
    not drop or rename any field the console shows.
    """
    from meho_backplane.api.v1.approvals import _reviewer_context_view

    op_id = "POST:/vcenter/vm/{vm}/power?action=start"
    tid = uuid.uuid4()
    composite_audit_id = uuid.uuid4()
    child_audit_id = uuid.uuid4()
    await _seed_target(session, target_id=tid)
    await _seed_descriptor(session, op_id=op_id, summary="Power on a virtual machine")
    await _seed_audit(
        session,
        audit_id=composite_audit_id,
        path="vmware.composite.vm.power",
        payload={"op_id": "vmware.composite.vm.power"},
    )
    await _seed_audit(
        session,
        audit_id=child_audit_id,
        path="approval.request",
        parent_audit_id=composite_audit_id,
    )
    request = await _seed_request(
        session,
        op_id=op_id,
        target_id=tid,
        params={"vm": "vm-1042", "name": "web-01"},
        work_ref="gh:evoila/meho#42",
        request_audit_id=child_audit_id,
    )

    # The shared producer runs once; the console receives this dataclass.
    console_ctx = await resolve_reviewer_context(session, request)
    # The REST view projects from the same dataclass.
    rest_view = _reviewer_context_view(console_ctx)

    assert rest_view.target_name == console_ctx.target_name == "lab-vcenter"
    assert rest_view.target_product == console_ctx.target_product == "vmware"
    assert rest_view.target_version == console_ctx.target_version == "9.0"
    assert rest_view.subject == console_ctx.subject == "web-01 (vm-1042)"
    assert (
        rest_view.parent_composite_op_id
        == console_ctx.parent_composite_op_id
        == "vmware.composite.vm.power"
    )
    assert rest_view.summary == console_ctx.summary
    assert rest_view.blast_radius == console_ctx.blast_radius
