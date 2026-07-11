# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``connector_vault_forbidden`` structured error.

#2091 (Initiative #2150) acceptance criteria:

* A dispatch raising :exc:`hvac.exceptions.Forbidden` (Vault answering
  ``permission denied`` — the load-bearing case is credential resolution
  reading a ``target.secret_ref`` outside the readable per-tenant
  subtree, ``connectors/_shared/vault_creds.py``) returns a structured
  ``connector_vault_forbidden`` :class:`OperationResult` — NOT the bare
  ``connector_error: Forbidden`` that buried the cause in
  ``extras["exception_message"]`` and read exactly like a missing Vault
  grant (inviting the wrong fix: widening the deploy-owned Vault
  policy). The operator-facing ``error`` names the target's
  ``secret_ref``, the ``tenants/<tenant_id>/<name>`` convention, and the
  exact expected path; ``extras`` carries ``secret_ref`` /
  ``expected_secret_ref`` / ``exception_class`` / ``exception_message``.
* A target-less dispatch (a typed ``vault.*`` op denied by the Vault ACL
  itself) still yields the structured cause with the generic message
  shape — no fabricated ``secret_ref`` diagnosis.
* Any other hvac error (e.g. :exc:`hvac.exceptions.InvalidPath`) is
  unchanged — it falls through to the generic ``connector_error``
  flatten.
* A successful dispatch is unaffected.

The builder-shape tests mirror the #1649
``test_operations_connector_http_403`` discipline
(``docs/codebase/error-message-shape.md``): stable code, diagnostic
human message with a remediation imperative + doc reference, structured
``extras`` payload.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import result_connector_vault_forbidden
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / isolation fixtures (the sibling dispatcher-test pattern)
# ---------------------------------------------------------------------------

_TENANT: UUID = UUID("00000000-0000-0000-0000-000000000829")


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
    """Deterministic embedding stub so descriptor inserts don't pull ONNX."""
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
    """Yield an :class:`AsyncSession` against the autouse-migrated engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(*, sub: str = "op-vault-forbidden") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Vault-Forbidden Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for resolver lookups."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal target shape the resolver / dispatcher / connectors read."""

    def __init__(
        self,
        *,
        product: str = "vfb",
        version: str | None = "9",
        name: str = "vcf-prod",
        host: str = "vrli.corp.internal",
        port: int = 443,
        auth_model: str | None = "shared_service_account",
        secret_ref: str | None = "secret/meho/vcf-logs/logmaster",
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=version)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = name
        self.host = host
        self.port = port
        self.auth_model = auth_model
        self.secret_ref = secret_ref


def _make_forbidden(
    path: str = "secret/meho/vcf-logs/logmaster",
) -> hvac.exceptions.Forbidden:
    """Build the hvac exception the production credential read raises.

    hvac 2.4.0 renders a KV-v2 metadata denial as ``permission denied,
    on GET https://<vault>/v1/<mount>/metadata/<path>`` — the shape the
    consumer report quoted verbatim.
    """
    return hvac.exceptions.Forbidden(
        "1 error occurred:\n\t* permission denied",
        errors=["permission denied"],
        method="GET",
        url=f"https://vault.test/v1/secret/metadata/{path}",
    )


