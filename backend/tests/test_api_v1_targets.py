# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.targets`.

Coverage matrix (G0.3-T3 / Task #254 acceptance criteria):

* **List** — empty tenant returns []; product filter; keyset cursor.
* **Describe** — exact name; alias match (via resolve_target); 404 with
  near-misses.
* **Probe** — no connector registered → 501; target not found → 404.
* **Create** — 201 with full Target body; duplicate name → 409;
  operator role (not tenant_admin) → 403.
* **Update** — partial PATCH applies only supplied fields; operator
  role → 403.
* **Cross-tenant** — a target in tenant_a is invisible to a JWT scoped
  to tenant_b.
* **Unauthenticated** — every route returns 401 without a Bearer header.
* **Audit contextvar** — ``audit_target_id`` is bound after resolve_target
  succeeds; the AuditLog row's payload carries it on the next T4 pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.connectors.schemas import ProbeResult

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _operator_token,
    _settings_env,  # noqa: F401
)

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# GET /api/v1/targets — list
# ---------------------------------------------------------------------------


def test_list_targets_empty_for_new_tenant(client: TestClient) -> None:
    """A tenant with no targets returns an empty list."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_targets_returns_targets(client: TestClient) -> None:
    """Targets belonging to the tenant are returned as TargetSummary list."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vault",
        product="vault",
        host="10.1.0.2",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()}
    assert "rdc-vcenter" in names
    assert "rdc-vault" in names


@pytest.mark.asyncio
async def test_list_targets_product_filter(client: TestClient) -> None:
    """``?product=vsphere`` returns only vsphere targets."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="vc1", product="vsphere", host="10.1.0.1"
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="v1", product="vault", host="10.1.0.2"
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets?product=vsphere",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["product"] == "vsphere"


@pytest.mark.asyncio
async def test_list_targets_cursor_pagination(client: TestClient) -> None:
    """Cursor-based pagination returns only targets after the cursor name."""
    tenant_id = DEFAULT_TENANT_ID
    for name in ["alpha", "beta", "gamma"]:
        await _insert_target(
            tenant_id=uuid.UUID(tenant_id), name=name, product="ssh", host="10.0.0.1"
        )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets?cursor=beta",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    names = [t["name"] for t in response.json()]
    assert names == ["gamma"]


# ---------------------------------------------------------------------------
# GET /api/v1/targets/{name} — describe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_target_exact_name(client: TestClient) -> None:
    """Exact name match returns the full Target document."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vsphere",
        host="vcenter.corp.internal",
        port=443,
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/rdc-vcenter",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "rdc-vcenter"
    assert data["product"] == "vsphere"
    assert data["port"] == 443
    # Full Target must carry tenant_id
    assert "tenant_id" in data


@pytest.mark.asyncio
async def test_describe_target_alias_match(client: TestClient) -> None:
    """Alias match (element-equality) returns the same target."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
        aliases=["vcenter", "vc.corp.internal"],
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/vcenter",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    assert response.json()["name"] == "rdc-vcenter"


def test_describe_target_not_found_returns_404(client: TestClient) -> None:
    """No match returns 404 with ``error=no_target``."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/nonexistent",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "no_target"


@pytest.mark.asyncio
async def test_describe_target_not_found_includes_near_misses(client: TestClient) -> None:
    """404 detail includes near-misses when the prefix matches."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="rdc-vcenter", product="vsphere", host="10.1.0.1"
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="rdc-vault", product="vault", host="10.1.0.2"
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/rdc-v",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    near_miss_names = {m["name"] for m in detail["matches"]}
    assert "rdc-vcenter" in near_miss_names
    assert "rdc-vault" in near_miss_names


# ---------------------------------------------------------------------------
# POST /api/v1/targets/{name}/probe — probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_no_connector_returns_501(client: TestClient) -> None:
    """No connector registered for the target's product → 501."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id), name="my-nsx", product="nsx", host="10.0.0.1"
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/my-nsx/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 501
    assert "nsx" in response.json()["detail"]


