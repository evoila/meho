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
from meho_backplane.settings import get_settings

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

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    class _ImplB(Connector):
        product = "kclash"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
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
        product = "vmwarelike"
        # No supported_version_range → matches any target_version
        # including the no-fingerprint case (matches the resolver's
        # "v1-style + no range" pathway used by the test).

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            return fp

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    # v2-only registration — no register_connector("vmwarelike", ...)
    # call. The pre-#1142 /probe path would 501 on this shape. The triple
    # is aligned (``vmwarelike-rest-9.0`` parses back to ``vmwarelike``) so
    # it satisfies the #1816 registration round-trip hard-fail.
    register_connector_v2(
        product="vmwarelike",
        version="9.0",
        impl_id="vmwarelike-rest",
        cls=_V2OnlyConnector,
    )

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rdc-vcenter",
        product="vmwarelike",
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

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
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


@pytest.mark.asyncio
async def test_probe_forwards_operator_jwt_with_three_part_token(
    client: TestClient,
) -> None:
    """G0.16-T4 (#1306) probe-vs-dispatch convergence regression.

    The v0.8.0 dogfood cycle surfaced a ``vault OIDC malformed jwt: must
    have three parts`` error on probe for four connectors
    (``k8s-1.x``, ``vmware-rest-9.0``, ``sddc-rest-9.0``,
    ``nsx-rest-4.2``). Dispatch worked on the same targets; the bug was
    the probe route synthesised a system operator carrying a placeholder
    ``raw_jwt`` (``"system:connector-probe-placeholder-jwt"``) that is
    not a real compact-JWS token, and Vault's JWT/OIDC auth method
    rejected it before the credential read could land.

    The fix routes the **route operator** through the connector's
    ``fingerprint`` method on the REST probe route (the same shape the
    dispatch path uses), so the connector's Vault credentials loader
    sees the same real JWT both surfaces use.

    This test pins the contract:

    1. The route operator's JWT is forwarded to the connector's
       ``fingerprint`` method.
    2. The forwarded JWT has the compact-JWS shape (≥3 dot-separated
       parts) — the literal sanity check the issue body specifies as
       acceptance criterion 4 (".. the regression test asserts the
       probe path's outbound token has ≥3 dot-separated parts (compact-
       JWS sanity check)").
    3. The forwarded operator is **not** the system-operator
       placeholder (whose ``raw_jwt`` lacks dots and would be rejected
       by Vault).

    A future regression that drops the ``operator`` kwarg on the route
    side, or removes the ``operator`` parameter from a connector's
    ``fingerprint`` signature, fails this test directly. The four
    affected connectors stay protected by their own typed unit suites.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    captured: dict[str, Any] = {}

    class _OperatorCapturingConnector(Connector):
        product = "opcaptureprobe"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(
            self,
            target: Any,
            operator: Any = None,
        ) -> FingerprintResult:  # type: ignore[override]
            captured["operator"] = operator
            return FingerprintResult(
                vendor="test",
                product="opcaptureprobe",
                version="1.0",
                reachable=True,
                probed_at=datetime.now(UTC),
                probe_method="captured",
            )

        async def execute(
            self,
            target: Any,
            op_id: str,
            params: dict[str, Any],
        ) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product="opcaptureprobe",
        version="1.0",
        impl_id="opcaptureprobe",
        cls=_OperatorCapturingConnector,
    )

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rke2-infra-k8s",
        product="opcaptureprobe",
        host="capture.test",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/rke2-infra-k8s/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 200

    # The route operator was forwarded — not None, not the
    # synthesised system operator with the placeholder JWT.
    fwd_operator = captured["operator"]
    assert fwd_operator is not None, (
        "the route must forward the operator to connector.fingerprint; "
        "received None means the probe route is back to the pre-#1306 "
        "shape that synthesised a system operator with a placeholder JWT"
    )

    # Acceptance criterion 4 (literal): the outbound token has ≥3
    # dot-separated parts. A real compact-JWS has exactly three; the
    # pre-fix placeholder ``"system:connector-probe-placeholder-jwt"``
    # has zero dots and trips this assertion.
    jwt_parts = fwd_operator.raw_jwt.split(".")
    assert len(jwt_parts) >= 3, (
        "forwarded operator.raw_jwt does not look like a compact-JWS "
        f"(got {len(jwt_parts)} dot-separated parts; expected ≥3). "
        "This is the literal v0.8.0 ``malformed jwt: must have three "
        "parts`` failure mode — Vault would reject this token before "
        "the per-target credential read could land."
    )

    # And the forwarded operator is decidedly NOT the system-operator
    # placeholder, whose sub is the greppable sentinel.
    assert fwd_operator.sub != "system:connector-probe", (
        "the route forwarded the system-operator stand-in instead of "
        "the request operator — this is the exact regression #1306 "
        "fixes"
    )


@pytest.mark.asyncio
async def test_probe_fingerprint_exception_returns_structured_500(
    client: TestClient,
) -> None:
    """Connector ``fingerprint`` raises → structured 500, not bare 500.

    G0.15-T1 (#1210) acceptance criterion. Replays sub-signal A from
    ``claude-rdc-hetzner-dc#753``: ``POST /api/v1/targets/<resolvable
    target>/probe`` against a target whose connector resolves cleanly
    but whose ``fingerprint(target)`` raises (credential load fails,
    target unreachable, k8s API timing, etc.) used to surface as
    FastAPI's bare ``text/plain`` ``Internal Server Error``. The fix
    catches the exception around ``connector.fingerprint(...)`` and
    raises a ``HTTPException(500)`` carrying the T11 three-clause
    envelope: ``error`` code, the failing ``connector_id`` +
    ``target_name``, the underlying ``exception_class`` /
    ``exception_message``, and a ``docs`` reference back to the
    convention.

    The shape mirrors the dispatcher's ``connector_error`` envelope so
    probe + dispatch agree on what a connector failure looks like —
    the symmetry G0.14-T1 #1142 promised for the unresolvable case,
    now extended to the resolvable-but-failing case.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _FailingConnector(Connector):
        product = "k8sfail"
        version = "1.x"
        impl_id = "k8sfail"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise RuntimeError("kubeconfig credential load failed: secret/meho/k8s not found")

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product="k8sfail",
        version="1.x",
        impl_id="k8sfail",
        cls=_FailingConnector,
    )

    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="rke2-infra-k8s",
        product="k8sfail",
        host="10.10.0.1",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/rke2-infra-k8s/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 500
    # JSON content-type pins that the bare-500 fall-through to FastAPI's
    # ``text/plain`` default handler is closed — the structured envelope
    # is what an operator sees.
    assert response.headers["content-type"].startswith("application/json")
    detail = response.json()["detail"]
    # T11 convention compliance — stable ``error`` code, the failing
    # connector named, the target named, the exception class +
    # capped message, and a doc reference. Each assertion pins one
    # clause of the convention.
    assert detail["error"] == "fingerprint_failed"
    assert detail["connector_id"] == "k8sfail-1.x"
    assert detail["target_name"] == "rke2-infra-k8s"
    assert detail["exception_class"] == "RuntimeError"
    assert "kubeconfig credential load failed" in detail["exception_message"]
    assert detail["docs"] == "docs/codebase/error-message-shape.md"


