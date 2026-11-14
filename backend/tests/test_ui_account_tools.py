# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for "Your tools" on the account page, and "Sign out this tool".

A fake Keycloak admin API (respx) keeps the logins of each person and
answers like Keycloak 26 does for clients that ask for no consent (the MEHO
login clients):

* ``DELETE /users/{user id}/consents/{client id}`` ends only the offline
  login of that pair (204), or answers 404 when there is none. It leaves
  ordinary logins alone.
* ``GET /users/{user id}/sessions`` lists the person's ordinary logins,
  each with its ``clients`` map; ``DELETE /sessions/{id}`` ends one whole
  login, with every client in it.

:meth:`_FakeKeycloak.can_renew` says whether a refresh token of a
(person, client) pair still works: while an offline login of the pair, or
an ordinary login holding the client, is left. The live Keycloak test
``tests/integration/test_keycloak_tool_sign_out.py`` checks the same rules
against a real Keycloak in CI.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.tool_sign_out import TOOL_SIGN_OUT_AUDIT_PATH, tool_kinds
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditEventClass, AuditLog, Tenant
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
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir, static_src_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.account_tools import (
    FAILED_TEXT,
    FORBIDDEN_TEXT,
    NOT_CONFIGURED_TEXT,
    NOTHING_TO_SIGN_OUT_TEXT,
    PARTLY_SIGNED_OUT_TEXT,
    SHARED_KEPT_TEXT,
    SHARED_ONLY_TEXT,
    SIGNED_OUT_TEXT,
)
from meho_backplane.ui.templating import reset_templating_for_testing

_ISSUER = "https://keycloak.test/realms/meho"
_ADMIN_URL = "https://keycloak.test/admin/realms/meho"
_TOKEN_URL = f"{_ISSUER}/protocol/openid-connect/token"
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ME = "5d0c1a52-0000-4000-8000-00000000aaaa"
_OTHER = "5d0c1a52-0000-4000-8000-00000000bbbb"
_PAGE = "/ui/account"
_SIGN_OUT = "/ui/account/tools/sign-out"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "meho-cli")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", _ADMIN_URL)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "true")
    _reset()
    yield
    _reset()


def _reset() -> None:
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "false")
    get_settings.cache_clear()


def _app() -> FastAPI:
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


def _client_for(operator_sub: str) -> tuple[TestClient, str]:
    """Seed a tenant and a web session; return a client and its CSRF token."""

    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            if await session.get(Tenant, _TENANT) is None:
                session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=_TENANT,
                access_token="not-a-real-jwt",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    session_id = asyncio.run(_do())
    client = TestClient(_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return client, csrf


def _headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "HX-Request": "true"}


def _seed_audit(operator_sub: str, azp: str, occurred_at: datetime) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    event_class=AuditEventClass.OPERATION_EXECUTION,
                    occurred_at=occurred_at,
                    operator_sub=operator_sub,
                    tenant_id=_TENANT,
                    method="POST",
                    path="/mcp",
                    status_code=200,
                    payload={"client": {"azp": azp, "channel": "token"}},
                )
            )

    asyncio.run(_do())


def _sign_out_rows() -> list[AuditLog]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.path == TOOL_SIGN_OUT_AUDIT_PATH)
            )
            return list(result.scalars())

    return asyncio.run(_do())


def _assert_one_failed_row(error: str, *, outcome: str = "failed") -> dict[str, Any]:
    rows = _sign_out_rows()
    assert len(rows) == 1
    assert rows[0].status_code == 502
    assert rows[0].payload["outcome"] == outcome
    assert rows[0].payload["error"] == error
    return dict(rows[0].payload)


