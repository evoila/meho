# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured error envelope on the caller result AND the audit payload (#2680).

Two halves, each pinned directly at its layer:

* HALF 1 (caller-facing) -- ``result_connector_error`` enriches its extras
  with ``http_status`` + the extracted ``upstream_message`` when the raised
  exception is an :exc:`httpx.HTTPStatusError` (the 404 / 429 / 5xx statuses
  the dispatcher's ``_classify_http_status_error`` leaves to the generic arm).
  Before #2680 a 5xx flattened to a bare ``connector_error`` whose only
  free-text was ``str(exc)`` -- the httpx status line, not the vendor body.

* HALF 2 (durable audit) -- ``_build_audit_payload`` threads a passed
  ``error_extras`` dict into ``payload["error"]`` so the DISPATCH audit row
  records the same envelope the caller received, not merely
  ``result_status='error'``. This is the persistence-layer pin the DoD asks
  for; the end-to-end wiring (dispatch -> audit_and_broadcast_safe ->
  write_audit_row) is exercised against a real DB in
  ``test_connectors_argocd_write_e2e.py``.

* HALF 3 (fail-closed audit precondition) -- on the success path any
  dispatch, read or write, whose DISPATCH audit row cannot commit returns
  ``audit_commit_failed`` instead of ``status='ok'`` (CLAUDE.md postulate 7
  / v0.1-spec §6): ``execution_outcome='uncertain'``, ``retry=False``, no
  ``audit_id``, and no phantom broadcast. It is never a ``connector_error``,
  because the connector call itself worked.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._audit import _build_audit_payload
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.redaction import RedactionManifestEntry, RedactionMiddlewareResult
from meho_backplane.settings import get_settings


def _http_status_error(status_code: int, *, json_body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://argocd.test/api/v1/applications/guestbook/sync")
    response = httpx.Response(status_code, json=json_body, request=request)
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


# ---------------------------------------------------------------------------
# HALF 1 -- result_connector_error 5xx enrichment
# ---------------------------------------------------------------------------


def test_connector_error_http_status_error_carries_upstream_body() -> None:
    """A 5xx HTTPStatusError adds http_status + the upstream body message."""
    exc = _http_status_error(
        500, json_body={"code": 13, "message": "application dry-run failed: pruning Service"}
    )
    result = result_connector_error("argocd.app.sync", exc, 1.0)

    # Top-level summary is unchanged so existing string matchers keep working.
    assert result.error == "connector_error: HTTPStatusError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "HTTPStatusError"
    # New detail is additive and connector-agnostic.
    assert result.extras["http_status"] == 500
    assert "application dry-run failed" in result.extras["upstream_message"]


def test_connector_error_non_http_omits_http_fields() -> None:
    """A non-HTTP exception is unchanged: no http_status / upstream_message keys."""
    result = result_connector_error("op.read", RuntimeError("boom"), 1.0)
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "RuntimeError"
    assert "http_status" not in result.extras
    assert "upstream_message" not in result.extras


# ---------------------------------------------------------------------------
# HALF 2 -- _build_audit_payload error-envelope threading
# ---------------------------------------------------------------------------


def _descriptor() -> Any:
    # _build_audit_payload reads only these attributes; a namespace avoids a
    # DB-backed EndpointDescriptor for a pure-composition test.
    return SimpleNamespace(
        op_id="argocd.app.sync",
        source_kind="typed",
        product="argocd",
        version="3.x",
        impl_id="argocd-api",
    )


def test_build_audit_payload_threads_error_envelope() -> None:
    """A passed error_extras dict lands verbatim under payload['error']."""
    envelope = {
        "error_code": "connector_error",
        "http_status": 500,
        "upstream_message": "application dry-run failed: pruning Service",
    }
    payload = _build_audit_payload(
        _descriptor(),
        "params-hash",
        "error",
        error_extras=envelope,
    )
    assert payload["result_status"] == "error"
    assert payload["error"] == envelope
    # Persisted as a copy, not the caller's live dict.
    assert payload["error"] is not envelope


def test_build_audit_payload_no_error_extras_leaves_key_absent() -> None:
    """A success/non-error write carries no 'error' key."""
    payload = _build_audit_payload(_descriptor(), "params-hash", "ok")
    assert "error" not in payload


def test_build_audit_payload_empty_error_extras_leaves_key_absent() -> None:
    """An empty envelope writes no 'error' key (no empty-dict noise)."""
    payload = _build_audit_payload(
        _descriptor(),
        "params-hash",
        "error",
        error_extras={},
    )
    assert "error" not in payload


# ---------------------------------------------------------------------------
# HALF 3 -- fail-closed audit precondition on the write-class success path
# (S07 #295). These drive the real dispatch() to prove the caller-visible
# result, not just the helper wiring.
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
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record every :func:`publish_event` call so the test can assert none fired."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


class _NoOpConnector(Connector):
    """Connector class used only to satisfy resolver lookups in typed tests."""

    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> Any:
        raise NotImplementedError


async def _module_mutating_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level typed handler standing in for a completed vendor mutation."""
    return {"echo": params, "created": True}


_recording_handler_calls: list[dict[str, Any]] = []
_recording_handler_payload: dict[str, Any] = {}


async def _module_recording_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Module-level test handler so typed registration exercises import resolution."""
    _recording_handler_calls.append(params)
    return _recording_handler_payload


def _configure_recording_handler(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Set the module-level handler outcome and clear its per-test call trace."""
    _recording_handler_calls.clear()
    _recording_handler_payload.clear()
    _recording_handler_payload.update(raw_payload)
    return _recording_handler_calls


async def _module_streamed_non2xx_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Raise an HTTPStatusError carrying a REAL unread, then-closed streamed response.

    Reproduces the guest file-transfer failure shape at the dispatcher boundary:
    a non-2xx raised inside a ``client.stream(...)`` context before the body is
    read leaves ``exc.response`` **unread and closed**, so its ``.json()`` /
    ``.text`` raise :exc:`httpx.ResponseNotRead` (a ``StreamError`` /
    ``RuntimeError`` -- neither ``ValueError``/``UnicodeDecodeError`` nor
    ``httpx.HTTPError``). The body is a lazy async generator, not eager
    ``content=``, so the response is genuinely unread (an eager MockTransport
    body would be pre-read and would NOT reproduce the crash -- exactly the gap
    in the prior in-memory get-failure test).
    """

    async def _lazy_body() -> Any:
        yield b'{"message":'
        yield b'"boom"}'

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=_lazy_body())

    transport = httpx.MockTransport(_handler)
    async with (
        httpx.AsyncClient(transport=transport) as client,
        client.stream("GET", "https://transfer.example.test/guestFile") as response,
    ):
        response.raise_for_status()
    raise AssertionError("unreachable: raise_for_status must have raised")  # pragma: no cover


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target the resolver / dispatcher reads from."""

    def __init__(self, *, product: str = "demo") -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=None)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a0")
        self.name = "demo-target"
        self.host = "demo.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """A human operator so the v0.2 default-allow contract routes to AUTO_EXECUTE."""
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register_op(
    op_id: str,
    *,
    safety_level: str,
    embedding: AsyncMock,
    handler: Any = _module_mutating_handler,
) -> None:
    register_connector_v2(product="demo", version="", impl_id="", cls=_NoOpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=handler,
        summary="Demo op.",
        description="Demo op used by the S07 audit-precondition tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level=safety_level,
    )


def _raise_audit_commit(*_args: Any, **_kwargs: Any) -> Any:
    async def _boom() -> None:
        raise RuntimeError("audit DB unavailable")

    return _boom()


@pytest.fixture
def committed_audit_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record actual audit writes while retaining the real commit implementation."""
    original_write = audit_module.write_audit_row
    writes: list[dict[str, Any]] = []

    async def _record_after_commit(**kwargs: Any) -> None:
        await original_write(**kwargs)
        writes.append(kwargs)

    monkeypatch.setattr(audit_module, "write_audit_row", _record_after_commit)
    return writes


async def _committed_rows_for(op_id: str) -> list[AuditLog]:
    """Read freshly committed dispatch rows through a separate DB session."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return list(
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )


def _install_exploding_reducer() -> None:
    """Install a reducer failure carrying text that must never reach callers."""

    class _ExplodingReducer:
        async def reduce(
            self,
            payload: Any,
            schema: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
        ) -> tuple[Any, Any]:
            raise RuntimeError("reducer private diagnostic: raw-delivery-secret")

    set_default_reducer(_ExplodingReducer())


def _install_preserved_redaction(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Make the dispatch seam carry distinct raw/redacted audit artefacts."""
    redacted = {"credential": "[REDACTED:token]", "state": "complete"}
    manifest = (
        RedactionManifestEntry(
            rule="test-token-rule",
            pattern="token",
            action="redact",
            count=1,
            span=(0, 1),
            reason="test audit preservation",
            path="$.credential",
        ),
    )
    policy_id = "test-delivery-policy"

    def _redact(raw: Any, **_kwargs: Any) -> RedactionMiddlewareResult:
        assert raw == raw_payload
        return RedactionMiddlewareResult(
            raw=raw_payload,
            redacted=redacted,
            manifest=manifest,
            policy_id=policy_id,
        )

    monkeypatch.setattr(dispatcher_module, "apply_connector_boundary_redaction", _redact)
    return raw_payload, [entry.model_dump(mode="json") for entry in manifest], policy_id


def _assert_audit_commit_failed(result: Any) -> None:
    """The handler ran but its row did not commit: uncertain, no receipt, no retry."""
    assert result.status == "error"
    assert result.error.startswith("audit_commit_failed:")
    assert result.extras["error_code"] == "audit_commit_failed"
    assert result.extras["execution_outcome"] == "uncertain"
    assert result.extras["retry"] is False
    assert "Do not run it again" in result.extras["remediation"]
    # It is not a connector error: the connector call worked.
    assert "connector_error" not in result.error
    assert "exception_class" not in result.extras
    assert result.audit_id is None
    assert result.delivery is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "safety_level"),
    [
        ("demo.thing.create", "safe"),  # write class
        ("demo.thing.list", "safe"),  # safe read: no longer fail-open
        ("demo.thing.list", "caution"),  # approved-run tier
    ],
)
async def test_completed_dispatch_is_uncertain_when_audit_row_cannot_commit(
    op_id: str,
    safety_level: str,
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed handler whose audit row cannot commit never reports ok.

    Reads and writes follow one rule: no committed row, no success. The
    handler ran exactly once and the caller is told not to run it again.
    """
    calls = _configure_recording_handler({"state": "complete"})
    await _register_op(
        op_id,
        safety_level=safety_level,
        embedding=stub_embedding_service,
        handler=_module_recording_handler,
    )
    monkeypatch.setattr(audit_module, "write_audit_row", _raise_audit_commit)

    with patch.object(audit_module, "_log", Mock()) as mock_log:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="demo-1.x",
            op_id=op_id,
            target=_FakeTarget(),
            params={"name": "widget"},
        )

    _assert_audit_commit_failed(result)
    assert calls == [{"name": "widget"}]
    # No phantom broadcast when the audit commit fails.
    assert captured_events == []
    # The error-level dispatch_audit_failed log line still fires.
    logged = [call.args[0] for call in mock_log.exception.call_args_list if call.args]
    assert "dispatch_audit_failed" in logged


# ---------------------------------------------------------------------------
# HALF 4 -- committed receipt and unavailable delivery after response shaping
# failures (#3636). These tests deliberately drive dispatch end to end: the
# handler completed, redaction supplied audit artefacts, the reducer failed,
# then the audit/broadcast boundary chose the caller-visible outcome.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["demo.thing.list", "demo.thing.create"])
async def test_completed_handler_reducer_failure_keeps_committed_audit_receipt(
    op_id: str,
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    committed_audit_writes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read and write handlers are not retried when only shaping fails.

    The reducer cannot change the completed upstream action into a caller
    error. The response is unavailable, while its one committed audit row
    retains the raw payload and redaction provenance needed to investigate.
    """
    raw_payload = {"credential": "raw-delivery-secret", "state": "complete"}
    expected_raw, expected_manifest, expected_policy = _install_preserved_redaction(
        monkeypatch,
        raw_payload=raw_payload,
    )
    calls = _configure_recording_handler(raw_payload)

    await _register_op(
        op_id,
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_recording_handler,
    )
    _install_exploding_reducer()
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="demo-1.x",
            op_id=op_id,
            target=_FakeTarget(),
            params={"request": "once"},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result.status == "ok"
    assert result.error is None
    assert result.result is None
    assert result.handle is None
    assert result.delivery == "unavailable"
    assert result.audit_id is not None
    # Completed with a committed row: still ok, and the caller must not retry.
    assert result.extras["retry"] is False
    assert "error_code" not in result.extras
    assert "raw-delivery-secret" not in str(result)
    assert "private diagnostic" not in str(result)
    assert calls == [{"request": "once"}]
    assert len(committed_audit_writes) == 1
    assert committed_audit_writes[0]["audit_id"] == result.audit_id
    rows = await _committed_rows_for(op_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == result.audit_id
    assert row.status_code == 200
    assert row.payload["result_status"] == "ok"
    assert row.raw_payload == expected_raw
    assert row.redaction_manifest == expected_manifest
    assert row.payload["redaction_policy_id"] == expected_policy
    assert len(captured_events) == 1
    assert captured_events[0].audit_id == result.audit_id
    assert captured_events[0].result_status == "ok"


@pytest.mark.asyncio
async def test_completed_write_reducer_failure_fails_closed_when_audit_cannot_commit(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutation with failed shaping still cannot report success without its row."""
    raw_payload = {"credential": "raw-delivery-secret"}
    _install_preserved_redaction(monkeypatch, raw_payload=raw_payload)
    calls = _configure_recording_handler(raw_payload)

    await _register_op(
        "demo.thing.create",
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_recording_handler,
    )
    monkeypatch.setattr(audit_module, "write_audit_row", _raise_audit_commit)
    _install_exploding_reducer()
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="demo-1.x",
            op_id="demo.thing.create",
            target=_FakeTarget(),
            params={},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    _assert_audit_commit_failed(result)
    assert "raw-delivery-secret" not in str(result)
    assert "private diagnostic" not in str(result)
    assert calls == [{}]
    assert captured_events == []


@pytest.mark.asyncio
async def test_completed_safe_read_reducer_failure_is_uncertain_without_audit_receipt(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safe read with failed shaping and no committed row is not ok either."""
    raw_payload = {"credential": "raw-delivery-secret"}
    _install_preserved_redaction(monkeypatch, raw_payload=raw_payload)
    calls = _configure_recording_handler(raw_payload)

    await _register_op(
        "demo.thing.list",
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_recording_handler,
    )
    monkeypatch.setattr(audit_module, "write_audit_row", _raise_audit_commit)
    _install_exploding_reducer()
    try:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="demo-1.x",
            op_id="demo.thing.list",
            target=_FakeTarget(),
            params={},
        )
    finally:
        set_default_reducer(PassThroughReducer())

    _assert_audit_commit_failed(result)
    assert "raw-delivery-secret" not in str(result)
    assert "private diagnostic" not in str(result)
    assert calls == [{}]
    assert captured_events == []


@pytest.mark.asyncio
async def test_broadcast_failure_after_audit_commit_keeps_receipt(
    stub_embedding_service: AsyncMock,
    committed_audit_writes: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed fan-out cannot erase the receipt for an already committed row."""
    calls = _configure_recording_handler({"state": "complete"})

    async def _raise_broadcast(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("broadcast private diagnostic")

    await _register_op(
        "demo.thing.create",
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_recording_handler,
    )
    monkeypatch.setattr(audit_module, "publish_event", _raise_broadcast)
    result = await dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id="demo.thing.create",
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "ok"
    assert result.delivery == "complete"
    assert result.audit_id is not None
    assert "broadcast private diagnostic" not in str(result)
    assert calls == [{}]
    assert len(committed_audit_writes) == 1
    assert committed_audit_writes[0]["audit_id"] == result.audit_id
    rows = await _committed_rows_for("demo.thing.create")
    assert len(rows) == 1
    assert rows[0].id == result.audit_id
    assert rows[0].status_code == 200
    assert rows[0].payload["result_status"] == "ok"


# ---------------------------------------------------------------------------
# HALF 5 -- an HTTPStatusError carrying an UNREAD streamed response must not
# crash the dispatcher's downstream body-read enrichment (B1-REGRESSION,
# #3720). The prior guest-ops get-failure test used an in-memory (pre-read)
# response and so never exercised the dispatcher's .text/.json body read on a
# real unread stream. This drives dispatch() end to end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_streamed_non2xx_returns_structured_error_with_audit_row(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    committed_audit_writes: list[dict[str, Any]],
) -> None:
    """A handler raising HTTPStatusError with an UNREAD streamed response is safe.

    dispatch() must return a structured ``connector_error`` and commit a
    synchronous error-audit row, never let ``httpx.ResponseNotRead`` escape from
    ``_http_upstream_message`` (which would break both the never-raises and the
    append-only-audit contracts).
    """
    await _register_op(
        "demo.stream.get",
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_streamed_non2xx_handler,
    )

    # dispatch() returns rather than raising ResponseNotRead.
    result = await dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id="demo.stream.get",
        target=_FakeTarget(),
        params={},
    )

    # Structured connector_error, classified off the 500 status.
    assert result.status == "error"
    assert result.error == "connector_error: HTTPStatusError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["http_status"] == 500
    # The unread streamed body yields no extractable upstream message -- the
    # hardened helper returns None instead of raising ResponseNotRead.
    assert result.extras["upstream_message"] is None

    # A synchronous error-audit row was committed (append-only-audit postulate):
    # the raise happened BEFORE _audit_error_and_return at 21a4a4c0, so no row
    # landed; the fix restores it.
    assert len(committed_audit_writes) == 1
    rows = await _committed_rows_for("demo.stream.get")
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
    assert rows[0].payload["error"]["error_code"] == "connector_error"


# ---------------------------------------------------------------------------
# A handler that RETURNS an error result, instead of raising (#4274)
# ---------------------------------------------------------------------------


_returned_result: list[OperationResult] = []


async def _module_returning_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> OperationResult:
    """Typed handler that returns a built OperationResult instead of raising."""
    return _returned_result[0]


def _returned_error() -> OperationResult:
    return OperationResult(
        status="error",
        op_id="demo.thing.returns",
        error="demo_refused: the handler stopped before it did anything",
        duration_ms=0.0,
        extras={"error_code": "demo_refused", "remediation": "Do something else."},
    )


@pytest.mark.asyncio
async def test_handler_returned_error_result_writes_one_execution_row(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A returned error result gets exactly one execution row with its error_code."""
    returned = _returned_error()
    _returned_result[:] = [returned]
    await _register_op(
        "demo.thing.returns",
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_returning_handler,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id="demo.thing.returns",
        target=_FakeTarget(),
        params={},
    )

    rows = await _committed_rows_for("demo.thing.returns")
    assert len(rows) == 1
    row = rows[0]
    assert row.event_class == AuditEventClass.OPERATION_EXECUTION.value
    assert row.payload["result_status"] == "error"
    assert row.payload["error"]["error_code"] == "demo_refused"

    # The caller gets the handler's result as it was, plus the audit receipt.
    assert result.audit_id == row.id
    assert result.model_dump(exclude={"audit_id"}) == returned.model_dump(exclude={"audit_id"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "returned",
    [
        OperationResult(
            status="awaiting_approval",
            op_id="demo.thing.returns",
            duration_ms=0.0,
            extras={"approval_request_id": "00000000-0000-0000-0000-000000000001"},
        ),
        OperationResult(
            status="denied",
            op_id="demo.thing.returns",
            error="denied: policy denied",
            duration_ms=0.0,
        ),
    ],
    ids=["awaiting_approval", "denied"],
)
async def test_handler_returned_non_error_result_is_unchanged(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    returned: OperationResult,
) -> None:
    """Waiting and denied results stay as before: passed through, no new row."""
    _returned_result[:] = [returned]
    await _register_op(
        "demo.thing.returns",
        safety_level="safe",
        embedding=stub_embedding_service,
        handler=_module_returning_handler,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="demo-1.x",
        op_id="demo.thing.returns",
        target=_FakeTarget(),
        params={},
    )

    assert result == returned
    assert await _committed_rows_for("demo.thing.returns") == []
