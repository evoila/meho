# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.composite`.

Coverage matrix (G0.6-T7 / Task #398 acceptance criteria):

* :class:`DispatchChild` Protocol exists; :func:`get_dispatch_child`
  factory returns a callable bound to operator + target +
  parent_audit_id.
* T5's dispatcher's ``'composite'`` branch passes ``dispatch_child``
  to the handler (not raw ``dispatch``).
* ``parent_audit_id`` correctly bound: a composite with two children
  produces three audit rows; the two child rows have
  ``parent_audit_id`` = the composite's row id (real column, not
  just payload).
* Audit tree retrievable via SQL: ``SELECT id, parent_audit_id,
  op_id`` returns the expected tree shape.
* :attr:`Settings.composite_max_depth` wired (default 8); exceeding
  it raises :class:`CompositeRecursionLimitExceeded` with the depth
  + op_id chain.
* Composite-inside-composite (legitimate, depth-2) succeeds and
  produces the right nested ``parent_audit_id`` linkage.
* Over-depth call writes *no* audit row for the rejected sub-op;
  the parent composite's audit row records the structured error.
* Target inheritance: child sub-calls default to the parent's target;
  an explicit ``target=`` override on the child call wins.

The test module owns its own module-level composite handlers so
``import_handler`` can round-trip them; the dispatcher's
``handler_ref`` column points at the dotted path resolved against
:func:`importlib.import_module`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
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
    CompositeRecursionLimitExceeded,
    DispatchChild,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import parent_audit_id_var
from meho_backplane.operations.composite import (
    COMPOSITE_DEPTH_TOP_LEVEL,
    composite_depth_var,
    get_dispatch_child,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / fixtures
# ---------------------------------------------------------------------------


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
    """Deterministic embedding stub so ``register_typed_operation`` doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = "op-composite",
    tenant_id: UUID | None = None,
) -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Composite Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id or UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for resolver lookups."""

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
        name: str = "test-target",
        host: str = "test.example.com",
        port: int = 443,
        auth_model: str = "shared_service_account",
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=version)
        self.preferred_impl_id: str | None = None
        self.id: UUID = target_id or uuid.uuid4()
        self.name = name
        self.host = host
        self.port = port
        self.auth_model = auth_model


class _NoOpVaultConnector(Connector):
    """Resolver-satisfying connector class -- never actually called."""

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
# Module-level handlers used as test fixtures
# ---------------------------------------------------------------------------


async def _child_handler(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Plain typed child handler -- echoes params for assertion."""
    return {"echo": params, "target_id": str(getattr(target, "id", None))}


async def _two_child_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Composite that dispatches two children sequentially."""
    a = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        params={"path": params.get("path_a", "/a")},
    )
    b = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        params={"path": params.get("path_b", "/b")},
    )
    return {"a": a.status, "b": b.status}


async def _depth_two_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Composite that recurses into another composite (depth-2)."""
    inner = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.composite.inner",
        params=params,
    )
    return {"inner_status": inner.status}


async def _inner_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Inner composite that itself dispatches one typed child."""
    child = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        params={"path": params.get("path", "/inner")},
    )
    return {"child_status": child.status}


