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

* HALF 3 (fail-closed audit precondition, S07 #295) -- on the success
  path a write-class / post-approval dispatch whose DISPATCH audit row
  cannot commit returns a distinct ``connector_error`` instead of
  ``status='ok'`` (CLAUDE.md postulate 7 / v0.1-spec §6), with no
  phantom broadcast. Read-class ``safe`` ops keep the historical
  fail-open posture. The gate lives in
  :func:`~meho_backplane.operations.dispatcher._requires_durable_audit`
  and is honoured by ``audit_and_broadcast_safe``'s ``require_audit`` flag.
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

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import _build_audit_payload
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations.dispatcher import _requires_durable_audit
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


async def _register_op(op_id: str, *, safety_level: str, embedding: AsyncMock) -> None:
    register_connector_v2(product="demo", version="", impl_id="", cls=_NoOpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=_module_mutating_handler,
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


def test_requires_durable_audit_gates_on_write_class_and_safety_tier() -> None:
    """The gate is True for write-class OR non-``safe`` ops, False for safe reads."""
    # write-class op is fail-closed even at the safe tier ...
    assert _requires_durable_audit(SimpleNamespace(op_id="demo.thing.create", safety_level="safe"))
    # ... including the credential-write class.
    assert _requires_durable_audit(SimpleNamespace(op_id="vault.kv.put", safety_level="safe"))
    # A safe read-class op keeps the historical fail-open posture.
    assert not _requires_durable_audit(
        SimpleNamespace(op_id="demo.thing.list", safety_level="safe")
    )
    # A read-class op above the safe tier (the post-approval superset) is fail-closed.
    assert _requires_durable_audit(SimpleNamespace(op_id="demo.thing.list", safety_level="caution"))
    assert _requires_durable_audit(
        SimpleNamespace(op_id="demo.thing.get", safety_level="destructive")
    )


@pytest.mark.asyncio
async def test_write_class_dispatch_fails_closed_when_audit_row_cannot_commit(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write-class op whose DISPATCH audit row cannot commit returns connector_error."""
    await _register_op("demo.thing.create", safety_level="safe", embedding=stub_embedding_service)
    monkeypatch.setattr(audit_module, "write_audit_row", _raise_audit_commit)

    with patch.object(audit_module, "_log", Mock()) as mock_log:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="demo-1.x",
            op_id="demo.thing.create",
            target=_FakeTarget(),
            params={"name": "widget"},
        )

    # AC1: an error OperationResult, not wrap_ok_result.
    assert result.status != "ok"
    assert result.status == "error"
    # AC2: a distinct connector_error code, not a generic ok envelope.
    assert result.error == "connector_error: AuditCommitError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "AuditCommitError"
    # AC3: no phantom broadcast when the audit commit fails.
    assert captured_events == []
    # AC4: the error-level dispatch_audit_failed log line still fires.
    logged = [call.args[0] for call in mock_log.exception.call_args_list if call.args]
    assert "dispatch_audit_failed" in logged


@pytest.mark.asyncio
async def test_read_class_dispatch_stays_fail_open_when_audit_row_cannot_commit(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safe read-class op keeps the historical fail-open behaviour (still ok)."""
    await _register_op("demo.thing.list", safety_level="safe", embedding=stub_embedding_service)
    monkeypatch.setattr(audit_module, "write_audit_row", _raise_audit_commit)

    with patch.object(audit_module, "_log", Mock()) as mock_log:
        result = await dispatch(
            operator=_make_operator(),
            connector_id="demo-1.x",
            op_id="demo.thing.list",
            target=_FakeTarget(),
            params={},
        )

    # Fail-open preserved for read-class: the caller still sees ok.
    assert result.status == "ok", result.error
    # The broadcast is still skipped when the audit row does not land.
    assert captured_events == []
    # The failure is still recorded for the on-call.
    logged = [call.args[0] for call in mock_log.exception.call_args_list if call.args]
    assert "dispatch_audit_failed" in logged
