# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console routes that check no role carry the platform-admin role note too.

A platform admin may work in a tenant where they have no role group. There
every audit row of their requests carries ``tenant_role_source:
platform_admin``. The console writes that check a role get the note from
their token check (``test_platform_admin_role_source_audit.py``). The routes
here check no role. They get the note from the session dependency, which
reads what the session's last token check showed.

The tests drive the real app with a made-up platform admin login, the way a
person uses the console:

* The person signs in to tenant A, where they have a role group.
* They switch to tenant C, where they have none.
* They use one of the routes. Every audit row of that request carries the
  note, in both audit modes.

The same person in tenant A, and a person who is not a platform admin, get
no note. One more test starts in tenant C with no kept token check (the
first request after a restart).
"""

from __future__ import annotations

import base64
import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.revalidation import reset_read_revalidation_cache_for_testing
from meho_backplane.ui.auth.session_ref import session_ref
from meho_backplane.ui.auth.session_store import create_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.web_push.endpoints import check_subscription
from meho_backplane.web_push.keys import new_private_key, reset_key_cache_for_testing
from meho_backplane.web_push.recipients import LoginFacts
from meho_backplane.web_push.store import save_subscription
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.test_platform_admin_role_source_audit import (
    _TENANT_A,
    _TENANT_C,
    INSIDE,
    OUTSIDE,
    PLAIN,
    Login,
    _all_rows,
    _audit_mode,
    _noted,
    _reset,
    _run,
    _token,
)

_PUSH_ENDPOINT = "https://fcm.googleapis.com/fcm/send/browser-one"
_TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", f"{ISSUER.replace('/realms/', '/admin/realms/')}")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "true")
    monkeypatch.setenv("WEB_PUSH_ENABLED", "true")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", new_private_key())
    _reset()
    reset_key_cache_for_testing()
    yield
    _reset()
    reset_key_cache_for_testing()


@pytest.fixture
def key() -> Iterator[Any]:
    """An RSA keypair whose public half the mocked JWKS endpoint serves.

    The Keycloak admin login fails (401), so a tool sign-out writes its
    failed row without a call to a real Keycloak.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rsa_key = make_rsa_keypair("console-routes-role-source-kid")
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, public_jwks(rsa_key))
        mock.post(_TOKEN_URL).mock(return_value=httpx.Response(401))
        yield rsa_key


def _seed_tenants() -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for tenant_id, slug in ((_TENANT_A, "team-a"), (_TENANT_C, "team-c")):
                session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    _run(_do())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _seed_push(login: Login) -> str:
    """Store one browser of *login*; return the stored row's id."""
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    checked = check_subscription(
        endpoint=_PUSH_ENDPOINT, p256dh=_b64url(point), auth=_b64url(os.urandom(16))
    )

    async def _do() -> str:
        from sqlalchemy import select

        from meho_backplane.web_push.models import PushSubscription

        async with get_sessionmaker()() as session, session.begin():
            await save_subscription(
                session,
                operator_sub=login.sub,
                tenant_id=login.tenant_id,
                subscription=checked,
                facts=LoginFacts(tenant_role=None),
            )
            row_id = await session.scalar(
                select(PushSubscription.id).where(PushSubscription.endpoint == _PUSH_ENDPOINT)
            )
            return str(row_id)

    return _run(_do())


def _seed_session(
    access_token: str, sub: str, tenant_id: uuid.UUID, *, signed_in: bool
) -> uuid.UUID:
    """A session row. *signed_in* keeps what the login's token check showed."""

    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
                tenant_chosen=True,
                can_switch_tenant=True,
                tenant_admin=True if signed_in else None,
            )
            return uuid.UUID(str(decrypted.id))

    return _run(_do())


