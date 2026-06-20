# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit-query session-replay tree UI surface.

Initiative #1841 (G10.15 Audit-query forensic console), Task #1946 (T3).
The replay tree (``GET /ui/audit/sessions/{session_id}/replay``) is the
chronological parent/child lineage of one agent session, rendered as a tree,
with a hard fallback to a flat query when the session is over the row cap.

Acceptance criteria on issue #1946 covered here:

* AC1(a): a ``tenant_admin`` session renders the parent/child tree for a
  session with lineage (nodes carry ``depth``, children nest).
* AC1(b): an ``operator`` (and ``read_only``) session gets the 403 forbidden
  fragment ("session replay is a tenant-admin forensic action"), not the tree.
* AC1(c): a session with ``count > 10000`` renders the over-cap fallback
  notice with a link to ``/ui/audit?agent_session_id=<session_id>`` and does
  **not** call ``replay_session`` (the count-first guard short-circuits).
* AC1(d): a foreign / unknown ``session_id`` renders the empty state with no
  404 (``root=[]``).
* AC4: a ``credential_read`` node in the replayed tree, when its T2 drawer is
  opened, renders the 🔒 placeholder (the aggregate-only gate is honoured
  per-node).

Suite shape mirrors :mod:`backend.tests.test_ui_audit_drawer` (the T2
session-cookie HTTP edge + the JWT-role-lift reconstruction). The module name
is ``test_ui_audit_replay`` to stay distinct from T1's
``test_ui_audit_query`` / T2's ``test_ui_audit_drawer`` -- the flat
``test_ui_*.py`` convention.
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
    """Insert one ``audit_log`` row at ``_BASE + second``; return its id."""
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
        keypair = _make_rsa_keypair("ui-audit-replay-test-kid")
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


def _admin_replay() -> tuple[uuid.UUID, dict[str, Any], respx.MockRouter]:
    """Seed a tenant_admin session + return a started respx mock for the JWKS.

    Returns the seeded session id, the JWKS, and the *started* respx mock the
    caller stops in a ``finally``. The replay route lifts the admin role by
    re-verifying the JWT against the mocked discovery + JWKS.
    """
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    return session_id, jwks, mock


# ===========================================================================
# Authentication boundary
# ===========================================================================


def test_replay_unauthenticated_redirects_to_login() -> None:
    """``GET .../replay`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(f"/ui/audit/sessions/{uuid.uuid4()}/replay")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ===========================================================================
# AC1(a): a tenant_admin session renders the parent/child tree
# ===========================================================================


def test_replay_renders_tree_for_tenant_admin() -> None:
    """AC1(a): an admin lift renders the nested parent/child tree."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    root_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        second=0,
        op_id="agent.session.start",
        agent_session_id=session_uuid,
    )
    child_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        second=1,
        op_id="vsphere.vm.list",
        agent_session_id=session_uuid,
        parent_audit_id=root_id,
    )

    session_id, _jwks, mock = _admin_replay()
    try:
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/sessions/{session_uuid}/replay")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Full page chrome + the replay tree, not the forbidden / over-cap states.
    assert "<title>Session replay" in body
    assert 'role="tree"' in body
    assert "agent.session.start" in body
    assert "vsphere.vm.list" in body
    # The child node opens the T2 drawer on "details" click (per-node gate).
    assert f"/ui/audit/show/{child_id}" in body
    assert f"/ui/audit/show/{root_id}" in body
    # Not the forbidden / over-cap fallbacks.
    assert "tenant-admin forensic action" not in body
    assert "Session too large" not in body


# ===========================================================================
# AC1(b): operator / read_only sessions get the 403 forbidden fragment
# ===========================================================================


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
def test_replay_forbidden_for_non_admin_roles(role: TenantRole) -> None:
    """AC1(b): operator / read_only get the 403 fragment, not the tree."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    _seed_audit_row(tenant_id=_TENANT_A, op_id="agent.session.op", agent_session_id=session_uuid)

    session_id, jwks = _role_session(role)
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/sessions/{session_uuid}/replay")
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    body = response.text
    assert "tenant-admin forensic action" in body
    # The 403 never renders the tree or any audit row content.
    assert 'role="tree"' not in body
    assert "agent.session.op" not in body


def test_replay_forbidden_on_failed_role_lift() -> None:
    """AC1(b): a failed/soft role lift (plaintext token) gets the 403 fragment.

    The session's access token is the default plaintext placeholder, so the
    JWT re-verify in ``_resolve_role`` fails soft -> treated as a plain
    operator -> the route 403s rather than 5xx-ing.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    _seed_audit_row(tenant_id=_TENANT_A, op_id="agent.session.op", agent_session_id=session_uuid)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/sessions/{session_uuid}/replay")

    assert response.status_code == 403, response.text
    assert "tenant-admin forensic action" in response.text


# ===========================================================================
# AC1(c): count > 10000 renders the over-cap fallback; replay_session skipped
# ===========================================================================


