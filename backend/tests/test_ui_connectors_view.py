# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Connectors UI surface.

Initiative #340 (G10.3 Connectors + Targets UI), Task #873 (G10.3-T1).
The acceptance criteria on issue #873 are:

* ``/ui/connectors`` lists tenant targets (name, aliases, product,
  host, auth_model, vpn icon, last_probed_at relative, status); HTMX
  column-sort + product filter re-render the table.
* ``/ui/connectors/<name>`` shows the full row + fingerprint card +
  recent-ops + the available-operations matrix grouped by
  ``operation_group`` (collapsed; expandable) with ``when_to_use``
  subtitles + per-op ``safety_level`` + ``requires_approval``.
* Re-probe (tenant_admin) POSTs ``/api/v1/targets/{name}/probe`` and
  swaps the updated fingerprint card in place; failure shows the
  probe ``reason`` in an alert.
* Recent-ops card live-updates via the SSE feed filtered to
  ``target=<name>``.
* Cross-tenant isolation: a target/op from another tenant never
  renders.
* ``ruff`` + ``mypy`` clean; ``pytest -n auto
  backend/tests/test_ui_connectors_view.py`` passes.

Suite shape:

* :func:`_build_app` constructs a minimal FastAPI app wired the
  same way :mod:`backend.tests.test_ui_topology_table._build_app`
  does (UI session + CSRF middlewares, BFF auth router, UI router).
* :func:`_seed_session_sync` writes a ``web_session`` row with a
  real Keycloak-minted access token (signed by the test JWKS) so
  the role-probe / re-probe deps can re-verify the token and pick
  up the right :class:`TenantRole`.
* :func:`_FakeConnector` is a minimal :class:`Connector` subclass
  registered via :func:`register_connector_v2` so the re-probe
  path resolves a known impl and returns a deterministic
  :class:`FingerprintResult`.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    Tenant,
)
from meho_backplane.db.models import (
    Target as TargetORM,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

# Two stable tenant ids -- shape matches the topology suite so the
# cross-tenant isolation assertion has concrete state to lean on.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

# Stable operator subs.
_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors :func:`backend.tests.test_ui_memory_list._bff_env` /
    :func:`backend.tests.test_ui_topology_table._bff_env` so the
    chassis Keycloak / Vault / DB / encryption-key baseline is
    identical across the UI test surface. Cache + global-state
    resets on both setup + teardown so a failing test cannot leak
    ``_TEMPLATES`` / session-engine state into the next case.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _empty_connector_registry() -> Iterator[None]:
    """Reset the v2 connector registry between tests.

    The detail + probe paths consult the v2 registry to pick a
    connector class. A test that registers a fake connector must not
    leak that registration into the next test.
    """
    clear_registry()
    yield
    clear_registry()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the connectors UI tests."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert one ``tenant`` row so the target FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_target(
    *,
    tenant_id: uuid.UUID,
    name: str,
    product: str = "vmware",
    host: str = "host.example.test",
    aliases: list[str] | None = None,
    fingerprint: dict[str, Any] | None = None,
    vpn_required: bool = False,
    updated_at: datetime | None = None,
) -> uuid.UUID:
    """Insert one ``targets`` row and return its id."""
    target_id = uuid.uuid4()
    now = updated_at if updated_at is not None else datetime.now(UTC)

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TargetORM(
                    id=target_id,
                    tenant_id=tenant_id,
                    name=name,
                    aliases=aliases or [],
                    product=product,
                    host=host,
                    port=None,
                    fqdn=None,
                    secret_ref=None,
                    auth_model="shared_service_account",
                    vpn_required=vpn_required,
                    extras={},
                    notes=None,
                    fingerprint=fingerprint,
                    preferred_impl_id=None,
                    created_at=now,
                    updated_at=now,
                ),
            )

    asyncio.run(_do())
    return target_id