def test_probe_target_not_found_returns_404(client: TestClient) -> None:
    """Probe on a non-existent target returns 404 via resolve_target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/nonexistent/probe",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_probe_ambiguous_connector_returns_409(client: TestClient) -> None:
    """Two registered impls for the same product → 409 ``ambiguous_connector``.

    G0.14-T1 (#1142) acceptance criterion: ``/probe`` and dispatch
    agree on whether a target resolves. Pre-#1142 ``/probe`` consulted
    the v1 :func:`get_connector` registry only (which can hold at most
    one impl per product, so ambiguity was *unrepresentable* there);
    after #1142 it routes through
    :func:`resolve_connector_or_label` so the v2 tie-break ladder's
    ambiguous outcome surfaces as a structured 409 with the
    resolver's exception message naming the candidate set + the
    remediation step. This is the live ``rdc-rke2-infra-k8s`` shape
    from ``claude-rdc-hetzner-dc#697`` signal 19 — pre-fix the
    operator saw bare 500; post-fix they see the resolver's
    diagnostic.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _ImplA(Connector):
        product = "kclash"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    class _ImplB(Connector):
        product = "kclash"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(product="kclash", version="", impl_id="a", cls=_ImplA)
    register_connector_v2(product="kclash", version="", impl_id="b", cls=_ImplB)

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-clash",
        product="kclash",
        host="10.0.0.7",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/rdc-clash/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    # Resolver's diagnostic text rides verbatim — names the candidates
    # and the remediation step.
    assert "preferred_impl_id" in detail
    assert "kclash" in detail


@pytest.mark.asyncio
async def test_probe_resolves_v2_only_registration(client: TestClient) -> None:
    """A v2-only connector (no v1 entry) resolves via the shared helper.

    Pre-#1142 ``/probe`` used :func:`get_connector` (v1 only), so a
    target whose product was registered solely via
    :func:`register_connector_v2` (e.g. ``vmware-rest-9.0`` — has no
    v1 :func:`register_connector` entry) got 501 from probe even
    though the dispatcher resolved it cleanly via the v2 ladder.
    This test pins the after-fix behavior: a v2-only registration
    reaches the connector's :meth:`fingerprint` through ``/probe``.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    fp = FingerprintResult(
        vendor="vmware",
        product="vmware",
        version="9.0.2",
        reachable=True,
        probed_at=datetime.now(UTC),
        probe_method="version-endpoint",
    )

    class _V2OnlyConnector(Connector):
        product = "vmware-like"
        # No supported_version_range → matches any target_version
        # including the no-fingerprint case (matches the resolver's
        # "v1-style + no range" pathway used by the test).

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            return fp

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    # v2-only registration — no register_connector("vmware-like", ...)
    # call. The pre-#1142 /probe path would 501 on this shape.
    register_connector_v2(
        product="vmware-like",
        version="9.0",
        impl_id="vmware-rest",
        cls=_V2OnlyConnector,
    )

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vmware-like",
        host="vcenter.corp.internal",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/rdc-vcenter/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "vmware"
    assert body["version"] == "9.0.2"


@pytest.mark.asyncio
async def test_probe_invokes_connector(client: TestClient) -> None:
    """When a connector is registered, probe returns the FingerprintResult.

    Post-G0.3-T1.5 (#477) the probe verb returns the connector's
    :class:`FingerprintResult` (not :class:`ProbeResult` — that change
    was the 2026-05-14 amendment to Initiative #224). Persistence
    round-tripping against the DB is covered in
    :mod:`test_targets_fingerprint`; here we only assert the wire
    contract.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    fingerprint = FingerprintResult(
        vendor="hashicorp",
        product="vault",
        version="1.15.0",
        reachable=True,
        probed_at=datetime.now(UTC),
        probe_method="sys-health",
    )

    class _FakeConnector(Connector):
        product = "vault"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            return fingerprint

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("vault", _FakeConnector)

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/prod-vault/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "hashicorp"
    assert body["product"] == "vault"
    assert body["version"] == "1.15.0"
    assert body["reachable"] is True
    assert body["probe_method"] == "sys-health"


# ---------------------------------------------------------------------------
# POST /api/v1/targets — create
# ---------------------------------------------------------------------------


def test_create_target_returns_201(client: TestClient) -> None:
    """Valid body → 201 with full Target document."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "new-host", "product": "ssh", "host": "10.0.0.5"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "new-host"
    assert data["product"] == "ssh"
    assert data["host"] == "10.0.0.5"
    assert "id" in data
    assert "tenant_id" in data
    assert "created_at" in data


def test_create_target_duplicate_returns_409(client: TestClient) -> None:
    """Creating the same target name twice returns 409 on the second call."""
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r1 = client.post(
            "/api/v1/targets",
            json={"name": "dup-host", "product": "ssh", "host": "10.0.0.1"},
            headers=headers,
        )
        r2 = client.post(
            "/api/v1/targets",
            json={"name": "dup-host", "product": "ssh", "host": "10.0.0.1"},
            headers=headers,
        )
    assert r1.status_code == 201
    assert r2.status_code == 409


def test_create_target_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role (not ``tenant_admin``) is rejected with 403."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "should-fail", "product": "ssh", "host": "10.0.0.1"},
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


def test_create_target_with_all_fields(client: TestClient) -> None:
    """Create succeeds with all optional fields populated."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-vcenter",
                "aliases": ["vcenter", "vc"],
                "product": "vsphere",
                "host": "vcenter.corp.internal",
                "port": 443,
                "fqdn": "vcenter.corp.internal",
                "secret_ref": "secret/meho/vcenter",
                "auth_model": "impersonation",
                "vpn_required": True,
                "extras": {"dc": "fra1"},
                "notes": "Production vCenter",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["aliases"] == ["vcenter", "vc"]
    assert data["port"] == 443
    assert data["auth_model"] == "impersonation"
    assert data["vpn_required"] is True
    assert data["extras"] == {"dc": "fra1"}


# ---------------------------------------------------------------------------
# PATCH /api/v1/targets/{name} — update
# ---------------------------------------------------------------------------


def test_update_target_partial_fields(client: TestClient) -> None:
    """PATCH applies only supplied fields; unset fields are unchanged."""
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Create first
        client.post(
            "/api/v1/targets",
            json={"name": "patch-target", "product": "ssh", "host": "10.0.0.1", "port": 22},
            headers=headers,
        )
        # Patch only host
        response = client.patch(
            "/api/v1/targets/patch-target",
            json={"host": "new-host.corp.internal"},
            headers=headers,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["host"] == "new-host.corp.internal"
    assert data["port"] == 22  # untouched


def test_update_target_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role cannot PATCH a target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/some-target",
            json={"host": "x"},
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_update_target_not_found_returns_404(client: TestClient) -> None:
    """PATCH on a non-existent target returns 404 via resolve_target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/nonexistent",
            json={"host": "x"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: TestClient) -> None:
    """A target in tenant_a is invisible to a JWT scoped to tenant_b."""
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="rdc-vcenter",
        product="vsphere",
        host="10.1.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets/rdc-vcenter",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_b)}"},
        )
    assert response.status_code == 404
    # Near-misses must be empty — tenant_b has no targets
    assert response.json()["detail"]["matches"] == []


@pytest.mark.asyncio
async def test_list_cross_tenant_isolation(client: TestClient) -> None:
    """List does not expose targets from another tenant."""
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="tenant-a-target",
        product="ssh",
        host="10.0.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_b)}"},
        )
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


def test_list_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/targets")
    assert response.status_code == 401


def test_describe_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/targets/any-name")
    assert response.status_code == 401


def test_probe_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/targets/any-name/probe")
    assert response.status_code == 401


def test_create_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/targets", json={"name": "x", "product": "ssh", "host": "h"})
    assert response.status_code == 401


def test_update_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.patch("/api/v1/targets/any-name", json={"host": "x"})
    assert response.status_code == 401


def test_delete_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.delete("/api/v1/targets/any-name")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/v1/targets/{name} (G0.14-T4 #1145)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_target_soft_deletes_and_returns_204(client: TestClient) -> None:
    """DELETE on a live target returns 204 and stamps ``deleted_at``.

    Subsequent GET on the same name resolves to 404 because the
    resolver filters ``deleted_at IS NULL``; the row stays in the
    DB so the ``audit_log.target_id`` soft-FK keeps pointing at it.

    Also asserts the ``AuditMiddleware`` wrote an audit row with
    ``payload['op_id'] == 'targets.delete'`` -- the
    ``audit_op_id`` contextvar bound inside the route handler is the
    only signal cross-tenant audit queries (``meho audit query
    --op-id=targets.delete``) have to find the delete events.
    """
    from sqlalchemy import select as _select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import AuditLog
    from meho_backplane.db.models import Target as TargetORM

    tenant_id = DEFAULT_TENANT_ID
    t = await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="to-delete",
        product="ssh",
        host="10.0.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            "/api/v1/targets/to-delete",
            headers={"Authorization": f"Bearer {_admin_token(key, tenant_id)}"},
        )
    assert response.status_code == 204

    # The row stays in the DB but is now soft-deleted.
    sm = get_sessionmaker()
    async with sm() as session:
        row = (await session.execute(_select(TargetORM).where(TargetORM.id == t.id))).scalar_one()
    assert row.deleted_at is not None

    # The DELETE produced an audit row with op_id=targets.delete,
    # the correct target_id soft-FK, and the operator's sub from the
    # JWT. The contextvar binding inside the route handler is what
    # surfaces the canonical op_id (otherwise the middleware would
    # write http.delete:/api/v1/targets/{name} as a fallback).
    async with sm() as session:
        audit_rows = (
            (
                await session.execute(
                    _select(AuditLog)
                    .where(AuditLog.method == "DELETE")
                    .where(AuditLog.target_id == t.id),
                )
            )
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.payload["op_id"] == "targets.delete"
    assert audit.payload["op_class"] == "write"
    assert audit.status_code == 204
    assert audit.operator_sub == "admin-1"

    # A follow-up GET resolves to 404 because the resolver filters
    # deleted_at IS NULL.
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        follow_up = client.get(
            "/api/v1/targets/to-delete",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_target_excluded_from_list(client: TestClient) -> None:
    """Soft-deleted targets are excluded from ``GET /api/v1/targets``."""
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="live-one",
        product="ssh",
        host="10.0.0.1",
    )
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="doomed",
        product="ssh",
        host="10.0.0.2",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.delete(
            "/api/v1/targets/doomed",
            headers={"Authorization": f"Bearer {_admin_token(key, tenant_id)}"},
        )
        listing = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert listing.status_code == 200
    names = {t["name"] for t in listing.json()}
    assert names == {"live-one"}


@pytest.mark.asyncio
async def test_delete_target_not_found_returns_404(client: TestClient) -> None:
    """DELETE on a non-existent target returns 404 via resolve_target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            "/api/v1/targets/no-such-name",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_target_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role cannot DELETE a target."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            "/api/v1/targets/any",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_delete_target_cross_tenant_returns_404(client: TestClient) -> None:
    """A target in tenant_a is invisible to a DELETE from tenant_b (404, not 403)."""
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="tenant-a-target",
        product="ssh",
        host="10.0.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            "/api/v1/targets/tenant-a-target",
            headers={"Authorization": f"Bearer {_admin_token(key, tenant_b)}"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_target_with_graph_node_refs_returns_409(client: TestClient) -> None:
    """When the target is referenced by graph_node rows, DELETE returns 409
    with the count and the ``?force=true`` remediation hint.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import GraphNode, Tenant

    tenant_id = DEFAULT_TENANT_ID
    tenant_uuid = uuid.UUID(tenant_id)
    # graph_node.tenant_id is a real FK to tenant.id — seed the tenant
    # row first so the FK does not block the GraphNode insert. The
    # _insert_target helper does *not* seed tenant rows (targets.tenant_id
    # is a soft-FK; chassis-era audit rows have no tenant either).
    sm = get_sessionmaker()
    async with sm() as session:
        existing_tenant = await session.get(Tenant, tenant_uuid)
        if existing_tenant is None:
            # Migration ``0028_supersede_rdc_internal_seed`` seeds a
            # tenant with ``slug='default'`` (a different random UUID).
            # Use a per-tenant_uuid slug so the test's tenant row does
            # not collide with the migration seed's UNIQUE(slug)
            # constraint.
            session.add(
                Tenant(
                    id=tenant_uuid,
                    slug=f"tenant-{tenant_uuid}",
                    name=f"tenant-{tenant_uuid}",
                ),
            )
            await session.commit()
    t = await _insert_target(
        tenant_id=tenant_uuid,
        name="has-refs",
        product="k8s",
        host="10.0.0.1",
    )
    async with sm() as session:
        session.add(
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                kind="target",
                name="cluster-a",
                target_id=t.id,
                properties={},
                discovered_by="kubernetes",
                first_seen=_datetime.now(_UTC),
            ),
        )
        await session.commit()

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            "/api/v1/targets/has-refs",
            headers={"Authorization": f"Bearer {_admin_token(key, tenant_id)}"},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["kind"] == "target_has_references"
    assert detail["graph_node_refs"] == 1
    assert "force=true" in detail["message"]


@pytest.mark.asyncio
async def test_delete_target_with_graph_node_refs_force_true_succeeds(
    client: TestClient,
) -> None:
    """``?force=true`` proceeds with the soft-delete despite graph_node refs.

    Also asserts the audit row carries ``op_id='targets.delete'`` --
    the forced-delete path is the same code path as the unforced
    happy path (the ``force`` branch only skips the 409), so the
    audit-row contract must hold both ways.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from sqlalchemy import select as _select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import AuditLog, GraphNode, Tenant
    from meho_backplane.db.models import Target as TargetORM

    tenant_id = DEFAULT_TENANT_ID
    tenant_uuid = uuid.UUID(tenant_id)
    sm = get_sessionmaker()
    async with sm() as session:
        existing_tenant = await session.get(Tenant, tenant_uuid)
        if existing_tenant is None:
            # Migration ``0028_supersede_rdc_internal_seed`` seeds a
            # tenant with ``slug='default'`` (a different random UUID).
            # Use a per-tenant_uuid slug so the test's tenant row does
            # not collide with the migration seed's UNIQUE(slug)
            # constraint.
            session.add(
                Tenant(
                    id=tenant_uuid,
                    slug=f"tenant-{tenant_uuid}",
                    name=f"tenant-{tenant_uuid}",
                ),
            )
            await session.commit()
    t = await _insert_target(
        tenant_id=tenant_uuid,
        name="forced-delete",
        product="k8s",
        host="10.0.0.1",
    )
    async with sm() as session:
        session.add(
            GraphNode(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                kind="target",
                name="cluster-b",
                target_id=t.id,
                properties={},
                discovered_by="kubernetes",
                first_seen=_datetime.now(_UTC),
            ),
        )
        await session.commit()

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(
            "/api/v1/targets/forced-delete?force=true",
            headers={"Authorization": f"Bearer {_admin_token(key, tenant_id)}"},
        )
    assert response.status_code == 204

    async with sm() as session:
        row = (await session.execute(_select(TargetORM).where(TargetORM.id == t.id))).scalar_one()
    assert row.deleted_at is not None

    # Forced delete writes the same op_id=targets.delete audit row.
    async with sm() as session:
        audit_rows = (
            (
                await session.execute(
                    _select(AuditLog)
                    .where(AuditLog.method == "DELETE")
                    .where(AuditLog.target_id == t.id),
                )
            )
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.payload["op_id"] == "targets.delete"
    assert audit.payload["op_class"] == "write"
    assert audit.status_code == 204
    assert audit.operator_sub == "admin-1"


# ---------------------------------------------------------------------------
# PATCH /api/v1/targets/{name} product update (G0.14-T4 #1145)
# ---------------------------------------------------------------------------


def test_patch_product_unknown_returns_422(client: TestClient) -> None:
    """PATCH with an unknown product yields the structured 422.

    Mirrors the convention codified in T11
    (``docs/codebase/error-message-shape.md``): a snake_case
    ``kind``, a human ``message`` naming the offending value and
    the remediation, a machine-actionable ``valid_products`` list.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult
    from meho_backplane.connectors.schemas import ProbeResult as _ProbeResult

    class _K8sConnector(Connector):
        product = "k8s"

        async def probe(self, target: Any) -> _ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("k8s", _K8sConnector)

    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Create with valid product first.
        client.post(
            "/api/v1/targets",
            json={"name": "typo-target", "product": "k8s", "host": "10.0.0.1"},
            headers=headers,
        )
        # Now try to PATCH the product to a typo.
        response = client.patch(
            "/api/v1/targets/typo-target",
            json={"product": "kubernetes"},
            headers=headers,
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "unknown_product"
    assert detail["product"] == "kubernetes"
    assert "k8s" in detail["valid_products"]


def test_patch_product_valid_succeeds(client: TestClient) -> None:
    """PATCH with a registered product updates the row and bumps ``updated_at``.

    The recovery flow signal 6 wants: misregistered ``kubernetes``
    → corrected to ``k8s`` in-place.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult
    from meho_backplane.connectors.schemas import ProbeResult as _ProbeResult

    class _K8sConnector(Connector):
        product = "k8s"

        async def probe(self, target: Any) -> _ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    class _OtherConnector(Connector):
        product = "ssh"

        async def probe(self, target: Any) -> _ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector("k8s", _K8sConnector)
    register_connector("ssh", _OtherConnector)

    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={"name": "fix-me", "product": "ssh", "host": "10.0.0.1"},
            headers=headers,
        )
        response = client.patch(
            "/api/v1/targets/fix-me",
            json={"product": "k8s"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["product"] == "k8s"


def test_patch_product_null_returns_422_not_500(client: TestClient) -> None:
    """PATCH with ``{"product": null}`` yields a structured 422, not a 500.

    ``TargetUpdate.product`` is typed ``str | None`` with
    ``default=None`` because the absent-marker for "client did not
    send this field" is the only way Pydantic can distinguish
    "client said null" from "client said nothing" in v1
    (``model_dump(exclude_unset=True)`` keys on field presence, not
    value). Without an explicit null guard in the route handler, a
    client that sends ``{"product": null}`` reaches the ``setattr``
    loop, assigns ``None`` to ``Target.product`` (NOT NULL), and the
    flush trips an IntegrityError that surfaces to the operator as
    a 500. That violates T11's error-message-shape contract --
    callers cannot branch on ``500 Internal Server Error`` the same
    way they branch on a snake_case ``kind``. This test pins the
    handler-level null guard so a future refactor that drops it
    fails closed.
    """
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={"name": "null-target", "product": "ssh", "host": "10.0.0.1"},
            headers=headers,
        )
        response = client.patch(
            "/api/v1/targets/null-target",
            json={"product": None},
            headers=headers,
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "invalid_null"
    assert detail["field"] == "product"
    assert "null" in detail["message"].lower()


def test_patch_product_same_value_passes_without_validator(client: TestClient) -> None:
    """A PATCH that re-asserts the existing product passes without registry lookup.

    Edge case: an operator scripts a PATCH that sends every field
    including ``product=<current value>``. The validator must
    short-circuit on equality so a PATCH does not break when the
    connector registry is in a transient unregistered state at
    request time (e.g. mid-rolling-restart). Only product *changes*
    are validated against the registry.
    """
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Empty registry -- no connectors registered.
        client.post(
            "/api/v1/targets",
            json={"name": "stable", "product": "legacy", "host": "10.0.0.1"},
            headers=headers,
        )
        # PATCH the host while sending product=legacy (unchanged).
        response = client.patch(
            "/api/v1/targets/stable",
            json={"product": "legacy", "host": "new.host"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["product"] == "legacy"
    assert response.json()["host"] == "new.host"
