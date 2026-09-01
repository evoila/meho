# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-connector flight-recorder spans (#3217, F8).

Covers the typed span API + the v1 instrumentation wave per the decision of
record ``docs/decisions/dispatch-flight-recorder.md`` (F8, "all of them"):

* the shared typed-dispatch seam
  (:func:`meho_backplane.flight_recorder.typed.typed_dispatch_span`, wired at
  :func:`meho_backplane.operations._branches.dispatch_typed`) records a real
  (non-``opaque``) ``typed`` span for every typed op -- op id, target,
  duration, outcome -- keyed on ``impl_id``;
* the transport enricher seam
  (:func:`meho_backplane.flight_recorder.typed.record_typed_call`) redacts any
  request/response detail through the same fail-closed engine, proven on the
  shared SSH seam (``SshConnector._run_command``);
* the F7 invariant: a recorder / redaction failure can never fail, block, or
  slow a typed dispatch;
* the **registry-driven conformance sweep**: every registered typed connector
  family produces a ``typed`` span through the seam, so a newly added typed
  connector that skips the seam fails CI.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, NamedTuple
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder import typed as typed_mod
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
    run_typed_op_registrars,
)
from meho_backplane.operations._branches import dispatch_typed
from meho_backplane.redaction.flight_recorder import (
    BODY_OMITTED_MARKER,
    SECRET_FAMILY_OMITTED_MARKER,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


# ---------------------------------------------------------------------------
# DB-free helpers -- drive the seam against a manually-bound capture scope
# ---------------------------------------------------------------------------


def _op_ctx(
    *,
    op_id: str = "widget.thing.describe",
    connector_id: str = "widget-1.x",
    impl_id: str = "widget",
    product: str = "widget",
    tags: tuple[str, ...] = (),
) -> capture._OpContext:
    """Build a redaction op context with ``delete_shaped_patterns=()`` set.

    The explicit empty tuple keeps these unit tests settings-independent (no
    lazy ``get_settings`` read inside the redaction engine).
    """
    return capture._OpContext(
        op_id=op_id,
        connector_id=connector_id,
        impl_id=impl_id,
        product=product,
        tags=tags,
        body_paths=(),
        delete_shaped_patterns=(),
    )


@contextmanager
def _active_scope(op: capture._OpContext) -> Iterator[capture._CaptureScope]:
    """Bind an active capture scope + op context on the contextvars.

    Reaches into :mod:`meho_backplane.flight_recorder.capture` internals on
    purpose: the typed seam rides the *same* scope machinery the dispatcher
    binds, so a DB-free unit test drives it by setting those contextvars
    directly (the same shape ``begin_dispatch_capture`` produces).
    """
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    cap_token = capture._active_capture_var.set(scope)
    op_token = capture._op_context_var.set(op)
    try:
        yield scope
    finally:
        capture._op_context_var.reset(op_token)
        capture._active_capture_var.reset(cap_token)


class _Target:
    def __init__(self, name: str = "cap-target") -> None:
        self.name = name


async def _stub_typed_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


def _typed_spans(scope: capture._CaptureScope) -> list[Any]:
    return [s for s in scope.spans if s.span_kind == "typed"]


# ---------------------------------------------------------------------------
# Shared typed-dispatch seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typed_dispatch_span_records_typed_span() -> None:
    """The shared seam emits one ``typed`` span keyed on impl_id + op id."""
    op = _op_ctx(op_id="vault.kv.list", impl_id="vault", product="vault")
    with _active_scope(op) as scope:
        result = await dispatch_typed(
            handler=_stub_typed_handler,
            operator=None,  # type: ignore[arg-type]
            target=_Target("the-vault"),
            params={"secret_path": "kv/creds"},
        )
    assert result == {"ok": True}
    typed = _typed_spans(scope)
    assert len(typed) == 1
    span = typed[0]
    assert span.span_kind == "typed"
    assert span.name == "vault.kv.list"
    assert span.status == "ok"
    assert span.duration_ms is not None
    assert span.attributes["op_id"] == "vault.kv.list"
    assert span.attributes["impl_id"] == "vault"
    assert span.attributes["product"] == "vault"
    assert span.attributes["target"] == "the-vault"
    # Metadata-only: the (secret-shaped) params never reach the span.
    assert "params" not in span.attributes
    assert "request_body" not in span.attributes


@pytest.mark.asyncio
async def test_typed_dispatch_span_is_metadata_only_never_uncertain() -> None:
    """The shared seam records no body, so it never degrades the trace (F5)."""
    op = _op_ctx(op_id="vault.kv.read", impl_id="vault", product="vault")
    with _active_scope(op) as scope:
        await dispatch_typed(
            handler=_stub_typed_handler,
            operator=None,  # type: ignore[arg-type]
            target=_Target(),
            params={},
        )
    assert scope.redaction_uncertain is False


@pytest.mark.asyncio
async def test_typed_dispatch_span_records_error_outcome_and_reraises() -> None:
    """A handler exception is recorded as the outcome, then propagates (F7)."""

    async def _boom(target: Any, params: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("vendor exploded")

    op = _op_ctx(op_id="widget.do", impl_id="widget", product="widget")
    with _active_scope(op) as scope, pytest.raises(ValueError, match="vendor exploded"):
        await dispatch_typed(
            handler=_boom,
            operator=None,  # type: ignore[arg-type]
            target=_Target(),
            params={},
        )
    typed = _typed_spans(scope)
    assert len(typed) == 1
    assert typed[0].status == "error:ValueError"


@pytest.mark.asyncio
async def test_typed_dispatch_span_swallows_recorder_failure() -> None:
    """A raising recorder in the ``finally`` never fails the dispatch (F7)."""

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("recorder down")

    op = _op_ctx()
    with _active_scope(op):
        # Force the record path to raise; the seam's guarded ``finally`` must
        # swallow it and return the handler result byte-identically.
        original = typed_mod.record_typed_call
        typed_mod.record_typed_call = _boom  # type: ignore[assignment]
        try:
            result = await dispatch_typed(
                handler=_stub_typed_handler,
                operator=None,  # type: ignore[arg-type]
                target=_Target(),
                params={},
            )
        finally:
            typed_mod.record_typed_call = original  # type: ignore[assignment]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_capture_off_is_noop() -> None:
    """With no active scope the seam yields the handler result, no span, cheap."""
    # No _active_scope: the contextvar is unset.
    assert typed_mod.typed_span_start("x") is None
    result = await dispatch_typed(
        handler=_stub_typed_handler,
        operator=None,  # type: ignore[arg-type]
        target=_Target(),
        params={},
    )
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# record_typed_call redaction (the transport-enricher seam)
# ---------------------------------------------------------------------------


def test_record_typed_call_no_bodies_is_metadata_only() -> None:
    op = _op_ctx(op_id="k8s.pod.list", impl_id="k8s", product="k8s")
    with _active_scope(op) as scope:
        start = typed_mod.typed_span_start("k8s.pod.list")
        typed_mod.record_typed_call(start, kind="typed", status="ok", extra={"transport": "k8s"})
    span = _typed_spans(scope)[0]
    assert span.attributes["transport"] == "k8s"
    assert "request_body" not in span.attributes
    assert scope.redaction_uncertain is False


def test_record_typed_call_plaintext_body_omitted_and_uncertain() -> None:
    """A benign op's free-text body cannot be proven secret-free -> omit + degrade."""
    op = _op_ctx(op_id="widget.thing.describe", impl_id="widget", product="widget")
    with _active_scope(op) as scope:
        start = typed_mod.typed_span_start("cmd")
        typed_mod.record_typed_call(
            start,
            kind="typed",
            request_body="named-checkzone example.com /etc/zone",
            response_body="zone example.com/IN: loaded serial 42\nOK",
            request_content_type="text/plain",
            response_content_type="text/plain",
        )
    span = _typed_spans(scope)[0]
    assert span.attributes["request_body"] == BODY_OMITTED_MARKER
    assert span.attributes["response_body"] == BODY_OMITTED_MARKER
    assert span.attributes["body_recorded"] is True
    assert scope.redaction_uncertain is True  # F5 operator-only degrade


def test_record_typed_call_secret_family_body_excluded_but_certain() -> None:
    """A secret-family op never records a body, and that omission is certain."""
    op = _op_ctx(op_id="acme.token.mint", impl_id="widget", product="widget")
    with _active_scope(op) as scope:
        start = typed_mod.typed_span_start("mint")
        typed_mod.record_typed_call(
            start,
            kind="typed",
            request_body={"grant": "client_credentials"},
            response_body={"access_token": "super-secret"},
            request_content_type="application/json",
            response_content_type="application/json",
        )
    span = _typed_spans(scope)[0]
    assert span.attributes["body_recorded"] is False
    assert span.attributes["request_body"] == SECRET_FAMILY_OMITTED_MARKER
    assert span.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER
    # A placed family exclusion is deliberate + safe -> stays agent-readable.
    assert scope.redaction_uncertain is False


def test_record_typed_call_off_scope_is_noop() -> None:
    # No active scope: start marker is None, record is a no-op, nothing raises.
    assert typed_mod.typed_span_start("x") is None
    typed_mod.record_typed_call(None, kind="typed", request_body="anything")


# ---------------------------------------------------------------------------
# SSH transport enricher (the shared SSH seam)
# ---------------------------------------------------------------------------


class _FakeSshResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _FakeConn:
    def __init__(self, result: _FakeSshResult) -> None:
        self._result = result
        self.commands: list[str] = []

    async def run(self, cmd: str, check: bool = False) -> _FakeSshResult:
        self.commands.append(cmd)
        return self._result


class _StubSshConnector(SshConnector):
    product = "bind9"
    version = "9.x"
    impl_id = "bind9-ssh"

    def __init__(self, result: _FakeSshResult) -> None:
        super().__init__()
        self._fake = _FakeConn(result)

    async def _connect(self, target: Any, operator: Any = None) -> _FakeConn:  # type: ignore[override]
        return self._fake

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.mark.asyncio
async def test_ssh_command_emits_typed_span_with_omitted_bodies() -> None:
    """Every SSH command records one ``typed`` span; command + output dropped."""
    conn = _StubSshConnector(_FakeSshResult(stdout="zone loaded\n", exit_status=0))
    op = _op_ctx(op_id="bind9.zone.list", impl_id="bind9-ssh", product="bind9")
    with _active_scope(op) as scope:
        result = await conn._run_command(
            _Target("lab-dns"), "named-checkzone x /etc/z", operator=None
        )
    assert result.exit_status == 0
    typed = _typed_spans(scope)
    assert len(typed) == 1
    span = typed[0]
    assert span.attributes["transport"] == "ssh"
    assert span.attributes["exit_status"] == 0
    assert span.attributes["impl_id"] == "bind9-ssh"
    # SSH command + output are plain text -> engine drops both fail-closed.
    assert span.attributes["request_body"] == BODY_OMITTED_MARKER
    assert span.attributes["response_body"] == BODY_OMITTED_MARKER
    assert scope.redaction_uncertain is True  # SSH traces degrade to operator-only


@pytest.mark.asyncio
async def test_ssh_span_failure_never_breaks_the_command() -> None:
    """A raising recorder never propagates into the SSH dispatch path (F7)."""
    conn = _StubSshConnector(_FakeSshResult(stdout="ok", exit_status=0))

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("recorder down")

    op = _op_ctx(op_id="bind9.zone.list", impl_id="bind9-ssh", product="bind9")
    with _active_scope(op):
        original = typed_mod.record_typed_call
        typed_mod.record_typed_call = _boom  # type: ignore[assignment]
        try:
            result = await conn._run_command(_Target("lab-dns"), "echo hi", operator=None)
        finally:
            typed_mod.record_typed_call = original  # type: ignore[assignment]
    assert result.exit_status == 0  # the command result is untouched


@pytest.mark.asyncio
async def test_ssh_span_capture_off_is_noop() -> None:
    conn = _StubSshConnector(_FakeSshResult(stdout="ok", exit_status=0))
    # No active scope -> typed_span_start returns None -> no span, no error.
    result = await conn._run_command(_Target("lab-dns"), "echo hi", operator=None)
    assert result.exit_status == 0


# ---------------------------------------------------------------------------
# Registry-driven conformance sweep (F8: all typed families instrumented)
# ---------------------------------------------------------------------------

#: The typed connector families expected to register ``source_kind='typed'``
#: ops. Derived from the live registry, one entry per ``impl_id``. A newly
#: added typed family fails :func:`test_expected_typed_families_are_covered`
#: until it is added here -- the "name the gap" guard. github + hetzner_robot
#: are intentionally absent: they register composite / ingested ops, covered
#: by the composite + vendor_call seams, not the typed handler seam.
_EXPECTED_TYPED_IMPLS: frozenset[str] = frozenset(
    {
        "argocd-api",
        "bind9-ssh",
        "fleet-lcm",
        "fleet-rest",
        "gcloud-rest",
        "harbor-rest",
        "holodeck-ssh",
        "installer-rest",
        "k8s",
        "keycloak-admin",
        "loki-api",
        "mail-smtp",
        "mongodb-wire",
        "net-probe",
        "nsx-rest",
        "pfsense-ssh",
        "postgres-wire",
        "prometheus-api",
        "proxmox-api",
        "rabbitmq-management",
        "rke2-ssh",
        "sddc-rest",
        "sddc-vcf5",
        "secret-broker",
        "targets-registry",
        "tempo-api",
        "topology-graph",
        "vault",
        "vcd-rest",
        "vcfa-rest",
        "vcfa-vra8",
        "vmware-rest",
        "vrli-rest",
        "vrli-vrli8",
        "vrops-rest",
        "vrops-vrops8",
        "windns-ssh",
        "winsrv-ssh",
    }
)

#: Dual-impl products whose two implementations both register typed ops under
#: distinct ``impl_id``s (F8: keyed on impl_id, both covered).
_DUAL_IMPL_PAIRS: tuple[tuple[str, str], ...] = (
    ("fleet-rest", "fleet-lcm"),
    ("sddc-rest", "sddc-vcf5"),
    ("vcfa-rest", "vcfa-vra8"),
    ("vrli-rest", "vrli-vrli8"),
    ("vrops-rest", "vrops-vrops8"),
)


class _DescRef(NamedTuple):
    op_id: str
    impl_id: str
    product: str
    tags: tuple[str, ...]


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def typed_descriptors(stub_embedding_service: AsyncMock) -> list[_DescRef]:
    """Register every connector's typed ops, then return the typed descriptors.

    Registry-driven: runs the exact registrar set the FastAPI lifespan runs,
    so a newly added typed connector is enumerated automatically.
    """
    _eager_import_connectors()
    await run_typed_op_registrars(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    EndpointDescriptor.op_id,
                    EndpointDescriptor.impl_id,
                    EndpointDescriptor.product,
                    EndpointDescriptor.tags,
                ).where(EndpointDescriptor.source_kind == "typed")
            )
        ).all()
    return [
        _DescRef(op_id, impl_id, product, tuple(tags or ()))
        for op_id, impl_id, product, tags in rows
    ]


