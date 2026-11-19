# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's approval sign-in routes (#6282).

``/ui/auth/approval-sign-in`` sends a signed-in person to Keycloak with the
console's approval client ``meho-web-approve``, PKCE, ``prompt=login``,
``max_age=0`` and ``acr_values=meho-approve``. ``/ui/auth/approval-callback``
exchanges the code with that client and keeps the token on the session row,
only when it is the same person and the approval client. The normal console
login is unchanged: it still uses ``meho-web`` and no ``acr_values``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import respx

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import flow as oauth_flow
from meho_backplane.ui.auth.approval_token_store import approval_operator
from meho_backplane.ui.auth.routes import SESSION_COOKIE_NAME
from tests.conftest import DEFAULT_AUDIENCE, make_rsa_keypair, mint_token, public_jwks
from tests.test_ui_auth_flow import (
    _AUTHORIZATION_ENDPOINT,
    _bff_env,
    _https_client,
    _mock_oidc_metadata,
    _patch_oauth_token_transport,
)

__all__ = ["_bff_env"]

_CALLBACK = "https://meho.test/ui/auth/approval-callback"


def _token_body(access_token: str, *, refresh: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {"access_token": access_token, "expires_in": 900, "token_type": "Bearer"}
    if refresh:
        body["refresh_token"] = "refresh-token-value"
    return body


def _approval_token(key: Any, *, sub: str = "op-42", azp: str = "meho-web-approve") -> str:
    return mint_token(
        key,
        sub=sub,
        audience=DEFAULT_AUDIENCE,
        extra_claims={
            "azp": azp,
            "acr": "meho-approve",
            "auth_time": int(time.time()) - 5,
            "sid": "kc-sid-approve",
        },
    )


def _patch_token_answers(
    monkeypatch: pytest.MonkeyPatch, answers: list[dict[str, Any]]
) -> list[httpx2.Request]:
    """Answer each token request with the next body of *answers*."""
    calls: list[httpx2.Request] = []

    async def _handle(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json=answers[len(calls) - 1])

    class _TestOAuth2Client(oauth_flow.AsyncOAuth2Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx2.MockTransport(_handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(oauth_flow, "AsyncOAuth2Client", _TestOAuth2Client)
    return calls


def _query(location: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(location).query)


def _approval_round_trip(
    monkeypatch: pytest.MonkeyPatch, approval_access: str, key: Any
) -> tuple[Any, uuid.UUID, dict[str, list[str]], Any]:
    """Log in to the console, then run the approval sign-in.

    Returns ``(client, session id, the approval authorize query, the callback response)``.
    """
    login_access = mint_token(key, sub="op-42", audience=DEFAULT_AUDIENCE)
    client = _https_client()
    answers = [_token_body(login_access), _token_body(approval_access, refresh=False)]
    with respx.mock(assert_all_called=False) as router:
        _mock_oidc_metadata(router, jwks=public_jwks(key))
        calls = _patch_token_answers(monkeypatch, answers)
        login = client.get("/ui/auth/login?return_to=/ui/sentinel")
        state = _query(login.headers["location"])["state"][0]
        client.get(f"/ui/auth/callback?code=login-code&state={state}")
        session_id = uuid.UUID(client.cookies[SESSION_COOKIE_NAME])
        start = client.get("/ui/auth/approval-sign-in?return_to=/ui/approvals")
        assert start.status_code == 302, start.text
        assert start.headers["location"].startswith(_AUTHORIZATION_ENDPOINT)
        query = _query(start.headers["location"])
        callback = client.get(
            f"/ui/auth/approval-callback?code=approval-code&state={query['state'][0]}"
        )
    assert len(calls) == 2, "the approval code was not exchanged"
    assert parse_qs(calls[1].content.decode())["redirect_uri"] == [_CALLBACK]
    return client, session_id, query, callback


async def _stored(session_id: uuid.UUID) -> bytes | None:
    async with get_sessionmaker()() as db_session:
        row = await db_session.get(WebSession, session_id)
        assert row is not None
        return row.approval_token


def test_the_approval_sign_in_uses_the_approval_client_and_keeps_its_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = make_rsa_keypair("test-kid")
    approval_access = _approval_token(key)

    _client, session_id, query, callback = _approval_round_trip(monkeypatch, approval_access, key)

    assert query["client_id"] == ["meho-web-approve"]
    assert query["acr_values"] == ["meho-approve"]
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["0"]
    assert query["redirect_uri"] == [_CALLBACK]
    assert query["code_challenge_method"] == ["S256"]
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == "/ui/approvals"
    stored = asyncio.run(_stored(session_id))
    assert stored is not None
    assert approval_access.encode() not in stored

    async def _proof() -> Any:
        with respx.mock(assert_all_called=False) as router:
            _mock_oidc_metadata(router, jwks=public_jwks(key))
            return await approval_operator(session_id)

    operator = asyncio.run(_proof())
    assert operator is not None
    assert operator.azp == "meho-web-approve"
    assert operator.acr == "meho-approve"


@pytest.mark.parametrize(
    ("sub", "azp"),
    [("someone-else", "meho-web-approve"), ("op-42", "meho-web")],
    ids=["another-person", "another-client"],
)
def test_an_approval_sign_in_of_another_person_or_client_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, sub: str, azp: str
) -> None:
    key = make_rsa_keypair("test-kid")

    _client, session_id, _, callback = _approval_round_trip(
        monkeypatch, _approval_token(key, sub=sub, azp=azp), key
    )

    assert callback.status_code == 403
    assert "approval_sign_in_other_person" in callback.text
    assert asyncio.run(_stored(session_id)) is None


def test_the_approval_sign_in_without_a_session_goes_to_the_normal_login() -> None:
    response = _https_client().get("/ui/auth/approval-sign-in?return_to=/ui/approvals")

    assert response.status_code == 302
    assert response.headers["location"] == "/ui/auth/login?return_to=%2Fui%2Fapprovals"


def test_the_approval_callback_refuses_a_normal_login_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``state`` of the normal login is not an approval sign-in: the usual 400."""
    key = make_rsa_keypair("test-kid")
    client = _https_client()
    with respx.mock(assert_all_called=False) as router:
        _mock_oidc_metadata(router, jwks=public_jwks(key))
        calls = _patch_oauth_token_transport(
            monkeypatch, httpx2.Response(200, json=_token_body(_approval_token(key)))
        )
        login = client.get("/ui/auth/login?return_to=/ui/sentinel")
        state = _query(login.headers["location"])["state"][0]
        response = client.get(f"/ui/auth/approval-callback?code=c&state={state}")
    assert response.status_code in (302, 400)
    assert not calls


def test_the_normal_console_login_is_unchanged() -> None:
    """The normal login keeps the console client and asks for no level."""
    with respx.mock(assert_all_called=False) as router:
        _mock_oidc_metadata(router)
        response = _https_client().get("/ui/auth/login?return_to=/ui/approvals")
    query = _query(response.headers["location"])
    assert query["client_id"] == ["meho-web"]
    assert "acr_values" not in query
    assert "prompt" not in query


def test_an_empty_approval_client_setting_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    key = make_rsa_keypair("test-kid")
    login_access = mint_token(key, sub="op-42", audience=DEFAULT_AUDIENCE)
    client = _https_client()
    with respx.mock(assert_all_called=False) as router:
        _mock_oidc_metadata(router, jwks=public_jwks(key))
        _patch_oauth_token_transport(
            monkeypatch, httpx2.Response(200, json=_token_body(login_access))
        )
        login = client.get("/ui/auth/login?return_to=/ui/sentinel")
        state = _query(login.headers["location"])["state"][0]
        client.get(f"/ui/auth/callback?code=login-code&state={state}")
        monkeypatch.setenv("UI_KEYCLOAK_APPROVE_CLIENT_ID", "")
        get_settings.cache_clear()
        response = client.get("/ui/auth/approval-sign-in?return_to=/ui/approvals")
    assert response.status_code == 503
    assert "UI_KEYCLOAK_APPROVE_CLIENT_ID" in response.text
