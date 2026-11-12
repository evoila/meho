# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Notify me about approvals in this browser" switch on the account page.

* With the admin setting ``WEB_PUSH_ENABLED`` off, every push route answers
  404 and the page shows no switch.
* Switching on stores the subscription for the signed-in person only;
  switching off deletes it.
* An endpoint that is not a browser maker's push service is refused.
* A person removes only their own rows; a ``tenant_admin`` removes any row
  of their tenant, and none of another tenant.
* ``/ui/account/push/status`` is on only for the person who saved the
  browser, and off again after the row is removed.
* The private push key, the endpoint and the subscription keys never appear
  in a log line or an answer, also not when the database refuses a write.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.logging import configure_logging
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.routes import account_push
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from meho_backplane.web_push import store as store_module
from meho_backplane.web_push.endpoints import check_subscription
from meho_backplane.web_push.keys import (
    new_private_key,
    public_key_for,
    reset_key_cache_for_testing,
)
from meho_backplane.web_push.models import PushSubscription
from meho_backplane.web_push.recipients import LoginFacts
from meho_backplane.web_push.store import save_subscription
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ME = "op-self"
_OTHER = "op-other"

_PRIVATE_KEY = new_private_key()

_SUBSCRIBE = "/ui/account/push/subscribe"
_UNSUBSCRIBE = "/ui/account/push/unsubscribe"
_REMOVE = "/ui/account/push/remove"
_LIST = "/ui/account/push/subscriptions"
_STATUS = "/ui/account/push/status"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _browser_keys() -> dict[str, str]:
    """A made-up browser subscription key pair, as the browser sends it."""
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {"p256dh": _b64url(point), "auth": _b64url(os.urandom(16))}


def _subscription(path: str = "browser-one") -> dict[str, str]:
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/{path}", **_browser_keys()}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("WEB_PUSH_ENABLED", "true")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", _PRIVATE_KEY)
    resets = (
        get_settings.cache_clear,
        reset_fernet_cache_for_testing,
        reset_verifier_store_for_testing,
        reset_templating_for_testing,
        clear_discovery_cache,
        clear_jwks_cache,
        reset_engine_for_testing,
        reset_key_cache_for_testing,
    )
    for reset in resets:
        reset()
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    yield
    for reset in resets:
        reset()


def _setting_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")
    get_settings.cache_clear()
    reset_key_cache_for_testing()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _client(
    *,
    sub: str = _ME,
    tenant_id: uuid.UUID = _TENANT_A,
    role: TenantRole | None = None,
    app: FastAPI | None = None,
) -> tuple[TestClient, respx.MockRouter]:
    """A signed-in client; with *role*, the role lift sees that role.

    Without *role* the token cannot be verified, so the person counts as no
    admin (the role lift fails soft). The caller stops the mock.
    """
    mock = respx.mock(assert_all_called=False)
    mock.start()
    access_token = "not-a-real-jwt"
    if role is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            keypair = make_rsa_keypair("ui-push-test-kid")
        mock_discovery_and_jwks(mock, public_jwks(keypair))
        access_token = mint_token(
            keypair, sub=sub, tenant_id=str(tenant_id), tenant_role=role.value
        )

    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            made = await create_session(
                session,
                operator_sub=sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return made.id

    session_id = asyncio.run(_do())
    client = TestClient(app or _app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, token)
    client.headers.update({"X-CSRF-Token": token, "HX-Request": "true"})
    return client, mock


def _rows() -> list[PushSubscription]:
    async def _do() -> list[PushSubscription]:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(PushSubscription))
            return list(result.scalars())

    return asyncio.run(_do())


def _ids_of(client: TestClient, sub: dict[str, str]) -> uuid.UUID:
    response = client.post(_SUBSCRIBE, data=sub)
    assert response.status_code == 200, response.text
    return next(r.id for r in _rows() if r.endpoint == sub["endpoint"])


