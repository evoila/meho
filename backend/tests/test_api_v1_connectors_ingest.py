# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.connectors_ingest`.

Coverage matrix (G0.7-T6 / Task #406 acceptance criteria):

* **Route mounting.** All seven routes appear in the FastAPI app's
  route table; the OpenAPI document the test client builds advertises
  them.
* **POST /ingest.** Happy path runs the full pipeline; ``dry_run=true``
  short-circuits both the DB writes and the LLM call.
* **GET /.** Status filter narrows the list; built-ins surface
  alongside the operator's-tenant rows; cross-tenant rows are
  filtered out.
* **GET /{id}/review.** Operator-level read; cross-tenant probe → 404.
* **PATCH /{id}/groups/{key}.** Edit writes one audit row.
* **PATCH /{id}/operations/{op}.** Edit writes one audit row; the
  op_id path converter handles colon-prefixed natural keys.
* **POST /{id}/enable.** Idempotent transition; cascade to children.
* **POST /{id}/disable.** Idempotent transition; cascade.
* **RBAC.** Unauthenticated → 401; ``operator`` role on a mutating
  route → 403; ``tenant_admin`` → 200 / 204.
* **Tenant isolation.** Tenant A cannot read tenant B's connector;
  the response is 404, not 403.

Tests boot the FastAPI app with the production middleware stack
(``RequestContextMiddleware`` + ``AuditMiddleware``) so audit rows
are inserted into the autouse-migrated SQLite engine. The LLM client
is replaced via :func:`set_llm_client_factory` with a deterministic
stub so the ingest test paths don't need a real Anthropic key.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connectors_ingest import (
    router as connectors_ingest_router,
)
from meho_backplane.api.v1.connectors_ingest import (
    set_llm_client_factory,
)
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest import (
    GroupingResult,
    IngestionPipelineService,
    IngestionResult,
    InvalidSchemaError,
    InvalidSpecError,
    LlmClient,
    LlmOutputInvalid,
    OpIdCollision,
    UnsupportedSpecError,
    build_invalid_schema_detail,
    build_invalid_spec_detail,
    build_llm_output_invalid_detail,
    build_op_id_collision_detail,
    build_unsupported_spec_detail,
    default_llm_client_factory,
    ensure_connector_class_registered,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache + LLM-client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
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
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _reset_llm_client_factory() -> Iterator[None]:
    """Reset the module-level LLM-client factory between tests.

    Tests that exercise the ingest happy path install their own stub
    via :func:`set_llm_client_factory`; the fixture restores the
    default fail-closed factory after each test so a missing
    reset doesn't leak across the file.
    """
    yield
    set_llm_client_factory(default_llm_client_factory)


@pytest.fixture(autouse=True)
def _reset_ingest_job_registry() -> Iterator[None]:
    """Wipe the in-memory ingest job registry between tests.

    G0.16-T1 (#1303). The async ingest path stores job rows on a
    process-wide :class:`IngestJobRegistry` singleton; without this
    reset, a job_id minted by one test could collide with one
    expected to be 404 in another (test ordering is unstable under
    parallel pytest invocations). The reset runs before and after
    every test so a flaky teardown still hands the next test a
    clean slate.
    """
    from meho_backplane.operations.ingest import reset_job_registry_for_tests

    reset_job_registry_for_tests()
    yield
    reset_job_registry_for_tests()


# ---------------------------------------------------------------------------
# SSRF guard helpers (G0.16-T8, #95)
# ---------------------------------------------------------------------------

# A public IP used for mock getaddrinfo responses — ``93.184.216.34`` is
# IANA's example.com assignment; it is globally routable and non-special
# per the ipaddress module so the SSRF destination guard passes.
_INGEST_TEST_PUBLIC_IP = "93.184.216.34"

# Hostname for all spec mock endpoints in this module.
_SPEC_HOST = "specs.example.test"
_SPEC_BASE = f"https://{_SPEC_HOST}"

# All test hostnames that must resolve to a public IP via the mock.
_INGEST_TEST_HOSTS = frozenset(
    {
        "specs.example.test",
        "example.lab",
        "developer.broadcom.com",
        "keycloak.test",
        "vault.test",
    }
)


def _ingest_getaddrinfo(
    host: str, port: object, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Mock for ``socket.getaddrinfo`` in the SSRF guard.

    Returns a public IP for test hostnames so the destination guard
    accepts them; delegates to the real function for everything else so
    that the discovery/JWKS mock routes wired by respx still resolve.
    """
    if host in _INGEST_TEST_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_INGEST_TEST_PUBLIC_IP, 443))]
    return socket.getaddrinfo(host, port, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _mock_ssrf_getaddrinfo() -> Iterator[None]:
    """Patch the SSRF guard's getaddrinfo for the duration of every test.

    Since G0.16-T8 (#95) the spec fetcher resolves the hostname before
    opening any socket. Tests that pass ``https://specs.example.test/…``
    URIs would fail DNS resolution without this patch.
    """
    with patch(
        "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
        side_effect=_ingest_getaddrinfo,
    ):
        yield


def _register_spec_at_https(
    router: respx.MockRouter,
    spec_path: Path,
    *,
    path: str = "spec.yaml",
    content_type: str = "application/yaml",
) -> str:
    """Register spec file content at an HTTPS mock URL and return the URL.

    Reads ``spec_path`` from disk, registers the bytes at
    ``https://specs.example.test/<path>`` on ``router``, and returns
    the URL. All ingest tests that formerly passed a local file path to
    the ``uri`` field now call this helper instead.
    """
    url = f"{_SPEC_BASE}/{path}"
    router.get(url).mock(
        return_value=httpx.Response(
            200,
            content=spec_path.read_bytes(),
            headers={"content-type": content_type},
        )
    )
    return url


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """An :class:`AsyncMock` standing in for the fastembed singleton.

    Ingest happy-path tests patch the chassis embedding singleton to
    this stub so :func:`register_ingested_operations` doesn't try to
    download the ONNX model from huggingface.co. ``encode_one``
    returns a 384-dim vector of ``0.25`` — same shape
    :mod:`tests.test_operations_register_ingested` uses.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


class _StubLlmClient:
    """Deterministic :class:`LlmClient` for ingest happy-path tests.

    Returns valid Pass-1 / Pass-2 JSON payloads keyed on prompt
    content so :func:`run_llm_grouping` accepts the output verbatim
    and the test doesn't need to mock the upstream Anthropic
    Messages API.

    Not a Pydantic / dataclass — a thin class is enough to satisfy
    the :class:`LlmClient` Protocol's single async method.
    """

    def __init__(self, *, propose_response: str, assign_response: str) -> None:
        self._propose_response = propose_response
        self._assign_response = assign_response

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        # The grouping pass uses two distinct system prompts; route
        # off them rather than parsing user_prompt structure.
        if "Propose" in system_prompt or "propose" in system_prompt:
            return self._propose_response
        return self._assign_response


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mounting only the connectors-ingest router.

    Mirrors prod middleware so the AuditMiddleware writes its row
    into the autouse-migrated SQLite engine for audit-payload
    assertions.
    """
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(connectors_ingest_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_token(
    *, tenant_id: UUID | None = None, sub: str = "op-admin", kid: str = "kid-admin"
) -> tuple[Any, str]:
    """Mint a JWT for a ``tenant_admin`` operator.

    ``kid`` defaults to ``"kid-admin"``; pass a distinct value when a
    single test mints multiple tokens for distinct tenants and pins
    both keys into one JWKS document. Two keys sharing a ``kid``
    collide in the JWKS lookup (the validator picks the first match
    and rejects the second token as a signature mismatch).
    """
    key = _make_rsa_keypair(kid)
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(tid),
    )
    return key, token


def _operator_token(*, tenant_id: UUID | None = None, sub: str = "op-operator") -> tuple[Any, str]:
    """Mint a JWT for an ``operator`` role (not admin)."""
    key = _make_rsa_keypair("kid-operator")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tid),
    )
    return key, token


async def _seed_connector(
    *,
    tenant_id: UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
    group_count: int = 2,
    ops_per_group: int = 3,
    review_status: str = "staged",
    op_is_enabled: bool = False,
    source_kind: str = "ingested",
) -> list[uuid.UUID]:
    """Seed a connector with *group_count* groups + ops per group.

    ``source_kind`` defaults to ``"ingested"`` (the G0.7 spec-driven
    path the bulk of these tests exercise); pass ``"typed"`` or
    ``"composite"`` to seed a G3.x typed / composite connector. For
    typed/composite rows ``method`` / ``path`` are left ``None`` (per
    the dispatcher contract -- those columns are populated for ingested
    rows only).
    """
    sessionmaker = get_sessionmaker()
    is_ingested = source_kind == "ingested"
    group_ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for g_index in range(group_count):
            group_id = uuid.uuid4()
            group_key = f"group-{g_index}"
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    group_key=group_key,
                    name=f"Group {g_index}",
                    when_to_use=f"Use group {g_index} for things.",
                    review_status=review_status,
                ),
            )
            group_ids.append(group_id)
            for o_index in range(ops_per_group):
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=product,
                        version=version,
                        impl_id=impl_id,
                        op_id=f"GET:/api/v1/{group_key}/{o_index}",
                        source_kind=source_kind,
                        method="GET" if is_ingested else None,
                        path=f"/api/v1/{group_key}/{o_index}" if is_ingested else None,
                        group_id=group_id,
                        summary=f"Operation {o_index} in {group_key}",
                        is_enabled=op_is_enabled,
                    ),
                )
        await session.commit()
    return group_ids


async def _group_statuses(
    *,
    tenant_id: UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> dict[str, str]:
    """Return ``{group_key: review_status}`` for every group under the connector."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
        )
        if tenant_id is None:
            stmt = stmt.where(OperationGroup.tenant_id.is_(None))
        else:
            stmt = stmt.where(OperationGroup.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return {group.group_key: group.review_status for group in result.scalars().all()}


async def _audit_row_count(*, op_id: str) -> int:
    """Count audit_log rows whose ``path`` equals *op_id*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return len(list(result.scalars().all()))


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_all_routes_mounted_on_main_app() -> None:
    """The connectors-ingest routes show up in :mod:`meho_backplane.main`'s app.

    Acceptance criterion: ``OpenAPI surface visible at
    /api/v1/openapi.json``. Verified here by introspecting the
    main app's route table; the per-test fixture builds an
    isolated app and would miss a wire-up regression in main.py.

    G0.16-T1 (#1303) added the async-job polling route
    ``/api/v1/connectors/ingest/jobs/{job_id}``; the assertion
    pins it alongside the original seven.
    """
    from meho_backplane.main import app

    expected_paths = {
        "/api/v1/connectors/ingest",
        "/api/v1/connectors/ingest/jobs/{job_id}",
        "/api/v1/connectors",
        "/api/v1/connectors/{connector_id}/review",
        "/api/v1/connectors/{connector_id}/groups/{group_key}",
        "/api/v1/connectors/{connector_id}/operations/{op_id:path}",
        "/api/v1/connectors/{connector_id}/enable",
        "/api/v1/connectors/{connector_id}/disable",
    }
    actual_paths = {getattr(r, "path", None) for r in app.routes}
    missing = expected_paths - actual_paths
    assert not missing, f"missing routes: {missing}"


# ---------------------------------------------------------------------------
# RBAC: unauthenticated + insufficient role
# ---------------------------------------------------------------------------


def test_ingest_unauthenticated_returns_401(client: TestClient) -> None:
    """No Authorization header → 401."""
    response = client.post(
        "/api/v1/connectors/ingest",
        json={
            "product": "vmware",
            "version": "9.0",
            "impl_id": "vmware-rest",
            "specs": [{"uri": "/tmp/spec.yaml"}],
        },
    )
    assert response.status_code == 401


def test_ingest_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on the tenant_admin-gated /ingest → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "vmware",
                "version": "9.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": "/tmp/spec.yaml"}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_role"


def test_enable_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on /enable → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_disable_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on /disable → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/disable",
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_edit_group_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on PATCH /groups → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={"when_to_use": "Updated text."},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_edit_op_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on PATCH /operations → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "dangerous"},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_list_operator_role_returns_200(client: TestClient) -> None:
    """``operator`` role can call GET / (operator minimum).

    Asserts the RBAC contract only: response payload shape varies
    with whatever v2-registered connectors are present at test time
    (T5 #733 unions the class-side registry into the response), so
    the test pins the wire shape (``{"connectors": [...]}``) and the
    200 status code rather than the exact row set.
    """
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    body = response.json()
    assert "connectors" in body
    assert isinstance(body["connectors"], list)


# ---------------------------------------------------------------------------
# GET / -- list connectors + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_operator_tenant_and_builtins(
    client: TestClient,
) -> None:
    """An operator sees their tenant's connectors + built-ins (NULL tenant)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, product="vmware", impl_id="vmware-rest")
    await _seed_connector(tenant_id=None, product="nsx", impl_id="nsx")
    await _seed_connector(tenant_id=tenant_b, product="harbor", impl_id="harbor")

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    # T5 #733: filter to DB-backed rows; class-only v2 registrations
    # (``group_count == 0``) leak into the response from other tests'
    # lifespan boots and aren't the subject of this assertion.
    seen_ids = {c["connector_id"] for c in connectors if c["group_count"] > 0}
    assert seen_ids == {"vmware-rest-9.0", "nsx-9.0"}
    # tenant_b's harbor must not surface.
    assert "harbor-9.0" not in seen_ids


@pytest.mark.asyncio
async def test_list_status_staged_filters_by_aggregate_state(
    client: TestClient,
) -> None:
    """``?status=staged`` returns connectors with ≥1 staged group."""
    tenant_a = uuid.uuid4()
    # vmware: all staged
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        review_status="staged",
    )
    # nsx: all enabled
    await _seed_connector(
        tenant_id=tenant_a,
        product="nsx",
        impl_id="nsx",
        review_status="enabled",
    )

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors?status=staged",
            headers=_authed(token),
        )
    assert response.status_code == 200
    seen_ids = {c["connector_id"] for c in response.json()["connectors"]}
    assert seen_ids == {"vmware-rest-9.0"}


@pytest.mark.asyncio
async def test_list_status_enabled_requires_uniform_state(
    client: TestClient,
) -> None:
    """``?status=enabled`` returns only connectors with every group enabled."""
    tenant_a = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        review_status="enabled",
    )
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors?status=enabled",
            headers=_authed(token),
        )
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    assert len(connectors) == 1
    item = connectors[0]
    assert item["connector_id"] == "vmware-rest-9.0"
    assert item["enabled_group_count"] == item["group_count"]
    assert item["operation_count"] == 6  # 2 groups x 3 ops
    # The op rollup reads the per-op ``is_enabled`` bit, not the
    # group review_status: every group is enabled here but the
    # seeded ops carry ``is_enabled=False`` (G0.23-T5 / #1636).
    assert item["enabled_operation_count"] == 0


@pytest.mark.asyncio
async def test_list_operation_count_includes_typed_and_composite(
    client: TestClient,
) -> None:
    """The ``operation_count`` rollup counts typed + composite rows, not only ingested.

    Regression for Signal #4 in the 2026-05-20 RDC v0.3.0 dogfood (#728):
    ``_operation_count_by_connector`` used to filter ``source_kind ==
    "ingested"``, so typed connectors like ``bind9-ssh-9.x`` /
    ``k8s-1.x`` / ``vault-1.x`` -- whose groups surface (the groups
    aggregator has no source-kind filter) -- rolled up to
    ``operation_count: 0`` while their groups reported the real op
    counts. The paired queries must count the same universe of rows.

    Seeds one connector per ``source_kind`` value -- ingested (3 ops),
    typed (4 ops), composite (2 ops) -- and asserts each connector's
    ``operation_count`` matches the seeded total.
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        group_count=1,
        ops_per_group=3,
        source_kind="ingested",
    )
    await _seed_connector(
        tenant_id=tenant_a,
        product="bind9",
        impl_id="bind9-ssh",
        group_count=2,
        ops_per_group=2,
        source_kind="typed",
    )
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-composite",
        group_count=1,
        ops_per_group=2,
        source_kind="composite",
    )

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    by_id = {c["connector_id"]: c for c in response.json()["connectors"]}
    assert by_id["vmware-rest-9.0"]["operation_count"] == 3
    assert by_id["bind9-ssh-9.0"]["operation_count"] == 4
    assert by_id["vmware-composite-9.0"]["operation_count"] == 2


@pytest.mark.asyncio
async def test_list_splits_enabled_vs_total_operation_count(
    client: TestClient,
) -> None:
    """``enabled_operation_count`` counts ``is_enabled`` rows; ``operation_count`` counts all.

    G0.23-T5 (#1636): the v0.12.0 vmware campaign observed
    ``vmware-rest-9.0`` listing ~2,211 ingested ops of which only a
    small fraction were enabled (dispatchable), and nothing on the
    listing row said which of the two numbers ``operation_count``
    was. Seeds the same connector with 6 ops all disabled, flips 2
    to ``is_enabled=True``, and asserts the row splits 6-total /
    2-enabled -- the two numbers differing is the point of the test.
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        group_count=2,
        ops_per_group=3,
        op_is_enabled=False,
    )
    # Flip two ops to enabled so the two rollups must differ;
    # deterministic pick via op_id ordering.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor)
            .where(EndpointDescriptor.tenant_id == tenant_a)
            .order_by(EndpointDescriptor.op_id)
            .limit(2),
        )
        for descriptor in result.scalars().all():
            descriptor.is_enabled = True
        await session.commit()

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    # The unfiltered listing unions class-side "registered" rows for
    # every v2-registered connector; key on connector_id like the
    # sibling rollup tests instead of expecting a single row.
    by_id = {c["connector_id"]: c for c in response.json()["connectors"]}
    item = by_id["vmware-rest-9.0"]
    assert item["state"] == "ingested"
    assert item["operation_count"] == 6
    assert item["enabled_operation_count"] == 2


