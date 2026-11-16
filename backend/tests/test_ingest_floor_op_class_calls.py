# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Calls of ingested operations whose row stores a class set by a connector floor.

* A stored ``credential_read``: the call waits for a person, the approval
  request and the written audit row say ``credential_read``, and a standing
  grant does not cover it.
* A stored ``write``: the call runs, and the written audit row says ``write``.
* The call trace records no request body and no answer body for a stored
  ``credential_read``.
* After a switch to a sibling catalog, the class comes from the row that runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    clear_registry,
    get_connector_v2,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import _catalog_guard, dispatch, reset_dispatcher_caches
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.ingest.safety_floors import (
    _FLOORS,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from meho_backplane.settings import get_settings

_PRODUCT = "clientscalls"
_IMPL_ID = "clientscalls-rest"
_VERSION = "1.0"
_SIBLING_VERSION = "2.0"
_CLIENTS = "GET:/clients"
_PASSWORD = "made-up-" + "pass-value"

_SPEC = """
openapi: 3.0.3
info: {title: clients calls fixture, version: '1.0'}
paths:
  /clients:
    get:
      operationId: clients.list
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  properties: {name: {type: string}}
"""


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    yield
    _FLOORS.clear()
    reset_dispatcher_caches()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _class_floor(op_class: str) -> Any:
    def floor(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
        return proto.model_copy(update={"op_class": op_class})

    return floor


async def _ingest(
    service: AsyncMock, op_class: str | None, *, version: str = _VERSION, waits: bool = False
) -> EndpointDescriptor:
    """Ingest the fixture with a floor that sets *op_class*, and enable the op.

    *waits* sets the row to ``dangerous`` with the approval flag off, as a
    reviewer may: a service principal then needs a grant or a person.
    """
    if get_connector_v2(_PRODUCT, version, _IMPL_ID) is None:
        register_connector_v2(
            product=_PRODUCT, version=version, impl_id=_IMPL_ID, cls=_CONNECTORS[version]
        )
    _FLOORS.clear()
    if op_class is not None:
        register_ingest_safety_floor(
            product=_PRODUCT, impl_id=_IMPL_ID, floor=_class_floor(op_class)
        )
    await register_ingested_operations(
        product=_PRODUCT,
        version=version,
        impl_id=_IMPL_ID,
        spec_source="clients-calls.yaml",
        operations=parse_openapi("file:///clients-calls.yaml", content=_SPEC),
        embedding_service=service,
    )
    _FLOORS.clear()
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT,
                    EndpointDescriptor.version == version,
                    EndpointDescriptor.op_id == _CLIENTS,
                )
            )
        ).scalar_one()
        row.is_enabled = True
        if waits:
            row.safety_level, row.requires_approval = "dangerous", False
        assert row.op_class == op_class
        return row