# ---------------------------------------------------------------------------
# The admin setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", _LIST),
        ("POST", _SUBSCRIBE),
        ("POST", _UNSUBSCRIBE),
        ("POST", _STATUS),
        ("POST", _REMOVE),
    ],
)
def test_routes_answer_404_with_the_setting_off(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    _setting_off(monkeypatch)
    client, mock = _client()
    try:
        response = client.request(method, path, data=_subscription() if method == "POST" else None)
    finally:
        mock.stop()
    assert response.status_code == 404
    assert _rows() == []


def test_routes_answer_404_when_the_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "")
    get_settings.cache_clear()
    reset_key_cache_for_testing()
    client, mock = _client()
    try:
        response = client.post(_SUBSCRIBE, data=_subscription())
    finally:
        mock.stop()
    assert response.status_code == 404
    assert _rows() == []


def test_page_has_no_switch_with_the_setting_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _setting_off(monkeypatch)
    client, mock = _client()
    try:
        body = client.get("/ui/account").text
    finally:
        mock.stop()
    assert "data-push-switch" not in body
    assert "push-subscribe.js" not in body


def test_page_shows_the_switch_with_the_public_key_only() -> None:
    client, mock = _client()
    try:
        response = client.get("/ui/account")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "Notify me about approvals in this browser" in body
    assert f'data-vapid-public-key="{public_key_for(_PRIVATE_KEY)}"' in body
    assert "/ui/static/src/app/push-subscribe.js" in body
    assert _PRIVATE_KEY not in body


# ---------------------------------------------------------------------------
# Switch on and off
# ---------------------------------------------------------------------------


def test_switch_on_stores_the_subscription_for_the_current_person_only() -> None:
    client, mock = _client()
    sub = _subscription()
    try:
        response = client.post(_SUBSCRIBE, data={**sub, "operator_sub": _OTHER})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].operator_sub == _ME
    assert rows[0].tenant_id == _TENANT_A
    assert rows[0].endpoint == sub["endpoint"]
    assert rows[0].p256dh == sub["p256dh"]
    assert rows[0].auth == sub["auth"]
    assert rows[0].created_at is not None
    # The answer shows the push service, never the endpoint or the keys.
    assert "Google" in response.text
    for value in sub.values():
        assert value not in response.text


def test_switch_on_twice_keeps_one_row_and_moves_the_browser_to_the_new_person() -> None:
    sub = _subscription()
    me, mock_me = _client()
    other, mock_other = _client(sub=_OTHER)
    try:
        assert me.post(_SUBSCRIBE, data=sub).status_code == 200
        assert me.post(_SUBSCRIBE, data=sub).status_code == 200
        assert len(_rows()) == 1
        assert other.post(_SUBSCRIBE, data=sub).status_code == 200
    finally:
        mock_me.stop()
        mock_other.stop()
    rows = _rows()
    assert [r.operator_sub for r in rows] == [_OTHER]


def test_saving_the_same_browser_twice_in_one_transaction_keeps_one_row() -> None:
    """The save is one ``INSERT ... ON CONFLICT``, so a second save of the
    same browser replaces the row instead of hitting the unique constraint."""
    first = check_subscription(**_subscription())
    second = check_subscription(**{**_subscription(), "endpoint": first.endpoint})

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for checked in (first, second):
                await save_subscription(
                    session,
                    operator_sub=_ME,
                    tenant_id=_TENANT_A,
                    subscription=checked,
                    facts=LoginFacts(tenant_role=None),
                )

    asyncio.run(_do())
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].p256dh == second.p256dh
    assert rows[0].auth == second.auth


def test_a_person_keeps_at_most_ten_browsers() -> None:
    client, mock = _client()
    try:
        for n in range(12):
            assert client.post(_SUBSCRIBE, data=_subscription(f"b{n}")).status_code == 200
    finally:
        mock.stop()
    endpoints = {r.endpoint.rsplit("/", 1)[1] for r in _rows()}
    assert len(endpoints) == 10
    assert "b11" in endpoints


def test_switch_off_deletes_it() -> None:
    client, mock = _client()
    sub = _subscription()
    try:
        assert client.post(_SUBSCRIBE, data=sub).status_code == 200
        response = client.post(_UNSUBSCRIBE, data={"endpoint": sub["endpoint"]})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert _rows() == []


