# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request-level coverage of the console page of the admin block list."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from meho_backplane.api.v1 import principal_blocks as rest_principal_blocks
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth import principal_block
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    PRINCIPAL_BLOCKED_MESSAGE,
    PrincipalBlockUnavailableError,
    reset_block_cache,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, PrincipalBlock
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware, revalidation
from meho_backplane.ui.auth.flow import TokenExchangeResult
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.auth.revalidation import mark_session_validated
from meho_backplane.ui.auth.routes import _persist_session_from_tokens
from meho_backplane.ui.auth.session_store import rotate_refresh
from meho_backplane.ui.auth.tenant import save_session_tenant
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes import principal_blocks as ui_principal_blocks
from tests._oidc_jwt_helpers import mint_token, mock_discovery_and_jwks
from tests.test_ui_agent_grants import (
    _OP_A,
    _TENANT_A,
    _TENANT_B,
    _bff_env,
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_session_sync,
    _seed_tenant,
)

_PAGE = "/ui/blocked-people"
_UNBLOCK = f"{_PAGE}/unblock"
_BLOCKED = "person-to-block"


@pytest.fixture(autouse=True)
def _blocks_bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _bff_env.__wrapped__(monkeypatch)
    reset_block_cache()


def _build_app() -> FastAPI:
    """The console routes behind the production middleware, audit innermost."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_ui_router())
    app.include_router(rest_principal_blocks.router)
    return app


def _client(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant: Any = _TENANT_A,
    platform_admin: bool | None = None,
    principal_kind: str | None = None,
    sub: str = _OP_A,
    keys: Any = None,
    extra_claims: dict[str, Any] | None = None,
    fresh_sign_in: bool = False,
) -> Any:
    clear_jwks_cache()
    keypair, jwks = keys or _make_keypair_and_jwks()
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(tenant),
        tenant_role=role.value,
        platform_admin=platform_admin,
        principal_kind=principal_kind,
        extra_claims=extra_claims,
        fresh_sign_in=fresh_sign_in,
    )
    session_id = _seed_session_sync(tenant_id=tenant, access_token=token, operator_sub=sub)
    # What the login does: the session starts with the role of its token.
    mark_session_validated(
        session_id, tenant_id=tenant, tenant_admin=role is TenantRole.TENANT_ADMIN
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    client.session_id = session_id
    client.token = token
    return client, mock, csrf


def _confirmed(sub: str, shown: str = "") -> dict[str, str]:
    """The fields of the confirm page's form."""
    return {"sub": sub, "confirm": "yes", "shown": shown}


def _shown(confirm_page: Any) -> str:
    """The set of shown blocks that the form of a confirm page carries."""
    match = re.search(r'name="shown" value="([0-9a-f]+)"', confirm_page.text)
    assert match is not None, confirm_page.text
    return match.group(1)


def _confirm_form(client: Any, csrf: str, sub: str) -> dict[str, str]:
    """Open the confirm page of *sub* and return the fields of its form."""
    page = client.post(_UNBLOCK, data={"sub": sub}, headers=_csrf_headers(csrf))
    assert page.status_code == 200, page.text
    return _confirmed(sub, _shown(page))


def _seed_blocks(*rows: tuple[str, str, str]) -> None:
    """Insert ``(sub, scope, reason)`` block rows."""

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for sub, scope, reason in rows:
                session.add(
                    PrincipalBlock(
                        sub=sub,
                        scope=scope,
                        tenant_id=None if scope == "*" else uuid.UUID(scope),
                        reason=reason,
                        blocked_by_sub="someone",
                    )
                )

    asyncio.run(_do())


def _block_rows() -> list[tuple[str, str, str]]:
    async def _do() -> list[tuple[str, str, str]]:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(PrincipalBlock))).scalars().all()
        return sorted((r.sub, r.scope, r.reason) for r in rows)

    return asyncio.run(_do())


def _audit_payloads(op_id: str) -> list[dict[str, Any]]:
    async def _do() -> list[dict[str, Any]]:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        return [r.payload for r in rows if r.payload.get("op_id") == op_id]

    return asyncio.run(_do())


def _unblock_rows() -> list[dict[str, Any]]:
    """The ``principal.unblock`` rows that name removed blocks (not the request records)."""
    return [p for p in _audit_payloads("principal.unblock") if "unblocked_sub" in p]


def _tenants() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")


def test_tenant_admin_sees_only_the_blocks_of_their_tenant() -> None:
    _tenants()
    _seed_blocks(
        ("in-a", str(_TENANT_A), "reason a"),
        ("in-b", str(_TENANT_B), "reason b"),
        ("everywhere", "*", "reason all"),
    )
    client, mock, _csrf = _client()
    try:
        response = client.get(_PAGE)
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "in-a" in response.text and "reason a" in response.text
    assert "in-b" not in response.text
    assert "everywhere" not in response.text