class _Connector(Connector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    accepts_ingested_operations = True
    calls: ClassVar[list[str]] = []

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise AssertionError("an ingested op runs through _request_json")

    async def _request_json(self, target: Any, method: str, path: str, **_: Any) -> Any:
        type(self).calls.append(f"{method}:{path}")
        return [{"name": "a"}]


_CONNECTORS: dict[str, type[Connector]] = {_VERSION: _Connector}


class _Target:
    def __init__(self, version: str = _VERSION) -> None:
        self.id = uuid.uuid4()
        self.name = "clients-calls-target"
        self.product = _PRODUCT
        self.fingerprint = type("Fingerprint", (), {"version": version})()
        self.preferred_impl_id: str | None = None


def _operator(
    kind: PrincipalKind = PrincipalKind.USER, tenant_id: uuid.UUID | None = None
) -> Operator:
    return Operator(
        sub=f"sub-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id or uuid.uuid4(),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _dispatch(operator: Operator, *, version: str = _VERSION, target: Any = None) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=f"{_IMPL_ID}-{version}",
        op_id=_CLIENTS,
        target=target or _Target(),
        params={},
    )


async def _audit_payloads() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [dict(row.payload) for row in rows if row.payload.get("op_id") == _CLIENTS]


async def _approval_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


# -- point 5: a call with a stored class, end to end ----------------------------------


@pytest.mark.asyncio
async def test_stored_credential_read_waits_for_a_person_and_the_audit_row_says_so(
    stub_embedding_service: AsyncMock,
) -> None:
    await _ingest(stub_embedding_service, "credential_read")
    _Connector.calls = []

    result = await _dispatch(_operator())

    assert result.status == "awaiting_approval", (result.status, result.error)
    assert _Connector.calls == []
    (request,) = await _approval_requests()
    assert request.proposed_effect["op_class"] == "credential_read"
    payloads = await _audit_payloads()
    assert payloads, "the call wrote no audit row"
    assert {payload["op_class"] for payload in payloads} == {"credential_read"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_class", "status"), [("credential_read", "awaiting_approval"), (None, "ok")]
)
async def test_a_standing_grant_does_not_cover_a_stored_credential_read(
    stub_embedding_service: AsyncMock, op_class: str | None, status: str
) -> None:
    """The grant covers the ``dangerous`` row with no class, not the one with the class."""
    await _ingest(stub_embedding_service, op_class, waits=True)
    _Connector.calls = []
    operator = _operator(PrincipalKind.SERVICE)
    target = _Target()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
                op_id=_CLIENTS,
                connector_id=f"{_IMPL_ID}-{_VERSION}",
                target_id=target.id,
                reason="written straight into the table",
                created_by_sub="someone-else",
            )
        )

    result = await _dispatch(operator, target=target)

    assert result.status == status, (result.status, result.error)
    async with get_sessionmaker()() as session:
        grant_uses = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "approval.decision")))
            .scalars()
            .all()
        )
    if op_class is None:
        assert _Connector.calls == ["GET:/clients"]
        assert len(grant_uses) == 1
        return
    assert _Connector.calls == []
    assert grant_uses == []
    payloads = await _audit_payloads()
    assert {payload["op_class"] for payload in payloads} == {"credential_read"}


@pytest.mark.asyncio
async def test_stored_write_runs_and_the_audit_row_says_write(
    stub_embedding_service: AsyncMock,
) -> None:
    await _ingest(stub_embedding_service, "write")
    _Connector.calls = []

    result = await _dispatch(_operator())

    assert result.status == "ok", (result.status, result.error)
    assert _Connector.calls == ["GET:/clients"]
    payloads = await _audit_payloads()
    assert payloads, "the call wrote no audit row"
    assert {payload["op_class"] for payload in payloads} == {"write"}


# -- point 3: the call trace records no body for a stored credential_read -------------


class _HttpClients(HttpConnector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    accepts_ingested_operations = True

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _trace_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"op-class-{tenant_id.hex[:8]}",
                name="op class trace tenant",
                flight_recorder_enabled=True,
            )
        )
    return tenant_id