def test_switch_off_never_deletes_the_row_of_another_person() -> None:
    sub = _subscription()
    other, mock_other = _client(sub=_OTHER)
    me, mock_me = _client()
    try:
        assert other.post(_SUBSCRIBE, data=sub).status_code == 200
        response = me.post(_UNSUBSCRIBE, data={"endpoint": sub["endpoint"]})
    finally:
        mock_other.stop()
        mock_me.stop()
    assert response.status_code == 200
    assert [r.operator_sub for r in _rows()] == [_OTHER]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://push.example/send/abc",
        "https://fcm.googleapis.com.push.example/send/abc",
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://user@fcm.googleapis.com/fcm/send/abc",
        "https://10.0.0.1/send/abc",
        "",
    ],
)
def test_an_endpoint_on_an_unknown_host_is_refused(endpoint: str) -> None:
    client, mock = _client()
    try:
        response = client.post(_SUBSCRIBE, data={**_subscription(), "endpoint": endpoint})
    finally:
        mock.stop()
    assert response.status_code == 422
    assert "push" in response.text
    assert _rows() == []


def test_bad_subscription_keys_are_refused() -> None:
    client, mock = _client()
    try:
        response = client.post(_SUBSCRIBE, data={**_subscription(), "auth": "c2hvcnQ"})
    finally:
        mock.stop()
    assert response.status_code == 422
    assert "auth" in response.text
    assert _rows() == []


def test_subscribe_needs_the_csrf_token() -> None:
    client, mock = _client()
    client.headers.pop("X-CSRF-Token")
    client.cookies.delete(CSRF_COOKIE_NAME)
    try:
        response = client.post(_SUBSCRIBE, data=_subscription())
    finally:
        mock.stop()
    assert response.status_code == 403
    assert _rows() == []


# ---------------------------------------------------------------------------
# See and remove
# ---------------------------------------------------------------------------


def test_a_person_sees_only_their_own_subscriptions() -> None:
    other, mock_other = _client(sub=_OTHER)
    me, mock_me = _client()
    try:
        other_id = _ids_of(other, _subscription("theirs"))
        my_id = _ids_of(me, _subscription("mine"))
        body = me.get(_LIST).text
    finally:
        mock_other.stop()
        mock_me.stop()
    assert f'data-push-row="{my_id}"' in body
    assert str(other_id) not in body
    assert "All browsers in this tenant" not in body


def test_a_person_removes_their_own_but_not_another_persons() -> None:
    other, mock_other = _client(sub=_OTHER)
    me, mock_me = _client()
    try:
        other_id = _ids_of(other, _subscription("theirs"))
        my_id = _ids_of(me, _subscription("mine"))
        refused = me.post(_REMOVE, data={"subscription_id": str(other_id)})
        bad_id = me.post(_REMOVE, data={"subscription_id": "not-a-uuid"})
        removed = me.post(_REMOVE, data={"subscription_id": str(my_id)})
    finally:
        mock_other.stop()
        mock_me.stop()
    assert refused.status_code == 404
    assert bad_id.status_code == 404
    assert removed.status_code == 200, removed.text
    assert [r.id for r in _rows()] == [other_id]


def test_a_tenant_admin_sees_and_removes_any_subscription_of_the_tenant() -> None:
    other, mock_other = _client(sub=_OTHER)
    admin, mock_admin = _client(sub="op-admin", role=TenantRole.TENANT_ADMIN)
    try:
        other_id = _ids_of(other, _subscription("theirs"))
        listing = admin.get(_LIST).text
        removed = admin.post(_REMOVE, data={"subscription_id": str(other_id)})
    finally:
        mock_other.stop()
        mock_admin.stop()
    assert f'data-push-row="{other_id}"' in listing
    assert _OTHER in listing
    assert removed.status_code == 200, removed.text
    assert _rows() == []