class _FakeKeycloak:
    """The logins of each person, served like the Keycloak 26 admin API.

    *logins*: one ordinary login per (user id, client id) pair. *offline*:
    the offline logins. *shared*: ordinary logins that hold several clients
    of one person.
    """

    def __init__(
        self,
        router: respx.MockRouter,
        logins: Iterable[tuple[str, str]] = (),
        *,
        offline: Iterable[tuple[str, str]] = (),
        shared: list[tuple[str, set[str]]] | None = None,
    ) -> None:
        self.offline = set(offline)
        self.sessions: dict[str, tuple[str, set[str]]] = {}
        for user_id, client_id in logins:
            self.sessions[f"s-{len(self.sessions)}"] = (user_id, {client_id})
        for user_id, client_ids in shared or []:
            self.sessions[f"s-{len(self.sessions)}"] = (user_id, set(client_ids))
        self.deletes: list[tuple[str, str]] = []
        self.session_deletes: list[str] = []
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.delete(url__regex=rf"^{_ADMIN_URL}/users/[^/]+/consents/[^/]+$").mock(
            side_effect=self._delete_consent
        )
        router.get(url__regex=rf"^{_ADMIN_URL}/users/[^/]+/sessions$").mock(
            side_effect=self._list_sessions
        )
        router.delete(url__regex=rf"^{_ADMIN_URL}/sessions/[^/]+$").mock(
            side_effect=self._delete_session
        )

    def can_renew(self, user_id: str, client_id: str) -> bool:
        if (user_id, client_id) in self.offline:
            return True
        return any(u == user_id and client_id in c for u, c in self.sessions.values())

    def _delete_consent(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.raw_path.decode().split("/")
        user_id, client_id = unquote(parts[-3]), unquote(parts[-1])
        self.deletes.append((user_id, client_id))
        if (user_id, client_id) not in self.offline:
            return httpx.Response(404, json={"error": "Consent nor offline token not found"})
        self.offline.discard((user_id, client_id))
        return httpx.Response(204)

    def _list_sessions(self, request: httpx.Request) -> httpx.Response:
        user_id = unquote(request.url.raw_path.decode().split("/")[-2])
        body = [
            {"id": sid, "userId": u, "clients": {f"uuid-{c}": c for c in sorted(clients)}}
            for sid, (u, clients) in self.sessions.items()
            if u == user_id
        ]
        return httpx.Response(200, json=body)

    def _delete_session(self, request: httpx.Request) -> httpx.Response:
        session_id = unquote(request.url.raw_path.decode().split("/")[-1])
        self.session_deletes.append(session_id)
        if self.sessions.pop(session_id, None) is None:
            return httpx.Response(404, json={"error": "Sesssion not found"})
        return httpx.Response(204)


def _post(client: TestClient, csrf: str, tool: str, **extra: str) -> Any:
    return client.post(_SIGN_OUT, data={"tool": tool, **extra}, headers=_headers(csrf))


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_switch_off_hides_the_section_and_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, {(_ME, "meho-mcp-desktop")})
        page = client.get(_PAGE)
        response = _post(client, csrf, "claude-desktop")
    assert page.status_code == 200, page.text
    assert "Your tools" not in page.text
    assert 'data-field="your-tools"' not in page.text
    assert response.status_code == 404
    assert kc.deletes == []
    assert kc.session_deletes == []
    assert _sign_out_rows() == []


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_section_lists_tool_kinds_with_last_use() -> None:
    now = datetime.now(UTC)
    recent = (now - timedelta(days=1)).replace(microsecond=0)
    _seed_audit(_ME, "meho-mcp-code", recent - timedelta(hours=2))
    _seed_audit(_ME, "meho-mcp-code", recent)
    _seed_audit(_ME, "meho-cli", now - timedelta(days=120))  # too old
    _seed_audit(_OTHER, "meho-mcp-desktop", recent)  # another person
    client, _csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False):
        page = client.get(_PAGE)
    assert page.status_code == 200, page.text
    body = page.text
    assert "Your tools" in body
    for key in ("web", "cli", "claude-code", "claude-desktop", "other-mcp"):
        assert f'data-tool-row="{key}"' in body
    code_row = body.split('data-tool-row="claude-code"', 1)[1].split("data-tool-row=", 1)[0]
    assert recent.strftime("%Y-%m-%d %H:%M UTC") in code_row
    desktop_row = body.split('data-tool-row="claude-desktop"', 1)[1].split("data-tool-row=", 1)[0]
    assert "none in the last 90 days" in desktop_row
    cli_row = body.split('data-tool-row="cli"', 1)[1].split("data-tool-row=", 1)[0]
    assert "none in the last 90 days" in cli_row
    assert "access token lifetime" in body
    assert "not a" in body and "block" in body


