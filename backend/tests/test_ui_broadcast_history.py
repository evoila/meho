# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast history pane (#2549, #3758, #3762).

Since #3758 the pane reads stored agent announcements from the archive
(Broadcast carries agent announcements only):

* ``GET /ui/broadcast/history`` renders the session tenant's stored
  announcements of the retention window into the seed data island,
  newest first; another tenant's announcements never appear.
* The pane has no kind or operation filter (#3762); an old ``?kind=``
  link still renders the announcements.
* Announcement free text is emitted through Jinja ``| tojson`` (which
  escapes ``<`` / ``>`` / ``&``) and bound via Alpine ``x-text``, so an
  ``activity`` carrying ``<script>`` never reaches the DOM as markup.
* A database error degrades the pane to its empty state, not a 500.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import (
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
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
from meho_backplane.ui.routes.broadcast import history as history_module
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.broadcast_admission_helpers import seed_announcement
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

from ._ui_role_helpers import allow_ui_operator_role


@pytest.fixture(autouse=True)
def _ui_operator_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console pages under test sit behind the ``operator`` role check."""
    allow_ui_operator_role(monkeypatch)


_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + broadcast env vars; reset caches around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
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


def _seed_session_sync(*, tenant_id: uuid.UUID = _TENANT_A) -> uuid.UUID:
    from meho_backplane.db.engine import get_sessionmaker

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-42",
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def _seed(**fields: Any) -> None:
    asyncio.run(seed_announcement(_TENANT_A, **fields))


def test_history_renders_stored_announcements_newest_first() -> None:
    session_id = _seed_session_sync()
    _seed(activity="older step")
    _seed(activity="rotating tokens on cluster X")
    client = _authenticated_client(session_id)
    response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    assert "agent_announcement" in body
    assert body.index("rotating tokens on cluster X") < body.index("older step")


def test_history_is_tenant_scoped() -> None:
    session_id = _seed_session_sync()
    asyncio.run(seed_announcement(uuid.uuid4(), activity="other tenant plan"))
    client = _authenticated_client(session_id)
    response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    assert "other tenant plan" not in response.text


def test_history_announcement_row_markup_is_agent_authored() -> None:
    """The row partial renders announcements only, with escaped activity binding."""
    session_id = _seed_session_sync()
    _seed()
    client = _authenticated_client(session_id)
    response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    assert 'x-text="ev.activity"' in body
    # No operation branch is left in the row (#3762).
    for gone in ('x-text="ev.op_id"', "openDrawer", "badgeClass(", "isAggregateOnly"):
        assert gone not in body
    assert "announcementTargets(ev)" in body
    assert "announcementMeta(ev)" in body
    assert "phaseBadgeClass(ev.phase)" in body


def test_history_activity_is_html_escaped_in_island() -> None:
    """A ``<script>``-bearing activity is escaped by ``| tojson`` in the island."""
    session_id = _seed_session_sync()
    _seed(activity="<script>alert(1)</script>")
    client = _authenticated_client(session_id)
    response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    body = response.text
    assert "<script>alert(1)</script>" not in body
    assert "\\u003cscript\\u003e" in body


def test_history_has_no_kind_filter() -> None:
    """No "All / Operations / Announcements" control; an old ``?kind=`` link is ignored."""
    session_id = _seed_session_sync()
    _seed(activity="rotating tokens on cluster X")
    client = _authenticated_client(session_id)
    response = client.get("/ui/broadcast/history", params={"kind": "operation"})
    assert response.status_code == 200
    assert "rotating tokens on cluster X" in response.text
    assert "kind=" not in response.text
    assert 'aria-label="Filter by event kind"' not in response.text


def test_history_database_error_renders_empty_pane() -> None:
    from sqlalchemy.exc import OperationalError

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("db down"))

    session_id = _seed_session_sync()
    with patch.object(history_module, "latest_announcements", _boom):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/history")
    assert response.status_code == 200
    assert "db down" not in response.text