def test_a_tenant_admin_cannot_remove_a_subscription_of_another_tenant() -> None:
    other, mock_other = _client(sub=_OTHER, tenant_id=_TENANT_B)
    admin, mock_admin = _client(sub="op-admin", role=TenantRole.TENANT_ADMIN)
    try:
        other_id = _ids_of(other, _subscription("theirs"))
        listing = admin.get(_LIST).text
        refused = admin.post(_REMOVE, data={"subscription_id": str(other_id)})
    finally:
        mock_other.stop()
        mock_admin.stop()
    assert str(other_id) not in listing
    assert refused.status_code == 404
    assert [r.id for r in _rows()] == [other_id]


# ---------------------------------------------------------------------------
# Does MEHO have this browser for me?
# ---------------------------------------------------------------------------


def test_status_is_on_only_for_the_person_who_saved_the_browser() -> None:
    sub = _subscription()
    me, mock_me = _client()
    other, mock_other = _client(sub=_OTHER)
    try:
        before = me.post(_STATUS, data={"endpoint": sub["endpoint"]})
        assert me.post(_SUBSCRIBE, data=sub).status_code == 200
        mine = me.post(_STATUS, data={"endpoint": sub["endpoint"]})
        theirs = other.post(_STATUS, data={"endpoint": sub["endpoint"]})
    finally:
        mock_me.stop()
        mock_other.stop()
    assert before.status_code == 200, before.text
    assert before.json() == {"on": False}
    assert mine.json() == {"on": True}
    assert theirs.json() == {"on": False}


def test_status_is_off_after_the_row_is_removed() -> None:
    sub = _subscription()
    me, mock = _client()
    try:
        my_id = _ids_of(me, sub)
        assert me.post(_REMOVE, data={"subscription_id": str(my_id)}).status_code == 200
        after = me.post(_STATUS, data={"endpoint": sub["endpoint"]})
    finally:
        mock.stop()
    assert after.json() == {"on": False}


def test_status_needs_the_csrf_token() -> None:
    client, mock = _client()
    client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(_STATUS, data={"endpoint": _subscription()["endpoint"]})
    finally:
        mock.stop()
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Secrets never in logs or answers
# ---------------------------------------------------------------------------


def test_keys_and_endpoint_never_reach_a_log_or_an_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sub = _subscription()
    answers: list[Any] = []
    with capture_logs() as captured:
        client, mock = _client()
        try:
            answers.append(client.get("/ui/account"))
            answers.append(client.post(_SUBSCRIBE, data=sub))
            answers.append(
                client.post(_SUBSCRIBE, data={**sub, "endpoint": "https://push.example/x"})
            )
            answers.append(client.get(_LIST))
            answers.append(client.post(_UNSUBSCRIBE, data={"endpoint": sub["endpoint"]}))
        finally:
            mock.stop()
    assert any(e["event"] == "ui_push_subscription_saved" for e in captured)
    logs = repr(captured) + "\n".join(r.getMessage() for r in caplog.records)
    texts = [logs] + [a.text + repr(dict(a.headers)) for a in answers]
    for text in texts:
        assert _PRIVATE_KEY not in text
        assert sub["endpoint"] not in text
        assert "push.example/x" not in text
        assert sub["p256dh"] not in text
        assert sub["auth"] not in text
    assert public_key_for(_PRIVATE_KEY) not in logs


def test_settings_repr_hides_the_private_key() -> None:
    assert _PRIVATE_KEY not in repr(get_settings())