async def _self_recursive_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Composite that calls itself unconditionally -- exercises the depth cap."""
    inner = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.composite.self_recursive",
        params=params,
    )
    return {"inner_status": inner.status}


async def _override_target_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: DispatchChild,
) -> dict[str, Any]:
    """Composite that overrides the child ``target`` on its sub-call."""
    alt_target = _FakeTarget(product="vault", target_id=uuid.UUID(params["alt_target_id"]))
    child = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        params={"hello": "world"},
        target=alt_target,
    )
    return {"child_result": child.result}


# ---------------------------------------------------------------------------
# Helper -- insert a composite descriptor row directly (T4 only handles typed)
# ---------------------------------------------------------------------------


async def _insert_composite_descriptor(
    *,
    session: AsyncSession,
    op_id: str,
    handler_ref: str,
    embedding: list[float],
) -> None:
    """Add one ``source_kind='composite'`` row pointing at *handler_ref*."""
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            source_kind="composite",
            method=None,
            path=None,
            handler_ref=handler_ref,
            summary=f"Composite {op_id}.",
            description=f"Composite test descriptor for {op_id}.",
            tags=[],
            parameter_schema={"type": "object"},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=embedding,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Protocol / factory unit tests
# ---------------------------------------------------------------------------


def test_dispatch_child_is_runtime_protocol() -> None:
    """:class:`DispatchChild` is a :class:`typing.Protocol` subclass."""
    # Protocols inherit from typing.Protocol; the membership check
    # works against typing._ProtocolMeta on Python 3.12.
    from typing import Protocol

    assert issubclass(type(DispatchChild), type(Protocol))


def test_composite_depth_var_defaults_to_top_level() -> None:
    """Top-level dispatches see :data:`COMPOSITE_DEPTH_TOP_LEVEL`."""
    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL
    assert COMPOSITE_DEPTH_TOP_LEVEL == 0


@pytest.mark.asyncio
async def test_get_dispatch_child_returns_callable_bound_to_parent_context() -> None:
    """The factory returns a callable that wraps *dispatch* + binds contextvars."""
    parent_id = uuid.uuid4()
    seen: list[dict[str, Any]] = []

    async def _fake_dispatch(
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult:
        # Snapshot the contextvars mid-dispatch so we can assert the
        # binding the factory installed.
        seen.append(
            {
                "operator_sub": operator.sub,
                "target_id": getattr(target, "id", None),
                "connector_id": connector_id,
                "op_id": op_id,
                "params": params,
                "parent_audit_id": parent_audit_id_var.get(),
                "depth": composite_depth_var.get(),
            }
        )
        return OperationResult(
            status="ok",
            op_id=op_id,
            result={"ok": True},
            duration_ms=0.0,
        )

    operator = _make_operator()
    target = _FakeTarget()
    child = get_dispatch_child(
        dispatch=_fake_dispatch,
        parent_operator=operator,
        parent_target=target,
        parent_audit_id=parent_id,
        parent_op_id="vault.composite.test",
    )

    result = await child(
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        params={"path": "/"},
    )
    assert result.status == "ok"
    assert seen[0]["operator_sub"] == operator.sub
    assert seen[0]["target_id"] == target.id
    assert seen[0]["parent_audit_id"] == parent_id
    assert seen[0]["depth"] == 1  # incremented for the recursive call
    # Outside the recursive call the contextvars are reset.
    assert parent_audit_id_var.get() is None
    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL


@pytest.mark.asyncio
async def test_get_dispatch_child_resets_contextvars_on_exception() -> None:
    """Contextvars reset even if the recursive dispatch raises."""

    async def _raising_dispatch(**_kwargs: Any) -> OperationResult:
        raise RuntimeError("boom")

    operator = _make_operator()
    target = _FakeTarget()
    child = get_dispatch_child(
        dispatch=_raising_dispatch,
        parent_operator=operator,
        parent_target=target,
        parent_audit_id=uuid.uuid4(),
        parent_op_id="vault.composite.test",
    )
    with pytest.raises(RuntimeError, match="boom"):
        await child(connector_id="vault-1.x", op_id="vault.kv.list", params={})
    # Contextvars must be clean after the exception so a sibling
    # sub-call in the same task isn't poisoned.
    assert parent_audit_id_var.get() is None
    assert composite_depth_var.get() == COMPOSITE_DEPTH_TOP_LEVEL


@pytest.mark.asyncio
async def test_get_dispatch_child_raises_when_depth_cap_breached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempted depth > cap raises :class:`CompositeRecursionLimitExceeded`."""
    monkeypatch.setenv("COMPOSITE_MAX_DEPTH", "2")
    get_settings.cache_clear()

    async def _fake_dispatch(**_kwargs: Any) -> OperationResult:  # pragma: no cover
        raise AssertionError("dispatch should not fire on over-depth")

    operator = _make_operator()
    target = _FakeTarget()
    child = get_dispatch_child(
        dispatch=_fake_dispatch,
        parent_operator=operator,
        parent_target=target,
        parent_audit_id=uuid.uuid4(),
        parent_op_id="vault.composite.test",
    )

    # Pre-set the depth to the cap so the next call breaches it.
    token = composite_depth_var.set(2)
    try:
        with pytest.raises(CompositeRecursionLimitExceeded) as excinfo:
            await child(connector_id="vault-1.x", op_id="vault.kv.list", params={})
        exc = excinfo.value
        assert exc.attempted_depth == 3
        assert exc.max_depth == 2
        assert exc.op_id_chain[-2:] == ("vault.composite.test", "vault.kv.list")
    finally:
        composite_depth_var.reset(token)