@pytest.fixture
def _registered_class_only_connectors() -> Iterator[None]:
    """Ensure harbor + sddc-manager are registered against the v2 registry.

    The autouse ``_isolate_global_registries`` fixture in conftest
    snapshots the registry before each test and restores after, so the
    direct ``register_connector_v2`` calls here are scoped to the
    enclosing test. We bypass the connector subpackages'
    ``__init__.py`` (whose import-time registration only fires once
    per process, after which subsequent tests would see an empty
    registry post-snapshot-restore) and call ``register_connector_v2``
    directly so every test that uses this fixture gets a deterministic
    registration. The ``key in registry`` guards make the fixture
    idempotent against the case where an earlier test in the same
    process already imported the subpackage and the snapshot kept
    the entry alive — without the guard the second call would raise
    ``RuntimeError: connector already registered``.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.connectors.registry import (
        all_connectors_v2,
        register_connector_v2,
    )
    from meho_backplane.connectors.sddc_manager.connector import (
        SddcManagerConnector,
    )

    existing = all_connectors_v2()
    if ("harbor", "2.x", "harbor-rest") not in existing:
        register_connector_v2(
            product="harbor",
            version="2.x",
            impl_id="harbor-rest",
            cls=HarborConnector,
        )
    if ("sddc-manager", "9.0", "sddc-rest") not in existing:
        register_connector_v2(
            product="sddc-manager",
            version="9.0",
            impl_id="sddc-rest",
            cls=SddcManagerConnector,
        )
    yield


@pytest.mark.asyncio
async def test_list_surfaces_register_connector_v2_only_entries(
    client: TestClient,
    _registered_class_only_connectors: None,
) -> None:
    """``register_connector_v2``-only connectors appear with zero counts.

    T5 (#733): connectors registered against the v2 registry but
    without any rows in ``operation_group`` / ``endpoint_descriptor``
    yet should surface in ``GET /api/v1/connectors`` so operators
    see "connector registered ⇒ visible in list". Built-in
    (``tenant_id IS NULL``); ``group_count`` / ``operation_count``
    both ``0``.

    G0.9.1-T1 (#773) extends T5 with two refinements:

    * Class-side-only rows carry ``state="registered"`` so an LLM /
      operator browsing the catalog distinguishes
      *registered-but-not-yet-dispatchable* from *ingested-and-ready*.
    * The emitted ``product`` is what
      :func:`~meho_backplane.operations._lookup.parse_connector_id`
      derives from the ``connector_id``, not the v2 registry's
      ``product`` field. For SDDC the registry stores
      ``product="sddc-manager"`` but the listing emits ``"sddc"`` —
      consistent with what the dispatcher derives from
      ``parse_connector_id("sddc-rest-9.0")`` and with
      ``SDDC_PRODUCT="sddc"`` writing into ``endpoint_descriptor``
      rows. When DB rows land under the parser-derived product the
      row transitions cleanly from ``state="registered"`` to
      ``state="ingested"`` without a ``connector_id`` change.

    G0.18-T2 (#1355) — the parser-derived ``"sddc"`` token the
    listing emits is bridged to the registry's canonical
    ``"sddc-manager"`` by the
    :data:`~meho_backplane.connectors.registry.PRODUCT_ALIASES`
    map at the write surface (see
    :func:`~meho_backplane.connectors.registry.canonical_product_token`).
    So an operator copying ``product`` out of this listing into
    ``POST /api/v1/targets`` succeeds: the alias normalises ``"sddc"``
    to the canonical ``"sddc-manager"`` before the registered-product
    validator runs, and the canonical token is what gets stored.
    The listing keeps emitting ``"sddc"`` (not ``"sddc-manager"``)
    because that is the parser-derived token, load-bearing for the
    #773 connector_id round-trip; the round-trip closure for the
    operator is now end-to-end (closes #1312 acceptance B,
    re-flagged by RDC #789 Finding 6).
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    by_id = {c["connector_id"]: c for c in connectors}

    assert "harbor-rest-2.x" in by_id
    harbor = by_id["harbor-rest-2.x"]
    assert harbor["product"] == "harbor"
    assert harbor["version"] == "2.x"
    assert harbor["impl_id"] == "harbor-rest"
    assert harbor["tenant_id"] is None
    assert harbor["group_count"] == 0
    assert harbor["staged_group_count"] == 0
    assert harbor["enabled_group_count"] == 0
    assert harbor["disabled_group_count"] == 0
    assert harbor["operation_count"] == 0
    assert harbor["enabled_operation_count"] == 0
    assert harbor["state"] == "registered"

    assert "sddc-rest-9.0" in by_id
    sddc = by_id["sddc-rest-9.0"]
    # The listing emits the parser-derived product ("sddc"), not the
    # v2 registry's "sddc-manager" — see test docstring for rationale.
    # G0.18-T2 (#1355): the value below is what an operator copies
    # into POST /api/v1/targets; round-trip closure is proved by
    # ``test_create_target_accepts_sddc_listing_alias`` in
    # ``test_api_v1_targets.py`` (the alias bridges this listing
    # token to the canonical "sddc-manager" before validation).
    assert sddc["product"] == "sddc"
    assert sddc["impl_id"] == "sddc-rest"
    assert sddc["version"] == "9.0"
    assert sddc["operation_count"] == 0
    assert sddc["enabled_operation_count"] == 0
    assert sddc["group_count"] == 0
    assert sddc["state"] == "registered"


@pytest.mark.asyncio
async def test_list_class_only_entries_excluded_under_status_narrowing(
    client: TestClient,
    _registered_class_only_connectors: None,
) -> None:
    """Class-only v2 entries are excluded under ``?status=staged``.

    A connector with zero groups has nothing to review; surfacing it
    under the review-queue filter would dilute the queue view. The
    union is for the default / ``?status=all`` listing only.
    """
    tenant_a = uuid.uuid4()
    # Seed one staged DB-backed connector so the result isn't empty.
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        review_status="staged",
    )
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors?status=staged",
            headers=_authed(token),
        )
    assert response.status_code == 200
    seen_ids = {c["connector_id"] for c in response.json()["connectors"]}
    assert "vmware-rest-9.0" in seen_ids
    assert "harbor-rest-2.x" not in seen_ids


# ---------------------------------------------------------------------------
# G0.13-T3 (#1133) next_step hint contract
# ---------------------------------------------------------------------------


@pytest.fixture
def _registered_class_only_with_uncatalogued_entry() -> Iterator[None]:
    """Register one v2 connector whose ``(product, version)`` is NOT in the catalog.

    Drives the not-in-catalog branch of :func:`_next_step_for_registered`.
    Uses a deliberately synthetic ``("custom-vendor", "1.0")`` triple — no
    catalog entry exists under that pair (the catalog ships seven curated
    products and ``custom-vendor`` is not one of them), so the hint must
    fall through to the manual-mode rationale.

    Re-uses :class:`HarborConnector` as the placeholder class because it
    has a no-arg construct path and the test only exercises the listing —
    the class is never instantiated.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.connectors.registry import (
        all_connectors_v2,
        register_connector_v2,
    )

    existing = all_connectors_v2()
    if ("custom-vendor", "1.0", "custom-rest") not in existing:
        register_connector_v2(
            product="custom-vendor",
            version="1.0",
            impl_id="custom-rest",
            cls=HarborConnector,
        )
    yield


@pytest.mark.asyncio
async def test_list_registered_row_carries_catalog_next_step_hint(
    client: TestClient,
    _registered_class_only_connectors: None,
) -> None:
    """Catalog-hit + ``catalog_ingest="supported"``: row carries the ``--catalog`` verb.

    G0.13-T3 (#1133) AC #1 + #3 (catalog-hit half):
    rows with ``state="registered"`` include a ``next_step`` field with
    ``verb`` + ``rationale``, and the hint correctly distinguishes "catalog
    has it" — the verb points at ``meho connector ingest --catalog
    <product>/<version>``.

    Harbor exercises the catalog-supported branch: its row's
    ``catalog_ingest`` defaults to ``"supported"`` (the upstream is
    `raw.githubusercontent.com` JSON, directly fetchable), so the hint
    points at the ``--catalog`` verb. The SDDC-as-catalog-hit case has
    moved to ``test_list_registered_row_spec_only_catalog_entry_points_at_spec``
    after G0.18-T8 (#1361) reclassified VCF-family rows as
    ``catalog_ingest: spec-only``.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    by_id = {c["connector_id"]: c for c in response.json()["connectors"]}

    harbor = by_id["harbor-rest-2.x"]
    assert harbor["state"] == "registered"
    assert harbor["next_step"] is not None
    assert harbor["next_step"]["verb"] == "meho connector ingest --catalog harbor/2.x"
    assert "catalog" in harbor["next_step"]["rationale"]
    assert "ingest" in harbor["next_step"]["rationale"]


@pytest.mark.asyncio
async def test_list_registered_row_spec_only_catalog_entry_points_at_spec(
    client: TestClient,
    _registered_class_only_connectors: None,
) -> None:
    """Catalog-hit + ``catalog_ingest="spec-only"``: row carries the ``--spec`` verb.

    G0.18-T8 (#1361) / RDC #789 N8. The VCF-family rows
    (``vmware/9.0``, ``sddc-manager/9.0``, ``nsx/9.0``) ship with
    ``catalog_ingest: spec-only`` because their upstream URLs are
    Broadcom Developer Portal HTML landing pages (vmware, sddc-manager)
    or fqdn-templated appliance URLs (nsx) — neither shape can drive
    ``meho connector ingest --catalog`` server-side. The previous hint
    ("spec available in catalog; run ingest") sent operators into a
    422; the refined hint points at the explicit-quadruple ``--spec``
    form using the catalog's native triple so the verb still
    copies-and-runs once the operator has the spec file in hand.

    SDDC is the load-bearing case: the listing emits the parser-derived
    ``product="sddc"`` but the catalog's native triple is
    ``("sddc-manager", "9.0", "sddc-rest")``; the hint uses the
    catalog's spelling so the operator's ``--product`` flag matches the
    registered class (canonical_product_token handles the
    listing-vs-registry split at write-time via PRODUCT_ALIASES, but
    the manual-mode ingest path takes the catalog's spelling
    verbatim).
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    by_id = {c["connector_id"]: c for c in response.json()["connectors"]}

    sddc = by_id["sddc-rest-9.0"]
    assert sddc["state"] == "registered"
    assert sddc["next_step"] is not None
    verb = sddc["next_step"]["verb"]
    # The refined hint must NOT promise the broken ``--catalog`` path.
    assert "--catalog" not in verb
    # And must direct the operator at ``--spec`` with the catalog's
    # native triple (so the registered class resolves at ingest time).
    assert "--product sddc-manager" in verb
    assert "--version 9.0" in verb
    assert "--impl sddc-rest" in verb
    assert "--spec" in verb
    rationale = sddc["next_step"]["rationale"]
    # Rationale names the reason so an operator (or LLM agent) knows
    # the catalog row isn't broken, it's just upstream-shape-bound.
    assert "HTML-portal" in rationale or "fqdn-templated" in rationale
    assert "--spec" in rationale


@pytest.mark.asyncio
async def test_list_registered_row_without_catalog_entry_points_at_manual_mode(
    client: TestClient,
    _registered_class_only_with_uncatalogued_entry: None,
) -> None:
    """Catalog-miss branch: not-in-catalog rows point at manual-mode ingest.

    G0.13-T3 (#1133) AC #2 + #3 (catalog-miss half): when the catalog
    doesn't carry the registered connector, the rationale says so and
    points at manual-mode ``meho connector ingest`` with ``--spec``.

    Uses a synthetic ``("custom-vendor", "1.0")`` v2 registration with
    no catalog entry. The hint must:

    * point at ``meho connector ingest --product custom-vendor --version
      1.0 --impl custom-rest --spec <upstream-openapi-uri>`` (the
      manual-mode invocation), echoing the registry's natural key so
      the operator copies the right values verbatim;
    * carry a rationale that says the catalog has no entry so the
      operator knows they need to source the OpenAPI spec themselves;
    * name the hand-authored on-ramp (#1533 / ci-07) so a spec-less
      product whose vendor publishes no OpenAPI at all doesn't read as
      a dead end — the operator can author a minimal OpenAPI 3.x and
      pass it via ``--spec file://…``.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    by_id = {c["connector_id"]: c for c in response.json()["connectors"]}

    custom = by_id["custom-rest-1.0"]
    assert custom["state"] == "registered"
    assert custom["next_step"] is not None
    verb = custom["next_step"]["verb"]
    assert "--catalog" not in verb
    assert "--product custom-vendor" in verb
    assert "--version 1.0" in verb
    assert "--impl custom-rest" in verb
    assert "--spec" in verb
    rationale = custom["next_step"]["rationale"]
    assert "not in catalog" in rationale
    assert "--spec" in rationale
    # The widened rationale (#1533) must name the hand-authored route so
    # a spec-less product doesn't read as a dead end.
    assert "author a minimal OpenAPI 3.x" in rationale
    assert "file://" in rationale


@pytest.mark.asyncio
async def test_list_ingested_row_omits_next_step_hint(
    client: TestClient,
) -> None:
    """``state="ingested"`` rows set ``next_step=None`` (no operator action remains).

    G0.13-T3 (#1133) AC #1 (ingested-rows half): an ingested connector's
    dispatcher resolves operations against it, so there is no workflow-
    completion verb to surface. The contract is ``next_step=null`` (we
    chose the null shape, documented in the schema, rather than the
    field-omission alternative — both were called out as
    implementer's-call in the task body).
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        group_count=1,
        ops_per_group=2,
    )
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    by_id = {c["connector_id"]: c for c in response.json()["connectors"]}

    vmware = by_id["vmware-rest-9.0"]
    assert vmware["state"] == "ingested"
    # Field is present in the wire shape (Pydantic emits the default) and
    # explicitly null — the catalog-completion verb only applies to the
    # registered-but-not-yet-ingested branch.
    assert "next_step" in vmware
    assert vmware["next_step"] is None


# ---------------------------------------------------------------------------
# G0.9.1-T1 (#773) listing-integrity contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_drops_stale_impl_id_rows_whose_connector_id_will_not_resolve(
    client: TestClient,
) -> None:
    """A stale-rename row that won't round-trip is filtered out.

    G0.9.1-T1 (#773) regression for Signal #6 in the 2026-05-21 RDC
    v0.3.1 dogfood. The consumer deploy carried ``operation_group`` /
    ``endpoint_descriptor`` rows under ``impl_id="kubernetes-asyncio"``
    from the G3.2 (#320) ship, but the connector now registers under
    ``impl_id="k8s"``. ``GET /api/v1/connectors`` emitted
    ``"kubernetes-asyncio-1.x"`` for the stale rows; every dispatcher
    call against that id then returned ``HTTP 404 UnknownConnector``
    because the parser derives ``product="kubernetes"`` from
    ``connector_id``, but the seeded rows are under
    ``product="k8s"``. Round-trip lost.

    The fix: drop any DB-backed row whose emitted ``connector_id``
    doesn't round-trip through
    :func:`~meho_backplane.operations._lookup.connector_exists`. The
    listing now never advertises an id the dispatcher cannot resolve.
    """
    tenant_a = uuid.uuid4()
    # Stale-rename row matching the consumer's deploy: rows live under
    # product="k8s" but with the pre-rename impl_id="kubernetes-asyncio".
    # build_connector_id emits "kubernetes-asyncio-1.x"; parse_connector_id
    # derives product="kubernetes", which connector_exists won't find.
    await _seed_connector(
        tenant_id=None,
        product="k8s",
        version="1.x",
        impl_id="kubernetes-asyncio",
        group_count=1,
        ops_per_group=2,
        source_kind="typed",
    )
    # Healthy control row so the listing isn't empty.
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        group_count=1,
        ops_per_group=1,
    )

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    seen_ids = {c["connector_id"] for c in connectors}

    assert "vmware-rest-9.0" in seen_ids
    assert "kubernetes-asyncio-1.x" not in seen_ids


@pytest.mark.asyncio
async def test_list_every_connector_id_round_trips_through_dispatcher(
    client: TestClient,
    _registered_class_only_connectors: None,
) -> None:
    """Every emitted ``connector_id`` resolves through ``connector_exists``.

    G0.9.1-T1 (#773) acceptance criterion #2 (verbatim):

        every ``connector_id`` returned by ``list_ingested_connectors``
        resolves true through
        ``connector_exists(parse_connector_id(connector_id))`` —
        asserted over a seeded DB that includes a stale-impl_id row
        and a class-side-only opless connector.

    Seeds a stale-rename row (filtered out), a healthy DB-backed row
    (kept, ``state="ingested"``), and exercises the class-side-only
    registered-but-empty path via the ``_registered_class_only_connectors``
    fixture (kept, ``state="registered"``). The assertion: for every
    row the listing returns where ``state == "ingested"``, the
    dispatcher's
    :func:`~meho_backplane.operations._lookup.connector_exists` returns
    ``True`` for the parsed triple. ``state == "registered"`` rows are
    explicitly *not* dispatchable yet; they're surfaced as a discovery
    signal, and the operator / agent reads ``state`` to know they
    can't call ops against them.
    """
    from meho_backplane.auth.operator import Operator, TenantRole
    from meho_backplane.operations._lookup import (
        connector_exists,
        parse_connector_id,
    )

    tenant_a = uuid.uuid4()
    # Stale-rename row — dispatcher cannot resolve emitted connector_id.
    await _seed_connector(
        tenant_id=None,
        product="k8s",
        version="1.x",
        impl_id="kubernetes-asyncio",
        group_count=1,
        ops_per_group=1,
        source_kind="typed",
    )
    # Healthy ingested row — dispatcher resolves cleanly.
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        group_count=1,
        ops_per_group=2,
    )

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    operator = Operator(
        sub="op-roundtrip",
        name="Round-Trip Probe",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_a,
        tenant_role=TenantRole.OPERATOR,
    )

    # Stale id must not surface.
    seen_ids = {c["connector_id"] for c in connectors}
    assert "kubernetes-asyncio-1.x" not in seen_ids
    # Healthy ingested row must surface as ingested.
    by_id = {c["connector_id"]: c for c in connectors}
    assert by_id["vmware-rest-9.0"]["state"] == "ingested"
    # Class-only registered rows must surface as registered.
    assert by_id["harbor-rest-2.x"]["state"] == "registered"

    # Round-trip every emitted id through the dispatcher resolve path.
    # ``state="ingested"`` rows must resolve through connector_exists;
    # ``state="registered"`` rows are documented not-yet-dispatchable
    # and the assertion below pins that contract too.
    for item in connectors:
        parsed = parse_connector_id(item["connector_id"])
        product, version, impl_id = parsed
        exists = await connector_exists(
            tenant_id=operator.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
        )
        if item["state"] == "ingested":
            assert exists, (
                f"ingested row {item['connector_id']!r} did not round-trip — "
                f"parsed={parsed}, but connector_exists returned False"
            )
        else:
            assert item["state"] == "registered"
            assert not exists, (
                f"registered (class-only) row {item['connector_id']!r} "
                f"unexpectedly resolves through connector_exists — the "
                f"row should be ingested, not registered"
            )


@pytest.fixture
def _every_v2_connector_registered() -> Iterator[None]:
    """Register every production v2 connector for the round-trip test.

    Each production connector subpackage performs its
    ``register_connector_v2`` call at import time, but the autouse
    ``_isolate_global_registries`` fixture snapshots-and-restores the
    registry around each test, so only the entries present at session
    start (i.e. none, post-restore) survive. Re-register the full
    production set directly so the round-trip test sees every entry
    rather than a partial slice.
    """
    from meho_backplane.connectors.bind9.connector import Bind9Connector
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.connectors.hetzner_robot.connector import HetznerRobotConnector
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.nsx.connector import NsxConnector
    from meho_backplane.connectors.registry import (
        all_connectors_v2,
        register_connector_v2,
    )
    from meho_backplane.connectors.sddc_manager.connector import (
        SddcManagerConnector,
    )
    from meho_backplane.connectors.vault.connector import VaultConnector
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

    existing = all_connectors_v2()
    entries: tuple[tuple[str, str, str, type], ...] = (
        ("bind9", "9.x", "bind9-ssh", Bind9Connector),
        ("harbor", "2.x", "harbor-rest", HarborConnector),
        ("hetzner-robot", "2026.04", "hetzner-rest", HetznerRobotConnector),
        ("k8s", "1.x", "k8s", KubernetesConnector),
        ("nsx", "9.0", "nsx-rest", NsxConnector),
        ("sddc-manager", "9.0", "sddc-rest", SddcManagerConnector),
        ("vault", "1.x", "vault", VaultConnector),
        ("vmware", "9.0", "vmware-rest", VmwareRestConnector),
    )
    for product, version, impl_id, cls in entries:
        if (product, version, impl_id) not in existing:
            register_connector_v2(
                product=product,
                version=version,
                impl_id=impl_id,
                cls=cls,
            )
    yield


@pytest.mark.asyncio
async def test_register_connector_v2_round_trip_lossless_for_every_entry(
    _every_v2_connector_registered: None,
) -> None:
    """The build/parse round-trip is lossless for every registered v2 entry.

    G0.9.1-T1 (#773) acceptance criterion #1 (verbatim):

        Round-trip unit test:
        ``parse_connector_id(build_connector_id(p, v, i)) == (p, v, i)``
        holds for every connector registered via ``register_connector_v2``
        / typed registration, including a case where
        ``product != impl_id.split("-")[0]``.

    The check is *operationally* strict: we don't require the parser
    to recover the registry's friendly ``product`` (SDDC is the
    canonical exception: registry ``product="sddc-manager"``, but the
    dispatcher derives ``"sddc"`` from ``impl_id="sddc-rest"``). What
    must hold is that:

    * the parser recovers ``(version, impl_id)`` losslessly — these
      are the natural-key columns the dispatcher matches on; and
    * when the registry's ``product`` differs from the parser's
      derived ``product``, the registry's ``product`` matches what
      :func:`~meho_backplane.connectors.kubernetes.SDDC_PRODUCT`-style
      constants write into ``endpoint_descriptor`` rows — i.e., the
      dispatcher's parsed product matches the DB row product, so a
      DB-backed row lookup succeeds.

    Concretely: ``product != impl_id.split("-")[0]`` is allowed when
    the v2-registry product is purely a friendly resolver-key label
    and DB writes use the parser-derived product (the documented SDDC
    convention). It is *not* allowed when the registry product would
    also be the DB-row product, because then the dispatcher's parse
    would miss every row. The class-side-only listing path
    (``_class_side_only_items``) enforces this by emitting the
    parser-derived ``product`` on each class-only row.

    This test exhaustively enumerates ``all_connectors_v2()`` and
    pins the property; new connectors added to the registry are
    automatically covered.
    """
    from meho_backplane.connectors.registry import all_connectors_v2
    from meho_backplane.operations._lookup import parse_connector_id
    from meho_backplane.operations.ingest._llm_grouping_internals import (
        build_connector_id,
    )

    registry = all_connectors_v2()
    # Sanity-check the registry actually has the SDDC entry that
    # exercises the product != impl_id.split("-")[0] case the
    # acceptance criterion calls out explicitly.
    assert ("sddc-manager", "9.0", "sddc-rest") in registry, (
        "SDDC v2 registration missing — the test relies on it as the "
        "canonical product != impl_id.split('-')[0] case"
    )

    for (product, version, impl_id), _cls in registry.items():
        if not version or not impl_id:
            # v1-compat shim (e.g. ("vault", "", "")) — not separately
            # registered, see _class_side_only_items docstring.
            continue
        connector_id = build_connector_id(product, version, impl_id)
        parsed_product, parsed_version, parsed_impl_id = parse_connector_id(
            connector_id,
        )
        # (version, impl_id) must round-trip losslessly — these are
        # the dispatcher's natural-key columns.
        assert parsed_version == version, (
            f"version round-trip lost for {(product, version, impl_id)}: "
            f"build={connector_id!r} parsed_version={parsed_version!r}"
        )
        assert parsed_impl_id == impl_id, (
            f"impl_id round-trip lost for {(product, version, impl_id)}: "
            f"build={connector_id!r} parsed_impl_id={parsed_impl_id!r}"
        )
        # product is allowed to differ from the registry's friendly
        # name only when the parser's derivation matches what DB
        # writes use (the SDDC convention). Catch any future registration
        # that quietly breaks this contract.
        derived_product = impl_id.split("-")[0]
        assert parsed_product == derived_product, (
            f"parser-derived product disagrees with impl_id-prefix "
            f"convention for {(product, version, impl_id)}: "
            f"derived={derived_product!r} parsed={parsed_product!r}"
        )


# ---------------------------------------------------------------------------
# GET /{id}/review -- read + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_review_returns_payload_for_operator_tenant(
    client: TestClient,
) -> None:
    """Operator-level read returns the review payload."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, group_count=2, ops_per_group=3)
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors/vmware-rest-9.0/review",
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["connector_id"] == "vmware-rest-9.0"
    assert body["product"] == "vmware"
    assert body["total_op_count"] == 6
    assert len(body["groups"]) == 2


@pytest.mark.asyncio
async def test_get_review_cross_tenant_returns_404(
    client: TestClient,
) -> None:
    """Tenant A cannot see tenant B's connector — 404, not 403."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b, product="vmware", impl_id="vmware-rest")
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors/vmware-rest-9.0/review",
            headers=_authed(token),
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_review_builtin_connector_visible_to_non_admin_operator(
    client: TestClient,
) -> None:
    """G0.13-T5 (#1135): GET /{id}/review on a global (``tenant_id IS NULL``) row.

    The listing endpoint promises "operator's-tenant rows +
    built-ins" and surfaces them; the review endpoint must honour
    the same scope rather than 404 on every global. Verifies the
    full HTTP-to-service two-pass round-trip for a non-admin
    operator role (the role that hit the bug on the daily-driver
    path).
    """
    operator_tenant = uuid.uuid4()
    await _seed_connector(
        tenant_id=None,
        product="vmware",
        impl_id="vmware-rest",
        group_count=2,
        ops_per_group=3,
    )
    key, token = _operator_token(tenant_id=operator_tenant)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors/vmware-rest-9.0/review",
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["connector_id"] == "vmware-rest-9.0"
    assert body["tenant_id"] is None
    assert body["total_op_count"] == 6


# ---------------------------------------------------------------------------
# PATCH /{id}/groups/{key}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_group_updates_and_writes_audit_row(
    client: TestClient,
) -> None:
    """PATCH on a group updates the row + writes one audit row."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={"when_to_use": "New description text.", "name": "Renamed"},
            headers=_authed(token),
        )
    assert response.status_code == 204
    audit_count = await _audit_row_count(op_id="meho.connector.edit_group")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_edit_group_empty_body_returns_400(
    client: TestClient,
) -> None:
    """PATCH with no fields → 400."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={},
            headers=_authed(token),
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_edit_group_cross_tenant_returns_404(
    client: TestClient,
) -> None:
    """Cross-tenant PATCH → 404."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={"name": "Renamed"},
            headers=_authed(token),
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id}/operations/{op_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_op_updates_safety_level_and_writes_audit_row(
    client: TestClient,
) -> None:
    """PATCH on an op updates safety_level + writes one audit row.

    Exercises the ``:path`` converter on the ``op_id`` segment so a
    colon-prefixed natural key (``"GET:/api/v1/group-0/0"``) survives
    URL routing intact. Since G0.23-T4 (#1630) the route returns 200
    with an ``EditOpResponse`` envelope (``warnings`` empty here — no
    enable in the body, no advisory to carry).
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "dangerous", "requires_approval": True},
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert response.json() == {"warnings": []}
    audit_count = await _audit_row_count(op_id="meho.connector.edit_op")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_edit_op_enable_on_auto_shim_connector_returns_structured_warning(
    client: TestClient,
) -> None:
    """``is_enabled=true`` on a shim-backed op → 200 + ``unreplaced_auto_shim`` warning.

    G0.23-T4 (#1630): the connector triple resolves to the synthesised
    ``GenericRestConnector`` auto-shim, so dispatch is a guaranteed
    ``connector_unsupported`` dead end — the REST response must carry
    the structured advisory naming the missing per-product subclass
    while the write itself still lands (audit row included).
    """
    tenant_a = uuid.uuid4()
    assert ensure_connector_class_registered(
        product="acme",
        version="1.2",
        impl_id="acme-rest",
        base_url=None,
    ), "expected a fresh auto-shim registration for the acme triple"
    await _seed_connector(
        tenant_id=tenant_a,
        product="acme",
        version="1.2",
        impl_id="acme-rest",
        group_count=1,
        ops_per_group=1,
    )
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/acme-rest-1.2/operations/GET:/api/v1/group-0/0",
            json={"is_enabled": True},
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert warning["code"] == "unreplaced_auto_shim"
    assert warning["connector_class"] == "AutoShim_acme_1_2_acme_rest"
    assert "per-product Connector subclass" in warning["message"]
    assert await _audit_row_count(op_id="meho.connector.edit_op") == 1


@pytest.mark.asyncio
async def test_edit_op_enable_on_hand_rolled_connector_no_warning(
    client: TestClient,
) -> None:
    """``is_enabled=true`` on a hand-rolled connector's op → 200 + empty warnings.

    Regression guard for G0.23-T4 (#1630): ``vmware-rest-9.0``
    resolves to ``VmwareRestConnector`` (priority 1, hand-rolled), so
    enabling stays advisory-free.
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"is_enabled": True},
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert response.json() == {"warnings": []}


@pytest.mark.asyncio
async def test_edit_op_invalid_safety_level_returns_422(
    client: TestClient,
) -> None:
    """``safety_level`` outside the enum → 422 (Pydantic-level rejection)."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "nuclear"},
            headers=_authed(token),
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /{id}/enable + POST /{id}/disable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_transitions_all_groups_and_cascades(
    client: TestClient,
) -> None:
    """POST /enable transitions every group to ``enabled`` + cascades ops."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 204
    statuses = await _group_statuses(tenant_id=tenant_a)
    assert set(statuses.values()) == {"enabled"}
    audit_count = await _audit_row_count(op_id="meho.connector.enable")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_enable_is_idempotent(client: TestClient) -> None:
    """Second POST /enable writes no additional audit row."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, review_status="enabled", op_is_enabled=True)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 204
    audit_count = await _audit_row_count(op_id="meho.connector.enable")
    assert audit_count == 0


@pytest.mark.asyncio
async def test_disable_transitions_and_writes_audit_row(
    client: TestClient,
) -> None:
    """POST /disable transitions every group to ``disabled``."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, review_status="enabled", op_is_enabled=True)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/disable",
            headers=_authed(token),
        )
    assert response.status_code == 204
    statuses = await _group_statuses(tenant_id=tenant_a)
    assert set(statuses.values()) == {"disabled"}
    audit_count = await _audit_row_count(op_id="meho.connector.disable")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_disable_is_idempotent(client: TestClient) -> None:
    """Second POST /disable writes no additional audit row.

    Mirrors :func:`test_enable_is_idempotent` so both transitions
    carry the same documented contract: replaying the call after
    every group is already in the target state is a no-op at the
    service-level audit row.
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, review_status="enabled", op_is_enabled=True)
    key, token = _admin_token(tenant_id=tenant_a)
    headers = _authed(token)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        first = client.post(
            "/api/v1/connectors/vmware-rest-9.0/disable",
            headers=headers,
        )
        assert first.status_code == 204
        second = client.post(
            "/api/v1/connectors/vmware-rest-9.0/disable",
            headers=headers,
        )
    assert second.status_code == 204
    statuses = await _group_statuses(tenant_id=tenant_a)
    assert set(statuses.values()) == {"disabled"}
    audit_count = await _audit_row_count(op_id="meho.connector.disable")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_enable_cross_tenant_returns_404(client: TestClient) -> None:
    """Cross-tenant enable → 404 (and no state changes leak to the other tenant)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 404
    # tenant_b's connector untouched
    statuses = await _group_statuses(tenant_id=tenant_b)
    assert set(statuses.values()) == {"staged"}


# ---------------------------------------------------------------------------
# POST /ingest happy path
# ---------------------------------------------------------------------------


def test_ingest_returns_503_when_llm_client_unavailable(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """No LLM-client factory wired → 503 from the route's mapper.

    The stub embedding service is injected so the parse + register
    phases run without hitting the network; the unmocked LLM client
    factory is what triggers the 503.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="spec-503.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 503
    assert "LLM client" in response.json()["detail"]


def test_ingest_dry_run_returns_parse_counts_without_writes(
    client: TestClient, tmp_path: Any
) -> None:
    """``dry_run=true`` parses the spec but does not write or run the LLM."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
  /items/{id}:
    get:
      summary: get item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="spec-dryrun.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": spec_url}],
                "dry_run": True,
            },
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ingestion"]["inserted_count"] == 2
    assert body["ingestion"]["connector_registered"] is False
    assert body["grouping"] is None


def test_ingest_happy_path_runs_full_pipeline(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """End-to-end: parse + register + group all run; response carries both
    blocks.

    The LLM client is stubbed via :func:`set_llm_client_factory` so
    the grouping pass doesn't need a real Anthropic key; the
    embedding pipeline is patched at the leaf
    :func:`encode_endpoint_text` site so the test doesn't pull
    fastembed from the network.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
  /items/{id}:
    get:
      summary: get item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: ok
""",
    )
    propose_json = (
        '[{"group_key": "items", "name": "Items", '
        '"when_to_use": "Use these operations to manage items."}]'
    )
    assign_json = '{"GET:/items": "items", "GET:/items/{id}": "items"}'
    set_llm_client_factory(
        lambda: _StubLlmClient(
            propose_response=propose_json,
            assign_response=assign_json,
        ),
    )

    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="spec-happy.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ingestion"]["inserted_count"] == 2
    # connector_registered may be False when a prior test (503-path)
    # already auto-registered the shim against the module-level
    # connectors registry; the v2 registry is process-global. The
    # load-bearing assertion is that the pipeline returned 200 with
    # the grouping pass populated.
    grouping = body["grouping"]
    assert grouping is not None
    assert grouping["groups_created"] == 1
    assert grouping["operations_assigned"] == 2


def test_ingest_bad_spec_returns_400(client: TestClient, tmp_path: Any) -> None:
    """Unparseable spec → 400 from the InvalidSpecError mapper."""
    spec_path = tmp_path / "bad.yaml"
    spec_path.write_text("not: a: valid spec")
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="bad-spec.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 400


def test_ingest_swagger_2_spec_returns_structured_unsupported_spec_400(
    client: TestClient, tmp_path: Any
) -> None:
    """Swagger 2.0 spec → 400 carrying the ``unsupported_spec`` envelope.

    #1610 regression (REST half of the MCP parity #1534 closed): the
    route used to collapse the typed :exc:`UnsupportedSpecError` to a
    bare ``detail="<str(exc)>"`` 400, so a REST/SDK caller had to
    re-parse prose to learn the spec flavour was the problem. The
    body now carries the shared builder's envelope — the stable
    ``unsupported_spec`` classifier plus the message that names the
    declared ``swagger`` version and the ``swagger2openapi`` /
    converter.swagger.io conversion remediation.
    """
    spec_path = tmp_path / "swagger2.yaml"
    spec_path.write_text(
        """swagger: '2.0'
info:
  title: legacy harbor
  version: '1.0'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="swagger2.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "legacy",
                "version": "1.0",
                "impl_id": "legacy-impl",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "unsupported_spec"
    assert "swagger='2.0'" in detail["message"]
    assert "swagger2openapi" in detail["message"]
    assert "converter.swagger.io" in detail["message"]


#: One case per parser-family ``SpecError`` sibling the REST 400 handler
#: maps (#1610): the exception instance the stubbed pipeline raises and
#: the shared builder whose envelope the wire body must equal verbatim.
#: Builders are imported from ``operations/ingest/error_envelopes`` via
#: the package root — the same single source of truth the MCP dispatch
#: table uses — so this test fails if the route ever re-grows a local
#: envelope shape.
_SPEC_ERROR_FAMILY_CASES = [
    pytest.param(
        InvalidSpecError("OpenAPI document must parse to a mapping, got list"),
        build_invalid_spec_detail,
        id="invalid_spec",
    ),
    pytest.param(
        UnsupportedSpecError("OpenAPI version '4.0.0' is not supported (expected 3.0.x or 3.1.x)"),
        build_unsupported_spec_detail,
        id="unsupported_spec",
    ),
    pytest.param(
        InvalidSchemaError("$ref '#/components/schemas/Missing' does not resolve"),
        build_invalid_schema_detail,
        id="invalid_schema",
    ),
    pytest.param(
        OpIdCollision(
            op_ids=["GET:/api/items"],
            product="test",
            version="1.0",
            impl_id="test-impl",
            existing_spec_source="a.yaml",
            incoming_spec_source="b.yaml",
        ),
        build_op_id_collision_detail,
        id="op_id_collision",
    ),
    pytest.param(
        LlmOutputInvalid(
            pass_name="propose_groups",
            raw_output="not json",
            parse_error=ValueError("invalid JSON"),
        ),
        build_llm_output_invalid_detail,
        id="llm_output_invalid",
    ),
]


@pytest.mark.parametrize(("exc", "builder"), _SPEC_ERROR_FAMILY_CASES)
def test_ingest_spec_error_family_returns_structured_400(
    client: TestClient,
    exc: Exception,
    builder: Callable[[Any], dict[str, Any]],
) -> None:
    """Every parser-family ``SpecError`` → 400 with its builder's envelope.

    #1610 — route-mapping test for the five-way dispatch in
    ``_spec_error_http_exception``. The pipeline is stubbed to raise
    each sibling so the test pins the route boundary only (the
    parser/registration/grouping triggers have their own tests); the
    load-bearing assertion is wire-body == builder output, which keeps
    the REST 400 ``detail`` and the MCP ``-32602`` ``error.data``
    member (same builders, see ``raise_invalid_params_for_spec_error``)
    from drifting.
    """
    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch.object(IngestionPipelineService, "ingest", AsyncMock(side_effect=exc)),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": "https://specs.example.test/never-fetched.yaml"}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == builder(exc)


def test_ingest_vcenter_9_under_label_8_returns_422(client: TestClient, tmp_path: Any) -> None:
    """G0.9-T8 regression — operator labels a vCenter-9 spec as ``version=8.0``.

    The spec-vs-label cross-check fires before the parser does its
    full operation walk and returns ``422 Unprocessable Entity`` with
    a structured detail naming both ``spec_info_versions`` (with the
    spec's ``9.0.3``) and ``requested_version`` (the operator's
    ``8.0``) so the error message tells the operator exactly what to
    fix.
    """
    spec_path = tmp_path / "vcenter.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: vCenter
  version: '9.0.3'
paths:
  /vcenter/vm:
    get:
      summary: list VMs
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="vcenter-9.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "vmware",
                "version": "8.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["kind"] == "spec_label_mismatch"
    assert detail["requested_version"] == "8.0"
    assert detail["spec_info_versions"] == [{"spec_uri": spec_url, "info_version": "9.0.3"}]
    # The operator-facing message must mention both versions.
    assert "9.0.3" in detail["message"]
    assert "8.0" in detail["message"]


def test_ingest_compatible_drift_succeeds(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """Spec ``info.version=9.0.3`` + label ``9.1`` → inexact-compatible.

    The cross-check classifies this as ``compatible`` (same major,
    different minor) and proceeds — only the cross-major mismatches
    raise 422.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '9.0.3'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    propose_json = (
        '[{"group_key": "items", "name": "Items", '
        '"when_to_use": "Use these operations to manage items."}]'
    )
    assign_json = '{"GET:/items": "items"}'
    set_llm_client_factory(
        lambda: _StubLlmClient(
            propose_response=propose_json,
            assign_response=assign_json,
        ),
    )

    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="spec-drift.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "drift-test",
                "version": "9.1",
                "impl_id": "drift-impl",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# set_llm_client_factory contract
# ---------------------------------------------------------------------------


def test_set_llm_client_factory_returns_previous_and_restores(
    client: TestClient,
) -> None:
    """The factory setter returns the previous value so callers can restore."""

    def custom_factory() -> LlmClient:  # type: ignore[empty-body]
        ...  # never called in this test

    previous = set_llm_client_factory(custom_factory)
    assert previous is default_llm_client_factory
    restored = set_llm_client_factory(previous)
    assert restored is custom_factory


# ---------------------------------------------------------------------------
# G0.9-T9 (#741) — version label coverage pre-flight at the HTTP layer
# ---------------------------------------------------------------------------


class _RangedTestConnector(Connector):
    """Hand-rolled :class:`Connector` for the 422 pre-flight HTTP tests.

    Pinned ``supported_version_range`` mirrors the real
    ``VmwareRestConnector`` shape so the operator-facing 422 detail
    looks like what a real misconfigured ingest produces.
    """

    product = "t9-vmware"
    version = "9.0"
    impl_id = "t9-vmware-rest"
    supported_version_range = ">=8.5,<10.0"
    priority = 1

    async def fingerprint(self, target: Any) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError


@pytest.fixture
def _registered_ranged_connector() -> Iterator[None]:
    """Register :class:`_RangedTestConnector` for the duration of the test.

    The v2 registry has no per-entry unregister API; this fixture
    inspects the underlying dict and pops the entry on teardown so
    the registration is scoped to the test rather than leaking into
    later cases. The product / impl_id are namespaced (``t9-…``) to
    avoid colliding with any prior auto-shim a happy-path ingest
    test may have left behind.
    """
    from meho_backplane.connectors import registry as _registry_mod
    from meho_backplane.connectors.registry import register_connector_v2

    register_connector_v2(
        product="t9-vmware",
        version="9.0",
        impl_id="t9-vmware-rest",
        cls=_RangedTestConnector,
    )
    try:
        yield
    finally:
        _registry_mod._REGISTRY_V2.pop(("t9-vmware", "9.0", "t9-vmware-rest"), None)


def test_ingest_returns_422_when_version_outside_registered_class_range(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
    _registered_ranged_connector: None,
) -> None:
    """A registered class with ``">=8.5,<10.0"`` + label ``"7.0"`` → 422.

    The pre-flight raises :exc:`UncoveredVersionLabel` (mapped to
    422); the response detail names the existing class so the
    operator can see exactly which range the label fell outside of.
    """
    spec_path = tmp_path / "vcenter.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: vcenter
  version: '7.0'
paths:
  /api/vcenter/cluster:
    get:
      summary: list clusters
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="vcenter-7.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "t9-vmware",
                "version": "7.0",
                "impl_id": "t9-vmware-rest",
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "version='7.0'" in detail
    assert ">=8.5,<10.0" in detail
    assert "_RangedTestConnector" in detail


def test_ingest_dry_run_returns_422_when_version_outside_registered_class_range(
    client: TestClient,
    tmp_path: Any,
    _registered_ranged_connector: None,
) -> None:
    """``dry_run=true`` runs the same pre-flight → 422.

    Catches the orphan-at-ingest mistake during validation so
    operators don't ship a misconfigured ingest by trusting a
    successful dry-run.
    """
    spec_path = tmp_path / "vcenter.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: vcenter
  version: '7.0'
paths:
  /api/vcenter/cluster:
    get:
      summary: list clusters
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="vcenter-7-dryrun.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "t9-vmware",
                "version": "7.0",
                "impl_id": "t9-vmware-rest",
                "specs": [{"uri": spec_url}],
                "dry_run": True,
            },
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    assert "version='7.0'" in response.json()["detail"]


# ---------------------------------------------------------------------------
# G0.14-T9 (#1150) — catalog-driven REST ingest shape
# ---------------------------------------------------------------------------


def _patch_catalog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: list[dict[str, Any]],
) -> None:
    """Install a fake packaged catalog for the route's resolver.

    The route handler calls :func:`load_catalog`, which is cached with
    ``functools.lru_cache``. The cleanest swap is to drop the cache and
    monkey-patch the module-level callable on the symbol the route
    actually imports — without that, an earlier test in the file may
    have warmed the cache against the on-disk catalog and the patched
    entries never reach the resolver.
    """
    from meho_backplane.operations.ingest import catalog as _catalog_mod
    from meho_backplane.operations.ingest.catalog import (
        ConnectorSpecCatalog,
        ConnectorSpecEntry,
    )

    fake = ConnectorSpecCatalog(entries=tuple(ConnectorSpecEntry(**e) for e in entries))
    _catalog_mod.load_catalog.cache_clear()
    # The route imports ``load_catalog`` via the
    # ``meho_backplane.operations.ingest`` package; patching the
    # package attribute is what the handler resolves at call time.
    import meho_backplane.api.v1.connectors_ingest as _route_mod

    monkeypatch.setattr(_route_mod, "load_catalog", lambda: fake)


def test_ingest_catalog_entry_resolves_and_ingests_successfully(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.14-T9: ``{catalog_entry: "vmware/9.0"}`` resolves server-side and ingests.

    The packaged catalog is patched to a single entry pointing at a
    local-file spec so the request body can stay minimal. The
    response carries the same shape an explicit-quadruple request
    against the resolved triple would produce — the catalog-driven
    shape is sugar over the existing ingest path.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: catalog
  version: '9.0'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    catalog_spec_url = f"{_SPEC_BASE}/catalog-spec.yaml"
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "vmware-t9",
                "version": "9.0",
                "impl_id": "vmware-rest-t9",
                "requires_connector_class": "VmwareRestConnector",
                "upstream": (catalog_spec_url,),
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        mock_router.get(catalog_spec_url).mock(
            return_value=httpx.Response(
                200,
                content=spec_path.read_bytes(),
                headers={"content-type": "application/yaml"},
            )
        )
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "vmware-t9/9.0", "dry_run": True},
            headers=_authed(token),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ingestion"]["inserted_count"] == 1
    # The resolved triple round-trips into the connector_id the response
    # echoes (`<impl_id>-<version>`) so a REST client sees the resolved
    # identity without needing to re-derive it.
    assert body["ingestion"]["connector_id"] == "vmware-rest-t9-9.0"


def test_ingest_catalog_entry_unknown_returns_structured_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.14-T9: unknown ``catalog_entry`` → structured 422 per T11 convention.

    The detail body carries ``detail="catalog_entry_not_found"`` plus
    the offending value and the list of valid alternatives so an agent
    can branch + retry without re-fetching the catalog.
    """
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "vmware-t9",
                "version": "9.0",
                "impl_id": "vmware-rest-t9",
                "requires_connector_class": "VmwareRestConnector",
                "upstream": ("https://example.lab/spec.yaml",),
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "nope/1.0"},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "catalog_entry_not_found"
    assert detail["catalog_entry"] == "nope/1.0"
    assert detail["available_entries"] == ["vmware-t9/9.0"]
    assert "error-message-shape" in detail["message"]


def test_ingest_catalog_entry_and_quadruple_supplied_returns_422_conflict(
    client: TestClient,
) -> None:
    """G0.14-T9: both request shapes supplied → 422 ``catalog_entry_conflict``.

    The validator on :class:`IngestRequest` rejects the mixed body at
    the framework boundary (FastAPI surfaces validator failures as 422
    with the message in ``detail[0]["msg"]``); per T11 convention the
    stable classifier is embedded in the message so clients can branch.
    """
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "catalog_entry": "vmware/9.0",
                "product": "vmware",
                "version": "9.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": "/abs/spec.yaml"}],
            },
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    # FastAPI renders validator failures as a list of error objects;
    # the message carries the convention's snake_case classifier so a
    # client can branch on ``catalog_entry_conflict`` without parsing
    # prose.
    messages = [e["msg"] for e in detail]
    assert any("catalog_entry_conflict" in m for m in messages), messages


def test_ingest_empty_body_returns_422_underspecified(
    client: TestClient,
) -> None:
    """G0.14-T9: neither request shape supplied → 422
    ``ingest_request_underspecified``.

    The empty-body shape used to fail "Field required" four times
    (one per quadruple field). The new validator collapses that into
    one classifier-bearing message so the operator's error is
    actionable: pick a shape.
    """
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    messages = [e["msg"] for e in detail]
    assert any("ingest_request_underspecified" in m for m in messages), messages


def test_ingest_catalog_entry_malformed_ref_returns_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.14-T9: ``catalog_entry`` without a ``/`` separator → structured 422.

    A reference shape miss is distinct from "valid shape, not in
    catalog" so an agent can branch differently — the first means
    "fix the slash"; the second means "pick a different entry".
    """
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "vmware-t9",
                "version": "9.0",
                "impl_id": "vmware-rest-t9",
                "requires_connector_class": "VmwareRestConnector",
                "upstream": ("https://example.lab/spec.yaml",),
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "vmware9.0"},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "catalog_entry_malformed"
    assert detail["catalog_entry"] == "vmware9.0"
    assert "error-message-shape" in detail["message"]


def test_ingest_catalog_entry_typed_connector_returns_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.14-T9: a typed-connector entry (``upstream=null``) → structured 422.

    The catalog ships typed connectors (vault, k8s, bind9) with
    ``upstream=null`` — there is no spec to ingest, but the entry
    exists in the catalog. The detail names the resolved triple so an
    interactive operator sees the entry exists but is intentionally
    typed.
    """
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "vault-t9",
                "version": "1.x",
                "impl_id": "vault-t9",
                "requires_connector_class": "VaultConnector",
                "upstream": None,
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "vault-t9/1.x"},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "catalog_entry_typed_connector"
    assert detail["catalog_entry"] == "vault-t9/1.x"
    assert detail["product"] == "vault-t9"
    assert detail["impl_id"] == "vault-t9"


def test_ingest_catalog_entry_templated_upstream_returns_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.14-T9: an fqdn-templated upstream URL → structured 422.

    Appliance-served catalog entries (NSX manager's
    ``https://<nsx-mgr-fqdn>/...``) cannot be dereferenced server-side
    — the placeholder needs an operator-supplied FQDN. The detail
    names the templated URL so the operator sees what to fill in and
    points at the explicit-quadruple fallback.
    """
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "nsx-t9",
                "version": "4.2",
                "impl_id": "nsx-rest-t9",
                "requires_connector_class": "NsxConnector",
                "upstream": ("https://<nsx-mgr-fqdn>/api/v1/spec/openapi/nsx_api.yaml",),
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "nsx-t9/4.2"},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "catalog_entry_templated_upstream"
    assert detail["catalog_entry"] == "nsx-t9/4.2"
    assert detail["templated_upstream"] == [
        "https://<nsx-mgr-fqdn>/api/v1/spec/openapi/nsx_api.yaml",
    ]


def test_ingest_catalog_entry_vmware_9_0_html_portal_returns_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.15-T2 (#1211): ``catalog_entry: vmware/9.0`` whose upstream serves
    HTML returns a structured 422 ``catalog_entry_upstream_not_spec`` envelope.

    Before this Task, the Broadcom Developer Portal landing page bytes fell
    through to the YAML decoder and surfaced as ``could not decode spec:
    while scanning for the next token found character that cannot start
    any token in '<file>', line 33, column 1`` -- the HTML doctype on line
    1, opening tags around line 33. The structured envelope replaces that
    with a T11-compliant detail body the operator can act on without
    cross-referencing the byte stream.
    """
    portal_url = "https://developer.broadcom.com/xapis/vsphere-automation-api/latest/"
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "vmware-t1211",
                "version": "9.0",
                "impl_id": "vmware-rest-t1211",
                "requires_connector_class": "VmwareRestConnector",
                "upstream": (portal_url,),
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        # The Broadcom Developer Portal returns HTML with a 200, not a
        # spec-shaped media type. Mock the fetch so the test exercises
        # the content-type guard without hitting the public internet.
        mock_router.get(portal_url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=(
                    b"<!doctype html><html><head><title>Broadcom</title></head><body></body></html>"
                ),
            ),
        )
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "vmware-t1211/9.0", "async": False},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "catalog_entry_upstream_not_spec"
    assert detail["catalog_entry"] == "vmware-t1211/9.0"
    assert detail["upstream_url"] == portal_url
    assert detail["content_type"] == "text/html; charset=utf-8"
    # T11 convention: the human-readable message names the values, the
    # remediation imperative, and the doc reference.
    assert "explicit-quadruple shape" in detail["message"]
    assert "error-message-shape" in detail["message"]


def test_ingest_catalog_entry_sddc_manager_9_0_html_portal_returns_422(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0.15-T2 (#1211): ``catalog_entry: sddc-manager/9.0`` has the same
    HTML-portal trap as ``vmware/9.0`` and surfaces the same structured 422.

    The two entries are the cycle's two confirmed offenders per
    ``claude-rdc-hetzner-dc#753`` sub-signal B; pinning both shapes in
    distinct tests guards against a future refactor that splits the
    detection logic by product.
    """
    portal_url = "https://developer.broadcom.com/xapis/sddc-manager-api/latest/"
    _patch_catalog(
        monkeypatch,
        entries=[
            {
                "product": "sddc-manager-t1211",
                "version": "9.0",
                "impl_id": "sddc-rest-t1211",
                "requires_connector_class": "SddcManagerConnector",
                "upstream": (portal_url,),
            },
        ],
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        mock_router.get(portal_url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<!doctype html><html></html>",
            ),
        )
        response = client.post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "sddc-manager-t1211/9.0", "async": False},
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "catalog_entry_upstream_not_spec"
    assert detail["catalog_entry"] == "sddc-manager-t1211/9.0"
    assert detail["upstream_url"] == portal_url
    assert detail["content_type"] == "text/html; charset=utf-8"


def test_ingest_explicit_quadruple_html_upstream_returns_422_without_catalog_field(
    client: TestClient,
) -> None:
    """G0.15-T2 (#1211): an explicit-quadruple request whose spec URL serves
    HTML returns the bare ``upstream_not_spec`` envelope (no ``catalog_entry``).

    Same guard, different envelope shape -- the route layer picks
    :func:`build_upstream_not_spec_detail` vs
    :func:`build_catalog_entry_upstream_not_spec_detail` based on
    whether the request started as catalog-driven. This test pins the
    bare shape; the catalog-driven shape is covered by the two tests
    above.
    """
    portal_url = "https://example.lab/not-a-spec.html"
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        mock_router.get(portal_url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<!doctype html><html></html>",
            ),
        )
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "explicit-quad",
                "version": "1.0",
                "impl_id": "explicit-quad",
                "specs": [{"uri": portal_url}],
                "dry_run": True,
            },
            headers=_authed(token),
        )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "upstream_not_spec"
    assert "catalog_entry" not in detail
    assert detail["upstream_url"] == portal_url
    assert detail["content_type"] == "text/html"


