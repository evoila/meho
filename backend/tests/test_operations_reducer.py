# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.reducer`.

Coverage matrix (G0.6-T6 / Task #397 acceptance criteria):

* :class:`Reducer` Protocol is structurally satisfied by
  :class:`PassThroughReducer` and by ad-hoc test doubles.
* :class:`PassThroughReducer` returns *(payload, None)* verbatim for
  every payload shape connectors emit (``dict``, ``list``, ``None``,
  scalars-via-wrap).
* :class:`PassThroughReducer.reduce` accepts ``schema`` and ``context``
  as optional and ignores both — no side effects, no mutation.
* :class:`ResultHandle` is frozen (Pydantic v2 :class:`ConfigDict` +
  ``frozen=True``) — field reassignment raises
  :class:`pydantic.ValidationError`.
* :class:`ResultHandle` round-trips through ``model_dump_json`` /
  ``model_validate_json`` losslessly.
* :class:`OperationResult.handle` accepts a :class:`ResultHandle`,
  defaults to ``None``, and round-trips alongside the rest of the
  fields.
* The dispatcher's pass-through path leaves :attr:`OperationResult.handle`
  ``None``; a swapped-in reducer that emits a handle propagates it onto
  the returned :class:`OperationResult`.

The dispatcher-integration assertions stand up the same in-memory wiring
:mod:`test_operations_dispatcher` uses, but only exercise the reducer
seam so a regression there fails this file first rather than the much
larger dispatcher suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult, ResultHandle
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.operations import (
    PassThroughReducer,
    Reducer,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / fixtures (mirror test_operations_dispatcher.py)
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
    """Reset dispatcher caches + connector registry + reducer slot per test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    # Always restore the pass-through default — tests that swap a reducer
    # in must not leak across files.
    set_default_reducer(PassThroughReducer())


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub matching the dispatcher test's contract."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace :func:`publish_event` with a recording stub.

    Mirrors the dispatcher-test fixture: the audit helper invokes
    ``publish_event`` via the imported reference inside
    :mod:`meho_backplane.operations._audit`; patching the module's
    attribute is sufficient -- no need to swap the broadcast package's
    bind. Tests need this to be present so the dispatcher's broadcast
    leg doesn't fail open against a missing Valkey broker.
    """
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeFingerprint:
    """Duck-typed fingerprint for tests that don't care about full identity."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target the resolver / dispatcher reads from.

    Mirrors the parts of :class:`~meho_backplane.db.models.Target` the
    substrate actually touches: ``product``, ``fingerprint.version``,
    ``preferred_impl_id``, plus ``id`` / ``name`` / ``host`` / ``port`` /
    ``auth_model`` for downstream consumers. Same shape the dispatcher
    test uses so the reducer-seam tests exercise the same code paths.
    """

    def __init__(
        self,
        *,
        product: str = "test-product",
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
        self.id: UUID = target_id or uuid4()
        self.name = name
        self.host = host
        self.port = port
        self.auth_model = auth_model


class _NoOpVaultConnector(Connector):
    """Connector stub that satisfies the resolver without doing any I/O."""

    product = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        from datetime import UTC, datetime

        return FingerprintResult(
            vendor="hashicorp",
            product="vault",
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="stub",
        )

    async def probe(self, target: Any) -> ProbeResult:
        from datetime import UTC, datetime

        return ProbeResult(ok=True, probed_at=datetime.now(UTC))

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:
        # Resolver-only stub — `dispatch_typed` calls module-level handlers,
        # not `Connector.execute`. The method exists to keep the ABC happy.
        raise NotImplementedError


async def _module_handler_target_params_only(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed-op handler echoing params — keeps the dispatcher's reducer-seam
    test independent of connector internals."""
    return {"echo": params, "product": getattr(target, "product", None)}


def _make_operator(*, sub: str = "oper-1") -> Operator:
    """Build a minimal :class:`Operator` for a dispatch call."""
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Protocol structural conformance
# ---------------------------------------------------------------------------


def test_pass_through_reducer_satisfies_reducer_protocol() -> None:
    """:class:`PassThroughReducer` is recognised as a :class:`Reducer`.

    :func:`typing.runtime_checkable` + ``isinstance`` is documented in
    PEP 544 to perform a structural check on async methods by name only
    (not by signature). The assertion catches the regression where T6
    accidentally drops the ``reduce`` method or renames it.
    """
    reducer = PassThroughReducer()
    assert isinstance(reducer, Reducer)


def test_arbitrary_class_with_reduce_satisfies_reducer_protocol() -> None:
    """Duck typing: any class with an async ``reduce`` method passes."""

    class _LooselyShapedReducer:
        async def reduce(
            self,
            payload: Any,
            schema: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
        ) -> tuple[Any, ResultHandle | None]:
            return payload, None

    assert isinstance(_LooselyShapedReducer(), Reducer)


def test_class_missing_reduce_method_fails_protocol_check() -> None:
    """A class with no ``reduce`` attribute fails ``isinstance(.., Reducer)``."""

    class _NotAReducer:
        async def some_other_method(self) -> None:
            return None

    assert not isinstance(_NotAReducer(), Reducer)


# ---------------------------------------------------------------------------
# PassThroughReducer behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_through_returns_dict_payload_verbatim() -> None:
    reducer = PassThroughReducer()
    payload = {"path": "/secret", "data": {"k": "v"}}
    summary, handle = await reducer.reduce(payload, None)
    assert summary is payload  # identity preserved
    assert handle is None


@pytest.mark.asyncio
async def test_pass_through_returns_list_payload_verbatim() -> None:
    reducer = PassThroughReducer()
    payload = [{"row": 1}, {"row": 2}, {"row": 3}]
    summary, handle = await reducer.reduce(payload, None)
    assert summary is payload
    assert handle is None


@pytest.mark.asyncio
async def test_pass_through_handles_none_payload() -> None:
    """Connectors returning a JSON ``null`` mustn't crash the seam."""
    reducer = PassThroughReducer()
    summary, handle = await reducer.reduce(None, None)
    assert summary is None
    assert handle is None


@pytest.mark.asyncio
async def test_pass_through_ignores_schema_and_context() -> None:
    """The default reducer reads neither; presence/absence is irrelevant."""
    reducer = PassThroughReducer()
    payload = {"a": 1}
    summary_no_args, _ = await reducer.reduce(payload)
    summary_with_schema, _ = await reducer.reduce(
        payload, {"type": "object", "properties": {"a": {"type": "integer"}}}
    )
    summary_with_context, _ = await reducer.reduce(
        payload, None, {"op_id": "vault.kv.read", "operator_sub": "oper-1"}
    )
    assert summary_no_args is payload
    assert summary_with_schema is payload
    assert summary_with_context is payload


# ---------------------------------------------------------------------------
# ResultHandle Pydantic model
# ---------------------------------------------------------------------------


def test_result_handle_construction_with_all_fields() -> None:
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="# 100 rows",
        schema_={"type": "array"},
        total_rows=100,
        sample_rows=[{"k": "v"}],
        ttl_seconds=3600,
    )
    assert handle.total_rows == 100
    assert handle.summary_md == "# 100 rows"
    assert handle.schema_ == {"type": "array"}


def test_result_handle_optional_fields_default_to_none() -> None:
    """``total_rows`` / ``sample_rows`` are optional; ``ttl_seconds`` is required."""
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="empty",
        schema_={},
        ttl_seconds=60,
    )
    assert handle.total_rows is None
    assert handle.sample_rows is None


def test_result_handle_is_frozen() -> None:
    """``ConfigDict(frozen=True)`` — field reassignment raises."""
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="x",
        schema_={},
        ttl_seconds=10,
    )
    with pytest.raises(ValidationError):
        handle.summary_md = "tampered"  # type: ignore[misc]


def test_result_handle_json_round_trip_lossless() -> None:
    """``model_dump_json`` → ``model_validate_json`` preserves every field."""
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="# 50 rows",
        schema_={"type": "array", "items": {"type": "object"}},
        total_rows=50,
        sample_rows=[{"k": 1}, {"k": 2}],
        ttl_seconds=7200,
    )
    payload = handle.model_dump_json()
    revived = ResultHandle.model_validate_json(payload)
    assert revived == handle


