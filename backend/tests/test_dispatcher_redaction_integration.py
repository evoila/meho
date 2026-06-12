# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector-boundary redaction middleware integration tests (#1071).

Acceptance criteria covered:

* An operation returning a payload with a bearer token / kubeconfig
  returns the **redacted** form to the caller; the raw is retrievable
  from the audit row; a manifest is recorded.
* User-path and agent-path calls both have redacted-out / raw-in-audit.
* Per-``connector_id`` policy selection works (a registered override
  beats the default).
* Default-safe: no policy registered → conservative default still
  strips credentials (not pass-through).

The tests live in a dedicated file (not appended to
``test_operations_dispatcher.py``) so the redaction-specific fixtures
(resolver-override reset, capturing audit row's redaction columns) and
the file's purpose stay focused. Shares the same plumbing the main
dispatcher tests use: typed-op registration against an in-memory
SQLite, recording broadcast publisher, autouse-reset.
"""

from __future__ import annotations

import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.redaction import (
    clear_overrides,
    parse_policy,
    register_policy,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars Settings requires; clear cache around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry + redaction overrides."""
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()
    yield
    reset_dispatcher_caches()
    clear_registry()
    clear_overrides()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation``
    doesn't pull ONNX."""
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
    """Yield an AsyncSession against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = "op-test",
    tenant_id: UUID | None = None,
    principal_kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
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
    """Duck-typed fingerprint."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target the resolver / dispatcher reads from."""

    def __init__(
        self,
        *,
        product: str = "vault",
        target_id: UUID | None = None,
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.id: UUID = target_id or uuid.uuid4()
        self.name = "test-target"
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


class _NoOpVaultConnector(Connector):
    """Stub connector so the v2 registry has something to resolve.

    Typed-op tests don't call into the connector's ``execute`` method
    (the dispatcher routes through the registered typed handler);
    the concrete methods raise so a future test that accidentally
    drives through the ingested path surfaces the mismatch loudly.
    """

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


# ---------------------------------------------------------------------------
# Test handlers
# ---------------------------------------------------------------------------


async def _handler_returning_bearer_token(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Simulate a connector that returns a bearer token in its response."""
    return {
        "token_header": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "kubeconfig": textwrap.dedent(
            """
            apiVersion: v1
            kind: Config
            clusters:
              - name: prod
                cluster:
                  server: https://k8s.example.com
            """
        ).strip(),
        "id": "deadbeef-1234-5678-90ab-cdef12345678",
    }


async def _handler_returning_refresh_token(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Simulate a connector response that embeds a refresh_token label.

    Mirrors real-world OAuth error bodies (e.g. an ingested OpenAPI connector
    echoing ``refresh_token: <value>`` in a structured error field) that
    previously slipped through Tier-1 because the _API_KEY alternation did
    not cover the bare ``refresh_token`` label.
    """
    # Fragments split so gitleaks' generic-api-key scanner does not
    # false-positive on the test source.
    raw_value = "rt_abcdefgh" + "ijkl1234"
    return {
        "error": "Authorization failed, refresh_token: " + raw_value,
        "hint": "token expired",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_path_redacts_response_and_audits_raw(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """User-path dispatch:

    * caller sees the redacted view (no bearer, no kubeconfig);
    * the audit row stores the raw payload verbatim;
    * the audit row stores the manifest with rule firings;
    * the audit row's payload carries the resolved policy id.
    """
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.user.read",
        handler=_handler_returning_bearer_token,
        summary="Read secrets via user-path.",
        description="Read.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.USER)
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.user.read",
        target=target,
        params={},
    )

    # 1) Caller sees the redacted view.
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    serialised = str(result.result)
    assert "Bearer eyJ" not in serialised
    assert "[REDACTED:authorization_header]" in serialised
    assert "[REDACTED:kubeconfig]" in serialised

    # 2) Audit row holds raw payload + manifest.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.user.read")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.status_code == 200
    assert row.raw_payload is not None
    assert "Bearer eyJ" in str(row.raw_payload)
    # 3) Manifest persisted as a list of dicts; at least authorization
    #    header + kubeconfig rules fired.
    assert isinstance(row.redaction_manifest, list)
    rule_names = {entry["rule"] for entry in row.redaction_manifest}
    assert "strip-authorization-header" in rule_names
    assert "strip-kubeconfig" in rule_names
    # 4) Policy id mirrored into payload.
    assert row.payload["redaction_policy_id"] == "connector-boundary-default"


@pytest.mark.asyncio
async def test_agent_path_redacts_response_and_audits_raw(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Agent-path dispatch (``PrincipalKind.AGENT``) walks the same
    redaction middleware: the agent / LLM never sees the raw bearer,
    the audit row still keeps the raw payload + manifest."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.agent.read",
        handler=_handler_returning_bearer_token,
        summary="Read secrets via agent-path.",
        description="Read.",
        parameter_schema={"type": "object"},
        # Safe op: an agent dispatching this auto-executes (no permission
        # row needed; the resolver's safety_level default is auto-execute
        # for safe ops).
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.AGENT)
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.agent.read",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error
    serialised = str(result.result)
    assert "Bearer eyJ" not in serialised
    assert "[REDACTED:authorization_header]" in serialised

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.agent.read")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert "Bearer eyJ" in str(row.raw_payload)
    assert isinstance(row.redaction_manifest, list)
    assert any(entry["rule"] == "strip-authorization-header" for entry in row.redaction_manifest)
    assert row.payload["redaction_policy_id"] == "connector-boundary-default"


@pytest.mark.asyncio
async def test_per_connector_id_policy_override_takes_effect(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """A policy registered for the call's connector_id replaces the
    default-safe answer. The override here only redacts UUIDs, so the
    bearer-token leaks through (proving the override is what fired,
    not the default)."""
    uuid_only = parse_policy(
        textwrap.dedent(
            """
            id: uuid-only-test
            version: 1
            rules:
              - name: redact-uuid
                pattern: uuid
                action: redact
                reason: "test override"
            """
        ).strip()
    )
    register_policy(uuid_only, connector_id="vault-1.x")

    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.override.read",
        handler=_handler_returning_bearer_token,
        summary="Read with an override policy.",
        description="Read.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.USER)
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.override.read",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error
    serialised = str(result.result)
    # UUID was redacted by the override.
    assert "[REDACTED:uuid]" in serialised
    # Bearer survived -- the override has no bearer rule, proving the
    # default was NOT applied for this call.
    assert "Bearer eyJ" in serialised

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "vault.override.read")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["redaction_policy_id"] == "uuid-only-test"


@pytest.mark.asyncio
async def test_default_safe_is_not_pass_through(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Without any override registered, a credential-shaped response
    is still redacted. The default policy must apply the named-pattern
    library, not let raw responses through."""
    # No register_policy() call here; clear_overrides ran in the autouse
    # fixture so the resolver falls through to the default.
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.default.read",
        handler=_handler_returning_bearer_token,
        summary="Read with default policy only.",
        description="Read.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.USER)
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.default.read",
        target=target,
        params={},
    )

    assert result.status == "ok", result.error
    assert "Bearer eyJ" not in str(result.result)
    assert "[REDACTED:" in str(result.result)


@pytest.mark.asyncio
async def test_refresh_token_label_redacted_in_caller_view_raw_retained_in_audit(
    stub_embedding_service: AsyncMock,
    captured_events: list[BroadcastEvent],
) -> None:
    """Regression: a connector response embedding ``refresh_token: <value>``
    must be redacted in the caller view AND the raw value must be retained in
    the audit row with a ``strip-api-key`` manifest entry.

    Acceptance criterion 5 from Task #94: covers one of the six labels that
    previously slipped through Tier-1 (``refresh_token``); the other five are
    covered by the unit tests in ``test_redaction_patterns.py``.
    """
    raw_value = "rt_abcdefgh" + "ijkl1234"  # same fragment split as the handler

    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.refresh-token.read",
        handler=_handler_returning_refresh_token,
        summary="Read with refresh_token in response.",
        description="Read.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )

    operator = _make_operator(principal_kind=PrincipalKind.USER)
    target = _FakeTarget(product="vault")

    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.refresh-token.read",
        target=target,
        params={},
    )

    # 1) Caller / agent view must not expose the raw token value.
    assert result.status == "ok", result.error
    serialised = str(result.result)
    assert raw_value not in serialised
    assert "[REDACTED:api_key]" in serialised

    # 2) Audit row keeps the unredacted raw payload.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(AuditLog.path == "vault.refresh-token.read")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.raw_payload is not None
    assert raw_value in str(row.raw_payload)

    # 3) Manifest records a strip-api-key firing.
    assert isinstance(row.redaction_manifest, list)
    rule_names = {entry["rule"] for entry in row.redaction_manifest}
    assert "strip-api-key" in rule_names