# ---------------------------------------------------------------------------
# End-to-end integration: composite dispatched through the real dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_composite_two_children_produces_audit_tree(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Composite with two child dispatches -> three audit rows + correct linkage."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list",
        handler=_child_handler,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.two_children",
        handler_ref="tests.test_operations_composite._two_child_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.composite.two_children",
        target=target,
        params={"path_a": "/foo", "path_b": "/bar"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["a"] == "ok"
    assert result.result["b"] == "ok"

    # Three audit rows total.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_({"vault.composite.two_children", "vault.kv.list"})
                    )
                )
            )
            .scalars()
            .all()
        )
    parent_rows = [r for r in rows if r.path == "vault.composite.two_children"]
    child_rows = [r for r in rows if r.path == "vault.kv.list"]
    assert len(parent_rows) == 1
    assert len(child_rows) == 2
    parent = parent_rows[0]
    # Parent has no parent of its own.
    assert parent.parent_audit_id is None
    # Both children link to the parent on the real column.
    for child in child_rows:
        assert child.parent_audit_id == parent.id

    # Audit-tree SQL query shape: SELECT id, parent_audit_id, op_id FROM
    # audit_log WHERE id IN (...) reconstructs the tree by parent_audit_id.
    audit_ids = {parent.id, *(c.id for c in child_rows)}
    async with sessionmaker() as fresh:
        tree_rows = (
            await fresh.execute(
                select(
                    AuditLog.id,
                    AuditLog.parent_audit_id,
                    AuditLog.path,
                ).where(AuditLog.id.in_(audit_ids))
            )
        ).all()
    tree = {row.id: row for row in tree_rows}
    assert tree[parent.id].parent_audit_id is None
    for child in child_rows:
        assert tree[child.id].parent_audit_id == parent.id


