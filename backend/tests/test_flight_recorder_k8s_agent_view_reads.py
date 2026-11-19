# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who reads which copy of the stored trace of a Kubernetes call.

* An agent reads the cleaned copy: through the trace handle on a
  ``call_operation`` answer (``result_query``), through
  ``GET /api/v1/audit/{id}/trace`` and through ``GET /api/v1/audit/traces``.
  Env values and the password on the command line are hidden there; the env
  names stay.
* A person and a service login read the stored trace as it is.
* When the copy cannot be made, an agent gets no trace.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import meho_backplane.api.v1.audit as audit_api
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes.trace_agent_view import ENV_VALUE_MARKER
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, Tenant
from meho_backplane.flight_recorder import SpanInput, load_trace, record_trace
from meho_backplane.flight_recorder.agent_read import materialize_agent_trace_handle
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.redaction.command_line import HIDDEN
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = UUID("55555555-5555-5555-5555-555555555555")
_BASE = datetime(2026, 11, 19, 9, 0, 0, tzinfo=UTC)
_FAKE = "made-up-value-not-real-3307"
_AGENT_SUB = "agent-sub"
_KEY = make_rsa_keypair("kid-k8s-agent-view")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()


class _FakeRedis:
    """In-memory async Valkey stand-in for the result handle store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    yield TestClient(app)


def _pod() -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {
            "name": "app-0",
            "managedFields": [{"manager": "kubectl", "fieldsV1": {"f:spec": {}}}],
        },
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "command": ["/app/server"],
                    "args": ["--password", _FAKE],
                    "env": [
                        {
                            "name": "DATABASE_URL",
                            "value": f"postgres://app:{_FAKE}@db.example.invalid/app",
                        }
                    ],
                }
            ]
        },
    }


def _spans(connector_id: str = "k8s-1.x") -> list[SpanInput]:
    return [
        SpanInput(
            span_kind="vendor_call",
            name="GET /api/v1/namespaces/default/pods",
            started_at=_BASE,
            duration_ms=Decimal("5.00"),
            status="200",
            attributes={
                "connector_id": connector_id,
                "op_id": "k8s.pod.list" if connector_id.startswith("k8s") else "other.read",
                "method": "GET",
                "url": "https://cluster.example.invalid/api/v1/namespaces/default/pods",
                "response_body": {"kind": "PodList", "items": [_pod()]},
                "body_recorded": True,
            },
        )
    ]


async def _seed(*, operator_sub: str = _AGENT_SUB, connector_id: str = "k8s-1.x") -> UUID:
    audit_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(
                Tenant(
                    id=_TENANT,
                    slug="tenant-k8s-view",
                    name="Tenant k8s view",
                    flight_recorder_enabled=True,
                    flight_recorder_agent_readable=True,
                )
            )
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=_BASE,
                operator_sub=operator_sub,
                tenant_id=_TENANT,
                method="POST",
                path="/mcp",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={"op_id": "k8s.pod.list", "op_class": "read"},
                event_class=AuditEventClass.OPERATION_EXECUTION,
            )
        )
        await session.commit()
    await record_trace(
        audit_id=audit_id,
        tenant_id=_TENANT,
        spans=_spans(connector_id),
        operator_sub=operator_sub,
        now=_BASE,
    )
    return audit_id


def _get(client: TestClient, path: str, *, principal_kind: str | None, sub: str) -> Any:
    token = mint_token(
        _KEY,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT),
        principal_kind=principal_kind,
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEY))
        return client.get(path, headers={"Authorization": f"Bearer {token}"})


def _container(trace: dict[str, Any]) -> dict[str, Any]:
    body = trace["spans"][0]["attributes"]["response_body"]
    container: dict[str, Any] = body["items"][0]["spec"]["containers"][0]
    return container


def _assert_cleaned(trace: dict[str, Any]) -> None:
    assert _FAKE not in json.dumps(trace)
    assert "managedFields" not in json.dumps(trace)
    container = _container(trace)
    assert container["env"] == [{"name": "DATABASE_URL", "value": ENV_VALUE_MARKER}]
    assert container["args"] == ["--password", HIDDEN]


async def test_agent_reads_cleaned_trace_through_the_rest_routes(client: TestClient) -> None:
    audit_id = await _seed()

    single = _get(client, f"/api/v1/audit/{audit_id}/trace", principal_kind="agent", sub=_AGENT_SUB)
    assert single.status_code == 200, single.text
    assert single.json()["trace_present"] is True
    _assert_cleaned(single.json()["trace"])

    search = _get(client, "/api/v1/audit/traces", principal_kind="agent", sub=_AGENT_SUB)
    assert search.status_code == 200, search.text
    items = search.json()["items"]
    assert len(items) == 1
    _assert_cleaned(items[0])


@pytest.mark.parametrize("principal_kind", [None, "service"], ids=["person", "service"])
async def test_person_and_service_login_read_the_stored_trace(
    client: TestClient, principal_kind: str | None
) -> None:
    audit_id = await _seed(operator_sub="svc-1")

    resp = _get(
        client, f"/api/v1/audit/{audit_id}/trace", principal_kind=principal_kind, sub="svc-1"
    )

    assert resp.status_code == 200, resp.text
    container = _container(resp.json()["trace"])
    assert container["env"][0]["value"] == f"postgres://app:{_FAKE}@db.example.invalid/app"
    assert container["args"] == ["--password", _FAKE]


async def test_agent_gets_no_trace_when_the_copy_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_id = await _seed()

    async def _boom(view: Any) -> Any:
        raise RecursionError("too deep")

    monkeypatch.setattr(audit_api, "agent_view_trace", _boom)

    single = _get(client, f"/api/v1/audit/{audit_id}/trace", principal_kind="agent", sub=_AGENT_SUB)
    assert single.status_code == 200, single.text
    assert single.json()["trace_present"] is False
    assert _FAKE not in single.text

    search = _get(client, "/api/v1/audit/traces", principal_kind="agent", sub=_AGENT_SUB)
    assert search.status_code == 200, search.text
    assert search.json()["items"] == []


def _operator() -> Operator:
    return Operator(
        sub=_AGENT_SUB,
        name="Agent",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        scopes=frozenset(),
        platform_admin=False,
    )


async def test_agent_trace_handle_holds_the_cleaned_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    audit_id = await _seed()
    operator = _operator()

    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)

    assert handle is not None
    assert _FAKE not in handle.model_dump_json()
    window = await read_result_window(operator, handle.handle_id, offset=0, limit=50)
    _assert_cleaned({"spans": window["rows"]})

    # The stored trace, which a person reads, is unchanged.
    async with get_sessionmaker()() as session:
        stored = await load_trace(session, audit_id=audit_id, tenant_id=_TENANT)
    assert stored is not None
    assert _FAKE in json.dumps(stored.spans[0].attributes)
    assert "managedFields" in json.dumps(stored.spans[0].attributes)


async def test_agent_trace_handle_of_a_non_kubernetes_call_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    audit_id = await _seed(connector_id="vmware-rest-9.0")
    operator = _operator()

    handle = await materialize_agent_trace_handle(operator=operator, audit_id=audit_id, store=store)

    assert handle is not None
    window = await read_result_window(operator, handle.handle_id, offset=0, limit=50)
    assert window["rows"][0]["attributes"] == _spans("vmware-rest-9.0")[0].attributes
