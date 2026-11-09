# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console session cookie: its ``__Host-`` name and the one reader.

* The cookie is set as ``__Host-meho_session`` with ``Secure``,
  ``Path=/`` and no ``Domain``.
* The session check, the CSRF check and logout read it the same way. A
  request that carries the cookie more than once counts as a request
  with no session on all three.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth.flow import clear_discovery_cache
from meho_backplane.ui.auth.routes import build_router, set_session_cookie
from meho_backplane.ui.auth.session_cookie import read_session_cookie
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER, DEFAULT_TENANT_ID

_BACKPLANE_URL = "https://meho.test"


@pytest.fixture(autouse=True)
def _console_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    clear_discovery_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    clear_discovery_cache()
    reset_engine_for_testing()


def _seed_session() -> uuid.UUID:
    async def _seed() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            row = await create_session(
                session,
                operator_sub="op-cookie",
                tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
                access_token="a",
                refresh_token="r",
                lifetime=timedelta(hours=1),
            )
            return row.id

    return asyncio.run(_seed())


def _is_revoked(session_id: uuid.UUID) -> bool:
    async def _check() -> bool:
        async with get_sessionmaker()() as session:
            row = await session.get(WebSession, session_id)
            assert row is not None
            return row.revoked_at is not None

    return asyncio.run(_check())


def _two_values(first: str, second: str) -> str:
    return f"{SESSION_COOKIE_NAME}={first}; {SESSION_COOKIE_NAME}={second}"


def _scope(*cookie_headers: bytes) -> dict[str, object]:
    return {"type": "http", "headers": [(b"cookie", value) for value in cookie_headers]}


# ---------------------------------------------------------------------------
# The cookie name and how it is set
# ---------------------------------------------------------------------------


def test_session_cookie_name_has_the_host_prefix() -> None:
    assert SESSION_COOKIE_NAME == "__Host-meho_session"


def test_session_cookie_is_set_secure_on_root_path_without_domain() -> None:
    response = RedirectResponse("/ui/")
    set_session_cookie(response, uuid.uuid4())
    header = response.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_NAME}=")
    attributes = [part.strip().lower() for part in header.split(";")[1:]]
    assert "secure" in attributes
    assert "httponly" in attributes
    assert "path=/" in attributes
    assert not any(attr.startswith("domain") for attr in attributes)


# ---------------------------------------------------------------------------
# The one reader
# ---------------------------------------------------------------------------


def test_reader_returns_the_single_value() -> None:
    scope = _scope(f"other=1; {SESSION_COOKIE_NAME}=abc; meho_csrf=x".encode())
    assert read_session_cookie(scope) == "abc"


@pytest.mark.parametrize(
    "cookie_headers",
    [
        pytest.param((), id="no-cookie-header"),
        pytest.param((b"other=1",), id="cookie-missing"),
        pytest.param((b"meho_session=abc",), id="name-without-prefix"),
        pytest.param((_two_values("abc", "def").encode(),), id="twice-in-one-header"),
        pytest.param((_two_values("abc", "abc").encode(),), id="same-value-twice"),
        pytest.param(
            (f"{SESSION_COOKIE_NAME}=abc".encode(), f"{SESSION_COOKIE_NAME}=def".encode()),
            id="once-in-each-of-two-headers",
        ),
        pytest.param((f"{SESSION_COOKIE_NAME}=\xe9".encode("latin-1"),), id="not-ascii"),
    ],
)
def test_reader_returns_none(cookie_headers: tuple[bytes, ...]) -> None:
    assert read_session_cookie(_scope(*cookie_headers)) is None


# ---------------------------------------------------------------------------
# Session check, CSRF check and logout with two values
# ---------------------------------------------------------------------------


def _session_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_router())

    @app.get("/ui/sentinel")
    async def sentinel() -> dict[str, str]:
        return {"ok": "true"}

    return app


def test_session_check_lets_one_value_through() -> None:
    session_id = str(_seed_session())
    client = TestClient(_session_app(), follow_redirects=False)
    response = client.get("/ui/sentinel", headers={"cookie": f"{SESSION_COOKIE_NAME}={session_id}"})
    assert response.status_code == 200


@pytest.mark.parametrize("valid_first", [True, False])
def test_session_check_treats_two_values_as_no_session(valid_first: bool) -> None:
    valid = str(_seed_session())
    other = str(uuid.uuid4())
    pair = (valid, other) if valid_first else (other, valid)
    client = TestClient(_session_app(), follow_redirects=False)
    response = client.get("/ui/sentinel", headers={"cookie": _two_values(*pair)})
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def _csrf_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/ui/action")
    async def action() -> dict[str, str]:
        return {"ok": "true"}

    return app


def _csrf_post(session_cookie: str, token: str) -> tuple[int, str | None]:
    client = TestClient(_csrf_app(), follow_redirects=False)
    response = client.post(
        "/ui/action",
        headers={
            "cookie": f"{session_cookie}; {CSRF_COOKIE_NAME}={token}",
            CSRF_HEADER_NAME: token,
        },
    )
    return response.status_code, response.headers.get("x-csrf-rejection-reason")


def test_csrf_check_accepts_one_value() -> None:
    session_id = str(uuid.uuid4())
    token = mint_csrf_token(session_id)
    assert _csrf_post(f"{SESSION_COOKIE_NAME}={session_id}", token) == (200, None)


@pytest.mark.parametrize("bound_first", [True, False])
def test_csrf_check_treats_two_values_as_no_session(bound_first: bool) -> None:
    bound = str(uuid.uuid4())
    other = str(uuid.uuid4())
    token = mint_csrf_token(bound)
    pair = (bound, other) if bound_first else (other, bound)
    assert _csrf_post(_two_values(*pair), token) == (403, "no_session")


@pytest.mark.parametrize("valid_first", [True, False])
def test_logout_with_two_values_revokes_no_session_and_clears_the_cookie(
    valid_first: bool,
) -> None:
    valid = _seed_session()
    other = str(uuid.uuid4())
    pair = (str(valid), other) if valid_first else (other, str(valid))
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(f"{DEFAULT_ISSUER}/.well-known/openid-configuration").respond(404)
        client = TestClient(_session_app(), follow_redirects=False)
        response = client.get("/ui/auth/logout", headers={"cookie": _two_values(*pair)})
    assert response.status_code == 302
    assert not _is_revoked(valid)
    set_cookie = response.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{SESSION_COOKIE_NAME.lower()}=")
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


def test_logout_with_one_value_revokes_the_session() -> None:
    session_id = _seed_session()
    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(f"{DEFAULT_ISSUER}/.well-known/openid-configuration").respond(404)
        client = TestClient(_session_app(), follow_redirects=False)
        response = client.get(
            "/ui/auth/logout", headers={"cookie": f"{SESSION_COOKIE_NAME}={session_id}"}
        )
    assert response.status_code == 302
    assert _is_revoked(session_id)
