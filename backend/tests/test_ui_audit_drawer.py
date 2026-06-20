# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit-query row detail drawer UI surface.

Initiative #1841 (G10.15 Audit-query forensic console), Task #1945 (T2).
The drawer (``GET /ui/audit/show/{audit_id}``) is the row drill-down: the
full request payload, the operation identity, the identifiers, lineage
deep-links (replay / parent), and -- the hard part -- the decision-#3
aggregate-only gate that withholds the payload for sensitive op classes.

Acceptance criteria on issue #1945 covered here:

* AC2(a): the drawer renders the full request payload for a ``read`` row.
* AC2(b): a ``credential_read`` (and an ``audit_query``) row renders the 🔒
  placeholder and no payload key in the response body.
* AC2(c): a row whose ``broadcast_detail_effective == "aggregate"`` is gated
  even if its op_class would otherwise show detail.
* AC2(d): a foreign-tenant ``audit_id`` returns 404 (not 403, not 200).
* AC2(e): a non-existent ``audit_id`` returns 404.
* AC5: the replay deep-link is TENANT_ADMIN-gated -- disabled for an
  operator-role lift, enabled (pointing at the replay surface) for a
  ``tenant_admin`` lift.

Suite shape mirrors :mod:`backend.tests.test_ui_audit_query` (the T1
session-cookie HTTP edge + the JWT-role-lift reconstruction). The module
name is ``test_ui_audit_drawer`` to stay distinct from T1's
``test_ui_audit_query`` and the pre-existing ``test_ui_audit`` (the BFF
audit-thread suite) -- the flat ``test_ui_*.py`` convention.
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
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, Tenant
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
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

#: The session's operator -- must match the seeded session's
#: ``operator_sub`` or the role lift fails the identity check.
_OPERATOR_SUB = "op-self"