def test_replay_over_cap_falls_back_to_flat_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1(c): an over-cap count renders the fallback + flat-query link.

    The count-first guard short-circuits: ``replay_session`` is NEVER called
    (the recursive build is skipped before it runs), and the over-cap notice
    deep-links to the T1 flat query pre-filtered by ``agent_session_id``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()

    # Force the count over the backend cap without seeding 10k rows, and spy on
    # replay_session to prove the count-first guard short-circuits.
    from meho_backplane.ui.routes.audit import routes as audit_routes

    over_cap_count: int = audit_routes._REPLAY_ROW_CAP + 1

    async def _fake_count(*_args: Any, **_kwargs: Any) -> int:
        return over_cap_count

    replay_called = False

    async def _spy_replay(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal replay_called
        replay_called = True
        return []

    monkeypatch.setattr(audit_routes, "_count_session_rows", _fake_count)
    monkeypatch.setattr(audit_routes, "replay_session", _spy_replay)

    session_id, _jwks, mock = _admin_replay()
    try:
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/sessions/{session_uuid}/replay")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The over-cap notice renders, not the tree.
    assert "Session too large" in body
    assert str(over_cap_count) in body
    # The one-click pivot to the T1 flat query, pre-bound to this session.
    assert f"/ui/audit?agent_session_id={session_uuid}" in body
    assert 'role="tree"' not in body
    # The count-first guard short-circuited: the recursive build never ran.
    assert replay_called is False


def test_replay_over_cap_uses_backend_cap_constant() -> None:
    """AC3: the over-cap threshold is single-sourced from the backend cap.

    The UI route references the backend ``_REPLAY_ROW_CAP`` constant rather
    than a duplicate literal that could drift; a count exactly at the cap is
    NOT over-cap (the guard is strict ``>``), one above it is.
    """
    from meho_backplane.api.v1 import audit as rest_audit
    from meho_backplane.ui.routes.audit import routes as audit_routes

    # The UI route imports the same object, not a copy.
    assert audit_routes._REPLAY_ROW_CAP is rest_audit._REPLAY_ROW_CAP


# ===========================================================================
# AC1(d): a foreign / unknown session renders the empty state, no 404
# ===========================================================================


def test_replay_unknown_session_renders_empty_state_no_404() -> None:
    """AC1(d): an unknown session id renders the empty state, never a 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    unknown = uuid.uuid4()

    session_id, _jwks, mock = _admin_replay()
    try:
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/sessions/{unknown}/replay")
    finally:
        mock.stop()

    # The empty session renders a 200 empty state, NOT a 404 (non-leakage).
    assert response.status_code == 200, response.text
    body = response.text
    assert "No rows recorded for this session" in body
    assert 'role="tree"' not in body


def test_replay_foreign_session_renders_empty_state_no_404() -> None:
    """AC1(d): a tenant-B session id is empty (no leak) for a tenant-A admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    foreign_session = uuid.uuid4()
    # A real tenant-B row carrying the session id -- it must NOT surface for a
    # tenant-A admin (the tenant boundary is orthogonal to the admin gate).
    _seed_audit_row(
        tenant_id=_TENANT_B,
        op_id="tenant.b.secret.op",
        agent_session_id=foreign_session,
        payload_extra={"leak": "must-not-appear"},
    )

    session_id, _jwks, mock = _admin_replay()
    try:
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/sessions/{foreign_session}/replay")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "No rows recorded for this session" in body
    # The foreign row never leaks into the tenant-A admin's replay.
    assert "tenant.b.secret.op" not in body
    assert "must-not-appear" not in body


# ===========================================================================
# AC4: a credential_read tree node opens the T2 drawer with the 🔒 gate
# ===========================================================================


def test_replay_credential_node_drawer_renders_lock_placeholder() -> None:
    """AC4: a ``credential_read`` node's drawer renders 🔒, never the payload.

    The tree node deep-links to the T2 drawer (``/ui/audit/show/{id}``); when
    that drawer is opened the per-node aggregate-only gate withholds the
    sensitive payload -- the same gate the flat query honours.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    cred_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        op_id="vault.kv.read",
        op_class="credential_read",
        agent_session_id=session_uuid,
        payload_extra={"secret_path": "kv/data/prod/db", "token": "should-never-render"},
    )

    session_id, _jwks, mock = _admin_replay()
    try:
        client = _authenticated_client(session_id)
        # The tree references the node's drawer deep-link...
        tree = client.get(f"/ui/audit/sessions/{session_uuid}/replay")
        assert tree.status_code == 200, tree.text
        assert f"/ui/audit/show/{cred_id}" in tree.text
        # ...and opening that drawer honours the aggregate-only gate.
        drawer = client.get(f"/ui/audit/show/{cred_id}")
    finally:
        mock.stop()

    assert drawer.status_code == 200, drawer.text
    body = drawer.text
    assert "aggregate-only" in body
    assert "secret_path" not in body
    assert "should-never-render" not in body


# ===========================================================================
# Route distinctness (AC5): the sessions prefix never collides with T2
# ===========================================================================


def test_replay_route_does_not_shadow_drawer_or_my_recent() -> None:
    """AC5: ``/ui/audit/sessions/.../replay`` is distinct from T2 routes.

    The literal ``my-recent`` and ``show/{audit_id}`` drawer routes still
    resolve to their own handlers -- the ``sessions`` prefix does not capture
    them. ``my-recent`` returns its quick-view fragment (200), and the drawer
    returns its 404 not-found fragment for an unknown id (not the replay
    forbidden / empty fragment).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        my_recent = client.get("/ui/audit/my-recent")
        drawer = client.get(f"/ui/audit/show/{uuid.uuid4()}")

    assert my_recent.status_code == 200, my_recent.text
    assert "My recent activity" in my_recent.text
    assert drawer.status_code == 404, drawer.text
    assert "Audit row not found" in drawer.text
