# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.dispatcher`.

Coverage matrix (G0.6-T5 / Task #396 acceptance criteria):

* ``dispatch()`` signature -- keyword-only ``operator`` /
  ``connector_id`` / ``op_id`` / ``target`` / ``params``.
* Lookup miss -> ``OperationResult(status='error',
  error='unknown_op: <id>', extras={'known_op_count': int})``.
* Schema validation miss -> ``OperationResult(status='error',
  error='invalid_params: …', extras={'validation_errors': [...]})``.
* Resolver miss -> ``OperationResult(status='error',
  error='no_connector: …', extras={'product', 'version'})``.
* Typed dispatch -- ``register_typed_operation()``-driven path:
  the handler runs, its return value lands as ``result.result``.
* Ingested dispatch -- the dispatcher builds the right httpx
  request (mocked via :meth:`HttpConnector._request_json`).
* Composite dispatch -- the handler receives a callable ``dispatch``
  and uses it to recurse; the recursive call lands a separate audit
  row with ``parent_audit_id`` set on the payload.
* Audit row written synchronously: one row per dispatch with the
  resolved ``op_id`` / ``target_id`` / ``params_hash`` /
  ``result_status``.
* Broadcast event emitted via the shipped :func:`publish_event` hook;
  the event carries the right ``op_class`` + redacted payload.
* Pass-through reducer: the dispatcher invokes the reducer; the v0.2
  default returns the response unchanged + a ``None`` handle.
* Handler-import error -> ``handler_unreachable`` error code.
* Policy gate -- ``requires_approval=True`` -> ``denied`` with the
  default-allow policy in v0.2.

The audit + broadcast assertions read the actual ``audit_log`` rows
written by the dispatcher (the conftest autouse fixture runs the
Alembic migration so the table exists). The broadcast publisher is
patched to a recording stub so tests can assert on the
:class:`BroadcastEvent` without standing up a Valkey container.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    import_handler,
    register_typed_operation,
    reset_dispatcher_caches,
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
    """Replace :func:`publish_event` with a recording stub.

    The audit helper invokes ``publish_event`` via the imported
    reference inside :mod:`meho_backplane.operations._audit`; patching
    the module's attribute is sufficient -- no need to swap the
    broadcast package's bind.
    """
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
    sub: str = "op-test",
    tenant_id: UUID | None = None,
    principal_kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip.

    Defaults to a human (``PrincipalKind.USER``) so the v0.2 default-allow
    contract applies; pass ``principal_kind=PrincipalKind.AGENT`` to
    exercise the G11.2-T3 per-(principal, op, target) resolver path.
    """
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id or UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for tests that don't care about full identity."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target the resolver / dispatcher reads from.

    Mirrors the parts of :class:`~meho_backplane.db.models.Target` that
    the substrate touches: ``product``, ``fingerprint.version``,
    ``preferred_impl_id``, plus ``id`` / ``name`` / ``host`` / ``port`` /
    ``auth_model`` for downstream consumers.
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
        self.id: UUID = target_id or uuid.uuid4()
        self.name = name
        self.host = host
        self.port = port
        self.auth_model = auth_model


# ---------------------------------------------------------------------------
# Module-level handlers used as test fixtures. Defined at module scope so
# ``import_handler`` can round-trip them.
# ---------------------------------------------------------------------------


async def _module_handler_returning_dict(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler returning a dict result."""
    return {"echo": params, "target_id": str(getattr(target, "id", None))}


async def _module_handler_target_params_only(
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler without the ``operator`` parameter."""
    return {"echo": params}


async def _module_composite_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: Any,
) -> dict[str, Any]:
    """Composite handler that recurses into ``vault.kv.list`` then returns.

    Uses the ``dispatch_child`` keyword (per T7 #398's
    :class:`~meho_backplane.operations.composite.DispatchChild`
    Protocol); the callable wraps :func:`dispatch` and handles
    audit-tree linkage + bounded-recursion automatically.
    """
    child_result = await dispatch_child(
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        params={"path": params.get("path", "/")},
    )
    return {"parent": "ok", "child_status": child_result.status}


async def _module_handler_raises(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler that always raises -- exercises the connector_error path."""
    raise RuntimeError("simulated handler explosion")


# ---------------------------------------------------------------------------
# import_handler unit tests
# ---------------------------------------------------------------------------


def test_import_handler_resolves_module_level_function() -> None:
    """Module-level function: dotted path resolves to the same callable."""
    ref = "tests.test_operations_dispatcher._module_handler_returning_dict"
    resolved = import_handler(ref)
    assert resolved is _module_handler_returning_dict


def test_import_handler_resolves_bound_class_method() -> None:
    """``module.Class.method`` resolves via the importlib + getattr walk."""

    class _Cls:
        async def m(self) -> dict[str, str]:  # pragma: no cover - method used as identity
            return {}

    # Stash a reference on this module so importlib can walk to it via
    # the dotted path the dispatcher uses.
    import sys

    sys.modules[__name__]._TestCls = _Cls  # type: ignore[attr-defined]
    try:
        resolved = import_handler("tests.test_operations_dispatcher._TestCls.m")
        assert resolved is _Cls.m
    finally:
        del sys.modules[__name__]._TestCls  # type: ignore[attr-defined]


def test_import_handler_raises_importerror_for_missing_module() -> None:
    """No prefix imports -> raise :class:`ImportError`."""
    with pytest.raises(ImportError):
        import_handler("definitely.not.a.real.module.path")


def test_import_handler_raises_importerror_for_missing_attr() -> None:
    """Module imports but symbol missing -> raise :class:`ImportError`."""
    with pytest.raises(ImportError):
        import_handler("tests.test_operations_dispatcher.does_not_exist")


def test_import_handler_raises_typeerror_for_non_callable() -> None:
    """Resolved symbol is non-callable -> raise :class:`TypeError`."""
    import sys

    sys.modules[__name__]._not_callable = 42  # type: ignore[attr-defined]
    try:
        with pytest.raises(TypeError):
            import_handler("tests.test_operations_dispatcher._not_callable")
    finally:
        del sys.modules[__name__]._not_callable  # type: ignore[attr-defined]


def test_import_handler_caches_resolved_handler() -> None:
    """Second call returns the same object without re-walking importlib."""
    ref = "tests.test_operations_dispatcher._module_handler_returning_dict"
    first = import_handler(ref)
    # Mutating the module attribute should not affect the cached value.
    import sys

    original = sys.modules[__name__]._module_handler_returning_dict  # type: ignore[attr-defined]
    sys.modules[__name__]._module_handler_returning_dict = None  # type: ignore[attr-defined]
    try:
        cached = import_handler(ref)
        assert cached is first
    finally:
        sys.modules[__name__]._module_handler_returning_dict = original  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lookup-miss path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_unknown_op_when_descriptor_missing(
    captured_events: list[BroadcastEvent],
) -> None:
    """No descriptor -> ``OperationResult.error`` starts with ``"unknown_op:"``."""
    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault",
        op_id="vault.kv.read.notpresent",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("unknown_op:")
    assert result.extras["error_code"] == "unknown_op"
    assert "known_op_count" in result.extras


# ---------------------------------------------------------------------------
# Param-validation path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_invalid_params_when_schema_violated(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Invalid params -> ``invalid_params`` error with ``validation_errors`` list."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_module_handler_returning_dict,
        summary="Read a KV v2 secret.",
        description="Read a secret.",
        parameter_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    # Missing required 'path' field.
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("invalid_params:")
    assert result.extras["error_code"] == "invalid_params"
    validation = result.extras["validation_errors"]
    assert isinstance(validation, list)
    assert any(err["validator"] == "required" for err in validation)


# ---------------------------------------------------------------------------
# Typed dispatch + audit + broadcast
# ---------------------------------------------------------------------------


class _NoOpVaultConnector(Connector):
    """Connector class used to satisfy resolver lookups in typed tests."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
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


@pytest.mark.asyncio
async def test_dispatch_typed_invokes_handler_and_returns_result(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Typed handler runs; its return value lands in ``result.result``."""
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
        op_id="vault.kv.read",
        handler=_module_handler_returning_dict,
        summary="Read a KV v2 secret.",
        description="Read a secret.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=target,
        params={"key": "value"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["echo"] == {"key": "value"}
    assert result.result["target_id"] == str(target.id)

    # Audit row written.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.kv.read")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.method == "DISPATCH"
    assert row.path == "vault.kv.read"
    assert row.status_code == 200
    assert row.operator_sub == operator.sub
    assert row.tenant_id == operator.tenant_id
    assert row.target_id == target.id
    assert row.payload["op_id"] == "vault.kv.read"
    assert row.payload["source_kind"] == "typed"
    assert row.payload["result_status"] == "ok"
    assert "params_hash" in row.payload

    # Broadcast event emitted.
    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.op_id == "vault.kv.read"
    assert event.op_class == "credential_read"  # vault.kv.read is in the allowlist
    assert event.result_status == "ok"
    assert event.tenant_id == operator.tenant_id
    assert event.audit_id == row.id
    # credential_read payload is aggregate-only -- the broadcast must
    # NOT contain the params.
    assert "params" not in event.payload


@pytest.mark.asyncio
async def test_dispatch_typed_handler_without_operator_param(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A typed handler with signature ``(target, params)`` is also accepted."""
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
        op_id="vault.kv.ping",
        handler=_module_handler_target_params_only,
        summary="Ping.",
        description="Ping.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.ping",
        target=target,
        params={"hello": "world"},
    )
    assert result.status == "ok", result.error
    assert result.result == {"echo": {"hello": "world"}}


# ---------------------------------------------------------------------------
# No-connector path (ingested only -- typed handlers don't require resolution)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_ingested_returns_no_connector_when_resolver_misses(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Ingested op with no matching connector -> ``no_connector`` error."""
    # Insert an ingested descriptor directly -- T4 only registers typed
    # rows, so we bypass it for the ingested test row.
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="ghost",
        version="1.0",
        impl_id="ghost",
        op_id="GET:/api/test",
        source_kind="ingested",
        method="GET",
        path="/api/test",
        handler_ref=None,
        summary="Ghost",
        description="Ghost endpoint with no connector registered.",
        tags=[],
        parameter_schema={},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=stub_embedding_service.encode_one.return_value,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()

    operator = _make_operator()
    target = _FakeTarget(product="ghost", version="1.0")

    result = await dispatch(
        operator=operator,
        connector_id="ghost-1.0",
        op_id="GET:/api/test",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("no_connector:")
    assert result.extras["error_code"] == "no_connector"
    # G0.14-T1 (#1142): the resolver's exception message rides under
    # extras.exception_message so operators see the diagnostic
    # without re-fetching the pod log.
    assert "exception_message" in result.extras
    assert "ghost" in result.extras["exception_message"]


# ---------------------------------------------------------------------------
# G0.14-T1 (#1142): typed/composite resolver miss must surface
# no_connector (matching the ingested branch), and the resolver's
# AmbiguousConnectorResolution must surface ambiguous_connector on
# both branches with the diagnostic message preserved verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_typed_returns_no_connector_when_resolver_misses(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Typed branch mirrors the ingested branch's resolver-miss label.

    Pre-G0.14-T1 (#1142) the typed/composite branch silently returned
    ``(None, None)`` on :exc:`NoMatchingConnector`, letting an unbound
    bound-method handler proceed to dispatch and re-surface as the
    misleading ``connector_error: RuntimeError: typed handler ...
    reached dispatch still unbound`` from :mod:`_branches`. After
    #1142 the typed branch returns the explicit ``no_connector``
    label (with the resolver's exception message in
    ``extras["exception_message"]``) so operators see the upstream
    diagnosis, matching ``signals 7`` and ``8`` of
    ``claude-rdc-hetzner-dc#697``.
    """
    # Register a typed op for a product, but DON'T register a
    # connector class for it — the resolver must miss.
    await register_typed_operation(
        product="phantom",
        version="1.x",
        impl_id="phantom",
        op_id="phantom.ping",
        handler=_module_handler_returning_dict,
        summary="Phantom op.",
        description="Phantom op for resolver-miss coverage.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="phantom")

    result = await dispatch(
        operator=operator,
        connector_id="phantom-1.x",
        op_id="phantom.ping",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("no_connector:")
    assert result.extras["error_code"] == "no_connector"
    # Resolver-message passthrough: the operator can read the resolver's
    # exception text right off the OperationResult.
    assert "exception_message" in result.extras
    assert "phantom" in result.extras["exception_message"]


@pytest.mark.asyncio
async def test_dispatch_typed_returns_ambiguous_when_resolver_cant_tiebreak(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Typed branch surfaces ``ambiguous_connector`` with the resolver's diagnostic message.

    Mirrors the live ``rdc-rke2-infra-k8s`` shape from
    ``claude-rdc-hetzner-dc#697`` signal 8: the Kubernetes connector
    self-registers under ``('k8s', '', '')`` (v1) AND ``('k8s',
    '1.x', 'k8s')`` (v2), the tie-break ladder treats both as
    equally specific (both have ``supported_version_range=None``),
    and :exc:`AmbiguousConnectorResolution` propagates. Pre-#1142
    that exception bubbled past the dispatcher as a bare 500; after
    #1142 it lands as a structured ``ambiguous_connector`` error
    with the message verbatim in ``extras["exception_message"]``.
    """

    class _ConflictA(Connector):
        product = "kclash"

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> OperationResult:
            raise NotImplementedError

    class _ConflictB(Connector):
        product = "kclash"

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> OperationResult:
            raise NotImplementedError

    # Two connectors for the same product, both with no version range
    # and equal priority (default 0) — the tie-break ladder ends
    # ambiguous after every step.
    from meho_backplane.connectors.registry import register_connector_v2 as _reg

    _reg(product="kclash", version="", impl_id="a", cls=_ConflictA)
    _reg(product="kclash", version="", impl_id="b", cls=_ConflictB)

    # Register a typed op against this product so the dispatcher
    # reaches the connector-resolution step on the typed branch.
    await register_typed_operation(
        product="kclash",
        version="1.x",
        impl_id="kclash",
        op_id="kclash.ping",
        handler=_module_handler_returning_dict,
        summary="Ping.",
        description="Ping.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="kclash")

    result = await dispatch(
        operator=operator,
        connector_id="kclash-1.x",
        op_id="kclash.ping",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("ambiguous_connector:")
    assert result.extras["error_code"] == "ambiguous_connector"
    # The resolver's exception text — naming the candidates and the
    # remediation step — rides verbatim. Operators see the same
    # diagnostic on the wire that the pod log emits.
    msg = result.extras["exception_message"]
    assert "preferred_impl_id" in msg
    assert "kclash" in msg


@pytest.mark.asyncio
async def test_dispatch_ingested_returns_ambiguous_when_resolver_cant_tiebreak(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Ingested branch catches ``AmbiguousConnectorResolution`` the same way.

    Both source-kind branches must mirror each other on the
    resolver's two diagnostic exception shapes — pre-#1142 the
    ingested branch caught ``NoMatchingConnector`` only, and any
    ambiguity from the resolver propagated past the dispatcher into
    FastAPI as a bare 500.
    """

    class _AmbA(Connector):
        product = "ghost"

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> OperationResult:
            raise NotImplementedError

    class _AmbB(Connector):
        product = "ghost"

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(  # type: ignore[override]
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> OperationResult:
            raise NotImplementedError

    from meho_backplane.connectors.registry import register_connector_v2 as _reg

    _reg(product="ghost", version="", impl_id="a", cls=_AmbA)
    _reg(product="ghost", version="", impl_id="b", cls=_AmbB)

    # Build an ingested descriptor that points at the ambiguous
    # product. Bypasses ``register_typed_operation`` because that
    # helper only writes typed/composite rows.
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="ghost",
        version="1.0",
        impl_id="ghost",
        op_id="GET:/api/probe",
        source_kind="ingested",
        method="GET",
        path="/api/probe",
        handler_ref=None,
        summary="Ghost probe.",
        description="Ghost probe.",
        tags=[],
        parameter_schema={},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=stub_embedding_service.encode_one.return_value,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()

    operator = _make_operator()
    target = _FakeTarget(product="ghost")

    result = await dispatch(
        operator=operator,
        connector_id="ghost-1.0",
        op_id="GET:/api/probe",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("ambiguous_connector:")
    assert result.extras["error_code"] == "ambiguous_connector"
    msg = result.extras["exception_message"]
    assert "preferred_impl_id" in msg
    assert "ghost" in msg


# ---------------------------------------------------------------------------
# Ingested dispatch -- builds the right httpx request (mocked)
# ---------------------------------------------------------------------------


class _FakeHttpConnector(HttpConnector):
    """Ingested-dispatch test fixture: records ``_request_json`` calls."""

    product = "demo"
    version = "1.0"
    impl_id = "demo-rest"
    supported_version_range = ">=1.0,<2.0"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
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

    async def _request_json(  # type: ignore[override]
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "target": target,
                "method": method,
                "path": path,
                "operator": operator,
                "params": params,
                "json": json,
            }
        )
        return {"ok": True, "method": method, "path": path}


@pytest.mark.asyncio
async def test_dispatch_ingested_builds_request_via_request_json(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Ingested dispatch: path vars substituted, query/body split, ``_request_json`` called."""
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    register_connector_v2(
        product="demo",
        version="1.0",
        impl_id="demo-rest",
        cls=_FakeHttpConnector,
    )

    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="demo",
        version="1.0",
        impl_id="demo-rest",
        op_id="GET:/api/test/{id}",
        source_kind="ingested",
        method="GET",
        path="/api/test/{id}",
        handler_ref=None,
        summary="Test op",
        description="Ingested test op.",
        tags=[],
        parameter_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "x-meho-param-loc": "path"},
                "filter": {"type": "string", "x-meho-param-loc": "query"},
            },
        },
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=stub_embedding_service.encode_one.return_value,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()

    operator = _make_operator()
    target = _FakeTarget(product="demo", version="1.0")

    result = await dispatch(
        operator=operator,
        connector_id="demo-rest-1.0",
        op_id="GET:/api/test/{id}",
        target=target,
        params={"id": "abc-123", "filter": "name=foo"},
    )

    # Recover the connector instance via the resolver to inspect calls.
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    instance = next(v for k, v in _CONNECTOR_INSTANCE_CACHE.items() if k is _FakeHttpConnector)
    assert isinstance(instance, _FakeHttpConnector)
    assert len(instance.calls) == 1
    call = instance.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/test/abc-123"
    assert call["params"] == {"filter": "name=foo"}
    assert call["operator"] is operator

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["method"] == "GET"
    assert len(captured_events) == 1


# ---------------------------------------------------------------------------
# Composite dispatch + audit-tree linkage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_composite_receives_dispatch_and_emits_child_row(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Composite handler receives ``dispatch`` + child audit row carries ``parent_audit_id``."""
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    # Register the child op first (typed) and the composite second.
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
    # Composite descriptor: inserted directly because T4's register
    # helper only emits typed rows.
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id="vault.composite.audit",
                source_kind="composite",
                method=None,
                path=None,
                handler_ref="tests.test_operations_dispatcher._module_composite_handler",
                summary="Composite audit.",
                description="Composite that lists secrets.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=stub_embedding_service.encode_one.return_value,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.composite.audit",
        target=target,
        params={"path": "/"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["parent"] == "ok"
    assert result.result["child_status"] == "ok"

    # Two audit rows -- composite parent + child kv.list.
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path.in_({"vault.composite.audit", "vault.kv.list"})
                    )
                )
            )
            .scalars()
            .all()
        )
    by_path = {r.path: r for r in rows}
    assert "vault.composite.audit" in by_path
    assert "vault.kv.list" in by_path
    parent = by_path["vault.composite.audit"]
    child = by_path["vault.kv.list"]
    # T7 (#398) ships the real ``audit_log.parent_audit_id`` column;
    # the child row carries the composite parent's id on the column
    # directly. The payload mirror is preserved for the broadcast-
    # event surface.
    assert child.parent_audit_id == parent.id
    assert child.payload.get("parent_audit_id") == str(parent.id)
    # The parent row has no parent of its own.
    assert parent.parent_audit_id is None
    assert "parent_audit_id" not in parent.payload


# ---------------------------------------------------------------------------
# Reducer is invoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_invokes_reducer_pass_through_by_default(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """The pass-through reducer leaves the response shape untouched."""
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
    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.list",
        target=target,
        params={"path": "/secret"},
    )
    assert result.status == "ok"
    # Pass-through reducer returns the response verbatim.
    assert result.result == {"echo": {"path": "/secret"}}
    # No result_handle on the pass-through path.
    assert "result_handle" not in result.extras


@pytest.mark.asyncio
async def test_dispatch_uses_swapped_reducer(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A non-default reducer (T6 will install one) sees the response."""

    seen: list[Any] = []

    class _RecordingReducer(PassThroughReducer):
        async def reduce(
            self,
            payload: Any,
            schema: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
        ) -> tuple[Any, Any]:
            seen.append(payload)
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

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    from meho_backplane.operations import set_default_reducer

    set_default_reducer(_RecordingReducer())
    try:
        await dispatch(
            operator=operator,
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=target,
            params={"path": "/secret"},
        )
        assert seen == [{"echo": {"path": "/secret"}}]
    finally:
        set_default_reducer(PassThroughReducer())


def test_isolate_global_registries_restores_default_reducer() -> None:
    """The autouse isolation fixture restores the default reducer (#981).

    A test that swaps the dispatcher's module-level default reducer (the
    app lifespan does exactly this in production) must not leak that
    binding into the next test on the same xdist worker. Drive the actual
    conftest fixture as a generator: run its setup, install a sentinel
    reducer mid-"test", then run its teardown and assert the pre-test
    reducer identity is back. Driving the fixture by hand (rather than
    relying on it autouse) makes the assertion observe the *post-teardown*
    state, which a test body otherwise can't see.

    ``@pytest.fixture`` wraps the generator function in a
    ``FixtureFunctionDefinition`` whose original is reachable via
    ``__wrapped__`` (set by the decorator); the type stub omits the
    attribute, so it's read through a typed cast.
    """
    from meho_backplane.operations import dispatcher, set_default_reducer
    from tests import conftest

    isolate_gen_fn = cast(
        "Callable[[], Iterator[None]]",
        conftest._isolate_global_registries.__wrapped__,  # type: ignore[attr-defined]
    )

    pre_test_reducer = dispatcher._DEFAULT_REDUCER

    gen = isolate_gen_fn()
    next(gen)  # setup: snapshot the current bindings

    sentinel = PassThroughReducer()
    set_default_reducer(sentinel)
    assert dispatcher._DEFAULT_REDUCER is sentinel

    with pytest.raises(StopIteration):
        next(gen)  # teardown: restore the snapshot

    assert dispatcher._DEFAULT_REDUCER is pre_test_reducer


# ---------------------------------------------------------------------------
# handler_unreachable + connector_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_handler_unreachable_when_import_fails(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """``handler_ref`` pointing at a deleted module -> ``handler_unreachable``."""
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_NoOpVaultConnector,
    )
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id="vault.kv.broken",
            source_kind="typed",
            method=None,
            path=None,
            handler_ref="this.module.does.not.exist.handler",
            summary="Broken.",
            description="Broken handler ref.",
            tags=[],
            parameter_schema={"type": "object"},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=stub_embedding_service.encode_one.return_value,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()

    operator = _make_operator()
    target = _FakeTarget(product="vault")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.broken",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("handler_unreachable:")
    assert result.extras["error_code"] == "handler_unreachable"


@pytest.mark.asyncio
async def test_dispatch_returns_connector_error_when_handler_raises(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Handler exception -> ``connector_error`` + audit row with ``result_status='error'``."""
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
        op_id="vault.kv.boom",
        handler=_module_handler_raises,
        summary="Boom.",
        description="Always raises.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.boom",
        target=target,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "RuntimeError"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.kv.boom")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_denies_dangerous_op_by_safety_level(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``safety_level='dangerous'`` → ``denied`` for an **agent** principal.

    No AgentPermission rows exist for the agent, so the resolver uses the
    ``safety_level`` default: ``dangerous`` → ``deny``. (Human/service
    principals keep the v0.2 default-allow contract — see
    ``test_dispatch_human_dangerous_op_auto_executes``.)
    """
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
        op_id="vault.kv.danger",
        handler=_module_handler_returning_dict,
        summary="Dangerous op.",
        description="Destructive operation requiring explicit permission grant.",
        parameter_schema={"type": "object"},
        safety_level="dangerous",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.AGENT)
    target = _FakeTarget(product="vault")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.danger",
        target=target,
        params={},
    )
    assert result.status == "denied"
    assert result.error is not None
    assert result.error.startswith("denied:")
    assert result.extras["error_code"] == "denied"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.kv.danger")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "denied"
    assert rows[0].status_code == 403

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "denied"


@pytest.mark.asyncio
async def test_dispatch_pending_caution_op_no_permission_row(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``safety_level='caution'`` + no permission row → ``awaiting_approval`` (agent).

    G11.2-T3: for an **agent** principal the ``caution`` default is
    ``needs-approval``. G11.2-T4: that verdict now routes through the
    durable approval queue — the dispatcher writes an
    :class:`~meho_backplane.db.models.ApprovalRequest` pending row plus a
    synchronous ``approval.request`` audit row and returns an
    ``awaiting_approval`` result (HTTP 202); the op itself does not
    execute. Humans keep auto-execute — see
    ``test_dispatch_human_caution_op_auto_executes``.
    """
    from meho_backplane.db.models import ApprovalRequest

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
        op_id="vault.kv.caution",
        handler=_module_handler_returning_dict,
        summary="Caution op.",
        description="Operation that requires approval by default.",
        parameter_schema={"type": "object"},
        safety_level="caution",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.AGENT)
    target = _FakeTarget(product="vault")
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.caution",
        target=target,
        params={},
    )
    assert result.status == "awaiting_approval"
    assert result.error is not None
    assert result.error.startswith("awaiting_approval:")
    assert result.extras["error_code"] == "awaiting_approval"
    assert "approval_request_id" in result.extras

    approval_request_id = UUID(result.extras["approval_request_id"])
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        pending_row = await fresh.get(ApprovalRequest, approval_request_id)
        assert pending_row is not None
        assert pending_row.status == "pending"
        assert pending_row.op_id == "vault.kv.caution"

        # The op itself did not execute → no audit row on its own path.
        op_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.kv.caution")))
            .scalars()
            .all()
        )
        assert len(op_rows) == 0

        # A synchronous "request" audit row landed for the approval.
        request_rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "approval.request")))
            .scalars()
            .all()
        )
    assert len(request_rows) == 1
    assert request_rows[0].status_code == 202

    # The approval-queue path writes audit directly, not via the
    # broadcast helper, so no broadcast event is emitted here.
    assert len(captured_events) == 0