def _representative_by_impl(descriptors: list[_DescRef]) -> dict[str, _DescRef]:
    by_impl: dict[str, _DescRef] = {}
    for desc in descriptors:
        by_impl.setdefault(desc.impl_id, desc)
    return by_impl


@pytest.mark.asyncio
async def test_typed_enumeration_is_not_vacuous(typed_descriptors: list[_DescRef]) -> None:
    """Guard against a broken enumeration silently covering nothing."""
    assert len(typed_descriptors) >= 200
    impls = {d.impl_id for d in typed_descriptors}
    assert len(impls) >= 30


@pytest.mark.asyncio
async def test_expected_typed_families_are_covered(typed_descriptors: list[_DescRef]) -> None:
    """Every expected typed family registers typed ops (regression guard)."""
    impls = {d.impl_id for d in typed_descriptors}
    missing = _EXPECTED_TYPED_IMPLS - impls
    assert not missing, f"expected typed families missing from the registry: {sorted(missing)}"
    added = impls - _EXPECTED_TYPED_IMPLS
    assert not added, (
        "new typed connector family/families not covered by the F8 wave -- add the "
        f"impl_id(s) to _EXPECTED_TYPED_IMPLS after confirming the seam spans them: {sorted(added)}"
    )


@pytest.mark.asyncio
async def test_dual_impl_products_have_both_impls(typed_descriptors: list[_DescRef]) -> None:
    """Both implementations of each dual-impl product register typed ops (F8)."""
    impls = {d.impl_id for d in typed_descriptors}
    for modern, legacy in _DUAL_IMPL_PAIRS:
        assert modern in impls and legacy in impls, (
            f"dual-impl product incomplete: {modern}/{legacy}"
        )


