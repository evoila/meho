# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Deployment specs and depot credentials stay out of flight-recorder traces.

Some write operations send a body with passwords to an appliance: a
deployment spec that holds the passwords of the systems it sets up and can
hold licence keys, the account of a software depot, which the appliance can
echo back, or a proxy account whose password MEHO reads from Vault at run
time and puts into the request body.

Each such operation carries the tag ``credential``. Its word is a secret tag
word of the flight recorder, so a trace keeps no request or answer body of
the operation's vendor calls, the same as for a credential write.

The operations here are synthetic: a small appliance connector the test
declares itself, with four write operations of those shapes (the public tree
holds no VMware connector). Each case runs the operation through the real
dispatcher, with the tenant's flight recorder on, against a fake appliance
(respx). The body holds a made-up password and, where the shape has one, a
made-up licence key. The test checks:

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
from meho_backplane.connectors._shared import vault_creds
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.flight_recorder import AGENT_TRACE_HANDLE_EXTRA_KEY, agent_read
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.redaction.flight_recorder import SECRET_FAMILY_OMITTED_MARKER
from meho_backplane.settings import get_settings

from ._result_store_helpers import MemoryResultStore

_TENANT_ID = UUID("00000000-0000-0000-0000-00000000c4e1")
_HOST = "spec-trace-appliance.test.invalid"
_PRODUCT = "specdeploy"
_VERSION = "1.x"
_IMPL_ID = "specdeploy-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_TAG = "credential"

_SPEC_VALIDATE_OP_ID = "specdeploy.spec.validate"
_SPEC_APPLY_OP_ID = "specdeploy.spec.apply"
_DEPOT_SET_OP_ID = "specdeploy.depot.set"
_PROXY_SET_OP_ID = "specdeploy.proxy.set"

#: Made-up values. The test fails if one of them shows up in a trace.
_PASSWORD = "Made-Up-Pass-4c1e-not-real"
_LICENCE_KEY = "FAKE0-AAAAA-BBBBB-CCCCC-4C1E0"
_SECRETS = (_PASSWORD, _LICENCE_KEY)


def _deploy_spec() -> dict[str, Any]:
    """A short deployment spec with a made-up password and licence key."""
    return {
        "name": "<deployment-name>",
        "managerSpec": {
            "hostname": "<manager-fqdn>",
            "rootPassword": _PASSWORD,
            "licenseKey": _LICENCE_KEY,
        },
        "nodes": [{"hostname": "<node-fqdn>", "adminPassword": _PASSWORD}],
    }


def _depot_settings() -> dict[str, Any]:
    """Depot settings with a made-up depot password (they hold no licence key)."""
    return {
        "account": {"username": "<depot-user>", "password": _PASSWORD},
        "hostname": "depot.test.invalid",
        "port": 443,
    }


