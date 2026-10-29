# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Deployment specs and depot credentials stay out of flight-recorder traces.

These operations send a body with passwords to the appliance:

* SDDC Manager: ``sddc.domain.validate`` and ``sddc.domain.create`` send the
  domain spec. It holds the passwords of the new domain and can hold licence
  keys.
* VCF Installer: ``installer.sddc.spec.validate``,
  ``installer.sddc.bringup.start``, ``installer.sddc.bringup.retry`` and the
  multi-step ``installer.composite.sddc.bringup`` send the SddcSpec, which
  holds the core passwords. ``installer.system.depot.set`` sends the depot
  password, and the answer can echo it.

Each of these operations carries the tag ``credential``. Its word is a secret
tag word of the flight recorder, so a trace keeps no request or answer body of
the operation's vendor calls, the same as for a credential write.

Each case runs the operation through the real dispatcher, with the tenant's
flight recorder on, against a fake appliance (respx). The body holds a made-up
password and, where the vendor shape has one, a made-up licence key. The test
checks:

* the stored trace: the vendor call is there, without its bodies, and no
  made-up value is anywhere in the trace;
* the agent's copy of the trace (the handle on the result and the rows the
  agent reads with ``result_query``): it holds the vendor call, and no made-up
  value.

A control runs the same case after it removes the tag from the stored
operation. Then the trace keeps the bodies, so the checks above are not
empty. The made-up password is still not in the trace: it sits under a
secret-named field, and the trace scrub replaces such values (#5031).

All values are made up, the hosts use the reserved ``.invalid`` domain, and no
live system is called.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.sddc_manager import (
    SDDC_CONNECTOR_ID,
    SDDC_DOMAIN_CREATE_OP_ID,
    SDDC_DOMAIN_VALIDATE_OP_ID,
    SDDC_IMPL_ID,
    SDDC_PRODUCT,
    SDDC_VERSION,
    SddcManagerConnector,
    register_sddc_typed_operations,
)
from meho_backplane.connectors.vcf_installer import (
    INSTALLER_BRINGUP_OP_ID,
    INSTALLER_BRINGUP_RETRY_OP_ID,
    INSTALLER_BRINGUP_START_OP_ID,
    INSTALLER_CONNECTOR_ID,
    INSTALLER_IMPL_ID,
    INSTALLER_PRODUCT,
    INSTALLER_SPEC_VALIDATE_OP_ID,
    INSTALLER_VERSION,
    InstallerConnector,
    register_installer_composite_operations,
    register_installer_typed_operations,
)
from meho_backplane.connectors.vcf_installer.typed_writes import INSTALLER_DEPOT_SET_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.flight_recorder import AGENT_TRACE_HANDLE_EXTRA_KEY, agent_read
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.redaction.flight_recorder import SECRET_FAMILY_OMITTED_MARKER
from meho_backplane.settings import get_settings

from ._result_store_helpers import MemoryResultStore

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000c4e1")
_SDDC_HOST = "sddc-spec-trace.test.invalid"
_INSTALLER_HOST = "installer-spec-trace.test.invalid"
_TOKEN_PATH = "/v1/tokens"
_TAG = "credential"

#: Made-up values. The test fails if one of them shows up in a trace.
_PASSWORD = "Made-Up-Pass-4c1e-not-real"
_LICENCE_KEY = "FAKE0-AAAAA-BBBBB-CCCCC-4C1E0"
_SECRETS = (_PASSWORD, _LICENCE_KEY)


def _domain_spec() -> dict[str, Any]:
    """A short DomainCreationSpec with a made-up password and licence key."""
    return {
        "domainName": "<domain-name>",
        "vcenterSpec": {
            "name": "<vcenter-name>",
            "rootPassword": _PASSWORD,
            "licenseKey": _LICENCE_KEY,
        },
        "nsxTSpec": {"nsxManagerAdminPassword": _PASSWORD, "licenseKey": _LICENCE_KEY},
    }


def _sddc_spec() -> dict[str, Any]:
    """A short SddcSpec with a made-up password and licence key."""
    return {
        "sddcId": "<sddc-id>",
        "vcenterSpec": {
            "vcenterHostname": "<vcenter-fqdn>",
            "rootVcenterPassword": _PASSWORD,
            "licenseKey": _LICENCE_KEY,
        },
        "sddcManagerSpec": {"hostname": "<sddc-manager-fqdn>", "rootPassword": _PASSWORD},
    }


def _depot_settings() -> dict[str, Any]:
    """Offline depot settings with a made-up depot password (they hold no licence key)."""
    return {
        "offlineAccount": {"username": "<depot-user>", "password": _PASSWORD},
        "depotConfiguration": {
            "isOfflineDepot": True,
            "hostname": "depot.test.invalid",
            "port": 443,
        },
    }


@dataclass(frozen=True)
class _Call:
    """One vendor call of an operation and the fake appliance's answer."""

    method: str
    path: str
    status: int
    answer: dict[str, Any]


@dataclass(frozen=True)
class _Case:
    """One operation: where it runs, its params and the vendor calls it makes.

    ``approved`` runs the dispatch that follows a person's approval, for the
    operations that wait for one.
    """

    op_id: str
    connector_id: str
    product: str
    version: str
    host: str
    params: dict[str, Any]
    calls: tuple[_Call, ...]
    approved: bool


_VALIDATION = {"id": "validation-0001", "executionStatus": "IN_PROGRESS"}
_VALIDATION_DONE = {
    "id": "validation-0001",
    "executionStatus": "COMPLETED",
    "resultStatus": "SUCCEEDED",
}
_TASK = {"id": "task-0001", "status": "IN_PROGRESS"}


def _sddc_case(op_id: str, call: _Call, *, approved: bool) -> _Case:
    return _Case(
        op_id=op_id,
        connector_id=SDDC_CONNECTOR_ID,
        product=SDDC_PRODUCT,
        version=SDDC_VERSION,
        host=_SDDC_HOST,
        params={"spec": _domain_spec()},
        calls=(call,),
        approved=approved,
    )


def _installer_case(op_id: str, params: dict[str, Any], *calls: _Call, approved: bool) -> _Case:
    return _Case(
        op_id=op_id,
        connector_id=INSTALLER_CONNECTOR_ID,
        product=INSTALLER_PRODUCT,
        version=INSTALLER_VERSION,
        host=_INSTALLER_HOST,
        params=params,
        calls=calls,
        approved=approved,
    )


_CASES = (
    _sddc_case(
        SDDC_DOMAIN_VALIDATE_OP_ID,
        _Call("POST", "/v1/domains/validations", 202, _VALIDATION),
        approved=False,
    ),
    _sddc_case(SDDC_DOMAIN_CREATE_OP_ID, _Call("POST", "/v1/domains", 202, _TASK), approved=True),
    _installer_case(
        INSTALLER_SPEC_VALIDATE_OP_ID,
        {"spec": _sddc_spec()},
        _Call("POST", "/v1/sddcs/validations", 202, _VALIDATION),
        approved=False,
    ),
    _installer_case(
        INSTALLER_BRINGUP_START_OP_ID,
        {"spec": _sddc_spec()},
        _Call("POST", "/v1/sddcs", 202, _TASK),
        approved=True,
    ),
    _installer_case(
        INSTALLER_BRINGUP_RETRY_OP_ID,
        {"id": "task-0001", "spec": _sddc_spec()},
        _Call("PATCH", "/v1/sddcs/task-0001", 202, _TASK),
        approved=True,
    ),
    # The multi-step bring-up: the check and the deploy call run under one op.
    _installer_case(
        INSTALLER_BRINGUP_OP_ID,
        {"spec": _sddc_spec()},
        _Call("POST", "/v1/sddcs/validations", 200, _VALIDATION_DONE),
        _Call("POST", "/v1/sddcs", 202, _TASK),
        approved=True,
    ),
    # The answer echoes the settings with the password, as the appliance can.
    _installer_case(
        INSTALLER_DEPOT_SET_OP_ID,
        {"settings": _depot_settings()},
        _Call("PUT", "/v1/system/settings/depot", 200, _depot_settings()),
        approved=True,
    ),
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset caches, the registry and the trace config; register both connectors."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    register_connector_v2(
        product=SDDC_PRODUCT, version=SDDC_VERSION, impl_id=SDDC_IMPL_ID, cls=SddcManagerConnector
    )
    register_connector_v2(
        product=INSTALLER_PRODUCT,
        version=INSTALLER_VERSION,
        impl_id=INSTALLER_IMPL_ID,
        cls=InstallerConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed embedding, so registration does not load the model."""
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session on the per-test database."""
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def result_store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """Point result handles and the agent's copy of a trace at one in-memory store."""
    store = MemoryResultStore()
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(agent_read, "get_result_handle_store", lambda: store)
    return store


@dataclass
class _Target:
    """A target the resolver and both connectors accept."""

    product: str
    version: str
    host: str
    name: str = "spec-trace-appliance"
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = _TENANT_ID
    port: int = 443
    secret_ref: str = "targets/spec-trace/appliance"
    auth_model: str = "shared_service_account"
    sso_realm: str = "vsphere.local"
    tls_server_name: str | None = None
    preferred_impl_id: str | None = None

    @property
    def fingerprint(self) -> Any:
        return SimpleNamespace(version=self.version)


def _operator() -> Operator:
    return Operator(
        sub="op-spec-trace",
        name="Spec Trace Operator",
        email=None,
        raw_jwt="op.spec.trace.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _service_login(_target: object, _operator: Operator) -> dict[str, str]:
    """A fixed service-account login, so no Vault read runs."""
    return {"username": "svc-spec-trace", "password": "svc-login-not-in-any-spec"}


async def _register_operations(session: AsyncSession) -> None:
    """Register the SDDC Manager and Installer ops; turn the tenant's recorder on."""
    await register_sddc_typed_operations()
    await register_installer_typed_operations()
    await register_installer_composite_operations()
    for cls in (SddcManagerConnector, InstallerConnector):
        connector = get_or_create_connector_instance(cls)
        connector._credentials_loader = _service_login  # type: ignore[attr-defined]
    session.add(
        Tenant(id=_TENANT_ID, slug="spec-trace", name="spec-trace", flight_recorder_enabled=True)
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _dispatch(case: _Case) -> Any:
    """Run *case* through the real dispatcher against a fake appliance."""
    target = _Target(product=case.product, version=case.version, host=case.host)
    async with respx.mock(base_url=f"https://{case.host}", assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "spec-trace-access-token"})
        routes = [
            mock.request(call.method, call.path).respond(call.status, json=call.answer)
            for call in case.calls
        ]
        result = await dispatch(
            operator=_operator(),
            connector_id=case.connector_id,
            op_id=case.op_id,
            target=target,
            params=case.params,
            _approved=case.approved,
        )
    assert result.status == "ok", result.error
    assert [route.call_count for route in routes] == [1] * len(routes)
    return result


async def _stored_trace(audit_id: UUID) -> tuple[DispatchTrace, list[DispatchTraceSpan]]:
    """The stored trace of one dispatch and its spans, in order."""
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = (
            (
                await s.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == trace.id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )
    return trace, list(spans)


def _vendor_spans(spans: list[DispatchTraceSpan], case: _Case) -> list[DispatchTraceSpan]:
    """The spans of the case's vendor calls (not the token login)."""
    wanted = {(call.method, call.path) for call in case.calls}
    return [
        span
        for span in spans
        if span.span_kind == "vendor_call"
        and any(
            span.attributes.get("method") == method
            and str(span.attributes.get("url", "")).endswith(path)
            for method, path in wanted
        )
    ]


def _as_text(spans: list[DispatchTraceSpan]) -> str:
    return json.dumps(
        [{"name": span.name, "attributes": span.attributes} for span in spans], default=str
    )


async def _agent_copy(result: Any) -> str:
    """The agent's copy of the trace: the handle on the result and every row it pages."""
    handle = result.extras[AGENT_TRACE_HANDLE_EXTRA_KEY]
    window = await read_result_window(_operator(), UUID(handle["handle_id"]), offset=0, limit=50)
    return json.dumps([handle, window], default=str)


def _assert_no_made_up_value(label: str, text: str) -> None:
    for value in _SECRETS:
        assert value not in text, f"a made-up value ending {value[-4:]!r} is in {label}"


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.op_id)
@pytest.mark.asyncio
async def test_the_trace_and_the_agent_copy_hold_no_body(
    case: _Case, session: AsyncSession, result_store: MemoryResultStore
) -> None:
    await _register_operations(session)

    result = await _dispatch(case)

    assert result.audit_id is not None
    trace, spans = await _stored_trace(result.audit_id)
    vendor_spans = _vendor_spans(spans, case)
    assert len(vendor_spans) == len(case.calls), "the trace should hold each vendor call"
    for span in vendor_spans:
        assert span.attributes["body_recorded"] is False
        assert span.attributes["request_body"] == SECRET_FAMILY_OMITTED_MARKER
        assert span.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER
    _assert_no_made_up_value("the stored trace", _as_text(spans))

    # The trace is still certain, so the agent gets its copy, without the bodies.
    assert trace.redaction_uncertain is False
    agent_copy = await _agent_copy(result)
    for call in case.calls:
        assert call.path in agent_copy, "the agent copy should hold the vendor call"
    _assert_no_made_up_value("the agent copy", agent_copy)
    stored = b"".join(result_store.client.values.values()).decode()
    _assert_no_made_up_value("the result store", stored)


async def _drop_the_tag(op_id: str) -> None:
    """Remove the tag from the stored operation, as if it had never had it.

    The dispatcher reads the operation from the database on every call, so the
    next dispatch runs without the tag.
    """
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id))
        ).scalar_one()
        assert _TAG in row.tags
        row.tags = [tag for tag in row.tags if tag != _TAG]
        await s.commit()


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.op_id)
@pytest.mark.asyncio
async def test_without_the_tag_the_trace_would_hold_the_body(
    case: _Case, session: AsyncSession, result_store: MemoryResultStore
) -> None:
    """Control: the tag is what keeps the body out, so the test above is not empty.

    Without the tag the bodies are recorded. The password is still masked,
    because it sits under a secret-named field (#5031).
    """
    await _register_operations(session)
    await _drop_the_tag(case.op_id)

    result = await _dispatch(case)

    assert result.audit_id is not None
    _, spans = await _stored_trace(result.audit_id)
    assert all(span.attributes["body_recorded"] for span in _vendor_spans(spans, case))
    assert "[REDACTED:param_name]" in _as_text(spans)
    assert _PASSWORD not in _as_text(spans)
    assert _PASSWORD not in await _agent_copy(result)