@pytest.mark.asyncio
async def test_every_typed_family_emits_a_typed_span_through_the_seam(
    typed_descriptors: list[_DescRef],
) -> None:
    """The load-bearing coverage proof: each typed impl produces a ``typed``
    (non-``opaque``) span through the shared seam, keyed on its impl_id.

    Registry-driven, so a typed op that bypasses the seam -- or a family added
    without instrumentation -- fails here (no span keyed on its impl_id).
    """
    representatives = _representative_by_impl(typed_descriptors)
    assert set(representatives) == _EXPECTED_TYPED_IMPLS
    for impl_id, desc in representatives.items():
        op = capture._op_context_from(desc, f"{impl_id}-conn")
        with _active_scope(op) as scope:
            await dispatch_typed(
                handler=_stub_typed_handler,
                operator=None,  # type: ignore[arg-type]
                target=_Target(f"{impl_id}-target"),
                params={},
            )
        typed = _typed_spans(scope)
        assert typed, f"impl {impl_id!r} produced no typed span through the seam"
        assert typed[0].span_kind == "typed"
        assert typed[0].attributes["impl_id"] == impl_id
        assert typed[0].attributes["op_id"] == desc.op_id


# ---------------------------------------------------------------------------
# Full-dispatch: composites do not double-record; typed children do span
# ---------------------------------------------------------------------------


