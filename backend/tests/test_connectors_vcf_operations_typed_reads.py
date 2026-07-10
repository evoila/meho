# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vROps typed read surface (Initiative #2266 T3, #2303).

Covers the audited read set converted from ingested-row curation to
**typed** ops (``source_kind="typed"``) on the connector's hand-rolled
HTTP Basic (+ optional ``auth-source``) session:

* ``vrops.liveness`` — ``GET /suite-api/api/versions/current`` (appliance
  liveness + identity; the documented reachability surface — vROps'
  CaSA ``/casa/health`` is a private, undocumented API).
* ``vrops.alert.list`` — ``GET /suite-api/api/alerts`` (alert triage).
* ``vrops.resource.query`` — ``POST /suite-api/api/resources/query`` (a
  body-shaped POST carrying a typed ``ResourceQuerySpec``).

Coverage matrix (per #2303 acceptance criteria):

* **Each op dispatches live** through :func:`~meho_backplane.operations.dispatch`
  against a respx-mocked vROps appliance and returns ``status="ok"`` with
  the payload — with **zero catalog ingest** (the descriptor rows come only
  from the typed registrar, never from an ingested spec). The in-process
  Vault fake exercises the real default credential loader (Basic auth).
* **`source_kind="typed"`** — the registered descriptor rows carry
  ``source_kind="typed"`` with a resolvable ``handler_ref`` (the #2262
  invariant: the dispatch path never touches an ingested descriptor row).
* **The query op is a body POST** — ``vrops.resource.query`` sends the
  ``ResourceQuerySpec`` body and paginates via query params.
* **auth-source rides every request** — both the GET (``_request_json``
  override) and the body POST (``_post_json`` override) carry
  ``?auth-source=<value>`` when the target federates identity, and omit it
  otherwise.
* **Metadata shape** — all three are ``safety_level="safe"``,
  ``requires_approval=False``, tagged ``read-only``, with
  ``additionalProperties=False`` parameter schemas and non-empty
  ``llm_instructions``; no write op is registered.

Mirrors :mod:`tests.test_connectors_vcf_operations_credread` for the
dispatch/Vault-fake lifecycle and :mod:`tests.test_connectors_argocd_reads`
for the typed-op registration + metadata invariants.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_operations import (
    VROPS_TYPED_OPS,
    VcfOperationsConnector,
    register_vcf_operations_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import import_handler, reset_handler_cache
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_CANARY_USERNAME = "svc-vrops-typed-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-typed-reads-vrops"

_PRODUCT = "vrops"
_VERSION = "9.0"
_IMPL_ID = "vrops-rest"
_CONNECTOR_ID = "vrops-rest-9.0"

_VROPS_HOST = "vrops-typed-reads.test.invalid"
_VROPS_BASE_URL = f"https://{_VROPS_HOST}"

_LIVENESS_PATH = "/suite-api/api/versions/current"
_ALERTS_PATH = "/suite-api/api/alerts"
_RESOURCES_QUERY_PATH = "/suite-api/api/resources/query"

_LIVENESS_RESPONSE: dict[str, Any] = {"releaseName": "9.0.0", "buildNumber": 23456789}
_ALERTS_RESPONSE: dict[str, Any] = {
    "alerts": [
        {"alertId": "a-1", "alertLevel": 5, "status": "ACTIVE", "resourceId": "r-1"},
    ],
    "pageInfo": {"page": 0, "pageSize": 1000, "totalCount": 1},
}
_RESOURCES_QUERY_RESPONSE: dict[str, Any] = {
    "resourceList": [
        {
            "identifier": "r-1",
            "resourceKey": {"name": "vm-01", "resourceKindKey": "VirtualMachine"},
        },
    ],
    "pageInfo": {"page": 0, "pageSize": 1000, "totalCount": 1},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VcfOperationsConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _TypedReadTarget:
    """Target satisfying both ``VcfOperationsTargetLike`` and the resolver shape."""

    def __init__(self, *, auth_source: str | None = None) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0b2")
        self.name = "vrops-typed-reads"
        self.host = _VROPS_HOST
        self.port = 443
        self.secret_ref = "targets/op-typed/vrops-typed-reads"
        self.auth_model = "shared_service_account"
        self.auth_source: str | None = auth_source


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-typed-vrops",
        name="Typed Reads vROps Operator",
        email=None,
        raw_jwt="op.typed.vrops.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b2"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _register_typed_ops(stub: AsyncMock) -> None:
    """Upsert VROPS_TYPED_OPS into the test DB via the real registrar."""
    await register_vcf_operations_typed_operations(embedding_service=stub)


# ---------------------------------------------------------------------------
# Live dispatch — each typed op hits the right path on a fresh (zero-catalog) DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "params", "method", "path", "payload"),
    [
        ("vrops.liveness", {}, "GET", _LIVENESS_PATH, _LIVENESS_RESPONSE),
        ("vrops.alert.list", {"activeOnly": True}, "GET", _ALERTS_PATH, _ALERTS_RESPONSE),
        (
            "vrops.resource.query",
            {"resourceKind": ["VirtualMachine"]},
            "POST",
            _RESOURCES_QUERY_PATH,
            _RESOURCES_QUERY_RESPONSE,
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
@pytest.mark.asyncio
async def test_each_typed_read_dispatches_live_and_returns_payload(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    """Each audited read dispatches end to end on a zero-catalog DB and returns the payload."""
    await _register_typed_ops(_stub_embedding)
    install_fake_client(
        monkeypatch, secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    )

    target = _TypedReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
        route = mock.request(method, path).respond(200, json=payload)
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=target,
            params=params,
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert route.called and route.call_count == 1
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None and sent_auth.startswith("Basic ")


@pytest.mark.asyncio
async def test_typed_ops_registered_with_source_kind_typed(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The audited reads register as source_kind='typed' with resolvable handlers.

    Proves the #2262 invariant: a typed op's dispatch resolves its
    ``handler_ref`` (a bound method), never an ingested descriptor row.
    """
    await _register_typed_ops(_stub_embedding)

    expected = {"vrops.liveness", "vrops.alert.list", "vrops.resource.query"}
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT,
                    EndpointDescriptor.version == _VERSION,
                    EndpointDescriptor.impl_id == _IMPL_ID,
                    EndpointDescriptor.op_id.in_(list(expected)),
                )
            )
        )
        .scalars()
        .all()
    )

    assert {r.op_id for r in rows} == expected
    for row in rows:
        assert row.source_kind == "typed", f"{row.op_id} is {row.source_kind}, want typed"
        assert row.handler_ref, f"{row.op_id} has no handler_ref"
        assert row.requires_approval is False
        assert row.safety_level == "safe"
        # The dotted handler_ref resolves to a callable (dispatch would too).
        assert callable(import_handler(row.handler_ref))


@pytest.mark.asyncio
async def test_resource_query_sends_body_and_paginates(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vrops.resource.query POSTs a ResourceQuerySpec body and paginates via query params."""
    await _register_typed_ops(_stub_embedding)
    install_fake_client(
        monkeypatch, secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    )

    params = {
        "resourceKind": ["VirtualMachine", "HostSystem"],
        "name": ["vm-01"],
        "statKey": "cpu|usage_average",
        "page": 0,
        "pageSize": 100,
    }
    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_RESOURCES_QUERY_PATH).respond(200, json=_RESOURCES_QUERY_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="vrops.resource.query",
            target=_TypedReadTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    request = route.calls[0].request
    body = json.loads(request.content)
    # Body carries the ResourceQuerySpec match fields, not the pagination.
    assert body == {
        "resourceKind": ["VirtualMachine", "HostSystem"],
        "name": ["vm-01"],
        "statKey": "cpu|usage_average",
    }
    # Pagination rides as query params.
    assert request.url.params["page"] == "0"
    assert request.url.params["pageSize"] == "100"


@pytest.mark.asyncio
async def test_auth_source_threads_on_get_and_post(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A federated target's auth-source rides both the GET and the body-POST reads."""
    await _register_typed_ops(_stub_embedding)
    install_fake_client(
        monkeypatch, secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    )
    target = _TypedReadTarget(auth_source="vIDM")

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
        alerts_route = mock.get(_ALERTS_PATH).respond(200, json=_ALERTS_RESPONSE)
        query_route = mock.post(_RESOURCES_QUERY_PATH).respond(200, json=_RESOURCES_QUERY_RESPONSE)

        alerts_result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="vrops.alert.list",
            target=target,
            params={"alertCriticality": "CRITICAL", "resourceId": ["r-1", "r-2"]},
        )
        query_result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="vrops.resource.query",
            target=target,
            params={"name": ["vm-01"]},
        )

    assert alerts_result.status == "ok", alerts_result.error
    assert query_result.status == "ok", query_result.error

    # GET path — auth-source merged by the _request_json override, plus the filters.
    alerts_params = alerts_route.calls[0].request.url.params
    assert alerts_params["auth-source"] == "vIDM"
    assert alerts_params["alertCriticality"] == "CRITICAL"
    resource_ids = [value for key, value in alerts_params.multi_items() if key == "resourceId"]
    assert resource_ids == ["r-1", "r-2"]

    # POST path — auth-source merged by the _post_json override.
    assert query_route.calls[0].request.url.params["auth-source"] == "vIDM"


@pytest.mark.asyncio
async def test_auth_source_omitted_when_unset(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no auth_source, the reads omit ?auth-source= (vROps' default local realm)."""
    await _register_typed_ops(_stub_embedding)
    install_fake_client(
        monkeypatch, secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    )

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_RESOURCES_QUERY_PATH).respond(200, json=_RESOURCES_QUERY_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id="vrops.resource.query",
            target=_TypedReadTarget(auth_source=None),
            params={"name": ["vm-01"]},
        )

    assert result.status == "ok", result.error
    assert "auth-source" not in route.calls[0].request.url.params


# ---------------------------------------------------------------------------
# Metadata invariants
# ---------------------------------------------------------------------------


def test_typed_ops_are_read_only_and_well_formed() -> None:
    """Every typed op is safe/no-approval/read-only with a closed parameter schema."""
    assert {op.op_id for op in VROPS_TYPED_OPS} == {
        "vrops.liveness",
        "vrops.alert.list",
        "vrops.resource.query",
    }
    for op in VROPS_TYPED_OPS:
        assert op.safety_level == "safe", f"{op.op_id} must be safe (read-only surface)"
        assert op.requires_approval is False, f"{op.op_id} must not require approval"
        assert "read-only" in op.tags, f"{op.op_id} missing read-only tag"
        assert op.parameter_schema.get("additionalProperties") is False, (
            f"{op.op_id} parameter_schema must set additionalProperties=False"
        )
        assert op.group_key, f"{op.op_id} must declare a group_key"
        assert op.llm_instructions, f"{op.op_id} must carry llm_instructions"
        assert op.llm_instructions.get("when_to_use", "").strip(), (
            f"{op.op_id} llm_instructions.when_to_use must be non-empty"
        )
