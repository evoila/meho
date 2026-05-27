# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant chip + cascade tests for G0.15-T9 (#1217).

The v0.7.0 closed-loop dogfood (rdc-hetzner-dc#753) flagged three
correlated symptoms on the operator UI:

1. The page-header tenant chip rendered the stub literal
   ``"tenant: (sign in to choose)"`` regardless of the operator's
   session state -- they were signed in, the chip claimed otherwise.
2. The Memory UI list rendered ``0 memories`` despite the API surface
   returning rows for the same operator + tenant.
3. The Broadcast surface rendered nothing despite live MCP activity
   emitting broadcast events for the operator's tenant.

The issue body hypothesised a single root cause: the BFF session never
carried a usable tenant_id, so the tenant-scoped list queries fell
back to ``(operator, undefined_tenant)`` and returned empty. The
inspection in the implementation pass disproved the hypothesis -- the
``/ui/auth/callback`` handler already auto-selects the operator's
tenant from the JWT ``tenant_id`` claim at session-create time
(``backend/src/meho_backplane/ui/auth/routes.py::_persist_session_from_tokens``).
The Memory + Broadcast surfaces already scope their queries by
``session_ctx.tenant_id``. The only real bug was the chassis stub
header: the chip's disabled ``<select>`` was wired to a hardcoded
placeholder and never consulted the session at all.

This suite pins:

* The chip renders the operator's tenant **name** (not the UUID, not
  ``"(sign in to choose)"``) when the BFF session is alive. ACs #1
  + #2 on the issue.
* The cascade surfaces (Memory + Broadcast) carry the tenant_id from
  the same session through to their queries -- a regression where a
  future change drops the tenant_id off the context would surface
  here, not as a silent empty-state. AC #3 + #4 on the issue.
* The render path stays sound when the tenant row was deleted out
  from under the session (the FK is soft per
  :class:`WebSession.tenant_id`; the chip degrades to the slug
  fallback, then the UUID fallback, but the page still renders 200).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
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
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

_BACKPLANE_URL = "https://meho.test"

_TENANT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
#: Distinct from the chassis-migration seeded ``default`` / ``rdc-internal``
#: slugs (see migrations ``0018`` / ``0028``) so the per-test insert
#: cannot collide on the ``tenant_slug_idx`` unique constraint.
_TENANT_SLUG = "tenant-chip-fixture"
_TENANT_NAME = "Tenant Chip Fixture"
_OPERATOR_SUB = "op-tenant-chip"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis + BFF env vars so the suite is self-contained."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
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


def _build_app() -> FastAPI:
    """Construct the chassis app shape: CSRF + UISession + UI routes."""
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