@pytest.mark.asyncio
async def test_dispatch_composite_nested_inside_composite(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Composite -> composite -> typed child: nested linkage and depth-2 succeeds."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list",
        handler=_child_handler,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.outer",
        handler_ref="tests.test_operations_composite._depth_two_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.inner",
        handler_ref="tests.test_operations_composite._inner_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.composite.outer",
        target=target,
        params={"path": "/nested"},
    )
    assert result.status == "ok", result.error

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_(
                            {
                                "vault.composite.outer",
                                "vault.composite.inner",
                                "vault.kv.list",
                            }
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    by_path = {r.path: r for r in rows}
    outer = by_path["vault.composite.outer"]
    inner = by_path["vault.composite.inner"]
    child = by_path["vault.kv.list"]
    # Tree shape: outer -> inner -> child.
    assert outer.parent_audit_id is None
    assert inner.parent_audit_id == outer.id
    assert child.parent_audit_id == inner.id


@pytest.mark.asyncio
async def test_dispatch_composite_recursion_cap_blocks_overdepth(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-depth dispatch -> ``connector_error`` + no audit row for rejected sub-op."""
    monkeypatch.setenv("COMPOSITE_MAX_DEPTH", "2")
    get_settings.cache_clear()

    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.self_recursive",
        handler_ref="tests.test_operations_composite._self_recursive_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.composite.self_recursive",
        target=target,
        params={},
    )
    # Trace under cap=2:
    #  * depth-0 dispatch -> handler runs (depth-var = 0)
    #  * handler calls dispatch_child -> pre-increment 0+1=1, ok, recurse
    #  * depth-1 dispatch -> handler runs (depth-var = 1)
    #  * handler calls dispatch_child -> pre-increment 1+1=2, ok, recurse
    #  * depth-2 dispatch -> handler runs (depth-var = 2)
    #  * handler calls dispatch_child -> pre-increment 2+1=3 > 2 -> RAISE
    #  * depth-2's _execute_and_audit catches the handler exception,
    #    writes its audit row with ``result_status='error'``, returns
    #    a ``connector_error`` OperationResult.
    #  * depth-1's handler receives that error result (it does NOT
    #    re-raise), records it, returns ``{"inner_status": "error"}``,
    #    depth-1 dispatch wraps that as a successful result (handler
    #    didn't raise) -> audit row with ``result_status='ok'``.
    #  * Same shape for depth-0: handler returns successfully,
    #    audit row ``result_status='ok'``, top-level result is ok.
    # So the over-depth call IS rejected before any audit row is
    # written for it (only three audit rows total, not four), and the
    # deepest composite carries the CompositeRecursionLimitExceeded
    # exception_class on its audit/result.
    assert result.status == "ok", result.error
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(AuditLog.path == "vault.composite.self_recursive")
                )
            )
            .scalars()
            .all()
        )
    # Exactly three audit rows -- one per dispatch that fired.
    # The would-be fourth (depth-3) was rejected before its dispatch
    # ran, so no row was written for it.
    assert len(rows) == 3
    error_rows = [r for r in rows if r.payload["result_status"] == "error"]
    ok_rows = [r for r in rows if r.payload["result_status"] == "ok"]
    assert len(error_rows) == 1
    assert len(ok_rows) == 2
    # Audit-tree shape under the cap:
    #   depth-0 (ok, parent=None)
    #     -> depth-1 (ok, parent=depth-0)
    #          -> depth-2 (error, parent=depth-1)
    by_id = {r.id: r for r in rows}
    depth_0 = next(r for r in rows if r.parent_audit_id is None)
    depth_1 = next(r for r in rows if r.parent_audit_id == depth_0.id)
    depth_2 = next(r for r in rows if r.parent_audit_id == depth_1.id)
    assert by_id[depth_2.id].payload["result_status"] == "error"
    assert by_id[depth_0.id].payload["result_status"] == "ok"
    assert by_id[depth_1.id].payload["result_status"] == "ok"


@pytest.mark.asyncio
async def test_dispatch_composite_default_max_depth_is_eight() -> None:
    """The default :attr:`Settings.composite_max_depth` is 8."""
    # No env override -> default value matches the documented contract.
    settings = get_settings()
    assert settings.composite_max_depth == 8


@pytest.mark.asyncio
async def test_dispatch_composite_child_inherits_target_unless_overridden(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Child sub-call defaults to parent target; explicit ``target=`` wins."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list",
        handler=_child_handler,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        embedding_service=stub_embedding_service,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.override_target",
        handler_ref="tests.test_operations_composite._override_target_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    operator = _make_operator()
    parent_target = _FakeTarget(product="vault")
    alt_target_id = uuid.uuid4()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.composite.override_target",
        target=parent_target,
        params={"alt_target_id": str(alt_target_id)},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    child_result = result.result["child_result"]
    assert isinstance(child_result, dict)
    # The child handler echoed its target.id; the override wins over
    # the parent's target.
    assert child_result["target_id"] == str(alt_target_id)