class Console:
    """A console session of *login*, in the tenant *login* works in."""

    def __init__(self, key: Any, login: Login, *, after_restart: bool = False) -> None:
        self.login = login
        token = _token(key, login)
        # A second session of the same person, for the two revoke routes.
        self.other_session = _seed_session(token, login.sub, _TENANT_A, signed_in=True)
        if after_restart:
            session_id = _seed_session(token, login.sub, login.tenant_id, signed_in=False)
            reset_read_revalidation_cache_for_testing()
        else:
            # Signed in to tenant A, like the login callback.
            session_id = _seed_session(token, login.sub, _TENANT_A, signed_in=True)
        self.client = TestClient(app, follow_redirects=False)
        self.client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))
        self.client.cookies.set(CSRF_COOKIE_NAME, csrf)
        self.headers = {"X-CSRF-Token": csrf, "HX-Request": "true"}
        if not after_restart and login.tenant_id != _TENANT_A:
            switched = self.post("/ui/tenant/switch", {"tenant": str(login.tenant_id)})
            assert switched.status_code == 303, switched.text

    def post(self, path: str, data: dict[str, str]) -> Any:
        return self.client.post(path, data=data, headers=self.headers)


def _revoke_others(console: Console) -> Any:
    return console.post("/ui/account/sessions/revoke-others", {})


def _revoke_one(console: Console) -> Any:
    ref = session_ref(console.other_session)
    return console.post("/ui/account/sessions/revoke", {"session_ref": ref})


def _tool_sign_out(console: Console) -> Any:
    return console.post("/ui/account/tools/sign-out", {"tool": "claude-desktop"})


def _push_status(console: Console) -> Any:
    _seed_push(console.login)
    return console.post("/ui/account/push/status", {"endpoint": _PUSH_ENDPOINT})


def _push_remove(console: Console) -> Any:
    row_id = _seed_push(console.login)
    return console.post("/ui/account/push/remove", {"subscription_id": row_id})


def _push_unsubscribe(console: Console) -> Any:
    _seed_push(console.login)
    return console.post("/ui/account/push/unsubscribe", {"endpoint": _PUSH_ENDPOINT})


def _memory_preview(console: Console) -> Any:
    return console.post("/ui/memory/preview", {"body": "**hello**"})


_ROUTES: dict[str, Callable[[Console], Any]] = {
    "sessions-revoke-others": _revoke_others,
    "sessions-revoke": _revoke_one,
    "tools-sign-out": _tool_sign_out,
    "push-status": _push_status,
    "push-remove": _push_remove,
    "push-unsubscribe": _push_unsubscribe,
    "memory-preview": _memory_preview,
}

#: With the wrapper rows retired, a preview is a routine request: it leaves
#: no audit row at all (its request usage event records it).
_NO_ROW_WHEN_RETIRED = {"memory-preview"}


def _route_rows(console: Console, route: str) -> list[AuditLog]:
    before = {row.id for row in _all_rows()}
    response = _ROUTES[route](console)
    assert response.status_code == 200, (response.status_code, response.text)
    return [row for row in _all_rows() if row.id not in before]


def _assert_rows(rows: list[AuditLog], login: Login, *, route: str, retired: bool) -> None:
    if retired and route in _NO_ROW_WHEN_RETIRED:
        assert rows == []
        return
    assert rows, "the request left no audit row"
    for row in rows:
        assert row.operator_sub == login.sub, row.payload
        assert row.tenant_id == login.tenant_id, (row.path, row.payload)
    where = [(row.path, (row.payload or {}).get("op_id")) for row in rows]
    if login.expect_note:
        assert all(_noted(row) for row in rows), where
    else:
        assert not any(_noted(row) for row in rows), where


@pytest.mark.parametrize("retired", [False, True], ids=["wrapper-rows", "wrapper-rows-retired"])
@pytest.mark.parametrize(
    "login",
    [OUTSIDE, INSIDE, PLAIN],
    ids=["platform-admin-without-role-group", "platform-admin-in-own-tenant", "plain"],
)
@pytest.mark.parametrize("route", list(_ROUTES))
def test_the_note_follows_where_the_role_came_from(
    monkeypatch: pytest.MonkeyPatch, key: Any, route: str, login: Login, retired: bool
) -> None:
    _audit_mode(monkeypatch, retired=retired)
    _seed_tenants()
    console = Console(key, login)
    rows = _route_rows(console, route)
    _assert_rows(rows, login, route=route, retired=retired)


def test_first_request_after_a_restart_checks_the_token_once_and_has_the_note(
    key: Any,
) -> None:
    _seed_tenants()
    console = Console(key, OUTSIDE, after_restart=True)
    rows = _route_rows(console, "sessions-revoke-others")
    _assert_rows(rows, OUTSIDE, route="sessions-revoke-others", retired=False)