def test_result_handle_serialises_schema_under_trailing_underscore() -> None:
    """The on-the-wire field name is ``schema_`` (not ``schema``).

    Pydantic v2 emits the Python attribute name verbatim when no
    ``alias`` is set; the trailing underscore avoids collision with
    Pydantic's deprecated :meth:`BaseModel.schema` method and stays
    consistent between the wire payload and the Python attribute.
    """
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="x",
        schema_={"type": "object"},
        ttl_seconds=10,
    )
    dumped = handle.model_dump(mode="json")
    assert "schema_" in dumped
    assert dumped["schema_"] == {"type": "object"}


# ---------------------------------------------------------------------------
# OperationResult.handle field
# ---------------------------------------------------------------------------


def test_operation_result_handle_defaults_to_none() -> None:
    """Existing callers (every connector pre-T6) keep their contract."""
    result = OperationResult(
        status="ok",
        op_id="vault.kv.read",
        result={"value": "secret"},
        duration_ms=12.3,
    )
    assert result.handle is None


def test_operation_result_accepts_handle() -> None:
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="# 1000 rows",
        schema_={"type": "array"},
        total_rows=1000,
        ttl_seconds=3600,
    )
    result = OperationResult(
        status="ok",
        op_id="vault.kv.list",
        result={"summary": "see handle"},
        duration_ms=42.0,
        handle=handle,
    )
    assert result.handle is handle