def test_web_console_row_has_no_sign_out_button() -> None:
    client, _csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False):
        body = client.get(_PAGE).text
    web_row = body.split('data-tool-row="web"', 1)[1].split("data-tool-row=", 1)[0]
    assert "Sign out this tool" not in web_row
    assert "Active sessions" in web_row


def test_card_has_no_inline_script() -> None:
    """The confirm box opens and closes through the shared modal script."""
    client, _csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False):
        body = client.get(_PAGE).text
    card = body.split('id="account-tools"', 1)[1].split('data-tool-row="other-mcp"', 1)[1]
    card = card.split("</ul>", 1)[0]
    assert "onclick" not in card
    assert 'data-open-dialog="account-tool-modal-other-mcp"' in card
    assert "data-close-dialog" in card
    script = (static_src_dir() / "app" / "modal-dialogs.js").read_text(encoding="utf-8")
    assert "[data-open-dialog]" in script
    assert "[data-close-dialog]" in script


def test_tool_kinds_skip_unset_and_repeated_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-mcp")
    get_settings.cache_clear()
    kinds = tool_kinds(get_settings())
    assert [k.key for k in kinds] == ["web", "claude-code", "claude-desktop"]
    assert [k.client_id for k in kinds] == ["meho-mcp", "meho-mcp-code", "meho-mcp-desktop"]


# ---------------------------------------------------------------------------
# Sign out one tool
# ---------------------------------------------------------------------------


def test_sign_out_desktop_keeps_the_other_tools() -> None:
    """Desktop of one person stops renewing; their Code and CLI, and the other person, do not.

    Both kinds of login: ordinary and offline.
    """
    client, csrf = _client_for(_ME)
    pairs = {
        (_ME, "meho-mcp-desktop"),
        (_ME, "meho-mcp-code"),
        (_ME, "meho-cli"),
        (_OTHER, "meho-mcp-desktop"),
    }
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, pairs, offline=pairs)
        response = _post(client, csrf, "claude-desktop")
    assert response.status_code == 200, response.text
    assert kc.deletes == [(_ME, "meho-mcp-desktop")]
    assert not kc.can_renew(_ME, "meho-mcp-desktop")
    assert kc.can_renew(_ME, "meho-mcp-code")
    assert kc.can_renew(_ME, "meho-cli")
    assert kc.can_renew(_OTHER, "meho-mcp-desktop")
    assert 'data-result="success"' in response.text
    assert SIGNED_OUT_TEXT.format(label="Claude Desktop") in html.unescape(response.text)
    assert "about five minutes" in html.unescape(response.text)
    rows = _sign_out_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == _ME
    assert row.tenant_id == _TENANT
    assert row.status_code == 200
    assert row.payload["outcome"] == "signed_out"
    assert row.payload["client_id"] == "meho-mcp-desktop"
    assert row.payload["tool"] == "claude-desktop"
    assert row.payload["offline_login_ended"] is True
    assert row.payload["sessions_ended"] == 1
    assert row.payload["shared_with"] == []
    assert "made-up-admin-secret" not in str(row.payload)
    assert "admin-tok" not in str(row.payload)


def test_sign_out_uses_the_session_person_not_a_form_field() -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, {(_ME, "meho-cli"), (_OTHER, "meho-cli")})
        response = _post(client, csrf, "cli", operator_sub=_OTHER, user_id=_OTHER)
    assert response.status_code == 200, response.text
    assert kc.deletes == [(_ME, "meho-cli")]
    assert not kc.can_renew(_ME, "meho-cli")
    assert kc.can_renew(_OTHER, "meho-cli")


@pytest.mark.parametrize("tool", ["", "web", "unknown", "meho-mcp-desktop", "../users"])
def test_unknown_or_web_tool_is_404(tool: str) -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, {(_ME, "meho-mcp-desktop")})
        response = _post(client, csrf, tool)
    assert response.status_code == 404
    assert kc.deletes == []
    assert kc.session_deletes == []
    assert _sign_out_rows() == []


