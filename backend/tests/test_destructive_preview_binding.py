# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the destructive-tier preview binding + blast-radius (#3197).

Task #3197 under Initiative #3183 (governed deletes), decision
``docs/decisions/governed-delete-operations.md`` requirements 2 and 3:

* **Requirement 2 — preview-result-hash binding.** ``preview_operation``
  emits a stable ``preview_hash``; a ``destructive`` op refuses to park
  unless the caller presents a hash that matches the dispatcher's
  server-recomputed preview. Missing hash, non-resolvable preview, or a
  mismatch all fail closed.
* **Requirement 3 — mandatory blast-radius statement.** A ``destructive``
  op cannot park with only the identifier-only default: its
  ``proposed_effect`` must carry a ``blast_radius`` block (object identity,
  enumerated child objects, irreversibility class), else the park is
  refused.

The keystone flow — a full park → human-approve → audited resume honouring
the bound hash — is covered by
``test_full_park_approve_resume_honours_bound_hash``. The approve-time
re-verification (a destructive row missing its binding is refused) is
covered by ``test_approve_refuses_destructive_row_without_binding``.

Mirrors the sibling ingested-dispatch fixtures in
``test_operations_request_preview.py`` (recording HTTP connector, in-memory
SQLite via the autouse-migrated engine, deterministic embedding stub) and
the park→approve→resume flow in ``test_connectors_argocd_write_e2e.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
    register_preview_builder,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    PreviewBindingMissingError,
    approve_request,
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

