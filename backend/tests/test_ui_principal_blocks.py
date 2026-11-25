# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request-level coverage of the console page of the admin block list."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, PrincipalBlock
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.routes import build_router as build_ui_router
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
    return app


def _client(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant: Any = _TENANT_A,
    platform_admin: bool | None = None,
    principal_kind: str | None = None,
    sub: str = _OP_A,
    keys: Any = None,
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
    )
    session_id = _seed_session_sync(tenant_id=tenant, access_token=token, operator_sub=sub)
    mock = respx.mock(assert_all_called=False)
    mock.start()
    mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return client, mock, csrf


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
        unblocked = client.post(
            f"{_PAGE}/unblock", data={"sub": _BLOCKED}, headers=_csrf_headers(csrf)
        )
        again = client.post(f"{_PAGE}/unblock", data={"sub": _BLOCKED}, headers=_csrf_headers(csrf))
        other_tenant = client.post(
            f"{_PAGE}/unblock", data={"sub": "in-b"}, headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert blocked.status_code == 303, blocked.text
    assert blocked.headers["location"] == _PAGE
    assert _BLOCKED in listed.text and "typed reason" in listed.text
    assert unblocked.status_code == 303
    assert unblocked.headers["location"] == _PAGE
    assert again.status_code == 404
    assert other_tenant.status_code == 404
    assert _block_rows() == [("in-b", str(_TENANT_B), "reason b")]
    (block_row,) = _audit_payloads("principal.block")
    assert block_row["blocked_sub"] == _BLOCKED and block_row["reason"] == "typed reason"
    (unblock_row,) = _audit_payloads("principal.unblock")
    assert unblock_row["unblocked_sub"] == _BLOCKED


def _request_rows(path: str) -> list[dict[str, Any]]:
    async def _do() -> list[dict[str, Any]]:
        async with get_sessionmaker()() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        return [r.payload for r in rows if r.method == "POST" and r.path == path]

    return asyncio.run(_do())


def test_a_block_and_an_unblock_each_write_one_row_with_their_name_and_one_request_row() -> None:
    _tenants()
    client, mock, csrf = _client()
    try:
        client.post(_PAGE, data={"sub": _BLOCKED, "reason": "r"}, headers=_csrf_headers(csrf))
        client.post(f"{_PAGE}/unblock", data={"sub": _BLOCKED}, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert len(_audit_payloads("principal.block")) == 1
    assert len(_audit_payloads("principal.unblock")) == 1
    (block_request,) = _request_rows(_PAGE)
    (unblock_request,) = _request_rows(f"{_PAGE}/unblock")
    assert block_request.get("op_id") != "principal.block"
    assert unblock_request.get("op_id") != "principal.unblock"


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


def test_unblock_names_the_login_and_its_scope_when_two_tenants_blocked_it() -> None:
    _tenants()
    _seed_blocks(
        ("shared-login", str(_TENANT_A), "reason a"), ("shared-login", str(_TENANT_B), "reason b")
    )
    platform, platform_mock, platform_csrf = _client(platform_admin=True)
    try:
        platform_page = platform.get(_PAGE)
    finally:
        platform_mock.stop()
    tenant, tenant_mock, tenant_csrf = _client()
    try:
        tenant_page = tenant.get(_PAGE)
        tenant_unblock = tenant.post(
            f"{_PAGE}/unblock", data={"sub": "shared-login"}, headers=_csrf_headers(tenant_csrf)
        )
    finally:
        tenant_mock.stop()
    everywhere = (
        "Unblock the login shared-login in all tenants? This removes every block of this login"
    )
    assert platform_page.text.count(f'data-confirm="{everywhere}') == 2
    assert "Unblock in all tenants" in platform_page.text
    assert 'data-confirm="Unblock the login shared-login in this tenant?"' in tenant_page.text
    assert "in all tenants" not in tenant_page.text
    assert tenant_unblock.status_code == 303
    assert _block_rows() == [("shared-login", str(_TENANT_B), "reason b")]
    platform, platform_mock, platform_csrf = _client(platform_admin=True)
    try:
        platform_unblock = platform.post(
            f"{_PAGE}/unblock", data={"sub": "shared-login"}, headers=_csrf_headers(platform_csrf)
        )
    finally:
        platform_mock.stop()
    assert platform_unblock.status_code == 303
    assert _block_rows() == []


def test_both_forms_post_with_the_token_in_a_hidden_field_and_work_without_htmx() -> None:
    _tenants()
    _seed_blocks(("in-a", str(_TENANT_A), "reason a"))
    client, mock, csrf = _client()
    try:
        page = client.get(_PAGE)
        blocked = client.post(_PAGE, data={"sub": _BLOCKED, "reason": "r", "csrf_token": csrf})
        refused = client.post(_PAGE, data={"sub": _OP_A, "reason": "r", "csrf_token": csrf})
        unblocked = client.post(f"{_PAGE}/unblock", data={"sub": "in-a", "csrf_token": csrf})
    finally:
        mock.stop()
    assert page.text.count('method="post"') == 2
    assert page.text.count(f'name="csrf_token" value="{csrf}"') == 2
    assert "hx-post" not in page.text
    assert 'onsubmit="return confirm(this.dataset.confirm);"' in page.text
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
            client.post(f"{_PAGE}/unblock", data={"sub": "in-a"}, headers=headers),
        ]
    finally:
        mock.stop()
    assert [r.status_code for r in responses] == [403, 403, 403]
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