def test_a_failed_write_logs_no_endpoint_and_no_key(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A database error quotes the statement's values; none of them may reach
    the log written by the real logging setup and request middleware."""
    sub = _subscription()

    async def _refused(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError(
            "INSERT INTO push_subscription (endpoint, p256dh, auth) VALUES (?, ?, ?)",
            (sub["endpoint"], sub["p256dh"], sub["auth"]),
            Exception("UNIQUE constraint failed: push_subscription.endpoint"),
        )

    monkeypatch.setattr(store_module, "_upsert", _refused)
    configure_logging()
    app = _app()
    app.add_middleware(RequestContextMiddleware)
    client, mock = _client(app=app)
    try:
        response = client.post(_SUBSCRIBE, data=sub)
    finally:
        mock.stop()
    out, err = capfd.readouterr()
    assert response.status_code == 503
    assert "Try again" in response.text
    assert "ui_push_subscription_save_failed" in out
    for text in (out, err, response.text):
        assert sub["endpoint"] not in text
        assert sub["p256dh"] not in text
        assert sub["auth"] not in text
    assert _rows() == []


# ---------------------------------------------------------------------------
# The login facts of the person, read from the login
# ---------------------------------------------------------------------------


def test_switch_on_stores_the_login_facts_of_a_tenant_admin() -> None:
    client, mock = _client(role=TenantRole.TENANT_ADMIN)
    try:
        assert client.post(_SUBSCRIBE, data=_subscription()).status_code == 200
    finally:
        mock.stop()
    (row,) = _rows()
    assert row.tenant_role == "tenant_admin"
    assert row.facts_read_at is not None


def test_switch_on_stores_the_role_of_an_operator_and_none_for_an_unknown_login() -> None:
    operator, mock_op = _client(role=TenantRole.OPERATOR)
    unknown, mock_unknown = _client(sub=_OTHER)
    try:
        assert operator.post(_SUBSCRIBE, data=_subscription("op")).status_code == 200
        assert unknown.post(_SUBSCRIBE, data=_subscription("unknown")).status_code == 200
    finally:
        mock_op.stop()
        mock_unknown.stop()
    assert sorted((row.operator_sub, row.tenant_role) for row in _rows()) == [
        (_OTHER, None),
        (_ME, "operator"),
    ]


def _store_row(*, endpoint: str, tenant_id: uuid.UUID, role: str | None) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await save_subscription(
                session,
                operator_sub=_ME,
                tenant_id=tenant_id,
                subscription=check_subscription(**{**_subscription(), "endpoint": endpoint}),
                facts=LoginFacts(tenant_role=role),
            )

    asyncio.run(_do())


def test_status_reads_the_login_facts_again() -> None:
    """A login group that changed counts from the next account page open,
    for the rows of the current tenant only."""
    here = "https://fcm.googleapis.com/fcm/send/here"
    there = "https://fcm.googleapis.com/fcm/send/there"
    _store_row(endpoint=here, tenant_id=_TENANT_A, role="tenant_admin")
    _store_row(endpoint=there, tenant_id=_TENANT_B, role="tenant_admin")
    client, mock = _client(role=TenantRole.OPERATOR)
    try:
        response = client.post(_STATUS, data={"endpoint": here})
    finally:
        mock.stop()
    assert response.json() == {"on": True}
    roles = {row.endpoint: row.tenant_role for row in _rows()}
    assert roles == {here: "operator", there: "tenant_admin"}

    clear_jwks_cache()  # the next login is signed with a new key
    admin, mock_admin = _client(role=TenantRole.TENANT_ADMIN)
    try:
        assert admin.post(_STATUS, data={"endpoint": here}).json() == {"on": True}
    finally:
        mock_admin.stop()
    assert {row.endpoint: row.tenant_role for row in _rows()}[here] == "tenant_admin"


def test_status_keeps_the_facts_when_the_login_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    here = "https://fcm.googleapis.com/fcm/send/here"
    _store_row(endpoint=here, tenant_id=_TENANT_A, role="tenant_admin")

    async def _no_login(_: object) -> None:
        return None

    monkeypatch.setattr(account_push, "resolve_ui_operator", _no_login)
    client, mock = _client(role=TenantRole.OPERATOR)
    try:
        response = client.post(_STATUS, data={"endpoint": here})
    finally:
        mock.stop()
    assert response.json() == {"on": True}
    assert [row.tenant_role for row in _rows()] == ["tenant_admin"]


def test_status_off_writes_nothing() -> None:
    _store_row(
        endpoint="https://fcm.googleapis.com/fcm/send/mine",
        tenant_id=_TENANT_A,
        role="tenant_admin",
    )
    client, mock = _client(role=TenantRole.OPERATOR)
    try:
        response = client.post(
            _STATUS, data={"endpoint": "https://fcm.googleapis.com/fcm/send/not-mine"}
        )
    finally:
        mock.stop()
    assert response.json() == {"on": False}
    assert [row.tenant_role for row in _rows()] == ["tenant_admin"]