_TENANT: UUID = UUID("00000000-0000-0000-0000-000000003197")
_OP_ID = "DELETE:/vms/{vm}"
_CONNECTOR_ID = "gh-rest-3"
_PARAMS = {"vm": "vm-42"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry, and isolate the preview registry."""
    builders_snapshot = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders_snapshot)


@pytest.fixture
def embedding() -> list[float]:
    return [0.1] * 384


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _operator(
    *, sub: str = "op-requester", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="Destructive-Binding Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """In-memory target shape the resolver / dispatcher / connectors read."""

    def __init__(self, *, name: str = "gh-prod") -> None:
        self.product = "gh"
        self.fingerprint = _FakeFingerprint(version="3")
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = name
        self.host = "api.github.com"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None
        self.fqdn: str | None = None


class _RecordingHttpConnector(HttpConnector):
    """Connector whose transport records calls instead of sending (no egress)."""

    product = "gh"
    version = "3"
    impl_id = "gh-rest"
    supported_version_range = ">=3,<4"
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"verb": verb, "path": path})
        return {"sent": True}

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"verb": method, "path": path})
        return {"sent": True}

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


def _register_recording_connector() -> _RecordingHttpConnector:
    register_connector_v2(product="gh", version="3", impl_id="gh-rest", cls=_RecordingHttpConnector)
    connector = get_or_create_connector_instance(_RecordingHttpConnector)
    assert isinstance(connector, _RecordingHttpConnector)
    return connector


async def _insert_destructive_descriptor(*, embedding: list[float]) -> None:
    """Seed one enabled, ``destructive``, ``requires_approval`` ingested delete op.

    ``requires_approval=True`` makes a ``USER`` dispatch park (reach the
    approval queue) deterministically, so the destructive-tier gate in
    ``_handle_needs_approval`` runs without depending on the #3196 USER-park
    routing.
    """
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="gh",
        version="3",
        impl_id="gh-rest",
        op_id=_OP_ID,
        source_kind="ingested",
        method="DELETE",
        path="/vms/{vm}",
        handler_ref=None,
        summary="Destroy a VM.",
        description="Ingested destructive delete test op.",
        tags=[],
        parameter_schema={
            "type": "object",
            "properties": {"vm": {"type": "string", "x-meho-param-loc": "path"}},
        },
        response_schema=None,
        llm_instructions=None,
        safety_level="destructive",
        requires_approval=True,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with get_sessionmaker()() as s:
        s.add(descriptor)
        await s.commit()


async def _blast_radius_builder(ctx: PreviewContext) -> dict[str, Any]:
    """A destructive-op preview builder that emits the mandatory blast-radius block."""
    return {
        "blast_radius": {
            "object": {"kind": "vm", "id": ctx.params.get("vm")},
            "children": [{"kind": "disk", "id": "disk-1"}],
            "irreversibility": "permanent",
        },
        "note": "delete preview",
    }


async def _seed_target(*, name: str = "gh-prod") -> TargetORM:
    async with get_sessionmaker()() as s:
        target = TargetORM(
            tenant_id=_TENANT,
            name=name,
            aliases=[],
            product="gh",
            host="api.github.com",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "3"},
            preferred_impl_id="gh-rest",
            notes="seeded by test_destructive_preview_binding",
        )
        s.add(target)
        await s.commit()
        await s.refresh(target)
        s.expunge(target)
        return target


async def _approval_row_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


# ---------------------------------------------------------------------------
# Requirement 2 — preview_operation emits a stable, param-sensitive hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_emits_stable_param_sensitive_hash(embedding: list[float]) -> None:
    """``preview_operation`` returns a 64-hex ``preview_hash`` that is stable for
    identical args and differs when the resolved request differs."""
    _register_recording_connector()
    await _insert_destructive_descriptor(embedding=embedding)

    env1 = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params={"vm": "vm-42"},
    )
    assert env1["status"] == "ok"
    assert isinstance(env1["preview_hash"], str)
    assert len(env1["preview_hash"]) == 64

    env2 = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params={"vm": "vm-42"},
    )
    assert env2["preview_hash"] == env1["preview_hash"]  # stable across identical args

    env3 = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params={"vm": "vm-99"},
    )
    assert env3["preview_hash"] != env1["preview_hash"]  # a different delete → different hash


# ---------------------------------------------------------------------------
# Requirement 3 — blast-radius validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "effect, is_valid",
    [
        (None, False),
        ({}, False),
        ({"blast_radius": "not-a-dict"}, False),
        ({"blast_radius": {"children": [], "irreversibility": "permanent"}}, False),  # no object
        (
            {"blast_radius": {"object": "vm-1", "irreversibility": "permanent"}},
            False,
        ),  # no children
        ({"blast_radius": {"object": "vm-1", "children": "x", "irreversibility": "y"}}, False),
        ({"blast_radius": {"object": "vm-1", "children": []}}, False),  # no irreversibility
        (
            {"blast_radius": {"object": "vm-1", "children": [], "irreversibility": "permanent"}},
            True,
        ),
    ],
)
def test_blast_radius_missing_reason(effect: dict[str, Any] | None, is_valid: bool) -> None:
    """The validator returns ``None`` only for a well-formed block; a reason otherwise."""
    reason = blast_radius_missing_reason(effect)
    assert (reason is None) is is_valid


# ---------------------------------------------------------------------------
# Requirement 2 — the dispatcher refuses to park without a matching binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_refuses_park_without_preview_hash(embedding: list[float]) -> None:
    """No ``preview_hash`` presented → ``preview_binding_required``, no row parked."""
    connector = _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _insert_destructive_descriptor(embedding=embedding)

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params=_PARAMS,
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert connector.calls == []
    assert await _approval_row_count() == 0


@pytest.mark.asyncio
async def test_dispatch_refuses_park_on_preview_hash_mismatch(embedding: list[float]) -> None:
    """A stale / forged ``preview_hash`` → ``preview_hash_mismatch``, no row parked."""
    connector = _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _insert_destructive_descriptor(embedding=embedding)

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params=_PARAMS,
        preview_hash="deadbeef" * 8,  # not the server-recomputed hash
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_hash_mismatch"
    assert connector.calls == []
    assert await _approval_row_count() == 0


@pytest.mark.asyncio
async def test_dispatch_refuses_park_when_hash_binds_different_params(
    embedding: list[float],
) -> None:
    """A hash previewed for other params (swap between preview and call) is refused."""
    _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _insert_destructive_descriptor(embedding=embedding)

    # Preview vm-99, then try to dispatch vm-42 with vm-99's hash.
    other = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params={"vm": "vm-99"},
    )
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params={"vm": "vm-42"},
        preview_hash=other["preview_hash"],
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_hash_mismatch"
    assert await _approval_row_count() == 0


# ---------------------------------------------------------------------------
# Requirement 3 — the dispatcher refuses to park without a blast-radius block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_refuses_park_without_blast_radius(embedding: list[float]) -> None:
    """A correct hash but no blast-radius builder → ``blast_radius_required``.

    No preview builder is registered, so ``proposed_effect`` carries only the
    generic params-echo — no blast-radius block — and the park is refused even
    though the preview-hash binding is satisfied.
    """
    connector = _register_recording_connector()
    await _insert_destructive_descriptor(embedding=embedding)

    env = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params=_PARAMS,
    )
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params=_PARAMS,
        preview_hash=env["preview_hash"],
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "blast_radius_required"
    assert connector.calls == []
    assert await _approval_row_count() == 0


# ---------------------------------------------------------------------------
# The keystone flow: park → human-approve → audited resume honouring the hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_park_approve_resume_honours_bound_hash(embedding: list[float]) -> None:
    """A destructive op with a matching hash + blast-radius parks, is approved by a
    second operator, and resumes to execution carrying the bound hash."""
    connector = _register_recording_connector()
    register_preview_builder(_OP_ID, _blast_radius_builder)
    await _seed_target(name="gh-prod")
    await _insert_destructive_descriptor(embedding=embedding)

    requester = _operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "gh-prod",
        "params": _PARAMS,
    }

    # 1) Preview → bind the hash.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"
    bound_hash = preview["preview_hash"]

    # 2) Governed call presenting the bound hash → parks.
    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert connector.calls == []  # nothing executed yet

    # 3) The parked row carries the bound hash + the blast-radius statement.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"] == {"kind": "vm", "id": "vm-42"}
    assert effect["blast_radius"]["children"] == [{"kind": "disk", "id": "disk-1"}]
    assert effect["blast_radius"]["irreversibility"] == "permanent"

    # 4) A different operator approves (four-eyes) — the binding re-verifies.
    approver = _operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) Audited resume → the bound delete executes exactly once.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume_result = await resume_dispatch_after_approval(
            operator=approver, request=row, params=None
        )
    assert resume_result.status == "ok"
    assert connector.calls == [{"verb": "DELETE", "path": "/vms/vm-42"}]


# ---------------------------------------------------------------------------
# Requirement 2 — approve-time re-verification of the binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_refuses_destructive_row_without_binding(embedding: list[float]) -> None:
    """A destructive row with no ``preview_hash`` cannot be approved (fail-closed).

    Constructs a destructive pending row directly with ``preview_hash=None``
    (the shape a park that bypassed the dispatcher gate would leave) and
    proves ``approve_request`` refuses it — the approve-time half of the
    binding, extending the ``params_hash`` swap-defence.
    """
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(sub="op-requester"),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=_PARAMS,
            params_hash="abc123",
            proposed_effect={"op_id": _OP_ID, "safety_level": "destructive"},
            preview_hash=None,
        )
        await s.commit()
        request_id = request.id

    approver = _operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        with pytest.raises(PreviewBindingMissingError):
            await approve_request(s, request_id, operator=approver, params=None)


@pytest.mark.asyncio
async def test_approve_allows_non_destructive_row_without_binding(embedding: list[float]) -> None:
    """A non-destructive row legitimately carries no binding and approves normally."""
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(sub="op-requester"),
            connector_id=_CONNECTOR_ID,
            op_id="POST:/vms",
            target=None,
            params=_PARAMS,
            params_hash="abc123",
            proposed_effect={"op_id": "POST:/vms", "safety_level": "dangerous"},
            preview_hash=None,
        )
        await s.commit()
        request_id = request.id

    approver = _operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