def test_sign_out_requires_csrf() -> None:
    client, _csrf = _client_for(_ME)
    client.cookies.delete(CSRF_COOKIE_NAME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, {(_ME, "meho-mcp-desktop")})
        response = client.post(_SIGN_OUT, data={"tool": "claude-desktop"})
    assert response.status_code == 403
    assert kc.deletes == []


def test_nothing_to_sign_out_writes_no_row() -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, set())
        response = _post(client, csrf, "claude-code")
    assert response.status_code == 200, response.text
    assert kc.deletes == [(_ME, "meho-mcp-code")]
    assert 'data-result="info"' in response.text
    assert NOTHING_TO_SIGN_OUT_TEXT.format(label="Claude Code") in html.unescape(response.text)
    assert _sign_out_rows() == []


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(500), httpx.ConnectError("refused")],
    ids=["server-error", "unreachable"],
)
def test_failed_keycloak_call_shows_an_error_and_writes_a_failed_row(answer: Any) -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.get(url__regex=r"/sessions$").mock(return_value=httpx.Response(200, json=[]))
        route = router.delete(url__regex=r"/consents/").mock(side_effect=[answer])
        response = _post(client, csrf, "claude-desktop")
    assert route.call_count == 1
    assert response.status_code == 200, response.text
    assert 'data-result="error"' in response.text
    assert FAILED_TEXT.format(label="Claude Desktop") in html.unescape(response.text)
    assert "signed out." not in response.text
    payload = _assert_one_failed_row("KeycloakAdminError")
    assert payload["offline_login_ended"] is False
    assert payload["sessions_ended"] == 0


def test_forbidden_answer_names_the_missing_right() -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.get(url__regex=r"/sessions$").mock(return_value=httpx.Response(200, json=[]))
        router.delete(url__regex=r"/consents/").mock(return_value=httpx.Response(403))
        response = _post(client, csrf, "claude-desktop")
    assert 'data-result="error"' in response.text
    assert FORBIDDEN_TEXT.format(label="Claude Desktop") in html.unescape(response.text)
    assert "manage-users" in html.unescape(response.text)
    _assert_one_failed_row("KeycloakForbiddenError")


def test_failed_admin_login_shows_an_error_and_writes_a_failed_row() -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        router.post(_TOKEN_URL).mock(return_value=httpx.Response(401))
        route = router.delete(url__regex=r"/consents/").mock(return_value=httpx.Response(204))
        response = _post(client, csrf, "claude-desktop")
    assert route.call_count == 0
    assert 'data-result="error"' in response.text
    assert FAILED_TEXT.format(label="Claude Desktop") in html.unescape(response.text)
    assert "made-up-admin-secret" not in response.text
    _assert_one_failed_row("KeycloakAdminError")


def test_admin_client_not_set_up_shows_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "")
    get_settings.cache_clear()
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False):
        response = _post(client, csrf, "claude-desktop")
    assert response.status_code == 200, response.text
    assert 'data-result="error"' in response.text
    assert NOT_CONFIGURED_TEXT in html.unescape(response.text)
    _assert_one_failed_row("KeycloakAdminNotConfiguredError")


# ---------------------------------------------------------------------------
# Ordinary logins, and logins shared with another tool
# ---------------------------------------------------------------------------


def test_sign_out_ends_an_ordinary_login_without_offline() -> None:
    """A login without offline access (``meho login --offline=false``) ends too."""
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, {(_ME, "meho-cli"), (_ME, "meho-mcp-code")})
        response = _post(client, csrf, "cli")
    assert response.status_code == 200, response.text
    assert 'data-result="success"' in response.text
    assert SIGNED_OUT_TEXT.format(label="Command line (meho)") in html.unescape(response.text)
    assert not kc.can_renew(_ME, "meho-cli")
    assert kc.can_renew(_ME, "meho-mcp-code")
    rows = _sign_out_rows()
    assert len(rows) == 1
    assert rows[0].payload["offline_login_ended"] is False
    assert rows[0].payload["sessions_ended"] == 1