def test_platform_admin_sees_every_block_and_blocks_in_all_tenants() -> None:
    _tenants()
    _seed_blocks(("in-b", str(_TENANT_B), "reason b"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        page = client.get(_PAGE)
        response = client.post(
            _PAGE, data={"sub": _BLOCKED, "reason": "all tenants"}, headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert "in-b" in page.text and "All tenants" not in page.text
    assert response.status_code == 303, response.text
    assert (_BLOCKED, "*", "all tenants") in _block_rows()


def test_tenant_admin_blocks_and_unblocks_in_their_own_tenant_with_one_audit_row_each() -> None:
    _tenants()
    _seed_blocks(("in-b", str(_TENANT_B), "reason b"))
    client, mock, csrf = _client()
    try:
        blocked = client.post(
            _PAGE,
            data={"sub": f" {_BLOCKED} ", "reason": "typed reason"},
            headers=_csrf_headers(csrf),
        )
        listed = client.get(_PAGE)
        form = _confirm_form(client, csrf, _BLOCKED)
        unblocked = client.post(_UNBLOCK, data=form, headers=_csrf_headers(csrf))
        again = client.post(_UNBLOCK, data=_confirmed(_BLOCKED), headers=_csrf_headers(csrf))
        other_tenant = client.post(_UNBLOCK, data=_confirmed("in-b"), headers=_csrf_headers(csrf))
        other_tenant_page = client.post(_UNBLOCK, data={"sub": "in-b"}, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert blocked.status_code == 303, blocked.text
    assert blocked.headers["location"] == _PAGE
    assert _BLOCKED in listed.text and "typed reason" in listed.text
    assert unblocked.status_code == 303
    assert unblocked.headers["location"] == _PAGE
    assert again.status_code == 404
    assert other_tenant.status_code == 404
    assert other_tenant_page.status_code == 404
    assert _block_rows() == [("in-b", str(_TENANT_B), "reason b")]
    (block_row,) = _audit_payloads("principal.block")
    assert block_row["blocked_sub"] == _BLOCKED and block_row["reason"] == "typed reason"
    (unblock_row,) = _audit_payloads("principal.unblock")
    assert unblock_row["unblocked_sub"] == _BLOCKED


def _request_rows(path: str) -> list[dict[str, Any]]:
    """The request records of the middleware for *path* (not the block rows)."""

    async def _do() -> list[dict[str, Any]]:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        return [
            r.payload
            for r in rows
            if r.method == "POST"
            and r.path == path
            and not str(r.payload.get("op_id", "")).startswith("principal.")
        ]

    return asyncio.run(_do())


def _routes_of(op_id: str) -> list[tuple[str, str]]:
    """The ``(method, path)`` of each audit row of *op_id*."""

    async def _do() -> list[tuple[str, str]]:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        return [(r.method, r.path) for r in rows if r.payload.get("op_id") == op_id]

    return asyncio.run(_do())


def test_a_block_and_an_unblock_each_write_one_row_with_their_name_and_one_request_row() -> None:
    _tenants()
    client, mock, csrf = _client()
    try:
        client.post(_PAGE, data={"sub": _BLOCKED, "reason": "r"}, headers=_csrf_headers(csrf))
        form = _confirm_form(client, csrf, _BLOCKED)
        client.post(_UNBLOCK, data=form, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert len(_audit_payloads("principal.block")) == 1
    assert len(_audit_payloads("principal.unblock")) == 1
    (block_request,) = _request_rows(_PAGE)
    unblock_requests = _request_rows(_UNBLOCK)
    assert len(unblock_requests) == 2
    assert block_request.get("op_id") != "principal.block"
    assert all(r.get("op_id") != "principal.unblock" for r in unblock_requests)


def test_a_page_action_names_the_console_route_in_its_audit_row() -> None:
    _tenants()
    client, mock, csrf = _client()
    try:
        client.post(_PAGE, data={"sub": _BLOCKED, "reason": "r"}, headers=_csrf_headers(csrf))
        form = _confirm_form(client, csrf, _BLOCKED)
        client.post(_UNBLOCK, data=form, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert _routes_of("principal.block") == [("POST", _PAGE)]
    assert _routes_of("principal.unblock") == [("POST", _UNBLOCK)]


def test_the_audit_rows_of_rest_calls_name_the_rest_method_and_path() -> None:
    _tenants()
    client, mock, _csrf = _client()
    headers = {"Authorization": f"Bearer {client.token}"}
    try:
        blocked = client.post(
            "/api/v1/principals/blocked", json={"sub": _BLOCKED, "reason": "r"}, headers=headers
        )
        unblocked = client.delete(f"/api/v1/principals/blocked/{_BLOCKED}", headers=headers)
    finally:
        mock.stop()
    assert blocked.status_code == 201, blocked.text
    assert unblocked.status_code == 200, unblocked.text
    # The request record of a REST call carries the same operation name.
    assert set(_routes_of("principal.block")) == {("POST", "/api/v1/principals/blocked")}
    assert set(_routes_of("principal.unblock")) == {
        ("DELETE", f"/api/v1/principals/blocked/{_BLOCKED}")
    }


def test_a_session_context_in_the_request_body_does_not_change_the_admin_check() -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"), ("in-b", str(_TENANT_B), "reason b"))
    keys = _make_keypair_and_jwks()
    other = mint_token(
        keys[0],
        sub="platform-person",
        tenant_id=str(_TENANT_B),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=True,
    )
    other_session = _seed_session_sync(
        tenant_id=_TENANT_B, access_token=other, operator_sub="platform-person"
    )
    client, mock, _csrf = _client(keys=keys)
    try:
        response = client.request(
            "GET",
            _PAGE,
            json={
                "session_id": str(other_session),
                "operator_sub": "platform-person",
                "tenant_id": str(_TENANT_B),
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "in-a" in response.text
    assert "in-b" not in response.text


def test_unblock_confirm_page_names_the_login_and_its_scope_when_two_tenants_blocked_it() -> None:
    _tenants()
    _seed_blocks(
        ("shared-login", str(_TENANT_A), "reason a"), ("shared-login", str(_TENANT_B), "reason b")
    )
    platform, platform_mock, platform_csrf = _client(platform_admin=True)
    try:
        platform_page = platform.get(_PAGE)
        platform_confirm = platform.post(
            _UNBLOCK, data={"sub": "shared-login"}, headers=_csrf_headers(platform_csrf)
        )
    finally:
        platform_mock.stop()
    tenant, tenant_mock, tenant_csrf = _client()
    try:
        tenant_page = tenant.get(_PAGE)
        tenant_confirm = tenant.post(
            _UNBLOCK, data={"sub": "shared-login"}, headers=_csrf_headers(tenant_csrf)
        )
        tenant_unblock = tenant.post(
            _UNBLOCK,
            data=_confirmed("shared-login", _shown(tenant_confirm)),
            headers=_csrf_headers(tenant_csrf),
        )
    finally:
        tenant_mock.stop()
    assert platform_page.text.count("Unblock in all tenants") == 2
    assert platform_confirm.status_code == 200
    assert "removes the blocks of this login below, in all tenants" in platform_confirm.text
    assert "reason a" in platform_confirm.text and "reason b" in platform_confirm.text
    assert "in all tenants" not in tenant_page.text
    assert tenant_confirm.status_code == 200
    assert "removes the block of this login in this tenant" in tenant_confirm.text
    assert "reason a" in tenant_confirm.text and "reason b" not in tenant_confirm.text
    assert tenant_unblock.status_code == 303
    assert _block_rows() == [("shared-login", str(_TENANT_B), "reason b")]
    platform, platform_mock, platform_csrf = _client(platform_admin=True)
    try:
        form = _confirm_form(platform, platform_csrf, "shared-login")
        platform_unblock = platform.post(_UNBLOCK, data=form, headers=_csrf_headers(platform_csrf))
    finally:
        platform_mock.stop()
    assert platform_unblock.status_code == 303
    assert _block_rows() == []


def test_the_first_click_on_unblock_only_shows_the_confirm_page() -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"), ("in-b", str(_TENANT_B), "reason b"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        first = client.post(_UNBLOCK, data={"sub": "in-a", "csrf_token": csrf})
        rows_after_first = _block_rows()
        audit_after_first = _audit_payloads("principal.unblock")
        confirmed = client.post(
            _UNBLOCK, data={**_confirmed("in-a", _shown(first)), "csrf_token": csrf}
        )
    finally:
        mock.stop()
    assert first.status_code == 200, first.text
    assert 'name="confirm" value="yes"' in first.text
    assert f'name="csrf_token" value="{csrf}"' in first.text
    assert rows_after_first == [
        ("in-a", str(_TENANT_A), "reason a"),
        ("in-b", str(_TENANT_B), "reason b"),
    ]
    assert audit_after_first == []
    assert confirmed.status_code == 303
    assert _block_rows() == [("in-b", str(_TENANT_B), "reason b")]


def test_the_page_never_unblocks_the_admins_own_login() -> None:
    _tenants()
    _seed_blocks((_OP_A, str(_TENANT_B), "own block in b"), ("in-b", str(_TENANT_B), "reason b"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        page = client.get(_PAGE)
        _seed_blocks((_OP_A, str(_TENANT_A), "own block in a"))
        first = client.post(_UNBLOCK, data={"sub": _OP_A}, headers=_csrf_headers(csrf))
        direct = client.post(_UNBLOCK, data=_confirmed(_OP_A), headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert page.status_code == 200, page.text
    assert "data-own-login" in page.text
    assert "meho principal unblock" in page.text
    assert f'name="sub" value="{_OP_A}"' not in page.text
    assert 'name="sub" value="in-b"' in page.text
    assert first.status_code == 403
    assert direct.status_code == 403
    assert direct.json()["detail"].startswith("own_login:")
    assert (_OP_A, str(_TENANT_A), "own block in a") in _block_rows()
    assert (_OP_A, str(_TENANT_B), "own block in b") in _block_rows()
    assert _audit_payloads("principal.unblock") == []


def _own_login_hint(page: str) -> str:
    start = page.index("data-own-login")
    return page[start : page.index("</td>", start)]


def test_the_own_login_hint_names_the_tenant_of_the_block() -> None:
    _tenants()
    _seed_blocks((_OP_A, str(_TENANT_B), "own block in b"))
    client, mock, _csrf = _client(platform_admin=True)
    try:
        page = client.get(_PAGE)
    finally:
        mock.stop()
    assert page.status_code == 200, page.text
    hint = _own_login_hint(page.text)
    assert f'(<span class="font-mono">{_TENANT_B}</span>)' in hint
    assert "run in the tenant of that block" in hint
    assert "meho principal unblock" in hint
    assert "fresh sign-in" in hint
    assert "only the block of that tenant" in hint
    assert str(_TENANT_A) not in hint
    assert f'name="sub" value="{_OP_A}"' not in page.text


def test_own_login_unblock_answers_403_naming_the_tenant_rule() -> None:
    _tenants()
    _seed_blocks((_OP_A, str(_TENANT_B), "own block in b"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        refused = client.post(_UNBLOCK, data=_confirmed(_OP_A), headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert detail.startswith("own_login:")
    assert "run in the tenant of that block" in detail
    assert "fresh sign-in" in detail
    assert "It removes only the block of that tenant." in detail
    assert "A block in every tenant stays until another platform admin removes it." in detail
    assert "A tenant admin cannot remove a block on their own login." in detail
    assert _block_rows() == [(_OP_A, str(_TENANT_B), "own block in b")]
    assert _audit_payloads("principal.unblock") == []


def test_unblock_answers_503_when_the_block_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"))

    async def _unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise PrincipalBlockUnavailableError("OperationalError")

    client, mock, csrf = _client()
    monkeypatch.setattr(principal_block, "_read_blocks", _unreadable)
    try:
        response = client.post(_UNBLOCK, data=_confirmed("in-a"), headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 503
    assert response.json()["detail"] == "principal_block_check_unavailable"
    assert _block_rows() == [("in-a", str(_TENANT_A), "reason a")]


class _DatabaseDown:
    """A database session that cannot be opened."""

    async def __aenter__(self) -> Any:
        raise OperationalError("INSERT", {}, Exception("database is down"))

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def test_a_valid_block_or_unblock_answers_503_when_the_table_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"))
    client, mock, csrf = _client()
    headers = {"Authorization": f"Bearer {client.token}"}
    try:
        # The page view fills the cached block list, so the session check
        # of the next requests does not read the table.
        assert client.get(_PAGE).status_code == 200
        form = _confirm_form(client, csrf, "in-a")
        assert principal_block._cache is not None

        def _down() -> _DatabaseDown:
            return _DatabaseDown()

        # The cached list stays valid; only a read or write of the table fails.
        monkeypatch.setattr(principal_block, "get_sessionmaker", lambda: _down)
        monkeypatch.setattr(
            principal_block, "_cache", dataclasses.replace(principal_block._cache, owner=_down)
        )
        console_block = client.post(
            _PAGE, data={"sub": _BLOCKED, "reason": "valid"}, headers=_csrf_headers(csrf)
        )
        console_unblock = client.post(_UNBLOCK, data=form, headers=_csrf_headers(csrf))
        rest_block = client.post(
            "/api/v1/principals/blocked", json={"sub": _BLOCKED, "reason": "valid"}, headers=headers
        )
    finally:
        mock.stop()
        monkeypatch.undo()
    for response in (console_block, console_unblock, rest_block):
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "principal_block_check_unavailable"
    assert _block_rows() == [("in-a", str(_TENANT_A), "reason a")]
    assert _unblock_rows() == []


def test_first_unblock_click_and_page_answer_503_when_the_block_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"))

    async def _unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("database is down"))

    client, mock, csrf = _client()
    monkeypatch.setattr(ui_principal_blocks, "list_blocked", _unreadable)
    try:
        first = client.post(_UNBLOCK, data={"sub": "in-a"}, headers=_csrf_headers(csrf))
        page = client.get(_PAGE)
    finally:
        mock.stop()
    assert first.status_code == 503, first.text
    assert first.json()["detail"] == "principal_block_check_unavailable"
    assert page.status_code == 503, page.text
    assert page.json()["detail"] == "principal_block_check_unavailable"
    assert _block_rows() == [("in-a", str(_TENANT_A), "reason a")]
    assert _audit_payloads("principal.unblock") == []


def test_old_forms_after_a_tenant_switch_are_refused_and_change_no_blocks() -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"), ("in-b", str(_TENANT_B), "reason b"))
    admin_in_both = {
        "meho_tenants": [f"{_TENANT_A}:tenant-a:Admins", f"{_TENANT_B}:tenant-b:Admins"]
    }
    client, mock, csrf = _client(extra_claims=admin_in_both)
    try:
        page = client.get(_PAGE)
        confirm_page = client.post(_UNBLOCK, data={"sub": "in-a", "csrf_token": csrf})
        asyncio.run(
            save_session_tenant(
                client.session_id, tenant_id=_TENANT_B, tenant_chosen=True, can_switch=True
            )
        )
        old = {"csrf_token": csrf, "meho_tenant": str(_TENANT_A)}
        block = client.post(_PAGE, data={**old, "sub": _BLOCKED, "reason": "r"})
        unblock = client.post(_UNBLOCK, data={**old, **_confirmed("in-a")})
        now_b = client.get(_PAGE)
    finally:
        mock.stop()
    field = f'name="meho_tenant" value="{_TENANT_A}"'
    assert page.text.count(field) == 2
    assert confirm_page.text.count(field) == 1
    assert block.status_code == 409 and unblock.status_code == 409
    assert unblock.json()["detail"] == "The tenant changed. Reload the page."
    assert now_b.status_code == 200 and "in-b" in now_b.text
    assert _block_rows() == [
        ("in-a", str(_TENANT_A), "reason a"),
        ("in-b", str(_TENANT_B), "reason b"),
    ]


def test_both_forms_post_with_the_token_in_a_hidden_field_and_work_without_htmx() -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"))
    client, mock, csrf = _client()
    try:
        page = client.get(_PAGE)
        blocked = client.post(_PAGE, data={"sub": _BLOCKED, "reason": "r", "csrf_token": csrf})
        refused = client.post(_PAGE, data={"sub": _OP_A, "reason": "r", "csrf_token": csrf})
        first = client.post(_UNBLOCK, data={"sub": "in-a", "csrf_token": csrf})
        unblocked = client.post(
            _UNBLOCK, data={**_confirmed("in-a", _shown(first)), "csrf_token": csrf}
        )
    finally:
        mock.stop()
    assert page.text.count('method="post"') == 2
    assert page.text.count(f'name="csrf_token" value="{csrf}"') == 2
    assert "hx-post" not in page.text
    assert "confirm(" not in page.text
    assert blocked.status_code == 303
    assert refused.status_code == 422
    assert "<h1" in refused.text and "You cannot block your own login." in refused.text
    assert unblocked.status_code == 303
    assert _block_rows() == [(_BLOCKED, str(_TENANT_A), "r")]


def test_blocking_your_own_login_shows_the_refusal_and_writes_no_block() -> None:
    _tenants()
    client, mock, csrf = _client()
    try:
        response = client.post(
            _PAGE, data={"sub": _OP_A, "reason": "oops"}, headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert response.status_code == 422
    assert "You cannot block your own login." in response.text
    assert _block_rows() == []
    assert _audit_payloads("principal.block") == []


@pytest.mark.parametrize(
    ("form", "field"),
    [
        ({"sub": "", "reason": "x"}, "sub"),
        ({"sub": "someone", "reason": "   "}, "reason"),
        ({"sub": "someone", "reason": "r" * 501}, "reason"),
        ({"sub": "s" * 256, "reason": "x"}, "sub"),
    ],
)
def test_a_bad_form_shows_the_error_next_to_its_field_and_writes_nothing(
    form: dict[str, str], field: str
) -> None:
    _tenants()
    client, mock, csrf = _client()
    try:
        response = client.post(_PAGE, data=form, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 422
    assert f'data-error-for="{field}"' in response.text
    assert _block_rows() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": TenantRole.OPERATOR},
        {"role": TenantRole.READ_ONLY},
        {"principal_kind": "service", "sub": "service:bot"},
        {"principal_kind": "agent", "sub": "agent:bot"},
        {
            "principal_kind": "runner",
            "sub": "runner:bot",
            "extra_claims": {"runner_id": "00000000-0000-0000-0000-0000000000f1"},
        },
    ],
)
def test_page_and_both_forms_refuse_a_caller_who_is_not_a_person_admin(
    kwargs: dict[str, Any],
) -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"))
    client, mock, csrf = _client(**kwargs)
    try:
        headers = _csrf_headers(csrf)
        responses = [
            client.get(_PAGE),
            client.post(_PAGE, data={"sub": _BLOCKED, "reason": "x"}, headers=headers),
            client.post(_UNBLOCK, data={"sub": "in-a"}, headers=headers),
            client.post(_UNBLOCK, data=_confirmed("in-a"), headers=headers),
        ]
    finally:
        mock.stop()
    assert [r.status_code for r in responses] == [403, 403, 403, 403]
    assert _block_rows() == [("in-a", str(_TENANT_A), "reason a")]


def test_a_form_without_the_csrf_token_is_refused() -> None:
    _tenants()
    client, mock, _csrf = _client()
    try:
        response = client.post(_PAGE, data={"sub": _BLOCKED, "reason": "x"})
    finally:
        mock.stop()
    assert response.status_code == 403
    assert _block_rows() == []


def test_the_menu_entry_shows_only_for_a_tenant_admin() -> None:
    _tenants()
    seen: dict[str, bool] = {}
    for name, role in (
        ("admin", TenantRole.TENANT_ADMIN),
        ("operator", TenantRole.OPERATOR),
        ("reader", TenantRole.READ_ONLY),
    ):
        client, mock, _csrf = _client(role=role)
        try:
            first = client.get("/ui/account")
            second = client.get("/ui/account")
        finally:
            mock.stop()
        assert first.status_code == 200 and second.status_code == 200, first.text
        seen[name] = _PAGE in first.text and _PAGE in second.text
        assert (_PAGE in first.text) == (_PAGE in second.text)
    assert seen == {"admin": True, "operator": False, "reader": False}


def test_the_menu_entry_follows_the_role_after_a_tenant_switch() -> None:
    _tenants()
    both = {"meho_tenants": [f"{_TENANT_A}:tenant-a:Admins", f"{_TENANT_B}:tenant-b:Operators"]}
    client, mock, _csrf = _client(extra_claims=both)
    try:
        in_a = client.get("/ui/account")
        asyncio.run(
            save_session_tenant(
                client.session_id, tenant_id=_TENANT_B, tenant_chosen=True, can_switch=True
            )
        )
        in_b = client.get("/ui/account")
    finally:
        mock.stop()
    assert in_a.status_code == 200 and in_b.status_code == 200, in_b.text
    assert f'data-meho-tenant="{_TENANT_A}"' in in_a.text
    assert f'data-meho-tenant="{_TENANT_B}"' in in_b.text
    assert _PAGE in in_a.text
    assert _PAGE not in in_b.text


def _store_token(session_id: uuid.UUID, keys: Any, role: TenantRole) -> None:
    """Put a token with *role* into the session row, as a token refresh does."""
    keypair, _jwks = keys
    token = mint_token(keypair, sub=_OP_A, tenant_id=str(_TENANT_A), tenant_role=role.value)

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await rotate_refresh(
                session,
                session_id,
                presented_refresh="refresh-token-plaintext",
                new_access_token=token,
                new_refresh_token="refresh-token-plaintext",
            )

    asyncio.run(_do())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (TenantRole.TENANT_ADMIN, TenantRole.OPERATOR),
        (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN),
    ],
    ids=["admin-becomes-operator", "operator-becomes-admin"],
)
def test_after_a_role_change_the_page_follows_at_once_and_the_menu_after_the_check_time(
    monkeypatch: pytest.MonkeyPatch, before: TenantRole, after: TenantRole
) -> None:
    _tenants()
    now = [1_000.0]
    monkeypatch.setattr(revalidation, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setenv("UI_SESSION_READ_REVALIDATION_SECONDS", "60")
    monkeypatch.setenv("AUDIT_CLIENT_IDENTITY", "false")
    get_settings.cache_clear()
    keys = _make_keypair_and_jwks()
    client, mock, _csrf = _client(role=before, keys=keys)
    try:
        start = client.get("/ui/account")
        _store_token(client.session_id, keys, after)
        page = client.get(_PAGE)
        now[0] += 59
        early = client.get("/ui/account")
        now[0] += 2
        late = client.get("/ui/account")
    finally:
        mock.stop()
    assert [r.status_code for r in (start, early, late)] == [200, 200, 200]
    assert page.status_code == (200 if after is TenantRole.TENANT_ADMIN else 403), page.text
    was_admin = before is TenantRole.TENANT_ADMIN
    assert (_PAGE in start.text) is was_admin
    assert (_PAGE in early.text) is was_admin
    assert (_PAGE in late.text) is (not was_admin)


class _OtherTabSwitchesTenant:
    """A token check during which another tab moves the session to tenant B."""

    async def __call__(self, decrypted: Any, *, expected_audience: str) -> tuple[Any, Any]:
        await save_session_tenant(
            decrypted.id, tenant_id=_TENANT_B, tenant_chosen=True, can_switch=True
        )
        operator = SimpleNamespace(
            tenant_role=TenantRole.TENANT_ADMIN, principal_kind=PrincipalKind.USER
        )
        return dataclasses.replace(decrypted, tenant_id=_TENANT_B), operator


@pytest.mark.parametrize("blocked_in_b", [False, True], ids=["not-blocked", "blocked-in-b"])
def test_a_refresh_that_finds_another_tenant_names_and_checks_that_tenant(
    monkeypatch: pytest.MonkeyPatch, blocked_in_b: bool
) -> None:
    _tenants()
    if blocked_in_b:
        _seed_blocks((_OP_A, str(_TENANT_B), "blocked in b"))
    admin_in_both = {
        "meho_tenants": [f"{_TENANT_A}:tenant-a:Admins", f"{_TENANT_B}:tenant-b:Admins"]
    }
    client, mock, _csrf = _client(extra_claims=admin_in_both)
    try:
        # The next request checks the token again; the check finds the session in tenant B.
        revalidation.reset_read_revalidation_cache_for_testing()
        monkeypatch.setattr(
            revalidation, "verify_access_token_with_refresh", _OtherTabSwitchesTenant()
        )
        page = client.get(_PAGE)
    finally:
        mock.stop()
    if blocked_in_b:
        assert page.status_code == 403, page.text
        assert page.text == PRINCIPAL_BLOCKED_MESSAGE
        return
    assert page.status_code == 200, page.text
    assert "Tenant tenant-b" in page.text
    assert "Tenant tenant-a" not in page.text
    assert f'name="meho_tenant" value="{_TENANT_B}"' in page.text
    assert f'name="meho_tenant" value="{_TENANT_A}"' not in page.text


def _signed_in(kind: str, sub: str, claims: dict[str, Any] | None) -> tuple[Any, Any, Any]:
    """Sign in through the real login step with a ``tenant_admin`` token of *kind*."""
    clear_jwks_cache()
    keypair, jwks = _make_keypair_and_jwks()
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        principal_kind=kind,
        extra_claims=claims,
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    mock_discovery_and_jwks(mock, jwks)
    tokens = TokenExchangeResult(
        access_token=token, refresh_token="refresh", expires_in=300, return_to="/ui"
    )
    session_id, _sub, _tenant, _chosen = asyncio.run(_persist_session_from_tokens(tokens))
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock, session_id


@pytest.mark.parametrize(
    ("kind", "sub", "claims", "sees_entry"),
    [
        ("user", _OP_A, None, True),
        ("service", "service:bot", None, False),
        (
            "runner",
            "runner:bot",
            {"runner_id": "00000000-0000-0000-0000-0000000000f1"},
            False,
        ),
    ],
    ids=["person", "service", "runner"],
)
def test_only_a_person_with_tenant_admin_gets_the_menu_entry_at_sign_in_and_at_a_token_check(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    sub: str,
    claims: dict[str, Any] | None,
    sees_entry: bool,
) -> None:
    _tenants()
    client, mock, session_id = _signed_in(kind, sub, claims)
    try:
        # What the sign-in stored, before any page view checks the token.
        at_sign_in = revalidation.session_is_tenant_admin(session_id, _TENANT_A)
        # Every page view checks the token again.
        monkeypatch.setenv("UI_SESSION_READ_REVALIDATION_SECONDS", "0")
        get_settings.cache_clear()
        page = client.get("/ui/account")
        at_token_check = revalidation.session_is_tenant_admin(session_id, _TENANT_A)
    finally:
        mock.stop()
    assert at_sign_in is sees_entry
    assert at_token_check is sees_entry
    if page.status_code == 200:
        assert (_PAGE in page.text) is sees_entry


def test_the_menu_flag_is_not_part_of_the_request_schema() -> None:
    assert "is_tenant_admin" not in UISessionContext.__dataclass_fields__
    schemas = _build_app().openapi()["components"]["schemas"]
    assert "is_tenant_admin" not in schemas.get("UISessionContext", {}).get("properties", {})


def test_the_hint_of_a_block_in_every_tenant_names_who_removes_it() -> None:
    # Another process blocks the admin in every tenant. The cached list does
    # not show it yet, so the admin still opens the page, and the page reads
    # the table.
    _tenants()
    client, mock, _csrf = _client(platform_admin=True)
    try:
        assert client.get(_PAGE).status_code == 200
        _seed_blocks((_OP_A, "*", "blocked everywhere"))
        page = client.get(_PAGE)
        refused = client.get(_PAGE)
    finally:
        mock.stop()
    assert page.status_code == 200, page.text
    hint = _own_login_hint(page.text)
    assert "blocked in all tenants" in hint
    assert "A block in every tenant stays until another platform admin removes it." in hint
    assert f'name="sub" value="{_OP_A}"' not in page.text
    assert refused.status_code == 200


def test_the_hint_of_a_tenant_admin_says_another_admin_removes_the_block() -> None:
    _tenants()
    client, mock, csrf = _client()
    try:
        assert client.get(_PAGE).status_code == 200
        _seed_blocks((_OP_A, str(_TENANT_A), "own block in a"))
        page = client.get(_PAGE)
        refused = client.post(_UNBLOCK, data=_confirmed(_OP_A), headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    hint = _plain(_own_login_hint(page.text))
    assert _missing_rule_sentences(hint, _TENANT_ADMIN_SENTENCES) == []
    assert "--reason" not in hint
    assert refused.status_code == 403
    assert "A tenant admin cannot remove a block on their own login." in refused.json()["detail"]


# Each row: who asks, where the block on their own login is, what the console
# answers, and what REST answers (an unblock of the own login, with a reason
# and a fresh sign-in).
_OWN_LOGIN_CASES = [
    # A platform admin removes only the block of the current tenant.
    (True, "none", 403, "own_login", 404, "principal_not_blocked"),
    (True, "other", 403, "own_login", 404, "principal_not_blocked"),
    (True, "own", 403, "blocked", 200, None),
    (True, "all", 403, "blocked", 403, "principal_blocked"),
    # A tenant admin never removes a block on their own login.
    (False, "none", 403, "own_login", 404, "principal_not_blocked"),
    (False, "other", 403, "own_login", 404, "principal_not_blocked"),
    (False, "own", 403, "blocked", 403, "principal_blocked"),
    (False, "all", 403, "blocked", 403, "principal_blocked"),
]


@pytest.mark.parametrize(
    ("platform_admin", "where", "console_status", "console_text", "rest_status", "rest_detail"),
    _OWN_LOGIN_CASES,
    ids=[
        f"{'platform' if case[0] else 'tenant'}-admin-block-in-{case[1]}-tenant"
        for case in _OWN_LOGIN_CASES
    ],
)
def test_an_own_login_unblock_answers_by_role_on_the_console_and_on_rest(
    platform_admin: bool,
    where: str,
    console_status: int,
    console_text: str,
    rest_status: int,
    rest_detail: str | None,
) -> None:
    _tenants()
    scope = {"none": None, "other": str(_TENANT_B), "own": str(_TENANT_A), "all": "*"}[where]
    if scope is not None:
        _seed_blocks((_OP_A, scope, "block on the own login"))
    client, mock, csrf = _client(platform_admin=platform_admin, fresh_sign_in=True)
    headers = {"Authorization": f"Bearer {client.token}"}
    try:
        console = client.post(_UNBLOCK, data=_confirmed(_OP_A), headers=_csrf_headers(csrf))
        rest = client.request(
            "DELETE",
            f"/api/v1/principals/blocked/{_OP_A}",
            json={"reason": "the block was a mistake"},
            headers=headers,
        )
    finally:
        mock.stop()
    assert console.status_code == console_status
    if console_text == "own_login":
        assert console.json()["detail"].startswith("own_login:")
    else:
        assert console.text == PRINCIPAL_BLOCKED_MESSAGE
    assert rest.status_code == rest_status, rest.text
    if rest_detail is not None:
        assert rest.json()["detail"].startswith(rest_detail)
    remaining = [row for row in _block_rows() if row[0] == _OP_A]
    expected = [] if rest_status == 200 else [(_OP_A, scope, "block on the own login")]
    assert remaining == ([] if scope is None else expected)
    # The console never removed a block on the own login.
    assert all(path != _UNBLOCK for _method, path in _routes_of("principal.unblock"))


def test_only_a_platform_admin_removes_a_tenant_admins_block_in_every_tenant() -> None:
    # The block is on another login, so it is not an own-login case. A tenant
    # admin of the tenant cannot remove a block in every tenant, on the console
    # or on REST. A platform admin can.
    _tenants()
    _seed_blocks((_BLOCKED, "*", "blocked everywhere"))
    client, mock, csrf = _client()
    headers = {"Authorization": f"Bearer {client.token}"}
    try:
        console = client.post(_UNBLOCK, data=_confirmed(_BLOCKED), headers=_csrf_headers(csrf))
        rest = client.request("DELETE", f"/api/v1/principals/blocked/{_BLOCKED}", headers=headers)
    finally:
        mock.stop()
    assert console.status_code == 404
    assert rest.status_code == 404
    assert _block_rows() == [(_BLOCKED, "*", "blocked everywhere")]
    assert _unblock_rows() == []
    client, mock, csrf = _client(platform_admin=True)
    try:
        form = _confirm_form(client, csrf, _BLOCKED)
        allowed = client.post(_UNBLOCK, data=form, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert allowed.status_code == 303
    assert _block_rows() == []
    (row,) = _unblock_rows()
    assert row["unblocked_sub"] == _BLOCKED and row["scopes"] == ["*"]


def test_a_platform_admin_removes_a_block_in_every_tenant_over_rest() -> None:
    _tenants()
    _seed_blocks((_BLOCKED, "*", "blocked everywhere"))
    client, mock, _csrf = _client(platform_admin=True)
    try:
        response = client.delete(
            f"/api/v1/principals/blocked/{_BLOCKED}",
            headers={"Authorization": f"Bearer {client.token}"},
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert response.json() == {"sub": _BLOCKED, "removed": 1}
    assert _block_rows() == []
    (row,) = _unblock_rows()
    assert row["unblocked_sub"] == _BLOCKED and row["scopes"] == ["*"]


_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_SENTENCES = ("The console never removes a block on the admin's own login.",)
_PLATFORM_ADMIN_SENTENCES = (
    "A platform admin removes a block on their own login with "
    "meho principal unblock <sub> --reason <text>, run in the tenant of that block, "
    "with a fresh sign-in.",
    "It removes only the block of that tenant.",
    "A block in every tenant stays until another platform admin removes it.",
)
_TENANT_ADMIN_SENTENCES = (
    "A tenant admin cannot remove a block on their own login.",
    "Another tenant admin of that tenant removes a block of that tenant, "
    "and a platform admin with tenant_admin in the current tenant "
    "removes every block of another login, "
    "on the Blocked people page or with meho principal unblock.",
    "Only a platform admin removes a block in every tenant.",
)
_RULE_SENTENCES = _CONSOLE_SENTENCES + _PLATFORM_ADMIN_SENTENCES + _TENANT_ADMIN_SENTENCES


def _plain(text: str) -> str:
    """The text on one line, without code marks or tags, so a wrapped line still matches.

    The user docs write the reason as ``"<why>"``, like their other commands.
    """
    text = re.sub(r"</?span[^>]*>", "", text).replace('"<why>"', "<text>")
    return re.sub(r"\s+", " ", text.replace("`", "")).strip()


def _doc_part(path: str, start: str, end: str) -> str:
    text = (_ROOT / path).read_text(encoding="utf-8")
    first = text.index(start)
    return text[first : text.index(end, first)]


def _missing_rule_sentences(text: str, sentences: tuple[str, ...] = _RULE_SENTENCES) -> list[str]:
    plain = _plain(text)
    return [sentence for sentence in sentences if sentence not in plain]


def _rule_places() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each place that states the rule: its text, and the sentences it must hold."""
    return {
        "the way-back part of principal-block.md": (
            _doc_part(
                "docs/codebase/principal-block.md",
                "A platform admin's way back:",
                "\nTo stop a platform admin",
            ),
            _RULE_SENTENCES,
        ),
        "the Console part of principal-block.md": (
            _doc_part("docs/codebase/principal-block.md", "Console: the page", "\n## Dependencies"),
            _RULE_SENTENCES,
        ),
        "the unblock line of ui.md": (
            _doc_part(
                "docs/codebase/ui.md",
                "- `POST /ui/blocked-people/unblock`",
                "\n\nEvery form is a plain",
            ),
            _RULE_SENTENCES,
        ),
        "the module docstring": (ui_principal_blocks.__doc__ or "", _RULE_SENTENCES),
        "the own_login text": (ui_principal_blocks._OWN_LOGIN_UNBLOCK, _RULE_SENTENCES),
        "the tenant-admin hint of the page": (
            _doc_part(
                "backend/src/meho_backplane/ui/templates/principal_blocks/index.html",
                "{% if not platform_admin %}",
                "{% elif person.scope == scope_all %}",
            ),
            _TENANT_ADMIN_SENTENCES,
        ),
        "the user docs (keycloak-realm.md)": (
            _doc_part(
                "docs-site/install/keycloak-realm.md",
                "The console page `/ui/blocked-people`",
                "Who blocks whom:",
            ),
            _PLATFORM_ADMIN_SENTENCES + _TENANT_ADMIN_SENTENCES,
        ),
    }


def test_the_way_back_rule_is_the_same_in_the_seven_places() -> None:
    places = _rule_places()
    assert len(places) == 7
    assert {
        name: _missing_rule_sentences(text, sentences) for name, (text, sentences) in places.items()
    } == {name: [] for name in places}


@pytest.mark.parametrize("place", sorted(_rule_places()))
def test_the_seven_places_check_notices_a_changed_rule_sentence(place: str) -> None:
    text, sentences = _rule_places()[place]
    rule = r"\s+".join(re.escape(word) for word in _TENANT_ADMIN_SENTENCES[0].split())
    changed, count = re.subn(
        rule, lambda m: re.sub(r"cannot\s+remove", "can remove", m.group()), text, count=1
    )
    assert count == 1
    assert _missing_rule_sentences(changed, sentences) == [_TENANT_ADMIN_SENTENCES[0]]