_BASE = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (read-surface baseline)."""
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


# ---------------------------------------------------------------------------
# Builders / seeding
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the audit UI tests."""
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
    """Insert one ``tenant`` row so FK + target-name joins resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_audit_row(
    *,
    tenant_id: uuid.UUID,
    second: int = 0,
    op_id: str = "vsphere.vm.list",
    op_class: str = "read",
    operator_sub: str = "op-actor",
    status_code: int = 200,
    payload_extra: dict[str, Any] | None = None,
    work_ref: str | None = None,
    parent_audit_id: uuid.UUID | None = None,
    agent_session_id: uuid.UUID | None = None,
    row_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one ``audit_log`` row at ``_BASE + second``; return its id.

    ``payload_extra`` merges into the base ``{op_id, op_class}`` payload so a
    test can stamp the G6.3 ``broadcast_detail_effective`` verdict or extra
    request params.
    """
    resolved_id = row_id or uuid.uuid4()
    payload: dict[str, Any] = {"op_id": op_id, "op_class": op_class}
    if payload_extra:
        payload.update(payload_extra)

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=resolved_id,
                    occurred_at=_BASE + timedelta(seconds=second),
                    operator_sub=operator_sub,
                    tenant_id=tenant_id,
                    method="POST",
                    path="/mcp",
                    status_code=status_code,
                    duration_ms=Decimal("1.0"),
                    payload=payload,
                    work_ref=work_ref,
                    parent_audit_id=parent_audit_id,
                    agent_session_id=agent_session_id,
                )
            )
        return resolved_id

    return asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OPERATOR_SUB,
    access_token: str = "access-token-plaintext",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token*; return its UUID."""

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


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _role_session(
    role: TenantRole,
    *,
    operator_sub: str = _OPERATOR_SUB,
    tenant_id: uuid.UUID = _TENANT_A,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real JWT carrying *role*."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-audit-drawer-test-kid")
    jwks = _public_jwks(keypair)
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id, operator_sub=operator_sub, access_token=access_token
    )
    return session_id, jwks


# ===========================================================================
# Authentication boundary
# ===========================================================================


def test_drawer_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/audit/show/{id}`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(f"/ui/audit/show/{uuid.uuid4()}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ===========================================================================
# AC2(a): a read/write row renders the full request payload
# ===========================================================================


def test_drawer_renders_full_payload_for_read_row() -> None:
    """AC2(a): a ``read`` row renders the request payload, no 🔒 gate."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        op_id="vsphere.vm.list",
        op_class="read",
        payload_extra={"datacenter": "dc-1", "cluster": "prod"},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{row_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="audit-drawer"' in body
    assert "vsphere.vm.list" in body
    # The request payload section renders the non-internal params.
    assert "datacenter" in body
    assert "dc-1" in body
    # No aggregate-only gate for a plain read.
    assert "aggregate-only" not in body
    # The audit-only classification keys are stripped from the rendered
    # payload (op_id/op_class are first-class fields, not request params).
    assert '"op_class"' not in body


# ===========================================================================
# AC2(b): credential_read / audit_query rows render the 🔒 gate, no payload
# ===========================================================================


@pytest.mark.parametrize(
    ("op_id", "op_class"),
    [
        ("vault.kv.read", "credential_read"),
        ("audit.query", "audit_query"),
    ],
)
def test_drawer_gates_sensitive_op_classes(op_id: str, op_class: str) -> None:
    """AC2(b): a sensitive-class row shows 🔒 and never renders its payload."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        op_id=op_id,
        op_class=op_class,
        payload_extra={"secret_path": "kv/data/prod/db", "token": "should-never-render"},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{row_id}")

    assert response.status_code == 200, response.text
    body = response.text
    # The 🔒 aggregate-only placeholder renders.
    assert "aggregate-only" in body
    # The sensitive request payload is never rendered.
    assert "secret_path" not in body
    assert "should-never-render" not in body


# ===========================================================================
# AC2(c): a broadcast_detail_effective="aggregate" row is gated regardless
# ===========================================================================


def test_drawer_gates_row_with_aggregate_effective_override() -> None:
    """AC2(c): ``broadcast_detail_effective="aggregate"`` gates a read row.

    The G6.3 resolver's recorded verdict wins over op-class classification:
    a row that would otherwise show full detail (``read``) is withheld when
    the effective key says ``aggregate``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        op_id="vsphere.vm.list",
        op_class="read",
        payload_extra={
            "broadcast_detail_effective": "aggregate",
            "datacenter": "dc-secret",
        },
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{row_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "aggregate-only" in body
    assert "dc-secret" not in body


def test_drawer_renders_full_when_effective_full_overrides_sensitive_class() -> None:
    """A ``broadcast_detail_effective="full"`` override un-gates a sensitive op.

    The recorded verdict is authoritative in both directions: a per-tenant
    override that flipped a normally-sensitive op to full detail renders the
    payload despite the op-class membership.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        op_id="vault.kv.read",
        op_class="credential_read",
        payload_extra={
            "broadcast_detail_effective": "full",
            "mount": "kv-public",
        },
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{row_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "aggregate-only" not in body
    assert "kv-public" in body


# ===========================================================================
# AC2(d): a foreign-tenant audit_id returns 404 (not 403, not 200)
# ===========================================================================


def test_drawer_foreign_tenant_id_returns_404() -> None:
    """AC2(d): a tenant-B row id returns 404 for a tenant-A session."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    foreign_id = _seed_audit_row(
        tenant_id=_TENANT_B,
        op_id="tenant.b.secret.op",
        payload_extra={"leak": "must-not-appear"},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{foreign_id}")

    # 404 (the boundary is opaque), never 403 (would confirm the id exists)
    # and never 200 (would leak the row).
    assert response.status_code == 404, response.text
    body = response.text
    assert "Audit row not found" in body
    assert "must-not-appear" not in body
    assert "tenant.b.secret.op" not in body


# ===========================================================================
# AC2(e): a non-existent audit_id returns 404
# ===========================================================================


def test_drawer_unknown_id_returns_404() -> None:
    """AC2(e): an id that matches no row returns the 404 not-found fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    unknown = uuid.uuid4()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{unknown}")
    assert response.status_code == 404, response.text
    assert "Audit row not found" in response.text


# ===========================================================================
# Lineage: parent-row deep-link
# ===========================================================================


def test_drawer_renders_parent_row_deep_link() -> None:
    """A row carrying ``parent_audit_id`` renders a parent-row deep-link."""
    _seed_tenant(_TENANT_A, "tenant-a")
    parent_id = _seed_audit_row(tenant_id=_TENANT_A, second=0, op_id="parent.op")
    child_id = _seed_audit_row(
        tenant_id=_TENANT_A, second=1, op_id="child.op", parent_audit_id=parent_id
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{child_id}")

    assert response.status_code == 200, response.text
    assert f"/ui/audit/show/{parent_id}" in response.text


# ===========================================================================
# AC5: the replay deep-link is TENANT_ADMIN-gated
# ===========================================================================


def test_drawer_replay_link_disabled_for_operator() -> None:
    """AC5: an operator-role lift renders the replay link disabled (tooltip)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A, op_id="agent.session.op", agent_session_id=session_uuid
    )
    # Plaintext token -> soft role lift fails to operator.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{row_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "btn-disabled" in body
    assert "tenant_admin role" in body
    # No enabled deep-link to the replay surface for a plain operator.
    assert f"/ui/audit/sessions/{session_uuid}/replay" not in body


def test_drawer_replay_link_enabled_for_tenant_admin() -> None:
    """AC5: a tenant_admin lift renders the replay link enabled + deep-linked."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A, op_id="agent.session.op", agent_session_id=session_uuid
    )
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{row_id}")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert f"/ui/audit/sessions/{session_uuid}/replay" in body


# ===========================================================================
# Deep-link: the ?audit_id= page query pre-renders the drawer
# ===========================================================================


def test_page_deep_link_pre_renders_drawer() -> None:
    """``GET /ui/audit?audit_id=<id>`` opens the page with the drawer rendered."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        op_id="vsphere.vm.poweron",
        op_class="write",
        payload_extra={"vm": "web-01"},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit?audit_id={row_id}")

    assert response.status_code == 200, response.text
    body = response.text
    # Full page chrome + the pre-rendered drawer for the deep-linked row.
    assert "<title>Audit" in body
    assert 'aria-label="Audit row detail drawer"' in body
    assert "web-01" in body


def test_page_deep_link_unknown_id_renders_page_without_drawer() -> None:
    """A bad ``?audit_id=`` renders the page (200) with no open drawer."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, op_id="vsphere.vm.list")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit?audit_id={uuid.uuid4()}")

    # The page itself is not a 404 -- only the dedicated drawer fragment is.
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Audit" in body
    assert 'aria-label="Audit row detail drawer"' not in body
