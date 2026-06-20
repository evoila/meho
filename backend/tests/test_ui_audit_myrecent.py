# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit-query my-recent quick view.

Initiative #1841 (G10.15 Audit-query forensic console), Task #1945 (T2).
The my-recent fragment (``GET /ui/audit/my-recent``) is the self-scoped
"what did I just do" shortcut: the calling operator's last-24h rows,
bound to ``principal=session.operator_sub`` so a second operator's
activity is never reachable.

Acceptance criteria on issue #1945 covered here:

* AC3: ``GET /ui/audit/my-recent`` returns only the session operator's own
  rows (a second operator's row never appears) and is ``OPERATOR``-reachable.
* AC4: the literal ``my-recent`` route is registered BEFORE the
  ``show/{audit_id}`` (and any other ``{param}``) route -- ``my-recent`` is
  never bound as an ``audit_id`` slug (first-match-wins).

Suite shape mirrors :mod:`backend.tests.test_ui_audit_query` (the T1
session-cookie HTTP edge). The module name is ``test_ui_audit_myrecent`` to
stay distinct from the sibling T2 ``test_ui_audit_drawer`` module and the
T1 ``test_ui_audit_query`` -- the flat ``test_ui_*.py`` convention.
"""

from __future__ import annotations

import asyncio
import uuid
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
from meho_backplane.ui.routes.audit import build_audit_router
from meho_backplane.ui.templating import reset_templating_for_testing

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")

#: The session's own operator. ``my-recent`` binds the query to this sub.
_OPERATOR_SELF = "op-self"
#: A second operator in the same tenant whose rows must NEVER surface on the
#: self operator's my-recent view.
_OPERATOR_OTHER = "op-other"

#: A fixed base timestamp inside the my-recent 24h window so the rows match.
_BASE = datetime.now(UTC) - timedelta(hours=1)

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
    operator_sub: str,
    second: int = 0,
    op_id: str = "vsphere.vm.list",
    op_class: str = "read",
) -> uuid.UUID:
    """Insert one ``audit_log`` row for *operator_sub*; return its id."""
    resolved_id = uuid.uuid4()
    payload: dict[str, Any] = {"op_id": op_id, "op_class": op_class}

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
                    status_code=200,
                    duration_ms=Decimal("1.0"),
                    payload=payload,
                )
            )
        return resolved_id

    return asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OPERATOR_SELF,
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


# ===========================================================================
# Authentication boundary
# ===========================================================================


def test_my_recent_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/audit/my-recent`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/audit/my-recent")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ===========================================================================
# AC3: my-recent is OPERATOR-self-scoped
# ===========================================================================


def test_my_recent_returns_only_session_operator_rows() -> None:
    """AC3: only the session operator's own rows appear; a peer's never do."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(
        tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF, second=0, op_id="self.op.mine"
    )
    _seed_audit_row(
        tenant_id=_TENANT_A, operator_sub=_OPERATOR_OTHER, second=1, op_id="other.op.theirs"
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/my-recent")

    assert response.status_code == 200, response.text
    body = response.text
    # The self operator's row is present; the peer operator's is not.
    assert "self.op.mine" in body
    assert "other.op.theirs" not in body
    # The self-scope label echoes the operator sub.
    assert _OPERATOR_SELF in body


def test_my_recent_is_operator_reachable_and_fragment_only() -> None:
    """AC3: my-recent is OPERATOR-reachable and a chrome-less fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF, op_id="reachable.op")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/my-recent")

    assert response.status_code == 200, response.text
    body = response.text
    assert "reachable.op" in body
    # Fragment: no full-page chrome (<title>) / no filter form.
    assert "<title>" not in body
    assert 'id="audit-filter-form"' not in body
    # The fragment wrapper id is the my-recent slot.
    assert 'id="audit-my-recent"' in body


def test_my_recent_renders_empty_state_for_quiet_operator() -> None:
    """An operator with no last-24h rows sees the empty-state copy."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Only a peer operator has activity; the self operator is quiet.
    _seed_audit_row(tenant_id=_TENANT_A, operator_sub=_OPERATOR_OTHER, op_id="peer.only.op")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/my-recent")

    assert response.status_code == 200, response.text
    body = response.text
    assert "No activity in the last 24 hours" in body
    assert "peer.only.op" not in body


def test_my_recent_rows_open_the_detail_drawer() -> None:
    """Each my-recent row carries the drawer-open affordance (shared partial)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    row_id = _seed_audit_row(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF, op_id="drawer.op")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/my-recent")

    assert response.status_code == 200, response.text
    # The row's "details" affordance hx-gets the drawer route for this id.
    assert f"/ui/audit/show/{row_id}" in response.text


# ===========================================================================
# AC4: literal my-recent route is registered before show/{audit_id}
# ===========================================================================


def test_my_recent_route_registered_before_param_route() -> None:
    """AC4: the literal ``my-recent`` route precedes ``show/{audit_id}``.

    First-match-wins routing resolves on declaration order, so the literal
    ``my-recent`` segment must be registered before the parametrised drawer
    route or a request to ``/ui/audit/my-recent`` would bind ``my-recent`` as
    an ``audit_id`` slug (and 422 on the UUID coercion).
    """
    router = build_audit_router()
    paths = [route.path for route in router.routes]  # type: ignore[attr-defined]
    assert "/ui/audit/my-recent" in paths
    assert "/ui/audit/show/{audit_id}" in paths
    assert paths.index("/ui/audit/my-recent") < paths.index("/ui/audit/show/{audit_id}")


def test_my_recent_is_not_bound_as_audit_id_slug() -> None:
    """``/ui/audit/my-recent`` resolves the literal route, not the drawer.

    A regression guard for AC4: if the parametrised drawer were registered
    first, ``my-recent`` would be coerced as a UUID ``audit_id`` and 422.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF, op_id="literal.route.op")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OPERATOR_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/my-recent")
    # Resolves the literal my-recent fragment (200), not a 422 UUID-coercion.
    assert response.status_code == 200, response.text
    assert 'id="audit-my-recent"' in response.text
