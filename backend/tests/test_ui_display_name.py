# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console names the signed-in person, never their Keycloak ``sub``.

The user menu used to show the ``sub`` twice: as the avatar initials
(``sub[:2] | upper``) and as the label and its ``title``. The session row
had no name, so there was nothing else to show. Since migration ``0191``
the login callback stores the token's ``name`` claim on the session,
Fernet-encrypted like the tokens, and the pages show that.

This suite pins:

* the name is stored encrypted and comes back on every way a session is
  loaded, including after a token refresh;
* the user menu, the dashboard greeting, the account page and the "My
  recent activity" panel show the name;
* **with no name** -- the path that regresses quietly -- those places show
  a neutral label, and the ``sub`` appears in none of them: not as the
  label, not as the ``title``, not as initials. The account page still
  shows the ``sub``, in its own row labelled as an ID.
"""

from __future__ import annotations

import asyncio
import re
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
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant, WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    load_session,
    load_session_for_update,
    reset_fernet_cache_for_testing,
    rotate_refresh,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import (
    get_jinja_env,
    person_initials,
    reset_templating_for_testing,
)
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

from ._ui_role_helpers import allow_ui_operator_role

_TENANT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_TENANT_SLUG = "display-name-fixture"
#: Distinctive, so a substring check cannot pass by accident. Its first
#: two letters upper-cased ("ZQ") are what the old avatar showed.
_SUB = "zq7c1e90-display-name-sub"
_SUB_INITIALS = _SUB[:2].upper()
_NAME = "Ada King Lovelace"


@pytest.fixture(autouse=True)
def _ui_operator_role(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_ui_operator_role(monkeypatch)


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    resets = (
        get_settings.cache_clear,
        reset_fernet_cache_for_testing,
        reset_verifier_store_for_testing,
        reset_templating_for_testing,
        clear_discovery_cache,
        clear_jwks_cache,
        reset_engine_for_testing,
    )
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()


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


def _seed(display_name: str | None) -> uuid.UUID:
    """Insert the tenant row and a session for ``_SUB``; return the session id."""

    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            session.add(Tenant(id=_TENANT_ID, slug=_TENANT_SLUG, name="Display Name Tenant"))
            decrypted = await create_session(
                session,
                operator_sub=_SUB,
                tenant_id=_TENANT_ID,
                access_token="not-a-real-jwt",
                refresh_token="refresh-plaintext",
                lifetime=timedelta(hours=1),
                display_name=display_name,
            )
            return decrypted.id

    return asyncio.run(_do())


def _get(path: str, session_id: uuid.UUID) -> Any:
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        return client.get(path)


def _user_chip(body: str) -> tuple[str, str, str]:
    """Return the user menu's ``(avatar inner HTML, label title, label text)``."""
    avatar = re.search(r'data-field="user-avatar"[^>]*>(.*?)</span>', body, re.S)
    label = re.search(r'data-field="user-label" title="([^"]*)">([^<]*)</span>', body)
    assert avatar is not None, "user menu avatar not rendered"
    assert label is not None, "user menu label not rendered"
    return avatar.group(1), label.group(1), label.group(2)


# ---------------------------------------------------------------------------
# Initials come from the name only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Ada King Lovelace", "AL"),
        ("ada lovelace", "AL"),
        ("Ada", "A"),
        ("  Ada   Lovelace  ", "AL"),
        ("(Ada) Lovelace", "AL"),
        ("Ada -", "A"),
        ("Đorđe Šimić", "ĐŠ"),
        ("ßiggi Müller", "SM"),
        ("ß", "S"),
        ("Ada 2nd", "A2"),
        ("² Ada", "A"),
        ("½", None),
        ("", None),
        ("   ", None),
        ("- --", None),
        (None, None),
    ],
)
def test_person_initials(name: str | None, expected: str | None) -> None:
    assert person_initials(name) == expected


# ---------------------------------------------------------------------------
# Storage: encrypted, returned on every load, kept across a refresh
# ---------------------------------------------------------------------------


async def _raw_display_name(session_id: uuid.UUID) -> bytes | None:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(WebSession.display_name).where(WebSession.id == session_id)
            )
        ).scalar_one()


