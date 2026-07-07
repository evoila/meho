# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.operations`.

Coverage matrix (G0.6-T8 / Task #399):

* ``GET /api/v1/operations/groups`` returns the same payload shape as the
  meta-tool; unauthenticated -> 401.
* ``GET /api/v1/operations/search`` rejects ``limit > 50`` at the
  Pydantic Query layer.
* ``POST /api/v1/operations/call`` (and ``/preview``) return the dispatcher's
  OperationResult envelope on the response body; **every target-failure
  mode** — absent / empty / empty-object / wrong JSON type / unresolvable
  name (string or dict shape) — rides that envelope (200 +
  ``extras.error_code`` ∈ {``target_required``, ``target_invalid_type``,
  ``no_target``}), invariant across typed connectors (#136 + #2110).
* ``GET /api/v1/operations/{descriptor_id}`` is gated on
  ``tenant_admin``; an OPERATOR-role token returns 403. A descriptor
  that doesn't exist returns 404.

The route tests use the shared OIDC helpers (``_oidc_jwt_helpers``) to
mint Bearer tokens through the real ``verify_jwt_and_bind`` chain --
same shape ``test_api_v1_targets`` does. The chassis settings
(KEYCLOAK_*, VAULT_*) come from the per-file ``_settings_env`` fixture.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER


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
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _empty_connector_registry() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    def _fake() -> AsyncMock:
        return service

    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        _fake,
    )
    return service


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(operations_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id)


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key,
        sub="admin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=tenant_id,
    )


async def _seed_group(
    *,
    product: str = "vault",
    version: str = "1.x",
    impl_id: str = "vault",
    group_key: str = "kv",
    name: str = "KV",
    when_to_use: str = "use this.",
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    group_id = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key=group_key,
                name=name,
                when_to_use=when_to_use,
                review_status="enabled",
            )
        )
    return group_id