class _DeployConnector(HttpConnector):
    """A synthetic appliance connector whose write operations send passwords.

    The handlers are bound methods, the shape the in-tree HTTP connectors
    use, so each vendor call runs through the shared HTTP seam that records
    the flight-recorder span.
    """

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {"Authorization": "Bearer spec-trace-session"}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError

    async def spec_validate(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Check a deployment spec; the body holds the spec's passwords."""
        return await self._post_json(
            target, "/v1/deployments/validations", operator=operator, json=params["spec"]
        )

    async def spec_apply(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Start a deployment; the body holds the spec's passwords."""
        return await self._post_json(
            target, "/v1/deployments", operator=operator, json=params["spec"]
        )

    async def depot_set(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Set the depot account; the appliance echoes the settings back."""
        return await self._post_json(
            target, "/v1/settings/depot", operator=operator, verb="PUT", json=params["settings"]
        )

    async def proxy_set(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Set the proxy account; the password comes from Vault at run time."""
        secret = await vault_creds.load_param_secret_data(
            target, operator, param="password_secret_ref"
        )
        body = {
            "host": params["host"],
            "username": params["username"],
            "password": secret["password"],
        }
        return await self._post_json(
            target, "/v1/settings/proxy", operator=operator, verb="PATCH", json=body
        )


@dataclass(frozen=True)
class _Call:
    """One vendor call of an operation and the fake appliance's answer."""

    method: str
    path: str
    status: int
    answer: dict[str, Any]


@dataclass(frozen=True)
class _Case:
    """One operation: its params, the vendor call it makes, and its approval.

    ``approved`` runs the dispatch that follows a person's approval, for the
    operations that wait for one.
    """

    op_id: str
    params: dict[str, Any]
    call: _Call
    approved: bool


_VALIDATION = {"id": "validation-0001", "status": "IN_PROGRESS"}
_TASK = {"id": "task-0001", "status": "IN_PROGRESS"}

_CASES = (
    _Case(
        _SPEC_VALIDATE_OP_ID,
        {"spec": _deploy_spec()},
        _Call("POST", "/v1/deployments/validations", 202, _VALIDATION),
        approved=False,
    ),
    _Case(
        _SPEC_APPLY_OP_ID,
        {"spec": _deploy_spec()},
        _Call("POST", "/v1/deployments", 202, _TASK),
        approved=True,
    ),
    # The answer echoes the settings with the password, as an appliance can.
    _Case(
        _DEPOT_SET_OP_ID,
        {"settings": _depot_settings()},
        _Call("PUT", "/v1/settings/depot", 200, _depot_settings()),
        approved=True,
    ),
    # The proxy password comes from Vault and rides the request body.
    _Case(
        _PROXY_SET_OP_ID,
        {
            "host": "proxy.test.invalid",
            "username": "<proxy-user>",
            "password_secret_ref": "kv/proxy",
        },
        _Call("PATCH", "/v1/settings/proxy", 202, _TASK),
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
    """Reset caches, the registry and the trace config; register the connector."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_DeployConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every Vault path an operation names holds the made-up password."""

    async def _load(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"password": _PASSWORD}

    monkeypatch.setattr(vault_creds, "load_param_secret_data", _load)


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
    """A target the resolver and the connector accept."""

    product: str = _PRODUCT
    version: str = _VERSION
    host: str = _HOST
    name: str = "spec-trace-appliance"
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = _TENANT_ID
    port: int = 443
    secret_ref: str = "targets/spec-trace/appliance"
    auth_model: str = "shared_service_account"
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


async def _register_operations(session: AsyncSession) -> None:
    """Register the four write operations; turn the tenant's recorder on."""
    handlers = {
        _SPEC_VALIDATE_OP_ID: (_DeployConnector.spec_validate, False),
        _SPEC_APPLY_OP_ID: (_DeployConnector.spec_apply, True),
        _DEPOT_SET_OP_ID: (_DeployConnector.depot_set, True),
        _PROXY_SET_OP_ID: (_DeployConnector.proxy_set, True),
    }
    for op_id, (handler, requires_approval) in handlers.items():
        await register_typed_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
            handler=handler,
            summary=f"Synthetic write {op_id}.",
            description=f"Synthetic write {op_id}; its body holds a password.",
            parameter_schema={"type": "object"},
            when_to_use=None,
            tags=[_TAG],
            safety_level="dangerous" if requires_approval else "caution",
            requires_approval=requires_approval,
        )
    session.add(
        Tenant(id=_TENANT_ID, slug="spec-trace", name="spec-trace", flight_recorder_enabled=True)
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _dispatch(case: _Case) -> Any:
    """Run *case* through the real dispatcher against a fake appliance."""
    async with respx.mock(base_url=f"https://{_HOST}", assert_all_called=False) as mock:
        route = mock.request(case.call.method, case.call.path).respond(
            case.call.status, json=case.call.answer
        )
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=case.op_id,
            target=_Target(),
            params=case.params,
            _approved=case.approved,
        )
    assert result.status == "ok", result.error
    assert route.call_count == 1
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
    """The spans of the case's vendor call."""
    return [
        span
        for span in spans
        if span.span_kind == "vendor_call"
        and span.attributes.get("method") == case.call.method
        and str(span.attributes.get("url", "")).endswith(case.call.path)
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
    assert len(vendor_spans) == 1, "the trace should hold the vendor call"
    span = vendor_spans[0]
    assert span.attributes["body_recorded"] is False
    assert span.attributes["request_body"] == SECRET_FAMILY_OMITTED_MARKER
    assert span.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER
    _assert_no_made_up_value("the stored trace", _as_text(spans))

    # The trace is still certain, so the agent gets its copy, without the bodies.
    assert trace.redaction_uncertain is False
    agent_copy = await _agent_copy(result)
    assert case.call.path in agent_copy, "the agent copy should hold the vendor call"
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