class _VaultForbiddenConnector(HttpConnector):
    """Connector whose dispatch fails credential resolution with hvac Forbidden.

    Mirrors the production shape: the shared operator-context reader
    (``connectors/_shared/vault_creds.py``) lets
    :exc:`hvac.exceptions.Forbidden` propagate raw out of the KV-v2
    read, through the connector's session/auth path, into the
    dispatcher's error-handling arms. The transport is short-circuited
    so the test needs no live Vault.
    """

    product = "vfb"
    version = "9"
    impl_id = "vfb-rest"
    supported_version_range = ">=9,<10"
    priority = 1

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise _make_forbidden()

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
        raise _make_forbidden()

    async def fingerprint(  # type: ignore[override]
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        # Ingested dispatch routes through _post_json / _request_json, not
        # execute; this concrete impl only makes the abstract base
        # instantiable.
        raise NotImplementedError


class _InvalidPathConnector(_VaultForbiddenConnector):
    """Same shape but raises hvac's InvalidPath -- the scope-boundary pin.

    Only :exc:`hvac.exceptions.Forbidden` is siphoned into the
    structured cause; every other hvac error falls through to the
    generic ``connector_error`` flatten unchanged.
    """

    impl_id = "vfb-rest-404"

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
        raise hvac.exceptions.InvalidPath("no value found")

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise hvac.exceptions.InvalidPath("no value found")


async def _insert_ingested_descriptor(
    *,
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    embedding: list[float],
    method: str = "GET",
    path: str = "/api/v1/events",
) -> None:
    """Seed one enabled ``source_kind='ingested'`` descriptor row."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        handler_ref=None,
        summary="Query events.",
        description="Ingested read test op.",
        tags=[],
        parameter_schema={"type": "object", "properties": {}},
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
    session.add(descriptor)
    await session.commit()


# ---------------------------------------------------------------------------
# Builder shape (docs/codebase/error-message-shape.md / #1141 convention)
# ---------------------------------------------------------------------------


def test_builder_shape_with_secret_ref_names_cause_and_convention() -> None:
    """Credential-resolution shape: ref, convention, expected path, remediation."""
    target = _FakeTarget()
    out = result_connector_vault_forbidden(
        "GET:/api/v1/events",
        _make_forbidden(),
        target,
        duration_ms=1.0,
        expected_secret_ref=f"tenants/{_TENANT}/vcf-prod",
    )

    assert out.status == "error"
    assert out.op_id == "GET:/api/v1/events"
    assert out.error is not None
    assert out.error.startswith("connector_vault_forbidden:")
    # Names the target's secret_ref and the likely out-of-subtree cause.
    assert "'secret/meho/vcf-logs/logmaster'" in out.error
    assert "outside the operator's readable per-tenant subtree" in out.error
    # Names the convention + the exact expected path.
    assert "tenants/<tenant_id>/<name>" in out.error
    assert f"tenants/{_TENANT}/vcf-prod" in out.error
    # Remediation imperative + the policy warning + doc references.
    assert "Do NOT widen the backplane's Vault policy" in out.error
    assert "docs/codebase/connectors-vault-tenant-scope.md" in out.error
    assert "docs/codebase/error-message-shape.md" in out.error
    # The hvac message tails the operator-facing string.
    assert "Vault said:" in out.error
    assert "permission denied" in out.error
    # Structured, machine-usable extras.
    assert out.extras["error_code"] == "connector_vault_forbidden"
    assert out.extras["secret_ref"] == "secret/meho/vcf-logs/logmaster"
    assert out.extras["expected_secret_ref"] == f"tenants/{_TENANT}/vcf-prod"
    assert out.extras["exception_class"] == "Forbidden"
    assert "permission denied" in out.extras["exception_message"]


def test_builder_shape_without_target_is_generic() -> None:
    """Target-less shape: generic Vault-authorization cause, no fabricated ref."""
    out = result_connector_vault_forbidden(
        "vault.token.create",
        hvac.exceptions.Forbidden("permission denied"),
        None,
        duration_ms=1.0,
    )
    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("connector_vault_forbidden:")
    assert "under the operator's identity" in out.error
    # No secret_ref diagnosis fabricated for a target-less denial.
    assert "secret_ref" not in out.error
    assert out.extras["error_code"] == "connector_vault_forbidden"
    assert out.extras["secret_ref"] is None
    assert out.extras["expected_secret_ref"] is None
    assert out.extras["exception_class"] == "Forbidden"


def test_builder_shape_without_expected_ref_falls_back_to_convention() -> None:
    """No derivable expected path: the convention template is named instead."""
    out = result_connector_vault_forbidden(
        "GET:/api/v1/events",
        _make_forbidden(),
        _FakeTarget(),
        duration_ms=1.0,
        expected_secret_ref=None,
    )
    assert out.error is not None
    assert "'tenants/<tenant_id>/<name>' on the 'secret' mount" in out.error
    assert out.extras["expected_secret_ref"] is None


def test_builder_caps_oversized_message() -> None:
    """A pathological hvac message is capped like the sibling builders."""
    out = result_connector_vault_forbidden(
        "GET:/x",
        hvac.exceptions.Forbidden("x" * 400),
        _FakeTarget(),
        duration_ms=0.5,
    )
    message = out.extras["exception_message"]
    assert isinstance(message, str)
    assert message.endswith("...<truncated>")
    assert len(message) == 256 + len("...<truncated>")


def test_builder_empty_message_has_no_dangling_tail() -> None:
    """An empty exception message leaves no dangling ``Vault said:`` tail.

    hvac's own ``VaultError.__str__`` always renders a non-empty string
    (it appends ``, on <method> <url>``), so a plain empty exception
    pins the guard for any future raise shape.
    """
    out = result_connector_vault_forbidden(
        "GET:/x",
        Exception(),
        _FakeTarget(),
        duration_ms=0.5,
    )
    assert out.error is not None
    assert "Vault said:" not in out.error


# ---------------------------------------------------------------------------
# Write-identity-forbidden builder shape (#2331)
# ---------------------------------------------------------------------------


def test_write_forbidden_names_path_identity_and_write_stanza() -> None:
    """Write shape: path, acting identity, §6.1 stanza, do-not-widen warning."""
    from meho_backplane.operations._errors import result_connector_vault_write_forbidden

    out = result_connector_vault_write_forbidden(
        "vault.kv.put",
        _make_forbidden("targets/op-x/prod"),
        duration_ms=1.0,
        write_path="secret/data/targets/op-x/prod",
        identity_hint="op-x",
    )
    assert out.status == "error"
    assert out.op_id == "vault.kv.put"
    assert out.error is not None
    assert out.error.startswith("vault_write_identity_forbidden:")
    # Names the denied data path + the acting identity.
    assert "'secret/data/targets/op-x/prod'" in out.error
    assert "'op-x'" in out.error
    # Frames it as a write-identity gap, not a read/secret_ref diagnosis.
    assert "lacks 'create'/'update'" in out.error
    assert "secret_ref" not in out.error
    # Remediation names the write stanza + probe + the policy-doc contract.
    assert "§6.1" in out.error
    assert "§6.2" in out.error
    assert "Do NOT widen the backplane's shared Vault policy" in out.error
    assert "docs/cross-repo/connector-vault-policy.md" in out.error
    # hvac tail present.
    assert "Vault said:" in out.error
    assert "permission denied" in out.error
    # Structured, machine-usable extras.
    assert out.extras["error_code"] == "vault_write_identity_forbidden"
    assert out.extras["path"] == "secret/data/targets/op-x/prod"
    assert out.extras["identity_hint"] == "op-x"
    assert out.extras["doc_ref"].startswith("docs/cross-repo/connector-vault-policy.md")
    assert out.extras["exception_class"] == "Forbidden"
    assert "permission denied" in out.extras["exception_message"]


def test_write_forbidden_omits_clauses_when_path_and_identity_absent() -> None:
    """No path / identity → no fabricated clause, extras carry None."""
    from meho_backplane.operations._errors import result_connector_vault_write_forbidden

    out = result_connector_vault_write_forbidden(
        "vault.kv.delete",
        hvac.exceptions.Forbidden("permission denied"),
        duration_ms=1.0,
        write_path=None,
        identity_hint=None,
    )
    assert out.error is not None
    assert out.error.startswith("vault_write_identity_forbidden:")
    assert "dispatched under identity" not in out.error
    assert out.extras["path"] is None
    assert out.extras["identity_hint"] is None


def test_write_forbidden_caps_oversized_message() -> None:
    """A pathological hvac message is capped like the read sibling."""
    from meho_backplane.operations._errors import result_connector_vault_write_forbidden

    out = result_connector_vault_write_forbidden(
        "vault.kv.put",
        hvac.exceptions.Forbidden("x" * 400),
        duration_ms=0.5,
        write_path="secret/data/x",
    )
    message = out.extras["exception_message"]
    assert isinstance(message, str)
    assert message.endswith("...<truncated>")


def test_write_forbidden_empty_message_has_no_dangling_tail() -> None:
    """An empty exception leaves no dangling ``Vault said:`` tail."""
    from meho_backplane.operations._errors import result_connector_vault_write_forbidden

    out = result_connector_vault_write_forbidden(
        "vault.kv.put",
        Exception(),
        duration_ms=0.5,
        write_path="secret/data/x",
    )
    assert out.error is not None
    assert "Vault said:" not in out.error


# ---------------------------------------------------------------------------
# Dispatcher conversion (the #2091 acceptance-criterion tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_converts_vault_forbidden_to_structured_cause(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """hvac Forbidden dispatch -> structured cause + audit row + event.

    The dispatcher catches :exc:`hvac.exceptions.Forbidden` ahead of the
    generic ``except Exception`` and emits the structured shape naming
    the target's ``secret_ref`` and the exact expected per-tenant path —
    not the pre-#2091 bare ``connector_error: Forbidden`` that read like
    a missing Vault grant.
    """
    register_connector_v2(
        product="vfb",
        version="9",
        impl_id="vfb-rest",
        cls=_VaultForbiddenConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vfb",
        version="9",
        impl_id="vfb-rest",
        op_id="GET:/api/v1/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vfb-rest-9",
        op_id="GET:/api/v1/events",
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_vault_forbidden:")
    assert result.extras["error_code"] == "connector_vault_forbidden"
    assert result.extras["secret_ref"] == "secret/meho/vcf-logs/logmaster"
    # The dispatcher derives the exact expected per-tenant path from the
    # operator's tenant + the target's name.
    assert result.extras["expected_secret_ref"] == f"tenants/{_TENANT}/vcf-prod"
    assert f"tenants/{_TENANT}/vcf-prod" in result.error
    # NOT the pre-#2091 flattened shape.
    assert not result.error.startswith("connector_error:")
    assert result.extras["error_code"] != "connector_error"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "GET:/api/v1/events")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_other_hvac_errors_fall_through_to_connector_error(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """hvac InvalidPath is unchanged -- generic ``connector_error`` flatten.

    Scope boundary (#2091 AC): only :exc:`hvac.exceptions.Forbidden` is
    siphoned into the structured cause; every other hvac error falls
    through to the existing generic catch.
    """
    register_connector_v2(
        product="vfb",
        version="9",
        impl_id="vfb-rest-404",
        cls=_InvalidPathConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="vfb",
        version="9",
        impl_id="vfb-rest-404",
        op_id="GET:/api/v1/events",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vfb-rest-404-9",
        op_id="GET:/api/v1/events",
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_error:")
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "InvalidPath"
    # Did NOT get reclassified as the Forbidden shape.
    assert "connector_vault_forbidden" not in result.error
    assert "expected_secret_ref" not in result.extras

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"