async def _seed_descriptor(
    *,
    op_id: str,
    summary: str = "Read.",
    description: str = "reads a secret.",
    tenant_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    llm_instructions: dict[str, Any] | None = None,
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    descriptor_id = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=descriptor_id,
                tenant_id=tenant_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=op_id,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_api_v1_operations._noop_handler",
                summary=summary,
                description=description,
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return descriptor_id


async def _noop_handler(
    operator: Any,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in handler used in dispatcher round-trips from the route tests."""
    return {"echo": params}


# ---------------------------------------------------------------------------
# GET /api/v1/operations/groups
# ---------------------------------------------------------------------------


def test_get_groups_requires_authentication(client: TestClient) -> None:
    """No Bearer header -> 401."""
    response = client.get("/api/v1/operations/groups?connector_id=vault-1.x")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_groups_returns_meta_tool_payload(
    client: TestClient,
    stub_embedding_service: AsyncMock,
) -> None:
    """The route returns the same shape :func:`list_operation_groups` produces."""
    await _seed_group(group_key="kv", name="KV v2", when_to_use="use for kv.")

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=vault-1.x",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["connector_id"] == "vault-1.x"
    assert any(g["group_key"] == "kv" for g in body["groups"])


def test_get_groups_unknown_connector_returns_404(client: TestClient) -> None:
    """G0.8-T5: an unknown connector_id is a 404 (was a misleading 200 []).

    The empty-200 conflated "unknown connector" with "known connector,
    no enabled groups" — a real dogfood evaluator concluded the catalog
    was empty when 40 descriptors existed; the id was just mis-shaped.
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=ghost-9.9",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "ghost-9.9" in detail
    assert "<impl_id>-<version>" in detail


def test_get_groups_bare_product_name_returns_404(client: TestClient) -> None:
    """AC: a bare product slug (`vault`) names no connector -> 404, not 200 []."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=vault",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404
    assert "vault" in response.json()["detail"]


class _GhostRestConnector(Connector):
    """v2-registered class whose ``connector_id`` round-trips losslessly.

    ``ghost-rest-9.0`` → ``(product="ghost", version="9.0",
    impl_id="ghost-rest")``. No DB rows seeded → State-0.5
    registered-but-not-ingested (#1482).
    """

    product = "ghost"
    version = "9.0"
    impl_id = "ghost-rest"

    async def fingerprint(self, target, operator=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def probe(self, target):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def execute(self, target, op_id, params):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_get_groups_registered_not_ingested_returns_typed_404(client: TestClient) -> None:
    """#1482: a registered-but-0-row connector → 404 with a typed detail.

    The detail is a structured object carrying
    ``reason="connector_not_ingested"`` and the ``meho connector ingest …``
    next_step hint — distinct from the plain-string detail an *unknown*
    connector_id returns, so the two 404s stay distinguishable on REST.
    """
    register_connector_v2(
        product="ghost",
        version="9.0",
        impl_id="ghost-rest",
        cls=_GhostRestConnector,
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=ghost-rest-9.0",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason"] == "connector_not_ingested"
    assert detail["connector_id"] == "ghost-rest-9.0"
    assert detail["next_step"] is not None
    assert "ingest" in detail["next_step"]["verb"]


@pytest.mark.asyncio
async def test_get_groups_known_connector_zero_enabled_returns_empty_200(
    client: TestClient,
) -> None:
    """Regression guard: a KNOWN connector with no *enabled* groups still
    returns 200 [] — the "meaningful empty" case must be preserved."""
    # A staged (not enabled) group makes the connector known-as-data
    # while leaving zero enabled groups for the operator to see.
    await _seed_group(group_key="staged")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        # Flip the seeded group to a non-enabled review status so the
        # connector exists but exposes no enabled groups.
        from sqlalchemy import update

        await s.execute(
            update(OperationGroup)
            .where(OperationGroup.group_key == "staged")
            .values(review_status="staged")
        )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=vault-1.x",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    # G0.18-T5 #1358 — `next_cursor: null` is the documented "end of
    # listing" sentinel under keyset pagination on `group_key`.
    assert response.json() == {
        "connector_id": "vault-1.x",
        "groups": [],
        "next_cursor": None,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/operations/search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_search_returns_hits(
    client: TestClient,
    stub_embedding_service: AsyncMock,
) -> None:
    """A query against a seeded descriptor returns a ranked hit."""
    await _seed_descriptor(op_id="vault.kv.read", summary="Read a secret.")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&query=read",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["hits"]) == 1
    assert body["hits"][0]["op_id"] == "vault.kv.read"
    assert "query_duration_ms" in body


@pytest.mark.asyncio
async def test_get_search_canonical_q_returns_hits(
    client: TestClient,
    stub_embedding_service: AsyncMock,
) -> None:
    """The canonical ``q`` free-text param (#1854) drives the search like ``query``."""
    await _seed_descriptor(op_id="vault.kv.read", summary="Read a secret.")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&q=read",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["hits"][0]["op_id"] == "vault.kv.read"


def test_get_search_missing_query_returns_422(client: TestClient) -> None:
    """Neither ``q`` nor the deprecated ``query`` supplied -> typed 422 (#1854)."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 422
    assert "missing_query" in response.json()["detail"]


def test_get_search_q_and_query_conflict_returns_422(client: TestClient) -> None:
    """``q`` and the deprecated ``query`` disagreeing is a 422, not a silent pick (#1854)."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&q=read&query=write",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 422
    assert "ambiguous_free_text_filter" in response.json()["detail"]


def test_get_search_unknown_connector_returns_404(client: TestClient) -> None:
    """AC: /search behaves identically to /groups for an unknown connector."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=ghost-9.9&query=read",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404
    assert "ghost-9.9" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_search_known_connector_no_match_returns_empty_200(
    client: TestClient,
    stub_embedding_service: AsyncMock,
) -> None:
    """AC: a KNOWN connector with no matching ops returns 200 with [] hits
    (the known-empty case, distinct from unknown→404)."""
    # Connector is known-as-data (a seeded group) but has no descriptors,
    # so the query matches nothing.
    await _seed_group(group_key="kv")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&query=nonexistent",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["hits"] == []


def test_get_search_rejects_limit_over_50(client: TestClient) -> None:
    """``limit=51`` violates the Pydantic Query ``le=50`` constraint -> 422."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&query=read&limit=51",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/operations/call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["call", "preview"])
@pytest.mark.parametrize(
    ("connector_id", "op_id"),
    [
        ("vault-1.x", "vault.kv.read"),
        ("bind9-ssh-9.x", "bind9.zone.list"),
        ("gcloud-rest-1.0", "gcloud.about"),
    ],
    ids=["vault", "bind9", "gcloud"],
)
@pytest.mark.parametrize(
    ("target_value", "expected_code"),
    [
        ("", "target_required"),
        ({}, "target_required"),
        (12345, "target_invalid_type"),
        ("nonexistent-target", "no_target"),
        ({"name": "nonexistent-target"}, "no_target"),
    ],
    ids=[
        "empty_string",
        "empty_object",
        "wrong_json_type",
        "nonexistent_string",
        "nonexistent_dict",
    ],
)
def test_target_failure_rides_envelope(
    client: TestClient,
    surface: str,
    connector_id: str,
    op_id: str,
    target_value: object,
    expected_code: str,
) -> None:
    """#136 + #2110: every supplied-target failure mode on ``/call`` +
    ``/preview`` is a 200 dispatcher envelope with a switch-able
    ``extras.error_code`` — not a 400 (empty/empty-object), 422 (wrong JSON
    type), or 404 (unresolvable name). The sixth mode — ``target`` absent —
    is covered by
    ``test_post_call_absent_target_rides_target_required_envelope``.

    A consumer error-handler is a single switch on ``extras.error_code``:
    no HTTP-code branching, no ``detail``-shape parsing. ``/call`` and
    ``/preview`` behave identically, and the behavior is invariant across
    typed connectors (the #2110 cross-connector matrix: target handling
    lives in the shared meta-tool seam, ahead of any connector dispatch).
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/operations/{surface}",
            json={
                "connector_id": connector_id,
                "op_id": op_id,
                "target": target_value,
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["extras"]["error_code"] == expected_code
    if expected_code == "no_target":
        # Information-equivalent to the old 404 body: near-miss candidates ride
        # the envelope so the consumer keeps them without a 404 to parse.
        assert isinstance(body["extras"]["matches"], list)


@pytest.mark.parametrize("surface", ["call", "preview"])
@pytest.mark.parametrize(
    ("target_value", "expected_type"),
    [
        (12345, "integer"),
        (12.5, "number"),
        (True, "boolean"),
        ([{"name": "x"}], "array"),
    ],
    ids=["integer", "number", "boolean", "array"],
)
def test_target_wrong_json_type_rides_target_invalid_type_envelope(
    client: TestClient,
    surface: str,
    target_value: object,
    expected_type: str,
) -> None:
    """#2110 Option A: a wrong-JSON-typed ``target`` no longer 422s — it rides
    the envelope as ``target_invalid_type``, with the offending JSON-type name
    in ``extras.received_type`` so an agent can name what it sent. The body
    models keep the documented ``string | object | null`` OpenAPI schema
    (codegen unchanged) while the runtime accepts any JSON value.
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/operations/{surface}",
            json={
                "connector_id": "vault-1.x",
                "op_id": "vault.kv.read",
                "target": target_value,
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["error"].startswith("target_invalid_type:")
    assert body["extras"]["error_code"] == "target_invalid_type"
    assert body["extras"]["received_type"] == expected_type


@pytest.mark.parametrize("surface", ["call", "preview"])
def test_openapi_description_documents_uniform_target_envelope(surface: str) -> None:
    """#2110 AC: the published OpenAPI description matches the implemented
    behavior — it names every target-failure ``error_code`` (including the
    new ``target_invalid_type``), states the HTTP-200 envelope contract, and
    no longer claims a target-shaped 422 boundary.
    """
    app = _build_app()
    spec = app.openapi()
    raw = spec["paths"][f"/api/v1/operations/{surface}"]["post"]["description"]
    # Docstrings hard-wrap at the line width; collapse whitespace so the
    # assertions match phrases across line breaks.
    description = " ".join(raw.split())
    for code in ("target_required", "target_invalid_type", "no_target", "ambiguous_target"):
        assert code in description
    assert "HTTP 200" in description
    assert "422" not in description


@pytest.mark.asyncio
async def test_post_call_absent_target_rides_target_required_envelope(
    client: TestClient,
) -> None:
    """#136 mode 1 (unchanged): a target-requiring op invoked with ``target`` absent
    returns the ``target_required`` envelope through the route (200), not a 4xx.

    Seeds a self-first (connector-bound) typed op — ``target=None`` short-circuits
    to ``target_required`` at connector resolution (the dispatcher path proven by
    ``test_dispatch_self_first_handler_no_target_returns_target_required``); this
    asserts the route surfaces it as the envelope, consistent with the other
    resolution failures.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,  # global row — found for any operator's tenant
                product="ghost",
                version="9.0",
                impl_id="ghost-rest",
                op_id="ghost.self.first",
                source_kind="typed",
                method=None,
                path=None,
                # self-first handler: first param is ``self`` → requires a target.
                handler_ref="tests.test_api_v1_operations._GhostRestConnector.execute",
                summary="Self-first op.",
                description="Requires a target to bind its connector instance.",
                group_id=None,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            json={
                "connector_id": "ghost-rest-9.0",
                "op_id": "ghost.self.first",
                "target": None,
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["extras"]["error_code"] == "target_required"


def test_post_call_unknown_op_returns_200_with_error_envelope(client: TestClient) -> None:
    """The dispatcher's structured-error envelope rides on a 200 body."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            json={
                "connector_id": "vault-1.x",
                "op_id": "vault.does.not.exist",
                "target": None,
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error"].startswith("unknown_op:")


@pytest.mark.asyncio
async def test_post_call_accepts_bare_string_target(
    client: TestClient, stub_embedding_service: AsyncMock
) -> None:
    """G0.13-T2 #1132: ``target: "<name>"`` passes Pydantic body validation.

    The REST body's :class:`CallOperationBody` was widened from
    ``dict | None`` to ``str | dict | None``. A bare-string target must
    not surface as 422 (Pydantic body rejection). With a real target
    row seeded under the operator's tenant, the bare-string form
    reaches the dispatcher and returns the same structured-error
    envelope the dict form would (``unknown_op`` for the fake op_id).
    Both shapes round-trip to the same dispatch -- the acceptance
    criterion for this task.
    """
    from meho_backplane.db.models import Target as TargetORM

    sessionmaker = get_sessionmaker()
    target_id = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                name="rdc-vault",
                aliases=[],
                product="vault",
                host="vault.example.com",
                port=8200,
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

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            json={
                "connector_id": "vault-1.x",
                "op_id": "vault.does.not.exist",
                "target": "rdc-vault",
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    # 200 with a structured-error envelope; the body layer did not 422
    # and the resolver found the seeded target by name.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["extras"]["error_code"] == "unknown_op"


# ---------------------------------------------------------------------------
# GET /api/v1/operations/{descriptor_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_descriptor_returns_full_row_for_admin(
    client: TestClient,
) -> None:
    """An admin token sees the full descriptor incl. ``llm_instructions``."""
    descriptor_id = await _seed_descriptor(
        op_id="vault.kv.read",
        llm_instructions={"when_to_call": "use after search."},
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/{descriptor_id}",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["op_id"] == "vault.kv.read"
    assert body["llm_instructions"] == {"when_to_call": "use after search."}


def test_get_descriptor_requires_admin_role(client: TestClient) -> None:
    """An operator-role token gets 403 on the descriptor inspection route."""
    descriptor_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/{descriptor_id}",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403


def test_get_descriptor_unknown_id_returns_404(client: TestClient) -> None:
    """A descriptor id that doesn't exist returns 404."""
    descriptor_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/{descriptor_id}",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 404