async def _vendor_spans(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        traces = (
            (
                await session.execute(
                    select(DispatchTrace).where(DispatchTrace.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(traces) == 1
        spans = (
            (
                await session.execute(
                    select(DispatchTraceSpan).where(DispatchTraceSpan.trace_id == traces[0].id)
                )
            )
            .scalars()
            .all()
        )
    return [dict(span.attributes) for span in spans if "response_body" in span.attributes]


@pytest.mark.asyncio
@pytest.mark.parametrize("op_class", ["credential_read", None])
async def test_trace_holds_no_body_for_a_stored_credential_read(
    stub_embedding_service: AsyncMock, op_class: str | None
) -> None:
    """The approved call runs with the trace on; only the control row keeps its body."""
    _CONNECTORS[_VERSION] = _HttpClients
    try:
        await _ingest(stub_embedding_service, op_class)
    finally:
        _CONNECTORS[_VERSION] = _Connector
    tenant_id = await _trace_tenant()
    target = SimpleNamespace(
        id=uuid.uuid4(),
        name="clients-calls-target",
        product=_PRODUCT,
        fingerprint=SimpleNamespace(version=_VERSION),
        preferred_impl_id=None,
        host="vendor.example.com",
        port=443,
        tenant_id=tenant_id,
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )

    with respx.mock(base_url="https://vendor.example.com") as mock:
        mock.get("/clients").respond(200, json=[{"name": "client-one", "password": _PASSWORD}])
        result = await dispatch(
            operator=_operator(tenant_id=tenant_id),
            connector_id=f"{_IMPL_ID}-{_VERSION}",
            op_id=_CLIENTS,
            target=target,
            params={},
            _approved=True,
        )

    assert result.status == "ok", (result.status, result.error)
    (span,) = await _vendor_spans(tenant_id)
    stored = repr(span)
    assert _PASSWORD not in stored
    if op_class is None:
        assert span["body_recorded"] is True
        assert "client-one" in stored
        return
    assert span["body_recorded"] is False
    assert "client-one" not in stored


def test_body_exclusion_reads_the_stored_class() -> None:
    assert classify_body_exclusion(_CLIENTS).excluded is False
    assert classify_body_exclusion(_CLIENTS, stored_op_class="write").excluded is False
    exclusion = classify_body_exclusion(_CLIENTS, stored_op_class="credential_read")
    assert (exclusion.excluded, exclusion.family) == (True, "secret-bearing")


# -- point 4: after a switch to a sibling catalog, the class of the row that runs -----


class _GuardedConnector(_Connector):
    """One catalog per major version; the resolver picks the 2.0 catalog."""

    enforces_catalog_target_compatibility = True

    @classmethod
    def catalog_target_incompatibility(
        cls,
        *,
        descriptor_source_kind: str,
        target_product: str | None,
        target_version: str | None,
        selected_target_connector: type[Connector] | None,
    ) -> str | None:
        if target_version == cls.version:
            return None
        return f"catalog {cls.version} does not serve {target_version}"


class _GuardedOne(_GuardedConnector):
    version = _VERSION


class _GuardedTwo(_GuardedConnector):
    version = _SIBLING_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("named_class", "sibling_class", "status"),
    [
        pytest.param(None, "credential_read", "awaiting_approval", id="only-the-sibling-has-it"),
        pytest.param("credential_read", None, "ok", id="only-the-named-catalog-has-it"),
    ],
)
async def test_class_comes_from_the_row_that_runs_after_a_sibling_switch(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    named_class: str | None,
    sibling_class: str | None,
    status: str,
) -> None:
    """The call names the 1.0 catalog and runs on the 2.0 catalog.

    The ``reveal_secret`` key of a run's audit row is written only for a
    secret read, so it shows which row the secret read flag came from.
    """
    _CONNECTORS.update({_VERSION: _GuardedOne, _SIBLING_VERSION: _GuardedTwo})
    try:
        await _ingest(stub_embedding_service, named_class)
        await _ingest(stub_embedding_service, sibling_class, version=_SIBLING_VERSION)
    finally:
        _CONNECTORS.pop(_SIBLING_VERSION)
        _CONNECTORS[_VERSION] = _Connector
    monkeypatch.setattr(
        _catalog_guard, "resolve_connector_or_label", lambda target: (_GuardedTwo, None, None)
    )

    result = await _dispatch(_operator(), target=_Target(_SIBLING_VERSION))

    assert result.status == status, (result.status, result.error)
    (payload,) = await _audit_payloads()
    if status == "awaiting_approval":
        (request,) = await _approval_requests()
        assert request.connector_id == f"{_IMPL_ID}-{_SIBLING_VERSION}"
        assert request.proposed_effect["op_class"] == "credential_read"
        assert payload["op_class"] == "credential_read"
        return
    assert payload["connector_version"] == _SIBLING_VERSION
    assert payload["op_class"] == "read"
    assert "reveal_secret" not in payload