@pytest.fixture
def _reset_dispatch_state() -> Iterator[None]:
    """Clean dispatcher caches + connector registry around a full-dispatch test.

    Deliberately **not** autouse: the conformance sweep needs the eagerly
    imported connector registry left intact, so only the full-dispatch test
    below requests this reset.
    """
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def _capture_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = type("_F", (), {"version": None})()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.name = "cap-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _child_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params}


async def _two_child_composite(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> dict[str, Any]:
    a = await dispatch_child(connector_id="vault-1.x", op_id="vault.kv.list", params={"p": "a"})
    b = await dispatch_child(connector_id="vault-1.x", op_id="vault.kv.list", params={"p": "b"})
    return {"a": a.status, "b": b.status}


async def _seed_capture_tenant(slug: str) -> UUID:
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        s.add(Tenant(id=tenant_id, slug=slug, name=slug, flight_recorder_enabled=True))
        await s.commit()
    return tenant_id


def _make_operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="op-typed",
        name="Cap",
        email=None,
        raw_jwt="<jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _insert_composite_descriptor(
    *, session: AsyncSession, op_id: str, handler_ref: str, embedding: list[float]
) -> None:
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
            description=f"Composite {op_id}.",
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


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _spans_for_single_trace() -> list[DispatchTraceSpan]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        traces = list((await s.execute(select(DispatchTrace))).scalars().all())
        assert len(traces) == 1, f"expected exactly one trace, got {len(traces)}"
        return list(
            (
                await s.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == traces[0].id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_composite_children_span_but_composite_does_not_double_record(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    _reset_dispatch_state: None,
    _capture_broadcast: list[BroadcastEvent],
) -> None:
    """A composite's typed children each get a ``typed`` span; the composite
    parent (routed via ``dispatch_composite``) records only its
    ``composite_step`` markers -- no double ``typed`` span for the parent.
    """
    tenant_id = await _seed_capture_tenant("fr-typed-composite")
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.list",
        handler=_child_handler,
        summary="List.",
        description="List.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    await _insert_composite_descriptor(
        session=session,
        op_id="vault.composite.two",
        handler_ref="tests.test_flight_recorder_typed_spans._two_child_composite",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(tenant_id),
        connector_id="vault-1.x",
        op_id="vault.composite.two",
        target=_FakeTarget(),
        params={},
    )
    assert result.status == "ok", result.error

    spans = await _spans_for_single_trace()
    typed = [s for s in spans if s.span_kind == "typed"]
    steps = [s for s in spans if s.span_kind == "composite_step"]
    # Two typed children -> two typed spans; both keyed on the child op.
    assert len(typed) == 2
    assert {s.attributes["op_id"] for s in typed} == {"vault.kv.list"}
    assert {s.attributes["impl_id"] for s in typed} == {"vault"}
    # The composite parent itself never emits a typed span (no double-record).
    assert "vault.composite.two" not in {s.attributes.get("op_id") for s in typed}
    assert len(steps) == 2