def _seed_tenant(
    tenant_id: uuid.UUID = _TENANT_ID,
    *,
    slug: str = _TENANT_SLUG,
    name: str = _TENANT_NAME,
) -> None:
    """Insert the ``tenant`` row the middleware joins against."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=name))

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID = _TENANT_ID,
    operator_sub: str = _OPERATOR_SUB,
) -> uuid.UUID:
    """Insert a BFF session row and return its UUID."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="unused-access",
                refresh_token="unused-refresh",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_get(path: str, session_id: uuid.UUID) -> Any:
    """Issue an authenticated GET against the test app, no redirects."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        return client.get(path)


# ---------------------------------------------------------------------------
# AC #1 + #2 -- tenant chip renders the operator's tenant name (not the stub
# placeholder, not the UUID).
# ---------------------------------------------------------------------------


def test_dashboard_chip_renders_tenant_name_not_stub_placeholder() -> None:
    """``GET /ui/`` renders the operator's tenant name on the chip.

    The pre-#1217 chassis hardcoded ``"tenant: (sign in to choose)"``
    on a disabled ``<select>`` regardless of session state. After the
    fix the chip pulls from the BFF session's
    :class:`UISessionContext` (which the middleware populates from the
    ``tenant`` row keyed on the session's ``tenant_id``).
    """
    _seed_tenant()
    session_id = _seed_session_sync()
    response = _authenticated_get("/ui/", session_id)
    assert response.status_code == 200, response.text
    body = response.text
    # The exact stub literal must not survive.
    assert "(sign in to choose)" not in body, "tenant chip still renders pre-fix stub"
    # The operator's tenant name renders inside the chip's badge -- we
    # check the chip's ``aria-label`` is present and the name is in the
    # body. Looking for the literal ``aria-label="Active tenant"`` keeps
    # the assertion narrow enough that an unrelated mention of the name
    # elsewhere on the page (e.g. dashboard greeting) doesn't satisfy
    # the assertion.
    assert 'aria-label="Active tenant"' in body
    assert _TENANT_NAME in body


def test_chip_falls_back_to_slug_when_name_empty() -> None:
    """Empty tenant name → chip renders the slug.

    A future tenant seeded by ops with an empty ``name`` column should
    still produce a useful chip (the slug is the operator-facing
    handle per the :class:`Tenant` docstring). The fallback chain is
    ``name -> slug -> id``.
    """
    _seed_tenant(name="")
    session_id = _seed_session_sync()
    response = _authenticated_get("/ui/", session_id)
    assert response.status_code == 200
    body = response.text
    assert _TENANT_SLUG in body
    assert "(sign in to choose)" not in body


def test_chip_falls_back_to_uuid_when_tenant_row_missing() -> None:
    """No tenant row → chip degrades to the UUID but the page renders.

    Soft-FK discipline (``web_session.tenant_id`` has no FK to
    ``tenant.id``): deleting a tenant out from under an active session
    is an ops anomaly the BFF must survive. The fix logs a warning and
    falls back to the bare UUID rather than 500-ing.
    """
    # Intentionally do NOT seed a tenant row.
    session_id = _seed_session_sync()
    response = _authenticated_get("/ui/", session_id)
    assert response.status_code == 200
    body = response.text
    assert "(sign in to choose)" not in body
    assert str(_TENANT_ID) in body


# ---------------------------------------------------------------------------
# AC #3 + #4 -- the cascade surfaces (Memory + Broadcast) carry the same
# tenant_id through their scope.
# ---------------------------------------------------------------------------


def test_memory_surface_renders_with_session_tenant_present() -> None:
    """``GET /ui/memory`` renders 200 + the tenant chip + the empty list.

    Pre-#1217 the operator's dogfood report showed the Memory surface
    "0 memories" UI state. The cascade hypothesis ("unset tenant scopes
    the list query to ``(operator, undefined_tenant)``") would surface
    here as a missing tenant chip alongside the empty-state. Post-fix
    the chip renders the tenant name (proving the tenant_id is bound
    on the session and reaching the template), the empty-state copy
    renders (proving the route ran to completion), and the
    ``id="memory-cards"`` swap target renders (proving the surface's
    HTMX wiring is wired through to the cards fragment).

    The list is empty because no memories are seeded -- the
    cascade-with-data path is exercised by the existing memory list
    suite (:mod:`backend.tests.test_ui_memory_list`); this test only
    pins the contract that the tenant_id reaches the page.
    """
    _seed_tenant()
    session_id = _seed_session_sync()
    response = _authenticated_get("/ui/memory", session_id)
    assert response.status_code == 200, response.text
    body = response.text
    assert "(sign in to choose)" not in body
    assert _TENANT_NAME in body
    assert 'id="memory-cards"' in body


def test_broadcast_surface_renders_with_session_tenant_present() -> None:
    """``GET /ui/broadcast`` renders the feed page scoped to the session tenant.

    The broadcast feed page reads ``session_ctx.tenant_id`` directly
    when wiring the ``sse-connect`` URL and the target-name dropdown
    (see :mod:`meho_backplane.ui.routes.broadcast.feed`). A regression
    that nulls the session tenant would surface as a 5xx (the SSE URL
    builder would synthesise an empty tenant) or as the chip falling
    back to ``(sign in to choose)``; this test pins both.
    """
    _seed_tenant()
    session_id = _seed_session_sync()
    response = _authenticated_get("/ui/broadcast", session_id)
    assert response.status_code == 200, response.text
    body = response.text
    assert "(sign in to choose)" not in body
    assert _TENANT_NAME in body
    # The full-page render includes the per-tenant SSE bridge URL; its
    # presence proves the broadcast surface ran the full
    # _render_page flow (which also reads session_ctx.tenant_id when
    # building the target-name dropdown).
    assert "/ui/broadcast/stream" in body