@pytest.mark.asyncio
async def test_probe_fingerprint_exception_caps_message_length(
    client: TestClient,
) -> None:
    """A 1KB+ exception message is truncated in the 500 detail.

    Pins the leak-cap discipline: a misbehaving connector that stuffs a
    credential into a stringified exception cannot leak it through the
    operator-facing response body unbounded. The full message still
    lands in the structured log (via ``_log.exception``) where the
    operator with cluster access can read it; the response detail
    carries the capped form (256 chars + truncation sentinel) the
    same way ``_errors._EXC_MESSAGE_CAP`` caps the dispatcher's
    ``connector_error`` envelope.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    huge_message = "X" * 1024

    class _NoisyConnector(Connector):
        product = "k8snoisy"
        version = "1.x"
        impl_id = "k8snoisy"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise RuntimeError(huge_message)

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product="k8snoisy",
        version="1.x",
        impl_id="k8snoisy",
        cls=_NoisyConnector,
    )
    tenant_id = DEFAULT_TENANT_ID
    await _insert_target(
        tenant_id=uuid.UUID(tenant_id),
        name="noisy-k8s",
        product="k8snoisy",
        host="10.10.0.2",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/noisy-k8s/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_id)}"},
        )
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["exception_message"].endswith("...<truncated>")
    # 256 chars of payload + the truncation sentinel. Pins the cap
    # constant rather than the literal length so an intentional bump
    # in the constant updates one place.
    from meho_backplane.api.v1.targets import _PROBE_EXC_MESSAGE_CAP

    assert len(detail["exception_message"]) == _PROBE_EXC_MESSAGE_CAP + len("...<truncated>")


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
    """Create succeeds with all optional fields populated.

    The explicit ``secret_ref`` follows the canonical per-tenant
    convention (#1723) — the pre-#2091 fixture used a stale
    ``secret/meho/*`` path, which the #2091 write-time tenant-scope gate
    now rejects.
    """
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
                "secret_ref": f"tenants/{DEFAULT_TENANT_ID}/rdc-vcenter",
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
# POST /api/v1/targets — product-enum validation (G0.14-T3 #1144)
# ---------------------------------------------------------------------------


def _register_fake_k8s_connector() -> None:
    """Register a no-op connector under ``product='k8s'`` for enum tests.

    The autouse :func:`_empty_connector_registry` fixture clears the
    registry between tests, so each enum-validation test that needs a
    known set of valid products registers its own minimal stand-in
    here. ``product='k8s'`` mirrors the real dogfood case
    (``claude-rdc-hetzner-dc#697`` signal 5 — operator typed
    ``'kubernetes'``, real registration is ``'k8s'``).
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _FakeK8sConnector(Connector):
        product = "k8s"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=_FakeK8sConnector)


def test_create_target_unknown_product_returns_422(client: TestClient) -> None:
    """POST with a product no connector advertises returns a structured 422.

    G0.14-T3 (#1144) acceptance criterion. Replays the real-world
    typo from ``claude-rdc-hetzner-dc#697`` signal 5: the operator
    posts ``product='kubernetes'`` (the friendly common name) when the
    registered connector advertises ``'k8s'``. Before this PR the
    POST succeeded and the broken row was unrecoverable (no DELETE,
    no PATCH on ``product``). After this PR the POST is rejected at
    the validation boundary with a structured detail naming the
    typo'd value + the valid set + the convention doc.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-rke2-infra",
                "product": "kubernetes",
                "host": "10.10.0.1",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    # T11 convention compliance — a stable ``kind`` code, the offending
    # product value, the valid set, and a human-readable message naming
    # the doc reference. Each assertion pins one clause of the
    # convention.
    assert detail["kind"] == "unknown_product"
    assert detail["product"] == "kubernetes"
    assert detail["valid_products"] == ["k8s"]
    assert "kubernetes" in detail["message"]
    assert "k8s" in detail["message"]
    assert "docs/codebase/error-message-shape.md" in detail["message"]


def test_create_target_unknown_product_does_not_create_row(client: TestClient) -> None:
    """A 422 from the product-enum guard does not commit the row.

    Pinning that the validator runs BEFORE ``session.add(t)`` so a
    rejected POST does not leave a tombstone the operator would
    have to recover from. Re-POSTing the same name with a valid
    product must succeed (it would 409 if the previous insert had
    landed).
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r_typo = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-rke2-infra",
                "product": "kubernetes",
                "host": "10.10.0.1",
            },
            headers=headers,
        )
        # Retry with the right product; if the typo POST had committed
        # the row, this second POST would 409.
        r_retry = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-rke2-infra",
                "product": "k8s",
                "host": "10.10.0.1",
            },
            headers=headers,
        )
    assert r_typo.status_code == 422
    assert r_retry.status_code == 201


def test_create_target_valid_product_succeeds(client: TestClient) -> None:
    """POST with a registered product token still returns 201.

    Sanity-check that the new validator does not break the happy
    path: a request whose product matches the registered set
    proceeds to the existing insert + 201 flow unchanged.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-rke2-infra",
                "product": "k8s",
                "host": "10.10.0.1",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["product"] == "k8s"


def test_create_target_empty_registry_skips_validation(client: TestClient) -> None:
    """An empty connector registry does not block POST.

    The validator skips when ``registered_product_tokens()`` is
    empty -- that state means "no connectors imported" (test
    isolation, deploy booted before eager import ran), and
    rejecting every product in that state would be the wrong
    default. This pins that the existing test suite's
    create-target paths (which use ``product='ssh'`` /
    ``product='vsphere'`` without registering anything) continue
    to work. The lifespan calls ``_eager_import_connectors`` in
    production, so the empty-registry branch is a
    test-environment-only path.
    """
    # No connector registered — autouse fixture cleared the registry.
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "no-registry-target", "product": "ssh", "host": "10.0.0.1"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201


def test_create_target_unknown_product_lists_all_valid(client: TestClient) -> None:
    """The 422 ``valid_products`` lists every registered product, sorted.

    Multiple connectors registered → the rejected POST surfaces the
    full set so the operator does not need a second round-trip to
    ``GET /api/v1/connectors`` to find the right token. Sorted
    order is the stability contract — generators that diff the
    response body across releases stay deterministic.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _NoopConnector(Connector):
        product = "placeholder"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=_NoopConnector
    )
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=_NoopConnector)
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=_NoopConnector)

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "bad-product",
                "product": "kubernetes",  # not registered
                "host": "10.0.0.1",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    # Sorted, complete, no duplicates from v1-compat empty padding.
    assert detail["valid_products"] == ["k8s", "vault", "vmware"]


# ---------------------------------------------------------------------------
# #1814 (Initiative #1810) — SDDC product realigned to the short token
#
# RDC #789 Finding 6 originally caught that ``meho connector list``
# emits ``product="sddc"`` (parser-derived, load-bearing for the #773
# connector_id round-trip) while ``POST /api/v1/targets`` validated
# against the registry's then-canonical ``"sddc-manager"`` — bridged by
# the ``"sddc" -> "sddc-manager"`` ``PRODUCT_ALIASES`` entry (G0.18-T2
# #1355). #1814 realigned :class:`SddcManagerConnector` to register
# under the short, dispatch-canonical ``"sddc"`` token directly and
# dropped the now-redundant alias, so the listing token round-trips
# without canonicalisation and ``"sddc-manager"`` is no longer a valid
# write-surface token.
#
# These tests pin the post-realignment contract: ``product="sddc"``
# creates/patches/lists cleanly storing ``"sddc"``, and the retired long
# token now 422s.
# ---------------------------------------------------------------------------


def _register_fake_sddc_connector() -> None:
    """Register a no-op connector under the short ``sddc`` triple.

    Mirrors :func:`_register_fake_k8s_connector` but for the realigned
    SDDC surface. The real :class:`SddcManagerConnector` brings in
    adapter state we don't need for an enum-validation round-trip; a
    minimal stand-in under the post-#1814 ``(product="sddc",
    version="9.0", impl_id="sddc-rest")`` triple is enough to drive the
    validator and the stored-row assertion.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _FakeSddcConnector(Connector):
        product = "sddc"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    register_connector_v2(
        product="sddc",
        version="9.0",
        impl_id="sddc-rest",
        cls=_FakeSddcConnector,
    )


def test_create_target_accepts_sddc_short_token(client: TestClient) -> None:
    """POST with ``product="sddc"`` (listing + registry token) succeeds and stores ``"sddc"``.

    Post-#1814 the SDDC connector registers under the short,
    dispatch-canonical ``"sddc"`` token — the exact token
    ``meho connector list`` / GET /api/v1/connectors emits (load-bearing
    for the #773 connector_id round-trip). An operator copying that
    token straight into a create gets a 201 and the row stores ``"sddc"``
    verbatim (no alias canonicalisation step). This is the proof the
    realignment closed the operator-facing 422 the old alias bridged.
    """
    _register_fake_sddc_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "product": "sddc",
                "name": "rdc-sddc",
                "host": "sddc.corp.internal",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["product"] == "sddc"


def test_create_target_rejects_retired_sddc_manager_long_token(client: TestClient) -> None:
    """POST with the retired ``product="sddc-manager"`` long token now 422s.

    The other half of the realignment: ``"sddc-manager"`` is no longer a
    registered product token (the connector moved to ``"sddc"`` and the
    ``"sddc" -> "sddc-manager"`` alias was dropped), so the long spelling
    fails the registered-product validator. The 422 detail echoes the
    token the operator typed.
    """
    _register_fake_sddc_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "product": "sddc-manager",
                "name": "rdc-sddc-long",
                "host": "sddc.corp.internal",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["product"] == "sddc-manager"


def test_patch_target_accepts_sddc_short_token(client: TestClient) -> None:
    """PATCH with ``product="sddc"`` stores ``"sddc"`` in the row.

    Symmetric coverage with the create path — an operator
    typo-correcting a target via PATCH to the SDDC connector uses the
    short token and the post-update row carries ``"sddc"``.
    """
    _register_fake_sddc_connector()
    # Also register k8s so the create can succeed with a different
    # product before the PATCH flips it to sddc.
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        create = client.post(
            "/api/v1/targets",
            json={
                "name": "typo-correct",
                "product": "k8s",
                "host": "10.0.0.1",
            },
            headers=headers,
        )
        assert create.status_code == 201
        patch = client.patch(
            "/api/v1/targets/typo-correct",
            json={"product": "sddc"},
            headers=headers,
        )
    assert patch.status_code == 200
    assert patch.json()["product"] == "sddc"


def test_create_target_sddc_round_trips_through_list_endpoint(client: TestClient) -> None:
    """End-to-end: POST ``product="sddc"`` → GET /api/v1/targets returns ``"sddc"``.

    The realigned round-trip end-to-end: the stored + listed token is
    the short ``"sddc"`` the operator typed, with no spelling drift
    between the write and the read surfaces.
    """
    _register_fake_sddc_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={
                "product": "sddc",
                "name": "roundtrip-sddc",
                "host": "sddc.corp.internal",
            },
            headers=headers,
        )
        response = client.get("/api/v1/targets", headers=headers)
    assert response.status_code == 200
    rows = response.json()
    by_name = {row["name"]: row for row in rows}
    assert "roundtrip-sddc" in by_name
    assert by_name["roundtrip-sddc"]["product"] == "sddc"


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
# G0.15-T6 (#1215) — operator-asserted version + preferred_impl_id validation
# ---------------------------------------------------------------------------


def test_create_target_with_version_persisted(client: TestClient) -> None:
    """POST with explicit ``version`` persists the value on the row.

    G0.15-T6 (#1215) acceptance criterion: ``POST /api/v1/targets
    {"name": "test-vc", "product": "vmware", "version": "9.0", ...}``
    → 201 with ``version: "9.0"`` in the response body.

    The replays the v0.7.0 dogfood signal-6 reproducer (RDC #753):
    operator knows the target's product version up-front (consumer
    deployed vCenter 9.0 in `rdc-hetzner-dc`) and seeds it at create
    time so the very first dispatch resolves the versioned connector
    without round-tripping through PATCH.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "test-vc",
                "product": "k8s",
                "version": "1.31.0",
                "host": "10.0.0.10",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["version"] == "1.31.0"


def test_create_target_without_version_defaults_to_null(client: TestClient) -> None:
    """Omitting ``version`` lands ``null`` on the row (the wildcard-fallback shape).

    Mirrors the dogfood-typical case: operator creates a fresh target
    without knowing the version yet; the wildcard registration applied
    to every typed connector in the same PR keeps the target
    dispatchable.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "fresh-target",
                "product": "k8s",
                "host": "10.0.0.20",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["version"] is None


def test_update_target_sets_version_from_null(client: TestClient) -> None:
    """PATCH ``{"version": "9.0"}`` updates a target's version column.

    G0.15-T6 (#1215) acceptance criterion: ``PATCH /api/v1/targets/
    rdc-vcenter {"version": "9.0"}`` → 200 with the row's ``version``
    updated from null to ``"9.0"``. Closes the dogfood foot-gun where
    a fresh target with ``version=None`` had no operator-driven path
    to set the version (TargetUpdate omitted the field).
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Create without version
        post = client.post(
            "/api/v1/targets",
            json={"name": "version-bump", "product": "k8s", "host": "10.0.0.30"},
            headers=headers,
        )
        assert post.status_code == 201
        assert post.json()["version"] is None
        # Patch version in
        response = client.patch(
            "/api/v1/targets/version-bump",
            json={"version": "1.31.0"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["version"] == "1.31.0"


def test_update_target_clears_version_to_null(client: TestClient) -> None:
    """PATCH ``{"version": null}`` returns the row to the wildcard-fallback shape.

    Clearing the operator-asserted version is the inverse of setting
    it; the column is nullable so this is a legal PATCH (not a NOT NULL
    constraint violation). Operators use this to roll back a typo or
    to let the next probe re-fingerprint cleanly.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={
                "name": "version-clear",
                "product": "k8s",
                "version": "1.31.0",
                "host": "10.0.0.40",
            },
            headers=headers,
        )
        response = client.patch(
            "/api/v1/targets/version-clear",
            json={"version": None},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["version"] is None


def test_create_target_unknown_preferred_impl_id_returns_422(
    client: TestClient,
) -> None:
    """POST with an unregistered ``preferred_impl_id`` returns a structured 422.

    G0.15-T6 (#1215) acceptance criterion: the v0.7.0 dogfood (RDC
    #753) caught operators PATCHing typo'd ``preferred_impl_id`` values
    (e.g. ``"vmware-rest"`` instead of the full ``"vmware-rest-9.0"``)
    and the resolver silently ignored them. This validator surfaces
    the foot-gun at write time with the same error-message-shape as
    ``unknown_product``.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "typo-impl",
                "product": "k8s",
                "host": "10.0.0.50",
                "preferred_impl_id": "k8s-typo",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "unknown_preferred_impl_id"
    assert detail["preferred_impl_id"] == "k8s-typo"
    assert "k8s" in detail["valid_impl_ids"]


def test_create_target_known_preferred_impl_id_succeeds(client: TestClient) -> None:
    """POST with a registered ``preferred_impl_id`` is accepted."""
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "known-impl",
                "product": "k8s",
                "host": "10.0.0.51",
                "preferred_impl_id": "k8s",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["preferred_impl_id"] == "k8s"


def test_create_target_cross_product_preferred_impl_id_rejected(
    client: TestClient,
) -> None:
    """A ``k8s`` target cannot pin a ``vmware-rest-9.0`` impl (B1 regression).

    G0.16-T6 review-iter-1 B1 (#1312). Before this fix
    :func:`_registered_impl_ids` built a global allowlist that
    accepted any impl registered for any product, so a ``k8s``
    target could pass validation with
    ``preferred_impl_id="vmware-rest-9.0"`` -- the resolver would
    then silently ignore the override at dispatch time. That is
    the exact silent-ignore foot-gun G0.15-T6 (#1215) was created
    to close.

    The fix scopes the allowlist by ``body.product``. The
    structured 422 lists only the impl_ids registered **for that
    product**, so an operator pinning the wrong-product impl
    sees the actionable set instead of the cross-product noise.
    """
    # Register two distinct product/impl pairs. The k8s connector is
    # the target's product; the vmware-rest impl is the foot-gun.
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _FakeVmwareConnector(Connector):
        product = "vmware"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    _register_fake_k8s_connector()
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=_FakeVmwareConnector
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "cross-product",
                "product": "k8s",
                "host": "10.0.0.52",
                "preferred_impl_id": "vmware-rest-9.0",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "unknown_preferred_impl_id"
    assert detail["preferred_impl_id"] == "vmware-rest-9.0"
    # The valid set lists only k8s-product impls, not vmware-rest*.
    # Both the base ``"k8s"`` and the canonical versioned ``"k8s-1.x"``
    # form are accepted (Finding C); neither vmware form appears.
    assert set(detail["valid_impl_ids"]) == {"k8s", "k8s-1.x"}
    assert "vmware-rest" not in detail["valid_impl_ids"]
    assert "vmware-rest-9.0" not in detail["valid_impl_ids"]


def test_update_target_cross_product_preferred_impl_id_rejected(
    client: TestClient,
) -> None:
    """PATCH cannot pin an impl registered for a different product (B1).

    Same scenario as the POST regression, but at the PATCH boundary
    and against the effective post-update product so a single
    request changing both ``product`` and ``preferred_impl_id`` is
    validated against the new product. Pinning the matching pair
    succeeds; pinning a cross-product impl returns the structured
    422 listing only the new product's impls.
    """
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2
    from meho_backplane.connectors.schemas import FingerprintResult, OperationResult

    class _FakeVmwareConnector(Connector):
        product = "vmware"

        async def probe(self, target: Any) -> ProbeResult:
            raise NotImplementedError

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    _register_fake_k8s_connector()
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=_FakeVmwareConnector
    )
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={"name": "patch-cross", "product": "k8s", "host": "10.0.0.53"},
            headers=headers,
        )
        response = client.patch(
            "/api/v1/targets/patch-cross",
            json={"preferred_impl_id": "vmware-rest-9.0"},
            headers=headers,
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "unknown_preferred_impl_id"
    assert detail["preferred_impl_id"] == "vmware-rest-9.0"
    assert "vmware-rest" not in detail["valid_impl_ids"]
    assert "vmware-rest-9.0" not in detail["valid_impl_ids"]


def test_update_target_unknown_preferred_impl_id_returns_422(
    client: TestClient,
) -> None:
    """PATCH with an unregistered ``preferred_impl_id`` returns a structured 422."""
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={"name": "patch-impl", "product": "k8s", "host": "10.0.0.60"},
            headers=headers,
        )
        response = client.patch(
            "/api/v1/targets/patch-impl",
            json={"preferred_impl_id": "completely-unknown"},
            headers=headers,
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "unknown_preferred_impl_id"
    assert detail["preferred_impl_id"] == "completely-unknown"


def test_update_target_clear_preferred_impl_id_succeeds(client: TestClient) -> None:
    """PATCH ``{"preferred_impl_id": null}`` clears the override -- always valid."""
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        client.post(
            "/api/v1/targets",
            json={
                "name": "clear-impl",
                "product": "k8s",
                "host": "10.0.0.61",
                "preferred_impl_id": "k8s",
            },
            headers=headers,
        )
        response = client.patch(
            "/api/v1/targets/clear-impl",
            json={"preferred_impl_id": None},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["preferred_impl_id"] is None


@pytest.mark.asyncio
async def test_list_targets_envelope_v2_returns_unified_shape(
    client: TestClient,
) -> None:
    """``?envelope=v2`` returns the §2 unified shape (G0.16-T6 Finding A #1312).

    Default (no ``?envelope=``) stays the v0.8.0 bare list — pinned
    by the existing test suite. The opt-in returns
    ``{items, next_cursor}``: items always present,
    ``next_cursor`` is ``None`` when the page exhausted the
    matching set, the last-row ``name`` otherwise. The migration
    is non-breaking by design: the opt-in is a query parameter,
    not a versioned URL.
    """
    tenant = str(uuid.uuid4())
    for name in ("a-target", "b-target", "c-target"):
        await _insert_target(
            tenant_id=uuid.UUID(tenant),
            name=name,
            product="kubernetes",
            host="10.0.0.1",
        )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        bare = client.get(
            "/api/v1/targets?limit=2",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant)}"},
        )
        v2_full = client.get(
            "/api/v1/targets?envelope=v2&limit=2",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant)}"},
        )
        v2_last = client.get(
            "/api/v1/targets?envelope=v2&limit=2&cursor=b-target",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant)}"},
        )
    # Default shape unchanged — a bare list of 2 rows.
    assert bare.status_code == 200
    assert isinstance(bare.json(), list)
    assert len(bare.json()) == 2

    # v2 envelope — items + next_cursor.
    assert v2_full.status_code == 200
    body = v2_full.json()
    assert isinstance(body, dict)
    assert [row["name"] for row in body["items"]] == ["a-target", "b-target"]
    # Page filled to limit → cursor for the next page.
    assert body["next_cursor"] == "b-target"

    # Last page → cursor is None.
    body_last = v2_last.json()
    assert body_last["items"][0]["name"] == "c-target"
    assert body_last["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_targets_envelope_v2_exact_limit_terminal_page(
    client: TestClient,
) -> None:
    """``?envelope=v2`` emits ``next_cursor=null`` on an exact-``limit`` terminal page.

    G0.16-T6 review-iter-1 M1 (#1312). The pre-fix shape
    (``next_cursor = rows[-1].name if len(rows) >= limit``) emitted a
    non-null cursor when the row count was exactly ``limit`` -- a
    false-positive that contradicted the §2 contract and forced
    callers to issue a wasted round-trip per result set whose size
    divided ``limit`` evenly. The fix over-fetches ``limit + 1`` and
    infers ``has_more`` from the extra row's existence; this test
    pins the contract that a page returning *exactly* ``limit`` rows
    with no further matches gets ``next_cursor=null``.

    Five rows in the tenant; ``limit=5``. The page returns all five
    items and the cursor is null -- no extra round-trip.
    """
    tenant = str(uuid.uuid4())
    for name in ("a-t", "b-t", "c-t", "d-t", "e-t"):
        await _insert_target(
            tenant_id=uuid.UUID(tenant),
            name=name,
            product="kubernetes",
            host="10.0.0.1",
        )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.get(
            "/api/v1/targets?envelope=v2&limit=5",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant)}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [row["name"] for row in body["items"]] == ["a-t", "b-t", "c-t", "d-t", "e-t"]
    # Exact-limit terminal page -- no more matches, cursor is null.
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_target_field_set_superset_of_detail(client: TestClient) -> None:
    """List rows surface every field the detail endpoint exposes (no masking).

    G0.16-T6 Finding D (#1312). Per
    ``docs/codebase/api-shape-conventions.md`` §5 the list endpoint
    must not silently null out fields the detail endpoint returns.
    The v0.8.0 shape masked ``version``, ``secret_ref``, and
    ``preferred_impl_id`` on list rows; this regression test pins
    the new contract.
    """
    tenant_a = str(uuid.uuid4())
    await _insert_target(
        tenant_id=uuid.UUID(tenant_a),
        name="prod-vc-1",
        product="vsphere",
        host="vcenter.corp.internal",
        version="9.0",
        secret_ref="secret/meho/vc",
        preferred_impl_id="vmware-rest",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        list_resp = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_a)}"},
        )
        detail_resp = client.get(
            "/api/v1/targets/prod-vc-1",
            headers={"Authorization": f"Bearer {_operator_token(key, tenant_a)}"},
        )
    assert list_resp.status_code == 200
    assert detail_resp.status_code == 200
    list_row = next(r for r in list_resp.json() if r["name"] == "prod-vc-1")
    detail_row = detail_resp.json()
    # The only fields a list row is allowed to omit are the
    # operator-authored free-form blobs the convention doc names.
    allowed_omissions = {"notes", "extras"}
    masked = {k for k in detail_row if k not in allowed_omissions and k not in list_row}
    assert masked == set(), f"list silently masks {masked!r} relative to detail"
    # And the load-bearing routing fields specifically carry the
    # same non-null value the detail surface returned.
    for key_name in ("version", "secret_ref", "preferred_impl_id"):
        assert list_row[key_name] == detail_row[key_name]


def test_create_target_versioned_preferred_impl_id_succeeds(client: TestClient) -> None:
    """POST with the versioned ``preferred_impl_id`` form is accepted.

    G0.16-T6 Finding C (#1312). The canonical form per
    ``docs/codebase/api-shape-conventions.md`` §3 is versioned
    (``"impl_id-version"``). Both the base form (``"k8s"``) and the
    versioned form (``"k8s-1.x"``) must be accepted; the resolver
    normalizes both to the same connector.
    """
    _register_fake_k8s_connector()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "versioned-impl",
                "product": "k8s",
                "host": "10.0.0.70",
                "preferred_impl_id": "k8s-1.x",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["preferred_impl_id"] == "k8s-1.x"


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

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
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

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    class _OtherConnector(Connector):
        product = "ssh"

        async def probe(self, target: Any) -> _ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
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


# ---------------------------------------------------------------------------
# #1723 — per-tenant Vault KV secret_ref derivation at create / update
# ---------------------------------------------------------------------------


def test_create_target_derives_per_tenant_secret_ref(client: TestClient) -> None:
    """A create with no explicit ``secret_ref`` lands on ``tenants/<T>/<name>``.

    #1723: new targets default onto the per-tenant shared path so the
    default-on #1643 guard (``secret/tenants/{tenant_id}/``, #1725) enforces
    against it — not the retired per-``sub`` layout.
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "rdc-vcenter", "product": "ssh", "host": "10.0.0.5"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/rdc-vcenter"
    # Never the retired per-``sub`` layout.
    assert "targets/" not in data["secret_ref"]


def test_create_target_honours_explicit_secret_ref(client: TestClient) -> None:
    """An explicitly-supplied ``secret_ref`` is stored verbatim, not derived.

    The explicit value differs from the derived default
    (``tenants/<T>/custom``) while staying inside the operator's tenant
    subtree — proving the handler honours the override without
    re-deriving. (The pre-#2091 fixture used an out-of-tenant
    ``tenants/some-other/...`` path, which the write-time tenant-scope
    gate now rejects; the cross-tenant reject case is pinned by the
    #2091 test cluster below.)
    """
    key = make_rsa_keypair("kid-A")
    explicit_ref = f"tenants/{DEFAULT_TENANT_ID}/shared-vcenter-cred"
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "custom",
                "product": "ssh",
                "host": "10.0.0.5",
                "secret_ref": explicit_ref,
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["secret_ref"] == explicit_ref
    # Genuinely honoured verbatim — not silently re-derived to the
    # per-name default.
    assert response.json()["secret_ref"] != f"tenants/{DEFAULT_TENANT_ID}/custom"


@pytest.mark.asyncio
async def test_update_target_homes_unconfigured_secret_ref(client: TestClient) -> None:
    """A PATCH not touching ``secret_ref`` on a null-ref row derives the per-tenant path."""
    key = make_rsa_keypair("kid-A")
    # Row created out-of-band with no secret_ref (e.g. pre-#1723).
    await _insert_target(name="legacy-target", product="ssh", host="10.0.0.9", secret_ref=None)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/legacy-target",
            json={"host": "moved.host"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["host"] == "moved.host"
    assert data["secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/legacy-target"


@pytest.mark.asyncio
async def test_update_target_preserves_existing_secret_ref(client: TestClient) -> None:
    """A PATCH not touching ``secret_ref`` leaves an already-set ref untouched."""
    key = make_rsa_keypair("kid-A")
    await _insert_target(
        name="configured", product="ssh", host="10.0.0.9", secret_ref="tenants/x/configured"
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/configured",
            json={"host": "moved.host"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["secret_ref"] == "tenants/x/configured"


@pytest.mark.asyncio
async def test_update_target_clears_secret_ref_when_explicit_null(client: TestClient) -> None:
    """An explicit ``{"secret_ref": null}`` clears the ref and is not re-derived."""
    key = make_rsa_keypair("kid-A")
    await _insert_target(
        name="to-clear", product="ssh", host="10.0.0.9", secret_ref="tenants/x/to-clear"
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/to-clear",
            json={"secret_ref": None},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["secret_ref"] is None


# ---------------------------------------------------------------------------
# #2091 — secret_ref tenant-scope fail-fast at create / update
# ---------------------------------------------------------------------------


def test_create_target_rejects_secret_ref_outside_tenant_scope(client: TestClient) -> None:
    """POST with an out-of-subtree ``secret_ref`` is a structured 422, not a 201.

    #2091: a target whose ``secret_ref`` points outside the operator's
    readable per-tenant subtree imports clean but can never dispatch
    (Vault answers an opaque ``permission denied`` at credential
    resolution). The write-time gate rejects it with a T11-compliant
    detail naming the constraint, the rendered tenant prefix, and the
    exact expected per-tenant path.
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "vcf-logs",
                "product": "ssh",
                "host": "10.0.0.5",
                # The consumer-reported shape: the path a local CLI
                # wrapper reads, outside the per-tenant subtree.
                "secret_ref": "secret/meho/vcf-logs/logmaster",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "secret_ref_outside_tenant_scope"
    assert detail["secret_ref"] == "secret/meho/vcf-logs/logmaster"
    assert detail["tenant_prefix"] == f"secret/tenants/{DEFAULT_TENANT_ID}/"
    assert detail["expected_secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/vcf-logs"
    # The message names the constraint, the convention, and the remediation.
    assert "outside the operator's readable tenant subtree" in detail["message"]
    assert "tenants/<tenant_id>/<name>" in detail["message"]
    assert "Do NOT widen the backplane's Vault policy" in detail["message"]
    assert "docs/codebase/connectors-vault-tenant-scope.md" in detail["message"]

    # Fail-fast means no row landed: the name stays free for a corrected
    # re-import.
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        lookup = client.get(
            "/api/v1/targets/vcf-logs",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert lookup.status_code == 404


def test_create_target_rejects_cross_tenant_secret_ref_segment_boundary(
    client: TestClient,
) -> None:
    """A ``tenants/<T>-evil/...`` ref does not satisfy the ``tenants/<T>`` prefix.

    Segment-boundary semantics mirror
    :func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`:
    the prefix match cannot be satisfied by a sibling namespace that
    merely *starts with* the tenant id string.
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "boundary",
                "product": "ssh",
                "host": "10.0.0.5",
                "secret_ref": f"tenants/{DEFAULT_TENANT_ID}-evil/boundary",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["kind"] == "secret_ref_outside_tenant_scope"


def test_create_target_derived_default_always_passes_gate(client: TestClient) -> None:
    """Omitting ``secret_ref`` derives the per-tenant default — never rejected.

    The #1723 happy path is untouched by the #2091 gate: the derived
    default lands inside the rendered tenant prefix by construction.
    """
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "derived-ok", "product": "ssh", "host": "10.0.0.5"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/derived-ok"


@pytest.mark.asyncio
async def test_update_target_rejects_secret_ref_outside_tenant_scope(
    client: TestClient,
) -> None:
    """PATCH sending an out-of-subtree ``secret_ref`` is a structured 422.

    The row is left untouched — the gate runs before the ``setattr``
    loop, so a rejected PATCH mutates nothing.
    """
    key = make_rsa_keypair("kid-A")
    await _insert_target(
        name="patched",
        product="ssh",
        host="10.0.0.9",
        secret_ref=f"tenants/{DEFAULT_TENANT_ID}/patched",
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/patched",
            json={"secret_ref": "secret/meho/patched", "host": "moved.host"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["kind"] == "secret_ref_outside_tenant_scope"
        assert detail["expected_secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/patched"

        # Nothing was applied — neither the ref nor the piggy-backed host.
        lookup = client.get(
            "/api/v1/targets/patched",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert lookup.status_code == 200
    assert lookup.json()["secret_ref"] == f"tenants/{DEFAULT_TENANT_ID}/patched"
    assert lookup.json()["host"] == "10.0.0.9"


@pytest.mark.asyncio
async def test_update_target_accepts_in_tenant_explicit_secret_ref(
    client: TestClient,
) -> None:
    """PATCH with an in-subtree explicit ``secret_ref`` is honoured verbatim."""
    key = make_rsa_keypair("kid-A")
    await _insert_target(name="rehomed", product="ssh", host="10.0.0.9", secret_ref=None)
    explicit_ref = f"tenants/{DEFAULT_TENANT_ID}/shared-cred"
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/rehomed",
            json={"secret_ref": explicit_ref},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["secret_ref"] == explicit_ref


def test_create_target_secret_ref_gate_noop_when_guard_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``VAULT_KV_TENANT_SCOPE_PREFIX=""`` (guard disabled) skips the gate.

    A deploy still mid-migration has no defined subtree to enforce, so an
    out-of-subtree explicit ``secret_ref`` imports exactly as before
    #2091 (and dispatch surfaces any real Vault denial via the
    ``connector_vault_forbidden`` structured error).
    """
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "unguarded",
                "product": "ssh",
                "host": "10.0.0.5",
                "secret_ref": "secret/meho/unguarded",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    assert response.json()["secret_ref"] == "secret/meho/unguarded"
