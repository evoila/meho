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
* Policy gate -- ``requires_approval=True`` routes a human/service
  principal to the approval queue (``awaiting_approval``), not a
  hard-deny (G11.7-T1 #1401); agents floor to ``needs-approval``.

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
        tenant_id: UUID | None = None,
        name: str = "test-target",
        host: str = "test.example.com",
        port: int = 443,
        auth_model: str = "shared_service_account",
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=version)
        self.preferred_impl_id: str | None = None
        self.id: UUID = target_id or uuid.uuid4()
        # The shared HTTP client pool keys on ``target_cache_key``
        # (``(tenant_id, id)``); without ``tenant_id`` any double that
        # reaches the pool hits ``AttributeError`` (evoila/meho#1682).
        self.tenant_id: UUID = tenant_id or UUID("00000000-0000-0000-0000-00000000a0a0")
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


async def _module_handler_target_optional(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler that tolerates ``target=None``.

    No ``self`` parameter, so :func:`_handler_requires_target` returns
    ``False`` and the dispatcher routes it with ``connector_instance=None``
    even when no target is supplied (G0.20-T6 #1506 — the legitimately
    target-less case that must keep dispatching).
    """
    return {"echo": params, "target_is_none": target is None}


class _SelfFirstTypedConnector(Connector):
    """Connector with a self-first typed handler -- exercises target_required.

    ``keycloak.user.list`` is the production shape: a connector-bound
    (``self``-first) typed handler that can only run against a resolved
    connector instance, reached *through* the target. Defined at module
    scope so ``register_typed_operation``'s ``derive_handler_ref`` round-
    trips the dotted path back to the same function object.
    """

    product = "selffirst"
    version = "1.x"
    impl_id = "selffirst"

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

    async def list_things(
        self,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Self-first typed handler -- mirrors ``keycloak_user_list``'s shape."""
        return {"rows": [], "total": 0}


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


# ---------------------------------------------------------------------------
# G0.20-T6 (#1506): a target-requiring typed/composite op invoked with
# ``target=None`` must return a clean ``target_required`` usage error --
# NOT the opaque ``connector_error: RuntimeError`` the self-guard in
# ``dispatch_typed`` emits for a genuine instance-cache fault. The guard
# keys on handler SHAPE (self-first => needs target), so a legitimately
# target-less module-level handler still dispatches unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_self_first_handler_no_target_returns_target_required(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Self-first typed op + ``target=None`` -> structured ``target_required``.

    Pre-#1506 the dispatcher short-circuited connector resolution on
    ``target is None`` and let the self-first handler proceed unbound,
    tripping ``dispatch_typed``'s loud self-guard ``RuntimeError`` — which
    the generic ``except Exception`` then mislabelled as
    ``connector_error: RuntimeError`` ("...instance-cache fault..."), an
    internal-looking message for what is an omitted-argument usage error.
    The fix catches the no-target case at resolution time and returns the
    clean ``target_required`` envelope naming the op.
    """
    register_connector_v2(
        product="selffirst",
        version="",
        impl_id="",
        cls=_SelfFirstTypedConnector,
    )
    await register_typed_operation(
        product="selffirst",
        version="1.x",
        impl_id="selffirst",
        op_id="selffirst.thing.list",
        handler=_SelfFirstTypedConnector.list_things,
        summary="List things (self-first).",
        description="Self-first typed op requiring a target.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()

    result = await dispatch(
        operator=operator,
        connector_id="selffirst-1.x",
        op_id="selffirst.thing.list",
        target=None,
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("target_required:")
    assert result.extras["error_code"] == "target_required"
    assert result.extras["op_id"] == "selffirst.thing.list"
    # The misleading internal envelope must NOT surface.
    assert "connector_error" not in (result.error or "")
    assert result.extras["error_code"] != "connector_error"


@pytest.mark.asyncio
async def test_dispatch_module_level_handler_no_target_still_dispatches(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A legitimately target-less module-level typed op still dispatches.

    The no-target guard keys on handler SHAPE, not just
    ``source_kind``+``target``: a module-level handler (no ``self``) needs
    no connector instance, so ``target=None`` is valid and the dispatch
    runs with ``connector_instance=None`` (regression guard for #1506's
    second acceptance criterion).
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.targetless",
        handler=_module_handler_target_optional,
        summary="Target-less module-level op.",
        description="Module-level typed op that needs no target.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator()

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.targetless",
        target=None,
        params={"hello": "world"},
    )

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["echo"] == {"hello": "world"}
    assert result.result["target_is_none"] is True


def test_handler_requires_target_true_for_connector_declaring_composite() -> None:
    """A module-level composite declaring ``connector`` requires a target (#2255).

    The direct-session substrate (#2251) resolves the connector instance
    *from* the target and forwards it; dispatching with ``target=None``
    would leave ``connector=None`` and crash the handler on its first
    session call. The guard surfaces ``target_required`` instead -- the
    carry-forward from PR #2261's review for the I-B migrations.
    """
    from meho_backplane.operations.dispatcher import _handler_requires_target

    assert (
        _handler_requires_target(
            "tests.fixtures.composites.handlers.composite_connector_only_handler"
        )
        is True
    )


def test_handler_requires_target_false_for_dispatch_child_only_composite() -> None:
    """A module-level ``dispatch_child``-only composite still needs no target.

    Regression guard: extending the check to ``connector``-declaring
    handlers must not sweep in the existing ``dispatch_child``-only
    handlers, which keep dispatching with ``connector_instance=None``.
    """
    from meho_backplane.operations.dispatcher import _handler_requires_target

    assert (
        _handler_requires_target(
            "tests.fixtures.composites.handlers.composite_dispatch_child_handler"
        )
        is False
    )


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
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "target": target,
                "method": method,
                "path": path,
                "operator": operator,
                "params": params,
                "json": json,
                "extra_headers": extra_headers,
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
# Ingested write-body round-trip (#1656): the ``loc=="body"`` container
# param's *value* must go on the wire unwrapped. Drives the REAL
# ``HttpConnector._post_json`` httpx transport and captures the outbound
# request with respx, so the assertion is on the actual bytes a vendor API
# (here gh-rest issue-create) would receive — not a mocked transport seam.
# ---------------------------------------------------------------------------


class _RoundTripHttpConnector(HttpConnector):
    """Ingested-dispatch fixture exercising the real httpx ``_post_json``.

    Unlike :class:`_FakeHttpConnector` (which records ``_request_json``
    calls), this connector keeps :class:`HttpConnector`'s real transport so
    the request is genuinely serialized and sent — respx intercepts it at
    the wire. Only ``auth_headers`` and the three ABC methods are
    overridden; ``_base_url`` derives ``https://{target.host}`` from the
    target, which the test mocks via ``respx.mock(base_url=...)``.
    """

    product = "gh"
    version = "3.0"
    impl_id = "gh-rest"
    supported_version_range = ">=3.0,<4.0"

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

    async def auth_headers(  # type: ignore[override]
        self,
        target: Any,
        operator: Operator,
    ) -> dict[str, str]:
        return {"authorization": "Bearer test-token"}


def _gh_issue_create_descriptor(embedding: Any) -> Any:
    """Build a gh-rest ``POST:/repos/{owner}/{repo}/issues`` ingested descriptor.

    Mirrors the G0.7 ingester's output shape: ``owner``/``repo`` are
    ``x-meho-param-loc='path'`` and the requestBody is a single ``body``
    property tagged ``x-meho-param-loc='body'`` (see ``ingest.openapi``).
    """
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="gh",
        version="3.0",
        impl_id="gh-rest",
        op_id="POST:/repos/{owner}/{repo}/issues",
        source_kind="ingested",
        method="POST",
        path="/repos/{owner}/{repo}/issues",
        handler_ref=None,
        summary="Create an issue",
        description="Create an issue on a repository.",
        tags=[],
        parameter_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "x-meho-param-loc": "path"},
                "repo": {"type": "string", "x-meho-param-loc": "path"},
                "body": {"type": "object", "x-meho-param-loc": "body"},
            },
            "required": ["owner", "repo", "body"],
        },
        response_schema=None,
        llm_instructions=None,
        safety_level="caution",
        requires_approval=False,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issue_body",
    [
        pytest.param({"title": "X"}, id="title-only"),
        pytest.param({"title": "X", "body": "Y"}, id="title-and-body"),
    ],
)
async def test_dispatch_ingested_post_sends_unwrapped_request_body(
    issue_body: dict[str, Any],
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """gh-rest issue-create sends the body param's *value* (unwrapped) on the wire.

    Regression for #1656: the dispatcher previously serialized
    ``{"body": {"title": "X"}}`` (the ``{name: value}`` bucket) instead of
    ``{"title": "X"}`` (the value), which GitHub 422s. Asserts the captured
    outbound body is exactly the requestBody value and that a recorded 201
    round-trips to ``status='ok'``.
    """
    import json as _json

    import respx

    register_connector_v2(
        product="gh",
        version="3.0",
        impl_id="gh-rest",
        cls=_RoundTripHttpConnector,
    )

    session.add(_gh_issue_create_descriptor(stub_embedding_service.encode_one.return_value))
    await session.commit()

    operator = _make_operator()
    target = _FakeTarget(product="gh", version="3.0", host="api.github.test", port=443)

    with respx.mock(base_url="https://api.github.test", assert_all_called=True) as mock:
        route = mock.post("/repos/octocat/hello/issues").respond(
            201, json={"number": 7, "title": issue_body["title"]}
        )
        result = await dispatch(
            operator=operator,
            connector_id="gh-rest-3.0",
            op_id="POST:/repos/{owner}/{repo}/issues",
            target=target,
            params={"owner": "octocat", "repo": "hello", "body": issue_body},
        )

    assert route.called
    sent = route.calls.last.request
    # The path params substitute into the URL; the body param's *value* is
    # the wire body — never wrapped under the ``"body"`` key.
    assert sent.url.path == "/repos/octocat/hello/issues"
    # Exact-equality is the load-bearing check: a wire body equal to the
    # requestBody value proves the dispatcher did NOT wrap it under "body".
    assert _json.loads(sent.content) == issue_body

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["number"] == 7
    assert len(captured_events) == 1


def _gh_update_issue_descriptor(embedding: Any, *, verb: str) -> Any:
    """Build a gh-rest ``{verb}:/repos/{owner}/{repo}/issues/{number}`` descriptor.

    Carries a header-located param (``X-Trace``) alongside the path params and
    the requestBody container, so a dispatch can prove both the real verb and
    the header bucket reach the wire (#1968).
    """
    from datetime import UTC, datetime

    from meho_backplane.db.models import EndpointDescriptor

    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="gh",
        version="3.0",
        impl_id="gh-rest",
        op_id=f"{verb}:/repos/{{owner}}/{{repo}}/issues/{{number}}",
        source_kind="ingested",
        method=verb,
        path="/repos/{owner}/{repo}/issues/{number}",
        handler_ref=None,
        summary="Update an issue",
        description="Update an issue on a repository.",
        tags=[],
        parameter_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "x-meho-param-loc": "path"},
                "repo": {"type": "string", "x-meho-param-loc": "path"},
                "number": {"type": "integer", "x-meho-param-loc": "path"},
                "X-Trace": {"type": "string", "x-meho-param-loc": "header"},
                "body": {"type": "object", "x-meho-param-loc": "body"},
            },
            "required": ["owner", "repo", "number", "body"],
        },
        response_schema=None,
        llm_instructions=None,
        safety_level="caution",
        requires_approval=False,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["PUT", "PATCH", "DELETE"])
async def test_dispatch_ingested_honors_verb_and_forwards_header_param(
    verb: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """An ingested PUT/PATCH/DELETE reaches the wire with its real verb + header param.

    Regression for #1968: ``dispatch_ingested`` previously routed every
    non-idempotent verb through a hardcoded-``POST`` ``_post_json`` and dropped
    the header-located params bucket entirely. Drives the real httpx transport
    via respx so the assertion is on the actual outbound request.
    """
    import respx

    register_connector_v2(
        product="gh",
        version="3.0",
        impl_id="gh-rest",
        cls=_RoundTripHttpConnector,
    )

    session.add(
        _gh_update_issue_descriptor(stub_embedding_service.encode_one.return_value, verb=verb)
    )
    await session.commit()

    operator = _make_operator()
    target = _FakeTarget(product="gh", version="3.0", host="api.github.test", port=443)

    with respx.mock(base_url="https://api.github.test", assert_all_called=True) as mock:
        route = mock.request(verb, "/repos/octocat/hello/issues/7").respond(
            200, json={"number": 7, "state": "closed"}
        )
        result = await dispatch(
            operator=operator,
            connector_id="gh-rest-3.0",
            op_id=f"{verb}:/repos/{{owner}}/{{repo}}/issues/{{number}}",
            target=target,
            params={
                "owner": "octocat",
                "repo": "hello",
                "number": 7,
                "X-Trace": "trace-123",
                "body": {"state": "closed"},
            },
        )

    assert route.called
    sent = route.calls.last.request
    # The real declared verb is on the wire — not a downgraded POST.
    assert sent.method == verb
    # The header-located param reached the outgoing request.
    assert sent.headers["x-trace"] == "trace-123"
    # The auth header still rides alongside.
    assert sent.headers["authorization"] == "Bearer test-token"

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["state"] == "closed"
    assert len(captured_events) == 1


def test_unwrap_body_returns_value_not_wrapper() -> None:
    """`_unwrap_body` yields the body param's value (unwrapped), or None when empty.

    Locks the serialization contract shared by both body-carrying dispatch
    arms (POST/PUT/PATCH/DELETE *and* GET-with-body): the single
    `x-meho-param-loc='body'` param's value is the request body, never a
    `{name: value}` wrapper (#1656).
    """
    from meho_backplane.operations._branches import _unwrap_body

    # Empty bucket -> no body (httpx omits the request body).
    assert _unwrap_body({}) is None
    # Single body param -> its value, unwrapped (the GitHub issue-create shape).
    assert _unwrap_body({"body": {"title": "X"}}) == {"title": "X"}
    # The param name is irrelevant — the *value* is returned regardless.
    assert _unwrap_body({"payload": [1, 2, 3]}) == [1, 2, 3]


def test_unwrap_body_rejects_multiple_body_params() -> None:
    """More than one `loc=='body'` param is an ingest-modelling fault -> raise.

    A descriptor must carry exactly one requestBody container param. Failing
    loud beats silently picking one and sending a body the caller never
    asked for.
    """
    from meho_backplane.operations._branches import _unwrap_body

    with pytest.raises(RuntimeError, match="multiple 'body' params"):
        _unwrap_body({"body": {"title": "X"}, "extra": {"k": "v"}})


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
async def test_audit_row_stamps_policy_decision_per_verdict(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """#130 AC1: each governed dispatch stamps its gate verdict on ``policy_decision``.

    An ``auto-execute`` dispatch (safe op, human), a ``needs-approval`` park
    (caution op, agent → the ``approval.request`` row), and a ``deny`` (dangerous
    op, agent, no grant) each land the matching :class:`PermissionVerdict` on the
    ``audit_log`` row — queryable without joining ``method``+``path`` + parsing
    ``payload``.
    """
    from meho_backplane.db.models import ApprovalRequest

    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    for op_id, safety in (
        ("vault.kv.autoexec", "safe"),
        ("vault.kv.needsapproval", "caution"),
        ("vault.kv.deny", "dangerous"),
    ):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=_module_handler_returning_dict,
            summary=f"{safety} op.",
            description=f"A {safety} operation.",
            parameter_schema={"type": "object"},
            safety_level=safety,
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )

    target = _FakeTarget(product="vault")

    auto = await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id="vault.kv.autoexec",
        target=target,
        params={},
    )
    assert auto.status == "ok", auto.error

    parked = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.AGENT),
        connector_id="vault-1.x",
        op_id="vault.kv.needsapproval",
        target=target,
        params={},
    )
    assert parked.status == "awaiting_approval", parked.error

    denied = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.AGENT),
        connector_id="vault-1.x",
        op_id="vault.kv.deny",
        target=target,
        params={},
    )
    assert denied.status == "denied", denied.error

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        by_path = {row.path: row for row in (await fresh.execute(select(AuditLog))).scalars().all()}
        parked_request = await fresh.get(
            ApprovalRequest, UUID(parked.extras["approval_request_id"])
        )
        assert parked_request is not None

    # auto-execute dispatch row.
    assert by_path["vault.kv.autoexec"].policy_decision == "auto-execute"
    # deny dispatch row.
    assert by_path["vault.kv.deny"].policy_decision == "deny"
    # the parked "request" row carries the needs-approval verdict; the op's own
    # path wrote no row (it never executed).
    assert by_path["approval.request"].policy_decision == "needs-approval"
    assert "vault.kv.needsapproval" not in by_path


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
async def test_dispatch_human_requires_approval_op_queues(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """``requires_approval=True`` queues a **human**, not hard-deny (G11.7-T1 #1401).

    Pre-G11.7 the policy gate hard-denied a human/service principal on a
    ``requires_approval`` op. G11.7-T1 (#1401) routes them to the
    approval queue instead — ops-team operators are exactly the humans
    Phase C expects to run governed writes, so a hard-deny defeated the
    point. The op is parked + resumable (``awaiting_approval``), not
    ``denied``.
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.human_approval",
        handler=_module_handler_returning_dict,
        summary="Safe op flagged requires_approval.",
        description="requires_approval routes humans to the queue.",
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
    assert result.status == "awaiting_approval", result.error
    assert result.status != "denied"
    assert "approval_request_id" in result.extras


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


@pytest.mark.asyncio
async def test_park_populates_proposed_effect_from_builder(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A parked op with a registered preview builder stores its preview.

    G11.7 follow-up (#1437): when the policy gate routes an op to
    ``needs-approval``, the dispatcher invokes the op's opt-in
    ``proposed_effect`` builder and stores the result on the durable
    :class:`ApprovalRequest` row -- so the reviewer reads the preview in
    the approval queue, not just in the post-approval op result.
    """
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations._preview import (
        _PREVIEW_BUILDERS,
        PreviewContext,
        register_preview_builder,
    )

    async def _preview(ctx: PreviewContext) -> dict[str, Any]:
        return {"would_change": ["deployment/web"], "param_echo": ctx.params}

    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.preview_op",
        handler=_module_handler_returning_dict,
        summary="Op with a registered preview builder.",
        description="Parks for approval and populates proposed_effect.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    register_preview_builder("vault.kv.preview_op", _preview)
    try:
        result = await dispatch(
            operator=_make_operator(principal_kind=PrincipalKind.AGENT),
            connector_id="vault-1.x",
            op_id="vault.kv.preview_op",
            target=_FakeTarget(product="vault"),
            params={"path": "secret/data/x"},
        )
        assert result.status == "awaiting_approval"
        approval_request_id = UUID(result.extras["approval_request_id"])
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as fresh:
            row = await fresh.get(ApprovalRequest, approval_request_id)
            assert row is not None
            # The builder's preview landed under the {op_class, preview}
            # envelope -- not the identifier-only default.
            assert row.proposed_effect["op_class"] == "other"
            assert row.proposed_effect["preview"]["would_change"] == ["deployment/web"]
            # The identifier-only default keys are NOT present (a built
            # preview replaces the default, it doesn't merge into it).
            assert "op_id" not in row.proposed_effect
    finally:
        _PREVIEW_BUILDERS.pop("vault.kv.preview_op", None)


@pytest.mark.asyncio
async def test_park_without_builder_uses_identifier_default(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """An op with no preview builder parks with the identifier-only default.

    G11.7 follow-up (#1437): the hook is opt-in. An op that registers no
    builder must park exactly as before -- the durable row carries the
    ``{op_id, connector_id, target_id}`` default, with no error and no
    regression.
    """
    from meho_backplane.db.models import ApprovalRequest

    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    target = _FakeTarget(product="vault")
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.no_preview_op",
        handler=_module_handler_returning_dict,
        summary="Op without a preview builder.",
        description="Parks for approval; no proposed_effect builder registered.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    result = await dispatch(
        operator=_make_operator(principal_kind=PrincipalKind.AGENT),
        connector_id="vault-1.x",
        op_id="vault.kv.no_preview_op",
        target=target,
        params={},
    )
    assert result.status == "awaiting_approval"
    approval_request_id = UUID(result.extras["approval_request_id"])
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        row = await fresh.get(ApprovalRequest, approval_request_id)
        assert row is not None
        # Identifier-only default -- no built-preview envelope -- with the
        # catalog safety_level stamped on by the dispatcher seam (#1855).
        assert row.proposed_effect == {
            "op_id": "vault.kv.no_preview_op",
            "connector_id": "vault-1.x",
            "target_id": str(target.id),
            "safety_level": "safe",
        }
        assert "preview" not in row.proposed_effect


@pytest.mark.asyncio
async def test_park_merges_permission_preflight_onto_identifier_default(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A parked write with a registered permission preflight stores its banner.

    G0.20-T4 (#1504): the capability-only permission preflight runs and is
    merged under ``proposed_effect["permission_preflight"]`` — so the
    reviewer sees "this write will be denied" at park time. Since #1856,
    a non-credential write with no bespoke builder also carries the
    generic params-echo default, so the preflight rides on the
    ``{op_class, params_echo}`` base rather than the bare identifier
    default; the banner is present either way.
    """
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations._preview import (
        _PERMISSION_PREFLIGHTS,
        PreviewContext,
        register_permission_preflight,
    )

    async def _preflight(ctx: PreviewContext) -> dict[str, Any]:
        return {
            "check": "vault.capabilities-self",
            "path": f"secret/data/{ctx.params.get('path', '?')}",
            "required": ["create", "update"],
            "granted": ["read"],
            "will_be_denied": True,
            "principal_sub": ctx.operator.sub,
        }

    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    target = _FakeTarget(product="vault")
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.preflight_op",
        handler=_module_handler_returning_dict,
        summary="Op with a registered permission preflight.",
        description="Parks for approval; the preflight flags a will-be-denied write.",
        parameter_schema={"type": "object"},
        safety_level="caution",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    register_permission_preflight("vault.kv.preflight_op", _preflight)
    try:
        result = await dispatch(
            operator=_make_operator(principal_kind=PrincipalKind.AGENT),
            connector_id="vault-1.x",
            op_id="vault.kv.preflight_op",
            target=target,
            params={"path": "meho/test/x"},
        )
        assert result.status == "awaiting_approval"
        approval_request_id = UUID(result.extras["approval_request_id"])
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as fresh:
            row = await fresh.get(ApprovalRequest, approval_request_id)
            assert row is not None
            # Generic params-echo base (#1856) carries the op identity via
            # op_class + the echoed params; the preflight is merged in.
            assert row.proposed_effect["op_class"] == "other"
            assert row.proposed_effect["params_echo"] == {"path": "meho/test/x"}
            preflight = row.proposed_effect["permission_preflight"]
            assert preflight["will_be_denied"] is True
            assert preflight["required"] == ["create", "update"]
            assert preflight["granted"] == ["read"]
            # No raw secret material — only the capability banner.
            assert "data" not in row.proposed_effect
    finally:
        _PERMISSION_PREFLIGHTS.pop("vault.kv.preflight_op", None)


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