def test_name_is_stored_encrypted_and_loaded_back() -> None:
    session_id = _seed(_NAME)

    raw = asyncio.run(_raw_display_name(session_id))
    assert raw is not None
    assert _NAME.encode() not in raw
    assert b"Lovelace" not in raw

    async def _load() -> tuple[str | None, str | None]:
        async with get_sessionmaker()() as session, session.begin():
            loaded = await load_session(session, session_id)
            locked = await load_session_for_update(session, session_id)
        assert loaded is not None and locked is not None
        return loaded.display_name, locked.display_name

    assert asyncio.run(_load()) == (_NAME, _NAME)


@pytest.mark.parametrize("display_name", [None, "", "   "])
def test_missing_or_blank_name_stores_nothing(display_name: str | None) -> None:
    session_id = _seed(display_name)
    assert asyncio.run(_raw_display_name(session_id)) is None


def test_token_refresh_keeps_the_name() -> None:
    session_id = _seed(_NAME)

    async def _rotate() -> str | None:
        async with get_sessionmaker()() as session, session.begin():
            rotated = await rotate_refresh(
                session,
                session_id,
                presented_refresh="refresh-plaintext",
                new_access_token="new-access",
                new_refresh_token="new-refresh",
            )
        return rotated.display_name

    assert asyncio.run(_rotate()) == _NAME
    assert asyncio.run(_raw_display_name(session_id)) is not None


# ---------------------------------------------------------------------------
# Pages with a name
# ---------------------------------------------------------------------------


def test_user_menu_and_dashboard_show_the_name() -> None:
    response = _get("/ui/", _seed(_NAME))
    assert response.status_code == 200, response.text
    body = response.text

    avatar, title, label = _user_chip(body)
    assert avatar.strip() == "AL"
    assert title == _NAME
    assert label == _NAME
    assert f'data-field="signed-in-name">{_NAME}<' in body
    assert _SUB not in body


def test_account_page_shows_the_name_and_the_id_as_an_id() -> None:
    response = _get("/ui/account", _seed(_NAME))
    assert response.status_code == 200, response.text
    body = response.text

    assert f'data-field="display-name" title="{_NAME}">{_NAME}</dd>' in body
    assert ">Subject ID</dt>" in body
    assert f">{_SUB}</dd>" in body
    _, title, label = _user_chip(body)
    assert _SUB not in (title, label)


# ---------------------------------------------------------------------------
# Pages without a name: neutral, and never the sub
# ---------------------------------------------------------------------------


def test_no_name_user_menu_is_neutral_and_not_derived_from_the_sub() -> None:
    response = _get("/ui/", _seed(None))
    assert response.status_code == 200, response.text
    body = response.text

    avatar, title, label = _user_chip(body)
    assert label == "Your account"
    assert title == "Your account"
    assert "<svg" in avatar
    assert _SUB_INITIALS not in avatar
    assert _SUB not in avatar
    # The dashboard greeting names the tenant only.
    assert "Signed in to tenant" in body
    assert 'data-field="signed-in-name"' not in body
    # Nowhere on the page.
    assert _SUB not in body


def test_no_name_account_page_shows_a_neutral_name_row() -> None:
    response = _get("/ui/account", _seed(None))
    assert response.status_code == 200, response.text
    body = response.text

    assert "No name on this session." in body
    avatar, title, label = _user_chip(body)
    assert (title, label) == ("Your account", "Your account")
    assert _SUB_INITIALS not in avatar
    # The sub is on the page exactly where it is labelled as an ID.
    assert body.count(_SUB) == 2  # the ``title`` and the text of the Subject ID row
    assert f'title="{_SUB}"\n          >{_SUB}</dd>' in body


_MY_RECENT_WITH_NAME = (
    f'last 24h &middot; <span class="break-all" data-field="my-recent-name">{_NAME}</span>'
)


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [(_NAME, _MY_RECENT_WITH_NAME), (None, "last 24h</span>")],
)
def test_my_recent_label_is_the_name_or_nothing(display_name: str | None, expected: str) -> None:
    html = (
        get_jinja_env()
        .get_template("audit/_my_recent.html")
        .render(rows=[], session_display_name=display_name, operator_sub=_SUB)
    )
    assert expected in html
    assert _SUB not in html


def test_signed_out_user_menu_says_not_signed_in() -> None:
    """No session at all: the chip says so instead of "Your account"."""
    html = get_jinja_env().get_template("base.html").render(ready=True)
    _, title, label = _user_chip(html)
    assert (title, label) == ("Not signed in", "Not signed in")
