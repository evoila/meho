# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :func:`register_composite_operation`.

Coverage matrix (G3.1-T4 / Task #504 acceptance criteria):

* First-register path -- a brand-new ``(product, version, impl_id,
  op_id)`` key inserts one ``endpoint_descriptor`` row with
  ``source_kind="composite"``, ``handler_ref`` derived correctly,
  ``parameter_schema`` persisted, embedding computed once,
  ``is_enabled=True``, ``tenant_id IS NULL``.
* Skip-re-embed idempotency -- a second call with identical args is
  a no-op for the embedding pipeline (single ``encode_one`` call
  across both calls; one row in the table).
* Re-embed path -- changing ``summary`` / ``description`` between
  calls recomputes the embedding + updates every body-derived field
  + advances ``updated_at``.
* Handler-signature validation -- a handler without a
  ``dispatch_child`` parameter raises :class:`HandlerSignatureError`
  with a message naming the handler's dotted path.
* Cross-rejection (typed -> composite handler) --
  :func:`register_typed_operation` with a composite-shaped handler
  raises :class:`HandlerSignatureError` pointing at
  ``register_composite_operation`` as the right helper.
* Cross-rejection (composite -> typed handler) --
  :func:`register_composite_operation` with a typed-shaped handler
  raises :class:`HandlerSignatureError` with the
  ``dispatch_child``-required message.
* Closure / lambda / partial rejection inherits from
  :func:`derive_handler_ref` for composites identically to typed.
* Bound-method handler -- composite bound methods round-trip through
  the dotted-path derivation (``self`` dropped by the signature
  introspection).
* End-to-end dispatch -- a composite registered via the new helper,
  dispatched through the production :func:`dispatch` entrypoint, has
  its ``dispatch_child(...)`` routed back to a typed sub-op and the
  sub-op's audit row carries ``parent_audit_id`` = composite parent's
  audit row id.
* Defaults -- new composite rows land with ``safety_level="dangerous"``
  and ``requires_approval=True`` (vs typed's ``"safe"`` / ``False``).
* The composite helper accepts a caller-owned ``AsyncSession`` and
  does not commit; rollback discards the upsert.

The embedding service is mocked via the explicit
``embedding_service=`` parameter so tests don't pull fastembed or
ONNX runtime, mirroring the typed-register suite.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    HandlerRefError,
    HandlerSignatureError,
    dispatch,
    register_composite_operation,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings
from tests.fixtures.composites.handlers import (
    CompositeHandlerHost,
    composite_dispatch_child_handler,
    composite_module_level_handler,
    composite_typed_shaped_handler,
    typed_sub_op_handler,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so the upsert doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub for the e2e test."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


def _make_operator(*, sub: str = "op-composite-register") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Composite Register Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target shape the resolver / dispatcher reads from."""

    def __init__(
        self,
        *,
        product: str = "vault",
        version: str | None = None,
        target_id: UUID | None = None,
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=version)
        self.preferred_impl_id: str | None = None
        self.id: UUID = target_id or uuid.uuid4()
        self.name = "test-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


class _NoOpVaultConnector(Connector):
    """Resolver-satisfying connector class -- never actually called.

    The dispatcher's branch resolver instantiates this for typed +
    composite dispatch paths; the handler resolution is via the
    dotted ``handler_ref`` walk, not via this class' ``execute``.
    """

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# First-register path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_first_call_inserts_row_with_composite_source_kind(
    stub_embedding_service: AsyncMock,
) -> None:
    """First call inserts a row with ``source_kind='composite'`` + every field populated."""
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.composite.vm.create",
        handler=composite_module_level_handler,
        summary="Create a VM end-to-end via vSphere REST.",
        description=(
            "Orchestrates folder lookup, datastore selection, and VM creation "
            "into a single composite that calls the underlying typed ops."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"folder_name": {"type": "string"}},
        },
        response_schema={"type": "object"},
        group_key="vm-lifecycle-composites",
        when_to_use="VM-lifecycle composites: orchestrated create/delete/clone flows.",
        tags=["vm", "lifecycle"],
        llm_instructions={"when_to_call": "for fully orchestrated VM creation"},
        embedding_service=stub_embedding_service,
    )

    assert stub_embedding_service.encode_one.call_count == 1

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        result = await fresh.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.op_id == "vmware.composite.vm.create"
            )
        )
        row = result.scalar_one()

    assert isinstance(row.id, uuid.UUID)
    assert row.tenant_id is None
    assert row.product == "vmware"
    assert row.version == "9.0"
    assert row.impl_id == "vmware-rest"
    assert row.op_id == "vmware.composite.vm.create"
    assert row.source_kind == "composite"
    assert row.method is None
    assert row.path is None
    assert row.handler_ref == ("tests.fixtures.composites.handlers.composite_module_level_handler")
    assert row.summary.startswith("Create a VM")
    assert row.description.startswith("Orchestrates folder lookup")
    assert row.tags == ["vm", "lifecycle"]
    assert row.parameter_schema == {
        "type": "object",
        "properties": {"folder_name": {"type": "string"}},
    }
    assert row.response_schema == {"type": "object"}
    assert row.llm_instructions == {"when_to_call": "for fully orchestrated VM creation"}
    # Composites default to dangerous + approval-gated.
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert row.is_enabled is True
    assert row.embedding == [0.1] * 384
    assert row.custom_description is None
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_register_composite_per_op_safety_override(
    stub_embedding_service: AsyncMock,
) -> None:
    """Read-only composites override the dangerous default at the call site."""
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.composite.vm.info",
        handler=composite_module_level_handler,
        summary="Aggregate VM info from multiple endpoints.",
        description="Read-only roll-up of VM metadata into one response.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=False,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vmware.composite.vm.info"
                )
            )
        ).scalar_one()

    assert row.safety_level == "safe"
    assert row.requires_approval is False


# ---------------------------------------------------------------------------
# Idempotency -- body-hash skip path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composite_same_args_twice_skips_reembed(
    stub_embedding_service: AsyncMock,
) -> None:
    """Re-call with identical args -> 1 row, embedding called exactly once."""
    kwargs: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "op_id": "vmware.composite.vm.create",
        "handler": composite_module_level_handler,
        "summary": "Create a VM.",
        "description": "Composite orchestrating VM creation.",
        "parameter_schema": {"type": "object"},
        "when_to_use": None,
        "tags": ["vm"],
        "embedding_service": stub_embedding_service,
    }
    await register_composite_operation(**kwargs)
    assert stub_embedding_service.encode_one.call_count == 1

    await register_composite_operation(**kwargs)
    assert stub_embedding_service.encode_one.call_count == 1, (
        "Embedding service must not be invoked when the embedding text is unchanged"
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == "vmware.composite.vm.create"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("summary", "Updated summary blurb."),
        ("description", "Updated description body."),
        ("tags", ["vm", "lifecycle", "added"]),
    ],
)
@pytest.mark.asyncio
async def test_register_composite_changed_embedding_text_triggers_reembed(
    stub_embedding_service: AsyncMock,
    field: str,
    new_value: Any,
) -> None:
    """Changing any embedding-text input triggers re-embed -- inherited from the shared path."""
    baseline: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "op_id": "vmware.composite.vm.create",
        "handler": composite_module_level_handler,
        "summary": "Create a VM.",
        "description": "Composite orchestrating VM creation.",
        "parameter_schema": {"type": "object"},
        "when_to_use": None,
        "tags": ["vm"],
        "embedding_service": stub_embedding_service,
    }
    await register_composite_operation(**baseline)
    assert stub_embedding_service.encode_one.call_count == 1

    updated = dict(baseline)
    updated[field] = new_value
    await register_composite_operation(**updated)
    assert stub_embedding_service.encode_one.call_count == 2, (
        f"Changing {field!r} must trigger a re-embed"
    )


# ---------------------------------------------------------------------------
# Handler-signature validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composite_rejects_typed_shaped_handler(
    stub_embedding_service: AsyncMock,
) -> None:
    """A handler without ``dispatch_child`` raises :class:`HandlerSignatureError`."""
    with pytest.raises(HandlerSignatureError, match="dispatch_child") as excinfo:
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.bad",
            handler=composite_typed_shaped_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    # Message names the handler's dotted path so the operator can find it.
    assert "composite_typed_shaped_handler" in str(excinfo.value)


@pytest.mark.asyncio
async def test_register_typed_rejects_composite_shaped_handler(
    stub_embedding_service: AsyncMock,
) -> None:
    """The typed helper rejects composite-shaped handlers symmetrically."""
    with pytest.raises(HandlerSignatureError, match="register_composite_operation") as excinfo:
        await register_typed_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.bad.typed",
            handler=composite_module_level_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    assert "composite_module_level_handler" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Cross-kind re-registration on an already-persisted natural key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composite_rejects_cross_kind_over_existing_typed_row(
    stub_embedding_service: AsyncMock,
) -> None:
    """An op already registered as ``typed`` cannot be re-registered as ``composite``.

    Regression test for the natural-key lookup gap: the unique key
    ``(tenant_id, product, version, impl_id, op_id)`` does not include
    ``source_kind``, so without the guard in
    :func:`_register_in_session` a stray composite registration on the
    same key would silently UPDATE everything except ``source_kind``.
    The dispatcher would then route through the typed branch (the
    persisted value) while the registrant believed it had switched the
    op to composite -- producing a :exc:`TypeError` at first dispatch
    when the typed branch invoked the composite handler without the
    ``dispatch_child`` kwarg.

    The contract is: registration-time fail-fast, with the handler's
    dotted path in the message so the operator can locate the
    misroute in lifespan logs.
    """
    # Seed: register as typed via the production helper.
    await register_typed_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.cross.kind.flip",
        handler=typed_sub_op_handler,
        summary="typed first",
        description="initial typed registration",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    # Attempt: re-register the same natural key as composite.
    with pytest.raises(HandlerSignatureError, match="cross-kind") as excinfo:
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.cross.kind.flip",
            handler=composite_module_level_handler,
            summary="composite second",
            description="attempted composite re-registration",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )

    # Error names both source_kinds + the op natural key.
    message = str(excinfo.value)
    assert "source_kind='typed'" in message
    assert "source_kind='composite'" in message
    assert "vmware.cross.kind.flip" in message

    # And the persisted row remains typed -- the failed registration
    # did not silently flip the row or leave it half-updated.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vmware.cross.kind.flip"
                )
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert row.handler_ref == "tests.fixtures.composites.handlers.typed_sub_op_handler"


@pytest.mark.asyncio
async def test_register_typed_rejects_cross_kind_over_existing_composite_row(
    stub_embedding_service: AsyncMock,
) -> None:
    """Symmetric guard: composite-first then typed re-register also raises."""
    # Seed: register as composite via the production helper.
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.cross.kind.flip.reverse",
        handler=composite_module_level_handler,
        summary="composite first",
        description="initial composite registration",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    # Attempt: re-register the same natural key as typed.
    with pytest.raises(HandlerSignatureError, match="cross-kind") as excinfo:
        await register_typed_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.cross.kind.flip.reverse",
            handler=typed_sub_op_handler,
            summary="typed second",
            description="attempted typed re-registration",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )

    message = str(excinfo.value)
    assert "source_kind='composite'" in message
    assert "source_kind='typed'" in message
    assert "vmware.cross.kind.flip.reverse" in message

    # Persisted row remains composite.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vmware.cross.kind.flip.reverse"
                )
            )
        ).scalar_one()
    assert row.source_kind == "composite"
    assert row.handler_ref == "tests.fixtures.composites.handlers.composite_module_level_handler"


@pytest.mark.asyncio
async def test_register_composite_idempotent_same_kind_after_guard(
    stub_embedding_service: AsyncMock,
) -> None:
    """The cross-kind guard does not regress same-kind re-registration.

    Re-registering the same op with the same ``source_kind`` must still
    take the skip-re-embed / re-embed UPDATE path -- the guard fires
    only on a mismatch.
    """
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.same.kind.reregister",
        handler=composite_module_level_handler,
        summary="initial",
        description="initial",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    # Same args twice -- no raise; the skip-re-embed UPDATE path runs.
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.same.kind.reregister",
        handler=composite_module_level_handler,
        summary="initial",
        description="initial",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id == "vmware.same.kind.reregister"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].source_kind == "composite"


@pytest.mark.asyncio
async def test_register_composite_signature_validation_runs_before_handler_ref(
    stub_embedding_service: AsyncMock,
) -> None:
    """Closure handler raises :class:`HandlerSignatureError` first -- it has no ``dispatch_child``.

    The order is intentional: the signature check is cheaper than the
    closure rejection (string scan of qualname) and gives the more
    actionable error message for the misregistered-helper case. A
    closure with the composite signature would still hit
    :class:`HandlerRefError` on the closure check below.
    """

    def make_closure() -> Any:
        async def inner(target: Any, params: dict[str, Any]) -> dict[str, Any]:
            return {}

        return inner

    with pytest.raises(HandlerSignatureError, match="dispatch_child"):
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.closure",
            handler=make_closure(),
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_composite_rejects_closure_with_dispatch_child(
    stub_embedding_service: AsyncMock,
) -> None:
    """A closure with the composite signature still falls to the closure check."""

    def make_composite_closure() -> Any:
        async def inner(
            operator: Any,
            target: Any,
            params: dict[str, Any],
            dispatch_child: Any,
        ) -> dict[str, Any]:
            return {}

        return inner

    with pytest.raises(HandlerRefError, match="closure"):
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.closure",
            handler=make_composite_closure(),
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_composite_rejects_lambda(
    stub_embedding_service: AsyncMock,
) -> None:
    """A lambda fails the signature check (no ``dispatch_child`` param)."""
    handler = lambda operator, target, params: {}  # type: ignore[misc]  # noqa: E731 - exercising rejection
    with pytest.raises(HandlerSignatureError, match="dispatch_child"):
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.lambda",
            handler=handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


@pytest.mark.asyncio
async def test_register_composite_rejects_functools_partial(
    stub_embedding_service: AsyncMock,
) -> None:
    """``functools.partial`` lacks ``__qualname__`` -- rejected by the shared derive step."""
    partial_handler = functools.partial(composite_module_level_handler)
    # The partial *does* expose the wrapped signature via ``inspect.signature``
    # -- it includes ``dispatch_child`` -- so the signature check passes and
    # the failure surfaces at ``derive_handler_ref``'s missing-attr branch.
    with pytest.raises(HandlerRefError):
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.partial",
            handler=partial_handler,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


# ---------------------------------------------------------------------------
# Bound-method handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composite_bound_method_handler_ref(
    stub_embedding_service: AsyncMock,
) -> None:
    """A composite bound method round-trips as ``module.Class.method``.

    The ``self`` parameter is the leading entry in the bound method's
    signature; :func:`_handler_parameter_names` drops it so the
    ``dispatch_child`` check still passes for bound methods.
    """
    host = CompositeHandlerHost()
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.composite.bound",
        handler=host.composite_bound_method,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vmware.composite.bound"
                )
            )
        ).scalar_one()
    assert row.handler_ref == (
        "tests.fixtures.composites.handlers.CompositeHandlerHost.composite_bound_method"
    )
    assert row.source_kind == "composite"


@pytest.mark.asyncio
async def test_register_composite_rejects_typed_bound_method(
    stub_embedding_service: AsyncMock,
) -> None:
    """A bound method without ``dispatch_child`` raises (``self``-drop still works)."""
    host = CompositeHandlerHost()
    with pytest.raises(HandlerSignatureError, match="dispatch_child"):
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.bad_bound",
            handler=host.typed_bound_method,
            summary="x",
            description="x",
            parameter_schema={"type": "object"},
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )


# ---------------------------------------------------------------------------
# Caller-owned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composite_caller_session_does_not_commit(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """When the caller passes a session, the helper flushes but does not commit."""
    await register_composite_operation(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="vmware.composite.session",
        handler=composite_module_level_handler,
        summary="x",
        description="x",
        parameter_schema={"type": "object"},
        session=session,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    # Fresh session sees nothing -- caller hasn't committed yet.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        assert (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vmware.composite.session"
                )
            )
        ).scalar_one_or_none() is None

    await session.commit()
    async with sessionmaker() as fresh_after:
        assert (
            await fresh_after.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vmware.composite.session"
                )
            )
        ).scalar_one() is not None


# ---------------------------------------------------------------------------
# End-to-end dispatch through the production dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_composite_dispatches_end_to_end_with_parent_audit(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Composite registered via the new helper dispatches; sub-op audit row links to parent.

    The load-bearing acceptance criterion: this Task's row-creation
    surface must produce composites that actually dispatch through
    the existing :func:`dispatch` entrypoint, with the
    G0.6-T7 (#398) audit-tree linkage intact.
    """
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )

    # Typed sub-op the composite will call via dispatch_child.
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=typed_sub_op_handler,
        summary="Read a secret.",
        description="Read a secret.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    # The composite under test, registered via the new helper.
    # Override the dangerous-by-default policy: v0.2's policy gate
    # denies any ``requires_approval=True`` row outright (no approval
    # workflow yet); the test focuses on the dispatch + audit-tree
    # plumbing, not the policy gate.
    await register_composite_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.composite.read_one",
        handler=composite_dispatch_child_handler,
        summary="Composite reading one secret.",
        description="Dispatches a single child sub-op for an end-to-end test.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=False,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.composite.read_one",
        target=target,
        params={
            "sub_connector_id": "vault-1.x",
            "sub_op_id": "vault.kv.read",
            "sub_params": {"path": "/secret/data/foo"},
        },
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["sub_status"] == "ok"
    # The sub-op received the params we threaded through dispatch_child.
    assert result.result["sub_result"]["echo"] == {"path": "/secret/data/foo"}

    # Audit-tree assertion: two rows, child links to parent on the real column.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_({"vault.composite.read_one", "vault.kv.read"})
                    )
                )
            )
            .scalars()
            .all()
        )
    parent_rows = [r for r in rows if r.path == "vault.composite.read_one"]
    child_rows = [r for r in rows if r.path == "vault.kv.read"]
    assert len(parent_rows) == 1
    assert len(child_rows) == 1
    parent = parent_rows[0]
    child = child_rows[0]
    assert parent.parent_audit_id is None
    assert child.parent_audit_id == parent.id