def test_operation_result_with_handle_round_trips_through_json() -> None:
    """End-to-end serialisation — the field survives the FastAPI / MCP wire."""
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="# rows",
        schema_={"type": "array"},
        total_rows=10,
        sample_rows=[{"a": 1}],
        ttl_seconds=600,
    )
    original = OperationResult(
        status="ok",
        op_id="vault.kv.list",
        result={"summary": "x"},
        duration_ms=1.0,
        handle=handle,
    )
    revived = OperationResult.model_validate_json(original.model_dump_json())
    assert revived.handle == handle


# ---------------------------------------------------------------------------
# Dispatcher integration — reducer seam end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_default_reducer_leaves_handle_none(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """v0.2 default path: dispatch returns ``handle=None``."""
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
        handler=_module_handler_target_params_only,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        target=_FakeTarget(product="vault"),
        params={"path": "/secret"},
    )

    assert result.status == "ok"
    assert result.handle is None


@pytest.mark.asyncio
async def test_dispatch_propagates_reducer_handle_onto_operation_result(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Mock 'would-reduce' reducer: the produced handle lands on the result.

    Exercises the seam T6's real reducer will rely on — the dispatcher
    must read ``handle`` off the reducer's return tuple and propagate it
    onto :class:`OperationResult.handle`, not stash it in ``extras``.
    """
    forged_handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="# 250 secrets matched",
        schema_={"type": "array"},
        total_rows=250,
        sample_rows=[{"path": "a"}, {"path": "b"}],
        ttl_seconds=300,
    )

    class _ReducingStub:
        async def reduce(
            self,
            payload: Any,
            schema: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
        ) -> tuple[Any, ResultHandle | None]:
            return {"summary": "see handle"}, forged_handle

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
        handler=_module_handler_target_params_only,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    set_default_reducer(_ReducingStub())
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(product="vault"),
            params={"path": "/secret"},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "ok"
    assert result.handle == forged_handle
    # Reduced payload flows through ``result.result``; the raw handler
    # output (``{"echo": ...}``) is replaced by the reducer's summary.
    assert result.result == {"summary": "see handle"}
    # Legacy stash path is gone — T6 promoted the handle to a first-class
    # field on :class:`OperationResult`, no ``result_handle`` in ``extras``.
    assert "result_handle" not in result.extras


@pytest.mark.asyncio
async def test_dispatch_passes_context_to_reducer(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """The reducer sees ``op_id`` / ``operator_sub`` / ``target_id`` in context.

    Real reducers will use this for logging and per-op routing
    decisions; the dispatcher must populate it. Asserted on the
    presence of the documented keys, not exact values, so the test
    survives keys being added later.
    """
    seen_context: list[dict[str, Any]] = []

    class _ContextCapturingReducer(PassThroughReducer):
        async def reduce(
            self,
            payload: Any,
            schema: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
        ) -> tuple[Any, ResultHandle | None]:
            if context is not None:
                seen_context.append(context)
            return await super().reduce(payload, schema, context)

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
        handler=_module_handler_target_params_only,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    target_id = uuid4()
    set_default_reducer(_ContextCapturingReducer())
    try:
        await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(product="vault", target_id=target_id),
            params={"path": "/secret"},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert len(seen_context) == 1
    ctx = seen_context[0]
    assert ctx["op_id"] == "vault.kv.list"
    assert ctx["operator_sub"] == "oper-1"
    assert ctx["target_id"] == str(target_id)
    assert ctx["source_kind"] == "typed"
    # ``audit_id`` is a uuid4 the dispatcher generates per-call — assert
    # it's present and parseable rather than pinning a value.
    assert UUID(ctx["audit_id"])


@pytest.mark.asyncio
async def test_dispatch_returns_connector_error_when_reducer_raises(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Reducer exception lands as ``connector_error`` — dispatcher never raises.

    The dispatcher's module docstring contracts "never raises". v0.2's
    pass-through reducer can't raise, but ``set_default_reducer(...)``
    invites swappable real reducers (MinIO/S3 I/O, schema validation)
    that will. A reducer that raises must be caught inside ``dispatch()``,
    converted to a structured ``connector_error`` :class:`OperationResult`,
    audited, and broadcast — same shape the handler-call exception path
    produces.
    """

    class _ExplodingReducer:
        async def reduce(
            self,
            payload: Any,
            schema: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
        ) -> tuple[Any, ResultHandle | None]:
            raise RuntimeError("simulated reducer explosion")

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
        handler=_module_handler_target_params_only,
        summary="List secrets.",
        description="List secrets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    set_default_reducer(_ExplodingReducer())
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(product="vault"),
            params={"path": "/secret"},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "RuntimeError"
    # Audit row + broadcast event still fired — the failure is observable.
    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


# ---------------------------------------------------------------------------
# ResultHandle nested immutability (M1)
# ---------------------------------------------------------------------------


def test_result_handle_schema_field_is_deeply_immutable() -> None:
    """``ResultHandle.schema_`` is wrapped in :class:`MappingProxyType`.

    Pydantic v2's ``frozen=True`` blocks field reassignment but not nested
    mutation. The :meth:`_freeze_nested` model validator wraps the
    ``schema_`` dict in a :class:`types.MappingProxyType` so callers can't
    edit the schema after the reducer hands the handle back — matches the
    sibling :attr:`FingerprintResult.extras` /
    :attr:`OperationResult.extras` pattern in the same file.
    """
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="x",
        schema_={"type": "object", "properties": {"a": {"type": "integer"}}},
        ttl_seconds=10,
    )
    with pytest.raises(TypeError):
        handle.schema_["type"] = "array"  # type: ignore[index]
    with pytest.raises(TypeError):
        del handle.schema_["type"]  # type: ignore[attr-defined]