@pytest.mark.asyncio
async def test_dispatch_human_dangerous_op_auto_executes(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A **human** dispatching a ``dangerous`` op keeps the v0.2 contract.

    G11.2-T3 gates *agent* principals through the per-(principal, op,
    target) resolver; human/service principals are default-allow except
    ``requires_approval``. So a ``dangerous`` op with no
    ``requires_approval`` flag auto-executes for a human — the resolver's
    ``dangerous → deny`` default must not silently start denying human
    operators (the pre-fix regression that broke vault/vmware suites).
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.human_danger",
        handler=_module_handler_returning_dict,
        summary="Dangerous op dispatched by a human.",
        description="Destructive op; human path stays default-allow.",
        parameter_schema={"type": "object"},
        safety_level="dangerous",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.USER),
        connector_id="vault-1.x",
        op_id="vault.kv.human_danger",
        target=_FakeTarget(product="vault"),
        params={},
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_dispatch_human_caution_op_auto_executes(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A **human** dispatching a ``caution`` op auto-executes (v0.2 contract)."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.human_caution",
        handler=_module_handler_returning_dict,
        summary="Caution op dispatched by a human.",
        description="Caution op; human path stays default-allow.",
        parameter_schema={"type": "object"},
        safety_level="caution",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.USER),
        connector_id="vault-1.x",
        op_id="vault.kv.human_caution",
        target=_FakeTarget(product="vault"),
        params={},
    )
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_dispatch_human_requires_approval_op_denied(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``requires_approval=True`` still hard-denies a **human** (v0.2 contract).

    The approval queue (G11.2-T4) routes only agent runs to the pending
    path; human/service principals retain the v0.2 hard-deny so the
    enforcement signal does not silently disappear.
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.human_approval",
        handler=_module_handler_returning_dict,
        summary="Safe op flagged requires_approval.",
        description="requires_approval stays enforced for humans.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.USER),
        connector_id="vault-1.x",
        op_id="vault.kv.human_approval",
        target=_FakeTarget(product="vault"),
        params={},
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "denied"


@pytest.mark.asyncio
async def test_dispatch_agent_requires_approval_floors_safe_op_to_pending(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``requires_approval=True`` floors an agent's ``safe`` op to approval.

    For an agent the resolver would auto-execute a ``safe`` op by default,
    but ``requires_approval`` folds the verdict up to ``needs-approval``
    so a connector-flagged op is never auto-executed by an agent
    regardless of its ``safety_level`` (Major 5: ``requires_approval``
    stays a live enforcement signal for agents). G11.2-T4 routes that
    verdict to the durable approval queue → ``awaiting_approval``.
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.agent_approval",
        handler=_module_handler_returning_dict,
        summary="Safe op flagged requires_approval, dispatched by an agent.",
        description="Agent path floors requires_approval to needs-approval.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.AGENT),
        connector_id="vault-1.x",
        op_id="vault.kv.agent_approval",
        target=_FakeTarget(product="vault"),
        params={},
    )
    assert result.status == "awaiting_approval"
    assert result.extras["error_code"] == "awaiting_approval"
    assert "approval_request_id" in result.extras


# ---------------------------------------------------------------------------
# compute_params_hash
# ---------------------------------------------------------------------------


def test_compute_params_hash_is_order_independent() -> None:
    """Param ordering doesn't change the hash -- canonicalisation works."""
    from meho_backplane.operations import compute_params_hash

    a = compute_params_hash({"x": 1, "y": 2})
    b = compute_params_hash({"y": 2, "x": 1})
    assert a == b


def test_compute_params_hash_differs_on_value_change() -> None:
    """Different values -> different hash."""
    from meho_backplane.operations import compute_params_hash

    a = compute_params_hash({"x": 1})
    b = compute_params_hash({"x": 2})
    assert a != b


def test_compute_params_hash_handles_non_json_natives() -> None:
    """``default=str`` lets UUIDs / datetimes flow through without crashing."""
    from meho_backplane.operations import compute_params_hash

    h = compute_params_hash({"id": uuid.UUID("00000000-0000-0000-0000-000000000001")})
    assert isinstance(h, str)
    assert len(h) == 64