def _seed_audit_row(
    *,
    tenant_id: uuid.UUID,
    target_id: uuid.UUID,
    method: str = "GET",
    path: str = "/api/v1/example",
    status_code: int = 200,
    occurred_at: datetime | None = None,
) -> None:
    """Insert one ``audit_log`` row associated with *target_id*."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    occurred_at=occurred_at or datetime.now(UTC),
                    operator_sub="op-1",
                    method=method,
                    path=path,
                    status_code=status_code,
                    request_id=uuid.uuid4(),
                    duration_ms=Decimal("12.50"),
                    payload={},
                    tenant_id=tenant_id,
                    target_id=target_id,
                ),
            )

    asyncio.run(_do())


def _seed_group_and_op(
    *,
    tenant_id: uuid.UUID | None,
    product: str,
    version: str,
    impl_id: str,
    group_key: str,
    group_name: str,
    when_to_use: str,
    op_id: str,
    summary: str = "test op",
    safety_level: str = "safe",
    requires_approval: bool = False,
    review_status: str = "enabled",
    is_enabled: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one ``operation_group`` row + one ``endpoint_descriptor`` row."""
    group_id = uuid.uuid4()
    desc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    group_key=group_key,
                    name=group_name,
                    when_to_use=when_to_use,
                    review_status=review_status,
                ),
            )
            session.add(
                EndpointDescriptor(
                    id=desc_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    op_id=op_id,
                    source_kind="typed",
                    summary=summary,
                    group_id=group_id,
                    safety_level=safety_level,
                    requires_approval=requires_approval,
                    is_enabled=is_enabled,
                ),
            )

    asyncio.run(_do())
    return group_id, desc_id


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str = "unused",
    operator_sub: str = _OP_OPERATOR,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-connectors-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set (no JWKS mock)."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _authenticated_client_with_role_jwks(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes.

    The role probe + ``resolve_operator_or_403`` deps re-validate the
    BFF session's access token through the JWT chain; the chain needs
    the JWKS endpoint mocked for the test to resolve cleanly. The
    caller enters ``mock`` as a context manager.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id,
        access_token=access_token,
        operator_sub=operator_sub,
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Fake connector for re-probe path resolution
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    """Deterministic :class:`Connector` subclass for the re-probe tests.

    Returns a stable :class:`FingerprintResult` so assertions can check
    that the re-probe persisted the expected shape. Registered via
    :func:`register_connector_v2` in the tests that exercise the probe
    path; the autouse ``_empty_connector_registry`` fixture clears
    every registration between tests.

    ``supported_version_range = None`` makes the connector advertise
    "any version" so the resolver's specificity ladder picks it for
    every target carrying ``product == 'fakeprod'`` regardless of the
    target's cached fingerprint shape -- crucial for the re-probe
    happy-path test that exercises the never-probed branch (the
    target has no fingerprint yet; the resolver must still match).
    """

    product = "fakeprod"
    version = "1.0"
    impl_id = "fakeprod"
    supported_version_range = None

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        return FingerprintResult(
            vendor="FakeVendor",
            product="fakeprod",
            version="1.0",
            build="fake-build-42",
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="test",
        )

    async def probe(self, target: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def execute(  # pragma: no cover - unused
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> Any:
        raise NotImplementedError


class _NoConnectorMatchTarget:  # pragma: no cover - never instantiated
    """Sentinel for the no_connector branch -- we register no class."""


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/connectors`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/connectors")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/connectors/<name>`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/connectors/some-target")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# List view -- full page + HTMX fragment + empty state
# ---------------------------------------------------------------------------


def test_list_full_page_renders_seeded_targets() -> None:
    """``GET /ui/connectors`` with a session renders the full page + rows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="vmware-prod", product="vmware")
    _seed_target(tenant_id=_TENANT_A, name="vmware-dev", product="vmware")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors")
    assert response.status_code == 200, response.text
    body = response.text
    # G0.15-T10 #1218 -- page title uses the backend taxonomy "Targets"
    # so the noun on the page matches the row's underlying table; the
    # sidebar label stays "Connectors" for operator-facing parity with
    # the URL path (Option B in the issue body).
    assert "<title>Targets" in body
    assert "vmware-prod" in body
    assert "vmware-dev" in body
    # Sidebar link to /ui/connectors carries the active highlight.
    assert 'href="/ui/connectors"' in body
    # Product filter dropdown contains the seeded product.
    assert "All products" in body
    assert ">vmware<" in body
    # CSRF cookie set by the route.
    assert CSRF_COOKIE_NAME in response.cookies


def test_list_handles_empty_inventory() -> None:
    """An empty tenant renders the "no targets" empty-state row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors")
    assert response.status_code == 200, response.text
    assert "No targets match the current filter." in response.text


def test_list_htmx_request_returns_fragment_only() -> None:
    """``HX-Request: true`` returns the ``_table_rows.html`` partial only."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="fragment-target", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors", headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    body = response.text
    # Fragment starts with <tbody> and has no <html>/<body> chrome.
    assert "<tbody" in body
    assert "<html" not in body.lower()
    assert "<title>" not in body.lower()
    assert "fragment-target" in body


# ---------------------------------------------------------------------------
# List view -- sort
# ---------------------------------------------------------------------------


def test_list_sort_by_name_ascending_is_default() -> None:
    """Default sort is name ascending -- ``apple`` before ``banana``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="banana", product="p")
    _seed_target(tenant_id=_TENANT_A, name="apple", product="p")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors")
    body = response.text
    assert body.index("apple") < body.index("banana"), (
        "expected ascending name sort to place 'apple' before 'banana'"
    )


def test_list_sort_by_name_descending_inverts_order() -> None:
    """``dir=desc`` flips the order -- ``banana`` before ``apple``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="apple", product="p")
    _seed_target(tenant_id=_TENANT_A, name="banana", product="p")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors?sort=name&dir=desc")
    body = response.text
    assert body.index("banana") < body.index("apple")


def test_list_sort_by_unknown_column_returns_422() -> None:
    """An out-of-enum ``sort`` value 422s at the HTTP boundary."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors?sort=bogus")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# List view -- product filter
