# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``connector_unsupported`` structured error.

G0.23-T1 (#1627) acceptance criteria:

* A connector raising :exc:`NotImplementedError` on dispatch returns a
  structured ``connector_unsupported`` :class:`OperationResult` naming
  the cause + remediation -- NOT the bare
  ``connector_error: NotImplementedError`` that buried the diagnostic
  in ``extras["exception_message"]``. Both cause branches are covered:
  ``unsupported_feature`` (a hand-rolled connector rejecting the
  target's ``auth_model``) and ``unreplaced_auto_shim`` (the resolved
  connector is the ingest-time :class:`GenericRestConnector` shim).
* No information lost -- the two production raise-site messages
  (``VmwareRestConnector.auth_headers`` and the auto-shim's
  ``auth_headers``) are preserved **verbatim** in ``extras["detail"]``
  and inside the operator-facing ``error`` string.
* The structured envelope is reachable from both the REST dispatch
  response and the MCP dispatch tool: both transports return
  :func:`~meho_backplane.operations.meta_tools.call_operation`'s
  serialized envelope verbatim (``api/v1/operations.py::post_call``
  and ``mcp/tools/operations.py::_call_operation_handler``), so the
  parity test drives that shared funnel.

The builder-shape tests mirror the #1601
``test_result_composite_l2_disabled_shape_matches_t11_convention``
discipline (``docs/codebase/error-message-shape.md``): stable code,
diagnostic-bearing human message with a remediation imperative + doc
reference, structured ``extras`` payload.
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
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel, FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import result_connector_unsupported
from meho_backplane.operations.ingest.connector_registration import (
    ensure_connector_class_registered,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings / isolation fixtures (the sibling dispatcher-test pattern)
# ---------------------------------------------------------------------------

_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000c0c0")


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


def _make_operator(*, sub: str = "op-conn-unsupported") -> Operator:
    """Construct an :class:`Operator` directly -- no JWT round-trip."""
    return Operator(
        sub=sub,
        name="Connector-Unsupported Test Operator",
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
        product: str = "demo",
        version: str | None = "1.0",
        name: str = "demo-target",
        host: str = "demo.example.com",
        port: int = 443,
        auth_model: str | None = "shared_service_account",
    ) -> None:
        self.product = product
        self.fingerprint = _FakeFingerprint(version=version)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        # The shared HTTP client pool keys on ``target_cache_key``
        # (``(tenant_id, id)``); without ``tenant_id`` any double that
        # reaches the pool hits ``AttributeError`` (evoila/meho#1682).
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a0")
        self.name = name
        self.host = host
        self.port = port
        self.auth_model = auth_model
        self.secret_ref: str | None = None


class _AuthModelRejectingConnector(HttpConnector):
    """Hand-rolled connector whose ``auth_headers`` rejects the auth mode.

    Mirrors the ``VmwareRestConnector.auth_headers`` raise shape (the
    real connector is exercised separately in the verbatim-preservation
    test below) without dragging in the vSphere session machinery.
    """

    product = "demo"
    version = "1.0"
    impl_id = "demo-rest"
    supported_version_range = ">=1.0,<2.0"
    priority = 1

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        raise NotImplementedError(
            f"_AuthModelRejectingConnector only supports "
            f"auth_model='shared_service_account'; target {target.name!r} "
            f"requested auth_model={target.auth_model!r}"
        )

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
    ) -> OperationResult:
        raise NotImplementedError


async def _insert_ingested_descriptor(
    *,
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    embedding: list[float],
) -> None:
    """Seed one enabled ``source_kind='ingested'`` GET descriptor row."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind="ingested",
        method="GET",
        path="/api/widgets",
        handler_ref=None,
        summary="List widgets.",
        description="Ingested test op.",
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


def test_result_connector_unsupported_shape_unsupported_feature() -> None:
    """``unsupported_feature``: code + verbatim detail + config remediation + doc ref."""
    exc = NotImplementedError(
        "VmwareRestConnector only supports auth_model='shared_service_account'; "
        "target 'vc01' requested auth_model='per_user'"
    )
    out = result_connector_unsupported(
        "GET:/vcenter/vm",
        exc,
        cause="unsupported_feature",
        connector_class="VmwareRestConnector",
        duration_ms=1.0,
    )
    assert out.status == "error"
    assert out.op_id == "GET:/vcenter/vm"
    assert out.error is not None
    assert out.error.startswith("connector_unsupported:")
    # The raise-site diagnostic is promoted verbatim into the
    # operator-facing error string, not buried in extras alone.
    assert str(exc) in out.error
    # Remediation imperative: a config matter, not a code gap.
    assert "Re-check the target's configuration" in out.error
    assert "auth_model" in out.error
    # Doc reference for the connector auth contract.
    assert "docs/architecture/connector-auth.md" in out.error
    assert out.extras == {
        "error_code": "connector_unsupported",
        "cause": "unsupported_feature",
        "connector_class": "VmwareRestConnector",
        "detail": str(exc),
        # G0.25-T2 (#1753): additive field; None on the non-shim cause.
        "sibling_impl_id": None,
    }


def test_result_connector_unsupported_shape_unreplaced_auto_shim() -> None:
    """``unreplaced_auto_shim`` with no sibling: subclass-registration remediation.

    The no-sibling path (``sibling_impl_id`` omitted / ``None``) keeps
    the original wording — "register the per-product subclass, re-ingesting
    will NOT replace the shim". The sibling-present fork added by
    G0.25-T2 (#1753) is covered in ``test_connector_registration.py``.
    """
    exc = NotImplementedError(
        "auto-registered shim for ('acme', '1.0', 'acme-rest') must be "
        "replaced with a per-product Connector subclass before dispatch "
        "is enabled -- the operator's G3.x Initiative work adds "
        "auth_headers() per target.auth_model"
    )
    out = result_connector_unsupported(
        "GET:/api/widgets",
        exc,
        cause="unreplaced_auto_shim",
        connector_class="AutoShim_acme_1_0_acme_rest",
        duration_ms=1.0,
    )
    assert out.status == "error"
    assert out.error is not None
    assert out.error.startswith("connector_unsupported:")
    assert str(exc) in out.error
    # Remediation names the real fix (register the per-product subclass)
    # and explicitly rules out the wrong one (re-ingesting the spec).
    assert "Register the hand-rolled per-product Connector subclass" in out.error
    assert "re-ingesting the spec will NOT replace the shim" in out.error
    assert "docs/codebase/spec-ingestion.md" in out.error
    assert out.extras == {
        "error_code": "connector_unsupported",
        "cause": "unreplaced_auto_shim",
        "connector_class": "AutoShim_acme_1_0_acme_rest",
        "detail": str(exc),
        # G0.25-T2 (#1753): no hand-rolled sibling for this label.
        "sibling_impl_id": None,
    }


def test_result_connector_unsupported_caps_oversized_detail() -> None:
    """A pathological exception message is capped like ``connector_error``'s."""
    out = result_connector_unsupported(
        "GET:/x",
        NotImplementedError("x" * 400),
        cause="unsupported_feature",
        connector_class=None,
        duration_ms=0.5,
    )
    detail = out.extras["detail"]
    assert isinstance(detail, str)
    assert detail.endswith("...<truncated>")
    assert len(detail) == 256 + len("...<truncated>")
    # No connector class resolved -> the message names the handler.
    assert out.error is not None
    assert "The resolved handler" in out.error


# ---------------------------------------------------------------------------
# No information lost: the two production raise-site messages verbatim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_vmware_auth_model_message_preserved_verbatim() -> None:
    """The real ``VmwareRestConnector.auth_headers`` text survives verbatim."""

    async def _never_called_loader(target: Any, operator: Operator) -> Any:
        raise AssertionError("session loader must not run for a rejected auth_model")

    connector = VmwareRestConnector(session_loader=_never_called_loader)
    target = _FakeTarget(
        product="vmware",
        version="9.0",
        name="vcenter-per-user",
        auth_model=AuthModel.PER_USER.value,
    )
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, _make_operator())

    out = result_connector_unsupported(
        "GET:/vcenter/vm",
        exc_info.value,
        cause="unsupported_feature",
        connector_class=type(connector).__name__,
        duration_ms=2.0,
    )
    # Verbatim: the structured detail IS the raise-site message.
    assert out.extras["detail"] == str(exc_info.value)
    assert out.error is not None
    assert str(exc_info.value) in out.error
    # The message already names the target and both auth models.
    assert "vcenter-per-user" in out.error
    assert AuthModel.SHARED_SERVICE_ACCOUNT.value in out.error
    assert AuthModel.PER_USER.value in out.error


@pytest.mark.asyncio
async def test_real_auto_shim_message_preserved_verbatim() -> None:
    """The real :class:`GenericRestConnector` shim text survives verbatim."""
    assert ensure_connector_class_registered(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        base_url=None,
    )
    shim_cls = all_connectors_v2()[("acme", "1.0", "acme-rest")]
    shim = shim_cls()
    with pytest.raises(NotImplementedError) as exc_info:
        await shim.auth_headers(_FakeTarget(product="acme"), _make_operator())

    out = result_connector_unsupported(
        "GET:/api/widgets",
        exc_info.value,
        cause="unreplaced_auto_shim",
        connector_class=type(shim).__name__,
        duration_ms=2.0,
    )
    assert out.extras["detail"] == str(exc_info.value)
    assert out.error is not None
    assert str(exc_info.value) in out.error
    assert "must be replaced with a per-product Connector subclass" in out.error
    assert out.extras["connector_class"] == "AutoShim_acme_1_0_acme_rest"


# ---------------------------------------------------------------------------
# Dispatcher conversion (the #1627 acceptance-criterion unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_converts_auth_model_nie_to_connector_unsupported(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Unsupported-auth_model branch: structured result, audit row, event.

    The dispatcher catches the connector's :exc:`NotImplementedError`
    ahead of the generic ``except Exception`` and emits
    ``connector_unsupported`` with ``cause='unsupported_feature'`` --
    not the pre-#1627 bare ``connector_error: NotImplementedError``.
    """
    register_connector_v2(
        product="demo",
        version="1.0",
        impl_id="demo-rest",
        cls=_AuthModelRejectingConnector,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="demo",
        version="1.0",
        impl_id="demo-rest",
        op_id="GET:/api/widgets",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    target = _FakeTarget(auth_model="per_user", name="demo-per-user")
    result = await dispatch(
        operator=_make_operator(),
        connector_id="demo-rest-1.0",
        op_id="GET:/api/widgets",
        target=target,
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_unsupported:")
    assert result.extras["error_code"] == "connector_unsupported"
    assert result.extras["cause"] == "unsupported_feature"
    assert result.extras["connector_class"] == "_AuthModelRejectingConnector"
    # The raise-site diagnostic (naming target + requested mode) is the
    # operator-facing surface, verbatim.
    assert "demo-per-user" in result.error
    assert "auth_model='per_user'" in result.error
    assert result.extras["detail"] in result.error
    # NOT the pre-#1627 flattened shape.
    assert "connector_error" not in result.error
    assert result.extras["error_code"] != "connector_error"
    assert "exception_message" not in result.extras

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == "GET:/api/widgets")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


@pytest.mark.asyncio
async def test_dispatch_converts_auto_shim_nie_to_connector_unsupported(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """Auto-shim branch: the dispatcher classifies the shim via isinstance.

    The connector registered for the triple is the *real* synthesised
    :class:`GenericRestConnector` subclass the ingest pipeline
    registers (``ensure_connector_class_registered``), so the test
    pins the production resolution path end to end: resolver picks the
    shim -> ``auth_headers`` raises -> dispatcher emits
    ``cause='unreplaced_auto_shim'``.
    """
    assert ensure_connector_class_registered(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        base_url=None,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        op_id="GET:/api/widgets",
        embedding=stub_embedding_service.encode_one.return_value,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id="acme-rest-1.0",
        op_id="GET:/api/widgets",
        target=_FakeTarget(product="acme"),
        params={},
    )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_unsupported:")
    assert result.extras["error_code"] == "connector_unsupported"
    assert result.extras["cause"] == "unreplaced_auto_shim"
    assert result.extras["connector_class"] == "AutoShim_acme_1_0_acme_rest"
    # The shim's own message (naming the triple) is preserved verbatim
    # and the remediation steers at subclass registration, not re-ingest.
    assert "must be replaced with a per-product Connector subclass" in result.error
    assert result.extras["detail"] in result.error
    assert "Register the hand-rolled per-product Connector subclass" in result.error
    assert "connector_error" not in result.error

    assert len(captured_events) == 1
    assert captured_events[0].result_status == "error"


# ---------------------------------------------------------------------------
# REST / MCP parity: the shared call_operation funnel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_operation_envelope_carries_connector_unsupported(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
) -> None:
    """The serialized envelope both transports return carries the shape.

    ``POST /api/v1/operations/call`` returns
    ``await call_operation(...)`` verbatim and the MCP
    ``call_operation`` tool is a thin shim over the same function, so
    asserting on the serialized dict envelope here covers both
    surfaces -- the same structural parity argument the
    ``composite_l2_*`` envelopes rely on.
    """
    assert ensure_connector_class_registered(
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        base_url=None,
    )
    await _insert_ingested_descriptor(
        session=session,
        product="acme",
        version="1.0",
        impl_id="acme-rest",
        op_id="GET:/api/widgets",
        embedding=stub_embedding_service.encode_one.return_value,
    )
    # Seed a target row resolve_target() can find by name.
    target_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT,
                name="acme-prod",
                aliases=[],
                product="acme",
                # Operator-asserted version (G0.15-T6 #1215): the row is
                # never probed in this test, so the resolver matches the
                # shim's supported_version_range through this column.
                version="1.0",
                host="acme.example.com",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    envelope = await call_operation(
        _make_operator(),
        {
            "connector_id": "acme-rest-1.0",
            "op_id": "GET:/api/widgets",
            "target": {"name": "acme-prod"},
            "params": {},
        },
    )

    assert envelope["status"] == "error"
    assert envelope["error"].startswith("connector_unsupported:")
    assert envelope["extras"]["error_code"] == "connector_unsupported"
    assert envelope["extras"]["cause"] == "unreplaced_auto_shim"
    assert envelope["extras"]["detail"] in envelope["error"]