def test_result_handle_sample_rows_field_is_deeply_immutable() -> None:
    """``sample_rows`` is stored as a tuple of :class:`MappingProxyType`.

    Tuples block append/insert/remove at the container level; each row is
    wrapped in :class:`MappingProxyType` so per-row mutation also raises.
    """
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="x",
        schema_={"type": "array"},
        sample_rows=[{"k": 1}, {"k": 2}],
        ttl_seconds=10,
    )
    # Container is a tuple — no append/remove.
    assert isinstance(handle.sample_rows, tuple)
    # Per-row mutation raises.
    assert handle.sample_rows is not None  # narrow for type checker
    with pytest.raises(TypeError):
        handle.sample_rows[0]["k"] = 99  # type: ignore[index]


def test_result_handle_input_dict_is_not_aliased() -> None:
    """Mutating the caller's input dict after construction doesn't leak in.

    The :meth:`_freeze_nested` validator copies via ``dict(self.schema_)``
    before wrapping, so the wrapped mapping is independent of the caller's
    original dict.
    """
    schema_input: dict[str, Any] = {"type": "object"}
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="x",
        schema_=schema_input,
        ttl_seconds=10,
    )
    schema_input["type"] = "array"
    assert handle.schema_["type"] == "object"


def test_result_handle_round_trips_after_freezing() -> None:
    """``model_dump_json`` / ``model_validate_json`` still works after wrapping.

    :class:`MappingProxyType` is not JSON-serialisable by default; the
    paired ``@field_serializer`` mirrors the ``_serialize_extras`` pattern
    and converts it back to a plain ``dict`` / ``list`` on the wire.
    """
    handle = ResultHandle(
        handle_id=uuid4(),
        summary_md="# 50 rows",
        schema_={"type": "array", "items": {"type": "object"}},
        total_rows=50,
        sample_rows=[{"k": 1}, {"k": 2}],
        ttl_seconds=7200,
    )
    payload = handle.model_dump_json()
    revived = ResultHandle.model_validate_json(payload)
    assert revived == handle
    assert dict(revived.schema_) == {"type": "array", "items": {"type": "object"}}
    assert revived.sample_rows is not None
    assert [dict(r) for r in revived.sample_rows] == [{"k": 1}, {"k": 2}]