# ---------------------------------------------------------------------------


def test_list_filter_by_product_narrows_results() -> None:
    """``?product=ssh`` returns only ``ssh`` rows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="ssh-keep", product="ssh")
    _seed_target(tenant_id=_TENANT_A, name="vmware-hide", product="vmware")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors?product=ssh")
    body = response.text
    assert "ssh-keep" in body
    assert "vmware-hide" not in body


# ---------------------------------------------------------------------------
# List view -- cross-tenant isolation
# ---------------------------------------------------------------------------


def test_list_isolates_other_tenants_targets() -> None:
    """Tenant A's session never sees tenant B's targets in the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(tenant_id=_TENANT_A, name="tenant-a-only", product="ssh")
    _seed_target(tenant_id=_TENANT_B, name="tenant-b-secret", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors")
    body = response.text
    assert "tenant-a-only" in body
    assert "tenant-b-secret" not in body


# ---------------------------------------------------------------------------
# Detail view -- properties + recent ops + ops matrix
# ---------------------------------------------------------------------------


def test_detail_renders_target_properties_and_aliases() -> None:
    """The detail page surfaces the full target row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="vmware-prod",
        product="vmware",
        host="vcenter.example.test",
        aliases=["vc-prod", "vsphere-prod"],
        vpn_required=True,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/vmware-prod")
    assert response.status_code == 200, response.text
    body = response.text
    assert "vmware-prod" in body
    assert "vcenter.example.test" in body
    assert "vc-prod" in body
    assert "vsphere-prod" in body
    # VPN icon (lock) renders.
    assert "&#x1F512;" in body or "\U0001f512" in body
    # Breadcrumb back to the list.
    assert 'href="/ui/connectors"' in body


def test_detail_resolves_target_by_alias() -> None:
    """Looking up the target by an alias resolves the same row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="canonical-name",
        product="ssh",
        aliases=["alias-1"],
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/alias-1")
    assert response.status_code == 200, response.text
    assert "canonical-name" in response.text


def test_detail_returns_404_for_unknown_target() -> None:
    """An unknown target name returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/nonexistent")
    assert response.status_code == 404


def test_detail_isolates_other_tenants_target() -> None:
    """A target belonging to tenant B is invisible to tenant A's session."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(tenant_id=_TENANT_B, name="tenant-b-target", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/tenant-b-target")
    # Cross-tenant target name surfaces as 404 with the resolver's
    # ``no_target`` shape, never as a successful 200 rendering the
    # other tenant's row contents (host, fingerprint, ops, ...).
    assert response.status_code == 404
    # Negative: the rendered body must NOT contain the target's host
    # or product specifics from tenant B -- the only echo of the
    # name is the resolver's diagnostic detail (``"query": "tenant-b-target"``),
    # which is acceptable (the operator typed the name; the resolver
    # is allowed to echo it back).
    assert "host.example.test" not in response.text


def test_detail_renders_recent_ops_for_target() -> None:
    """The detail page surfaces the last 10 audit_log rows for the target."""
    _seed_tenant(_TENANT_A, "tenant-a")
    target_id = _seed_target(tenant_id=_TENANT_A, name="audited-target", product="ssh")
    _seed_audit_row(
        tenant_id=_TENANT_A,
        target_id=target_id,
        method="POST",
        path="/api/v1/operations/call",
        status_code=200,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/audited-target")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Recent operations" in body
    # The recent-ops card seeds its Alpine state via a JSON island; the
    # audit row's method/path land in the seed payload.
    assert "POST" in body
    assert "/api/v1/operations/call" in body


def test_detail_loads_recent_ops_controller_before_alpine() -> None:
    """The recent-ops controller script precedes the Alpine bundle (#1692).

    Both tags are ``defer``red, and deferred scripts execute in
    document order -- the only ordering that lets the controller's
    ``alpine:init`` listener register before the Alpine CDN bundle
    auto-starts (it fires ``alpine:init`` from a microtask at the end
    of its own script task, before any later deferred script runs). A
    regression here makes Alpine process
    ``x-data="connectorsRecentOps(...)"`` with the component
    unregistered: the recent-ops card renders dead, with no seeded
    rows and no live SSE updates.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="ordered-target", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/ordered-target")
    assert response.status_code == 200, response.text
    body = response.text
    controller_pos = body.index("/ui/static/src/app/connectors-feed.js")
    alpine_pos = body.index("/ui/static/src/vendor/alpine.min.js")
    assert controller_pos < alpine_pos


def test_detail_isolates_recent_ops_from_other_tenants() -> None:
    """Audit rows under another tenant's target id never seed into the card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    target_id = _seed_target(tenant_id=_TENANT_A, name="shared-name", product="ssh")
    # An audit row carrying a target_id from tenant B but bound to
    # tenant_id=tenant_B should never surface on tenant A's detail page
    # even though tenant A has its own target with the same name.
    _seed_audit_row(
        tenant_id=_TENANT_B,
        target_id=target_id,  # same id, wrong tenant on the audit row
        method="DELETE",
        path="/api/v1/secret-action",
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/shared-name")
    assert response.status_code == 200, response.text
    assert "/api/v1/secret-action" not in response.text


def test_detail_renders_ops_matrix_when_connector_resolves() -> None:
    """A target whose fingerprint resolves to a connector shows the ops matrix."""
    _seed_tenant(_TENANT_A, "tenant-a")
    register_connector_v2(
        product="fakeprod",
        version="1.0",
        impl_id="fakeprod",
        cls=_FakeConnector,
    )
    _seed_target(
        tenant_id=_TENANT_A,
        name="fp-target",
        product="fakeprod",
        fingerprint={"version": "1.0"},
    )
    _seed_group_and_op(
        tenant_id=None,  # global / built-in
        product="fakeprod",
        version="1.0",
        impl_id="fakeprod",
        group_key="vm-lifecycle",
        group_name="VM lifecycle",
        when_to_use="When you need to start, stop, or query VM state.",
        op_id="fakeprod.vm.start",
        summary="Start a VM",
        safety_level="dangerous",
        requires_approval=True,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/fp-target")
    assert response.status_code == 200, response.text
    body = response.text
    # Group name surfaces.
    assert "VM lifecycle" in body
    # when_to_use subtitle surfaces.
    assert "When you need to start" in body
    # The per-op row surfaces op_id + safety_level badge + approval flag.
    assert "fakeprod.vm.start" in body
    assert "dangerous" in body
    assert "approval" in body
    # Resolved connector_id rendered.
    assert "fakeprod-1.0" in body


def test_detail_renders_ambiguous_connector_alert_when_resolver_returns_ambiguous() -> None:
    """The matrix slot shows an alert when two impls tie on tie-break."""
    _seed_tenant(_TENANT_A, "tenant-a")

    # Two connectors registered for the same (product, version) with no
    # tie-breaker -- the resolver returns ambiguous_connector.
    class _FakeAlt(Connector):
        product = "fakeprod"
        version = "1.0"
        impl_id = "fakeprod-alt"
        supported_version_range = None

        async def fingerprint(
            self, target: Any, operator: Any = None
        ) -> FingerprintResult:  # pragma: no cover
            raise NotImplementedError

        async def probe(self, target: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        async def execute(  # pragma: no cover
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="fakeprod", version="1.0", impl_id="fakeprod", cls=_FakeConnector)
    register_connector_v2(product="fakeprod", version="1.0", impl_id="fakeprod-alt", cls=_FakeAlt)
    _seed_target(
        tenant_id=_TENANT_A,
        name="ambig",
        product="fakeprod",
        fingerprint={"version": "1.0"},
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/ambig")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Ambiguous connector resolution" in body


def test_detail_renders_no_connector_alert_when_no_match() -> None:
    """The matrix slot shows the no-connector alert when registry is empty."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="no-fp",
        product="unknown-product",
        fingerprint={"version": "1.0"},
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/no-fp")
    assert response.status_code == 200, response.text
    body = response.text
    assert "No matching connector" in body


def test_detail_no_connector_product_mismatch_surfaces_edit_delete_hint() -> None:
    """G0.15-T10 #1218 -- the ``product_mismatch`` branch surfaces the
    Edit / Delete remediation + the valid-products enum.

    Target's product slug (``unknown-product``) doesn't match any
    registered connector, so re-probing would re-dispatch through the
    same resolver with the same tuple and fail the same way. The
    remediation message must name Edit / Delete (not Re-probe) and
    surface the registered-product enum so the operator knows what
    values are acceptable for a PATCH.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # Register one connector so ``registered_product_tokens()`` is
    # non-empty; ``unknown-product`` is intentionally NOT in the set.
    register_connector_v2(product="fakeprod", version="1.0", impl_id="fakeprod", cls=_FakeConnector)
    _seed_target(
        tenant_id=_TENANT_A,
        name="mis-registered",
        product="unknown-product",
        fingerprint={"version": "1.0"},
    )
    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/mis-registered")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "No matching connector" in body
    # The product_mismatch hint names the Edit + Delete remediation; the
    # Re-probe verb must not be the only call to action in this branch.
    assert 'data-no-connector-cause="product_mismatch"' in body
    # Apostrophe may render escaped (``&#39;``) or raw depending on the
    # Jinja autoescape settings -- accept either.
    assert (
        "doesn&#39;t match any registered connector" in body
        or "doesn't match any registered connector" in body
    )
    assert "Edit" in body
    assert "Delete" in body
    # Valid-products enum is surfaced so the operator knows what to PATCH to.
    assert "Valid products:" in body
    assert "<code>fakeprod</code>" in body


def test_detail_no_connector_missing_fingerprint_surfaces_reprobe_hint() -> None:
    """G0.15-T10 #1218 -- the ``missing_fingerprint`` branch keeps the
    Re-probe remediation (and does NOT surface Edit / Delete copy).

    Target's product slug IS in the registered set, but the only
    matching connector advertises a versioned ``supported_version_range``;
    without ``fingerprint.version`` the resolver returns ``no_connector``
    on the versioned-match ladder. Re-probe is the correct verb here.
    """
    _seed_tenant(_TENANT_A, "tenant-a")

    class _VersionedFake(Connector):
        product = "fakeprod"
        version = "1.0"
        impl_id = "fakeprod"
        # Versioned advertisement -- requires a target version (fingerprint)
        # before the resolver can match. Without ``fingerprint.version``,
        # the resolver returns ``no_connector`` -- the missing_fingerprint
        # case.
        supported_version_range = ">=1.0"

        async def fingerprint(
            self, target: Any, operator: Any = None
        ) -> FingerprintResult:  # pragma: no cover
            raise NotImplementedError

        async def probe(self, target: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        async def execute(  # pragma: no cover
            self, target: Any, op_id: str, params: dict[str, Any]
        ) -> Any:
            raise NotImplementedError

    register_connector_v2(product="fakeprod", version="1.0", impl_id="fakeprod", cls=_VersionedFake)
    _seed_target(tenant_id=_TENANT_A, name="never-probed", product="fakeprod")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/never-probed")
    assert response.status_code == 200, response.text
    body = response.text
    assert "No matching connector" in body
    # missing_fingerprint cause -- Re-probe is the right verb, no Edit /
    # Delete remediation copy + no valid-products enum.
    assert 'data-no-connector-cause="missing_fingerprint"' in body
    assert "Re-probe" in body
    assert "doesn&#39;t match any registered connector" not in body
    assert "doesn't match any registered connector" not in body
    assert "Valid products:" not in body


# ---------------------------------------------------------------------------
# Detail view -- Delete button visibility (G0.15-T10 #1218)
# ---------------------------------------------------------------------------


def test_detail_renders_delete_button_for_tenant_admin() -> None:
    """G0.15-T10 #1218 -- the Delete button is visible to tenant_admin.

    Same RBAC gate as the Edit + Re-probe buttons: the server-side
    authority is :func:`resolve_operator_or_403` on the POST handler;
    the template hide is the UX affordance. Asserts the aria-label
    naming the target so a renderer typo can't silently break the
    affordance.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="del-me", product="ssh")
    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/del-me")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # The aria-label names the target -- present only on tenant_admin
    # renders. Also assert the HTMX hx-get points at the delete modal
    # route so a re-binding to a stale URL would fail visibly.
    assert 'aria-label="Delete del-me"' in body
    assert 'hx-get="/ui/connectors/del-me/delete"' in body


def test_detail_hides_delete_button_for_operator_role() -> None:
    """G0.15-T10 #1218 -- an operator (not tenant_admin) does NOT see Delete."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="op-no-del", product="ssh")
    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/op-no-del")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    # Same aria-label discipline as the re-probe button gate test.
    assert "Delete op-no-del" not in response.text


# ---------------------------------------------------------------------------
# Detail view -- fingerprint card + status + re-probe button visibility
# ---------------------------------------------------------------------------


def test_detail_fingerprint_card_renders_when_fingerprint_present() -> None:
    """The fingerprint card surfaces the cached fingerprint fields."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(
        tenant_id=_TENANT_A,
        name="fp-present",
        product="fakeprod",
        fingerprint={
            "version": "1.2.3",
            "build": "build-001",
            "vendor": "FakeVendor",
            "product": "fakeprod",
            "reachable": True,
            "probed_at": "2026-05-25T10:00:00+00:00",
            "probe_method": "test",
            "extras": {},
        },
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/fp-present")
    body = response.text
    assert "Fingerprint" in body
    assert "1.2.3" in body
    assert "build-001" in body


def test_detail_fingerprint_card_shows_never_state_when_no_fingerprint() -> None:
    """A target with no fingerprint yet shows the explanatory empty state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="no-fp-yet", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/no-fp-yet")
    body = response.text
    assert "No fingerprint captured yet" in body


def test_detail_hides_reprobe_button_for_operator_role() -> None:
    """An operator (not tenant_admin) does NOT see the re-probe button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="op-view", product="ssh")
    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/connectors/op-view")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    # The button carries an aria-label naming the target -- present on
    # tenant_admin renders, absent on operator renders. Assert on the
    # specific aria-label so the assertion fails if the gate breaks
    # silently.
    assert "Re-probe op-view" not in response.text


def test_detail_shows_reprobe_button_for_tenant_admin_role() -> None:
    """A tenant_admin sees the re-probe button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="admin-view", product="ssh")
    client, mock, _csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/admin-view")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Re-probe admin-view" in body
    # The button uses ``hx-post`` to the probe endpoint.
    assert 'hx-post="/ui/connectors/admin-view/probe"' in body


# ---------------------------------------------------------------------------
# Detail view -- SSE wiring to broadcast bridge with target filter
# ---------------------------------------------------------------------------


def test_detail_wires_sse_to_broadcast_stream_with_target_filter() -> None:
    """The recent-ops card carries an ``sse-connect`` URL filtered to the target."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="sse-target", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/sse-target")
    body = response.text
    assert 'sse-connect="/ui/broadcast/stream?target=sse-target"' in body
    assert 'sse-swap="broadcast"' in body


def test_detail_sse_url_percent_encodes_target_name() -> None:
    """A target name carrying reserved URL characters round-trips encoded."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="tricky name & co", product="ssh")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors/tricky%20name%20%26%20co")
    assert response.status_code == 200, response.text
    body = response.text
    # The bare "&" inside the name would break the SSE URL's query
    # parsing if interpolated verbatim. The percent-encoded form
    # survives. ``quote(safe='')`` encodes spaces as ``%20``.
    assert "target=tricky%20name%20%26%20co" in body


# ---------------------------------------------------------------------------
# Re-probe -- tenant_admin gate + happy path
# ---------------------------------------------------------------------------


def test_reprobe_succeeds_for_tenant_admin_and_swaps_fingerprint_card() -> None:
    """A tenant_admin re-probe persists the fingerprint and returns the card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    register_connector_v2(
        product="fakeprod",
        version="1.0",
        impl_id="fakeprod",
        cls=_FakeConnector,
    )
    _seed_target(
        tenant_id=_TENANT_A,
        name="reprobe-me",
        product="fakeprod",
        # No prior fingerprint so the resolver doesn't filter on a
        # stale version that the connector doesn't advertise. The
        # re-probe path's resolver consults
        # ``target.fingerprint.version`` to filter candidates; a stale
        # 0.5 against ``supported_version_range=">=1.0,<2.0"`` would
        # legitimately resolve to ``no_connector``. ``None`` lets the
        # specificity ladder pick the only registered class.
        fingerprint=None,
    )
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/reprobe-me/probe",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # The refreshed fingerprint card swaps in -- the fake connector
    # returned version 1.0.
    assert 'id="fingerprint-card"' in body
    assert "FakeVendor" in body
    assert "1.0" in body
    assert "fake-build-42" in body


def test_reprobe_rejects_operator_role_with_403() -> None:
    """An operator (non-admin) re-probe POST is rejected with 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    register_connector_v2(product="fakeprod", version="1.0", impl_id="fakeprod", cls=_FakeConnector)
    _seed_target(tenant_id=_TENANT_A, name="op-cant-probe", product="fakeprod")
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.post(
            "/ui/connectors/op-cant-probe/probe",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_reprobe_returns_alert_when_no_matching_connector() -> None:
    """The no_connector branch returns a 501 + alert fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # No connectors registered for this product.
    _seed_target(tenant_id=_TENANT_A, name="no-impl", product="unknown")
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/no-impl/probe",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 501, response.text
    body = response.text
    assert "No matching connector" in body


def test_reprobe_returns_404_for_unknown_target() -> None:
    """A re-probe POST against an unknown target returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.post(
            "/ui/connectors/nonexistent/probe",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# URL contract -- sort hrefs preserve product filter
# ---------------------------------------------------------------------------


def test_list_sort_hrefs_preserve_active_product_filter() -> None:
    """Sort-column hrefs carry ``?product=`` back into the next URL."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_target(tenant_id=_TENANT_A, name="t1", product="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/connectors?product=vmware")
    body = response.text
    # The header link to swap the sort direction must carry the filter
    # so a sort click doesn't drop ``?product=``.
    assert "product=vmware" in body