def test_ingest_packaged_catalog_html_portal_entries_carry_warning_notes() -> None:
    """G0.15-T2 (#1211) audit: every ``spec_info_version: null`` catalog
    entry whose ``upstream`` would reach the fetch path carries the
    "HTML-portal upstream; manual ingest required" warning in ``notes``.

    Sweep over the packaged catalog confirms ``vmware/9.0`` and
    ``sddc-manager/9.0`` -- the two confirmed offenders -- both carry
    the warning mirroring the ``harbor/2.x`` Swagger-2.0 precedent. The
    other ``spec_info_version: null`` entries are excluded by earlier
    422 gates -- ``nsx/9.0`` via ``catalog_entry_templated_upstream``
    (FQDN placeholder), the three typed connectors (``vault/1.x``,
    ``k8s/1.x``, ``bind9/9.x``) via ``catalog_entry_typed_connector``
    (``upstream: null``) -- so they never reach the fetch path the
    HTML-portal guard sits on. Test exists so a future contributor
    adding a third HTML-portal-style entry without the note tripping
    catches the omission at PR-review time.
    """
    from meho_backplane.operations.ingest.catalog import load_catalog

    catalog = load_catalog()

    # An entry reaches the HTTP fetch path only if every upstream URL
    # is non-templated; entries with any FQDN-templated URL (NSX) are
    # refused earlier by ``catalog_entry_templated_upstream`` (422)
    # before ``_load_spec_bytes`` runs, so they never trigger the
    # HTML-portal guard.
    def _reaches_fetch_path(urls: tuple[str, ...]) -> bool:
        return all(("<" not in url and ">" not in url) for url in urls)

    html_portal_entries = {
        (e.product, e.version)
        for e in catalog.entries
        if e.spec_info_version is None
        and e.upstream is not None
        and _reaches_fetch_path(e.upstream)
        and any(url.startswith("https://developer.broadcom.com/") for url in e.upstream)
    }
    assert ("vmware", "9.0") in html_portal_entries
    assert ("sddc-manager", "9.0") in html_portal_entries
    for product, version in html_portal_entries:
        entry = catalog.get(product, version)
        assert entry is not None
        assert "catalog_entry_upstream_not_spec" in entry.notes, (
            f"{product}/{version} upstream points at the Broadcom Developer "
            f"Portal but notes don't reference the 422 error code -- mirror "
            "the harbor/2.x Swagger-2.0 precedent."
        )