def test_shared_login_is_kept_and_named() -> None:
    """A login shared with the command line is kept; the line says so."""
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(
            router,
            {(_ME, "meho-mcp-desktop")},
            offline={(_ME, "meho-mcp-desktop")},
            shared=[(_ME, {"meho-mcp-desktop", "meho-cli"})],
        )
        response = _post(client, csrf, "claude-desktop")
    assert response.status_code == 200, response.text
    assert 'data-result="success"' in response.text
    assert SHARED_KEPT_TEXT.format(
        label="Claude Desktop", others="Command line (meho)"
    ) in html.unescape(response.text)
    assert kc.can_renew(_ME, "meho-cli")
    assert len(kc.session_deletes) == 1
    rows = _sign_out_rows()
    assert len(rows) == 1
    assert rows[0].payload["shared_with"] == ["meho-cli"]


def test_only_a_shared_login_changes_nothing() -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        kc = _FakeKeycloak(router, shared=[(_ME, {"meho-mcp-code", "meho-web"})])
        response = _post(client, csrf, "claude-code")
    assert response.status_code == 200, response.text
    assert 'data-result="info"' in response.text
    assert SHARED_ONLY_TEXT.format(label="Claude Code", others="Web console") in html.unescape(
        response.text
    )
    assert kc.session_deletes == []
    assert kc.can_renew(_ME, "meho-mcp-code")
    assert kc.can_renew(_ME, "meho-web")
    assert _sign_out_rows() == []


def test_failed_session_list_changes_nothing_and_writes_a_failed_row() -> None:
    """The session list is read before anything ends, so its failure changes nothing."""
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.get(url__regex=r"/sessions$").mock(return_value=httpx.Response(500))
        consent = router.delete(url__regex=r"/consents/").mock(return_value=httpx.Response(204))
        response = _post(client, csrf, "claude-desktop")
    assert consent.call_count == 0
    assert 'data-result="error"' in response.text
    _assert_one_failed_row("KeycloakAdminError")


def test_forbidden_session_delete_names_the_missing_right() -> None:
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.get(url__regex=r"/sessions$").mock(
            return_value=httpx.Response(
                200, json=[{"id": "s-1", "clients": {"u": "meho-mcp-desktop"}}]
            )
        )
        router.delete(url__regex=r"/consents/").mock(return_value=httpx.Response(404))
        router.delete(url__regex=r"/sessions/").mock(return_value=httpx.Response(403))
        response = _post(client, csrf, "claude-desktop")
    assert 'data-result="error"' in response.text
    assert FORBIDDEN_TEXT.format(label="Claude Desktop") in html.unescape(response.text)
    _assert_one_failed_row("KeycloakForbiddenError")


@pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(403)], ids=str)
def test_failed_session_delete_after_offline_ended_says_partly(answer: Any) -> None:
    """The offline login ended, then a session delete failed: the row says what ended."""
    client, csrf = _client_for(_ME)
    with respx.mock(assert_all_called=False) as router:
        router.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.get(url__regex=r"/sessions$").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "s-1", "clients": {"u": "meho-mcp-desktop"}},
                    {"id": "s-2", "clients": {"u": "meho-mcp-desktop"}},
                ],
            )
        )
        router.delete(url__regex=r"/consents/").mock(return_value=httpx.Response(204))
        router.delete(url__regex=r"/sessions/").mock(side_effect=[httpx.Response(204), answer])
        response = _post(client, csrf, "claude-desktop")
    assert 'data-result="error"' in response.text
    assert PARTLY_SIGNED_OUT_TEXT.format(label="Claude Desktop") in html.unescape(response.text)
    assert "could not sign out" not in response.text
    error = "KeycloakForbiddenError" if answer.status_code == 403 else "KeycloakAdminError"
    payload = _assert_one_failed_row(error, outcome="partly_signed_out")
    assert payload["offline_login_ended"] is True
    assert payload["sessions_ended"] == 1