def test_ingest_explicit_quadruple_still_works_regression(
    client: TestClient,
    tmp_path: Any,
) -> None:
    """G0.14-T9 regression guard: the historical explicit-quadruple shape
    still parses + validates after the schema gained ``catalog_entry``.

    The MCP admin tool and any pre-G0.14-T9 REST client send this
    shape; a regression here would break both. The smoke is just the
    dry-run happy path; the rest of the test file covers the deep
    ingest mechanics.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="spec-quadruple.yaml")
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test-quadruple",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": spec_url}],
                "dry_run": True,
            },
            headers=_authed(token),
        )
    assert response.status_code == 200, response.text
    assert response.json()["ingestion"]["inserted_count"] == 1


# ---------------------------------------------------------------------------
# G0.16-T1 (#1303) — async ingest must not block the request thread
# ---------------------------------------------------------------------------


_PER_OP_PADDING_LINES = "\n".join(
    f"        Resource paragraph {i}: exposes a vendor-shape OpenAPI surface the"
    " consumer dogfood reproduced; padding so the per-op YAML size lands"
    " in the representative-size band."
    for i in range(60)
)


def _build_large_openapi_fixture(spec_path: Any, *, op_count: int) -> int:
    """Write a representative-size OpenAPI fixture; return the file size in bytes.

    Generates *op_count* GET operations under
    ``/api/v{n}/resource_{i}/{id}``, each with a verbose summary, two
    parameters, a JSON-schema response body, plus a multi-paragraph
    description block (:data:`_PER_OP_PADDING_LINES`, ~5 KB) so the
    per-op serialised footprint matches the real-world
    ``vmware/9.0.0.0`` spec (~5-8 KB per op once description blocks
    land). The fixture is YAML so the parser walks the same code
    path as the live spec.

    The repro target from
    ``claude-rdc-hetzner-dc#771`` Finding 20 is **7.55 MB / 1275 ops**.
    A 1500-op fixture lands ≥ 7 MB which is representative enough for
    the non-blocking-budget assertion -- the issue body's
    "≥ 7 MB / ≥ 1000 ops" guidance applies. Live ingest of the actual
    vmware spec is reserved for the consumer-side dogfood in
    ``claude-rdc-hetzner-dc`` (Finding 20's signal directory).

    Per-op padding lives in a module-level constant rather than a
    per-op-derived string so the builder stays O(op_count) on
    operator count rather than O(op_count * padding_size) on byte
    count -- regenerating the same ~5 KB block per op would
    dominate the fixture builder's wall-clock budget for nothing.
    """
    chunks = [
        "openapi: 3.0.3\n",
        "info:\n",
        "  title: representative-size-stress-fixture\n",
        '  version: "9.0.0.0"\n',
        "  description: |\n",
        "    Stress fixture for G0.16-T1 (#1303).\n",
        "    See backend/tests/test_api_v1_connectors_ingest.py for the\n",
        "    non-blocking-budget assertion this fixture lands.\n",
        "paths:\n",
    ]
    for i in range(op_count):
        chunks.append(f"  /api/v1/resource_{i}/{{resource_id}}:\n")
        chunks.append("    get:\n")
        chunks.append(
            f"      summary: |\n"
            f"        Return the resource_{i} row identified by resource_id. The\n"
            f"        summary intentionally runs across multiple lines so the\n"
            f"        per-op YAML footprint matches the bulky real-world spec\n"
            f"        the consumer dogfood reproduces with.\n"
        )
        chunks.append("      description: |\n")
        chunks.append(
            f"        resource_{i} is a synthetic stand-in for one of the 1275\n"
            f"        typed REST operations in the vmware/9.0.0.0 OpenAPI spec.\n"
        )
        chunks.append(_PER_OP_PADDING_LINES)
        chunks.append("\n")
        chunks.append("      parameters:\n")
        chunks.append("        - name: resource_id\n")
        chunks.append("          in: path\n")
        chunks.append("          required: true\n")
        chunks.append(
            "          schema:\n"
            "            type: string\n"
            "            description: opaque resource identifier\n"
        )
        chunks.append("        - name: filter\n")
        chunks.append("          in: query\n")
        chunks.append("          required: false\n")
        chunks.append(
            "          schema:\n"
            "            type: string\n"
            "            description: optional vendor-shape filter string\n"
        )
        chunks.append("      responses:\n")
        chunks.append("        '200':\n")
        chunks.append("          description: ok\n")
        chunks.append("          content:\n")
        chunks.append("            application/json:\n")
        chunks.append("              schema:\n")
        chunks.append("                type: object\n")
        chunks.append("                properties:\n")
        chunks.append("                  resource_id: { type: string }\n")
        chunks.append("                  metadata: { type: object }\n")
        chunks.append("                  status: { type: string }\n")
        chunks.append("                  created_at: { type: string, format: date-time }\n")
    spec_path.write_text("".join(chunks))
    return spec_path.stat().st_size


@pytest.mark.asyncio
async def test_ingest_async_default_returns_202_with_job_handle(
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """G0.16-T1 (#1303): the default ``async=true`` shape returns 202 + handle.

    Acceptance criterion: ``Non-dry-run ingest of a 7.5 MB / 1275-op
    spec completes without crashing the pod (liveness probe never
    times out, restartCount does not increment, pod stays Ready)``.
    The kubelet liveness probe deadline is the asymptotic version
    of "the request returns inside a budget". The integration check
    is the consumer-side dogfood in ``claude-rdc-hetzner-dc`` --
    here we mirror the repro with a representative-size fixture
    (≥ 7 MB / ≥ 1000 ops) and assert the request completes in a
    non-blocking budget that's well under the 25-second probe
    deadline.

    The fixture is generated on the fly so the test file doesn't
    ship a 7 MB spec; ``op_count=1500`` lands around the right
    order of magnitude for the per-op count + total spec size that
    triggered the pod restart in production.

    Driven over an :class:`httpx.AsyncClient` + :class:`ASGITransport`
    rather than the sync :class:`TestClient`. The sync ``TestClient``
    runs each request inside its own short-lived anyio portal, which
    tears the background task down on the POST's return *and* drives
    the route handler under a portal that synchronously waits on the
    task before declaring the response sent -- both effects defeat
    the "the route returns immediately, the task runs off-thread"
    contract this acceptance test exists to pin. The
    :class:`ASGITransport` path is the production-shape loop:
    durable background task, response sent the moment the handler
    awaits :class:`JSONResponse.__call__`. The
    ``test_async_run_returns_handle_then_poll`` test in
    ``test_api_v1_agent_runs.py`` uses the same pattern for the same
    reason.

    The async path's design property: the route returns 202 + handle
    immediately and the heavy pipeline work runs off the request
    thread. The test asserts the route returns in well under 1
    second (a generous bound -- in practice it returns in tens of
    milliseconds because the parser only runs inside the background
    task) regardless of how long the actual pipeline takes.
    """
    import time as _time

    from httpx import ASGITransport

    from meho_backplane.main import app

    spec_path = tmp_path / "stress.yaml"
    spec_size = _build_large_openapi_fixture(spec_path, op_count=1500)
    # Sanity: the fixture lands in the representative-size regime.
    # The real-world target is 7.5 MB; we accept anything ≥ 5 MB so
    # an OpenAPI-emitter tweak that compresses the YAML still keeps
    # the test honest about "representative size".
    assert spec_size >= 5 * 1024 * 1024, (
        f"stress fixture too small ({spec_size} bytes); fail closed so a "
        "future contributor shrinking the fixture trips this assertion "
        "rather than silently weakening the budget guard"
    )
    propose_json = (
        '[{"group_key": "resources", "name": "Resources", '
        '"when_to_use": "Use these to manage synthetic stress resources."}]'
    )
    assign_json = "{}"
    set_llm_client_factory(
        lambda: _StubLlmClient(
            propose_response=propose_json,
            assign_response=assign_json,
        ),
    )
    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="stress-spec.yaml")
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
        ) as ac:
            request_start = _time.monotonic()
            response = await ac.post(
                "/api/v1/connectors/ingest",
                json={
                    "product": "stress-vmware",
                    "version": "9.0.0.0",
                    "impl_id": "stress-vmware-rest",
                    "specs": [{"uri": spec_url}],
                    # Default is ``async=true``; the assertion below
                    # is what the issue's "must not crash the pod"
                    # framing protects against. No need to set it
                    # explicitly.
                },
                headers=_authed(token),
            )
            request_duration_seconds = _time.monotonic() - request_start

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "running"
    assert "job_id" in body
    # ``poll_url`` is a relative path so a REST client can append it
    # to the same base url it POSTed to. The shape is documented in
    # docs/codebase/spec-ingestion.md.
    assert body["poll_url"] == f"/api/v1/connectors/ingest/jobs/{body['job_id']}"
    # The load-bearing assertion. The route returned long before the
    # background pipeline finishes, so the request thread is free to
    # serve liveness probes. The kubelet liveness deadline is 25 s
    # (default values for the helm chart's ``meho-backplane``
    # deployment); 3 s as a test budget is ~8x more headroom than
    # we need, ~10x faster than the legacy ~30 s blocking path that
    # tripped the probe in production. The asymmetry is for
    # SQLite contention in the test environment: the audit
    # middleware writes its row while the background task hammers
    # the same single-writer DB, so the response's middleware-side
    # tail is dominated by the test DB's connection contention.
    # In production with Postgres + pool size 10, the audit row
    # write grabs its own connection and the path returns in tens
    # of milliseconds (see docs/codebase/spec-ingestion.md
    # "Async ingest mode"). The budget gives us a clean
    # "the route did NOT serially-wait on the 30-second pipeline"
    # signal without false-positives on shared-CI machine load.
    assert request_duration_seconds < 3.0, (
        f"async ingest request blocked the event loop for "
        f"{request_duration_seconds:.2f} s; the route is supposed to "
        "fire the pipeline off the request thread and return 202 + "
        "handle inside the kubelet liveness-probe budget. See "
        "claude-rdc-hetzner-dc#771 Finding 20 for the pod-crash repro."
    )


def test_ingest_async_job_poll_returns_404_for_unknown_id(
    client: TestClient,
) -> None:
    """An unknown job_id returns 404 (mirrors the cross-tenant conflation).

    The polling endpoint reuses the
    :class:`ConnectorNotFoundError`-style 404 conflation -- an
    unknown id and a cross-tenant probe both surface as 404 so an
    operator cannot enumerate other tenants by status-code
    differential.
    """
    key, token = _admin_token()
    bogus_id = uuid.uuid4()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            f"/api/v1/connectors/ingest/jobs/{bogus_id}",
            headers=_authed(token),
        )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "ingest_job_not_found"


@pytest.mark.asyncio
async def test_ingest_async_job_poll_cross_tenant_is_404_not_403(
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """Tenant B polling tenant A's ingest job sees 404, not 403.

    Same conflation :class:`ReviewService` enforces on the read
    surfaces -- an operator must not be able to enumerate other
    tenants by status-code differential. The test seeds a job under
    tenant A (real flow: POST /ingest under tenant A's admin token)
    and then polls it under tenant B's admin token.

    Driven over :class:`ASGITransport` (not :class:`TestClient`)
    because the POST + the cross-tenant GET share a single event
    loop only that way -- see
    :func:`test_ingest_async_default_returns_202_with_job_handle`
    for the load-bearing rationale.
    """
    from httpx import ASGITransport

    from meho_backplane.main import app

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    propose_json = (
        '[{"group_key": "items", "name": "Items", "when_to_use": "Use these to manage items."}]'
    )
    assign_json = '{"GET:/items": "items"}'
    set_llm_client_factory(
        lambda: _StubLlmClient(
            propose_response=propose_json,
            assign_response=assign_json,
        ),
    )

    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()
    key_a, token_a = _admin_token(tenant_id=tenant_a_id, sub="op-admin-a", kid="kid-admin-a")
    key_b, token_b = _admin_token(tenant_id=tenant_b_id, sub="op-admin-b", kid="kid-admin-b")
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        # Both keys in one JWKS so the chassis JWT validator can
        # resolve either token's ``kid`` -- calling
        # ``_mock_discovery_and_jwks`` twice would have the second
        # call shadow the first, dropping key A from the published
        # JWKS and 401-ing the POST on the very signature it
        # produced.
        _mock_discovery_and_jwks(mock_router, _public_jwks(key_a, key_b))
        spec_url = _register_spec_at_https(mock_router, spec_path, path="spec-tenant-iso.yaml")
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://testserver",
        ) as ac:
            response = await ac.post(
                "/api/v1/connectors/ingest",
                json={
                    "product": "tenant-iso",
                    "version": "1.0",
                    "impl_id": "tenant-iso-impl",
                    "specs": [{"uri": spec_url}],
                },
                headers=_authed(token_a),
            )
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]

            cross_response = await ac.get(
                f"/api/v1/connectors/ingest/jobs/{job_id}",
                headers=_authed(token_b),
            )
    assert cross_response.status_code == 404, cross_response.text
    assert cross_response.json()["detail"] == "ingest_job_not_found"


# Silence unused-import lints on test seams reserved for sibling tasks.
_ = (AsyncMock, patch, GroupingResult, IngestionResult)
