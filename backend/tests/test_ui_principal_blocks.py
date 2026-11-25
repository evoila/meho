# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request-level coverage of the console page of the admin block list."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

import tests.test_ui_agent_grants as agent_grants_test
from meho_backplane.auth.operator import TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, PrincipalBlock
from tests._oidc_jwt_helpers import mint_token
from tests.test_ui_agent_grants import (
    _OP_A,
    _TENANT_A,
    _TENANT_B,
    _authenticated_client,
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_session_sync,
    _seed_tenant,
)

_PAGE = "/ui/blocked-people"
_BLOCKED = "person-to-block"


@pytest.fixture(autouse=True)
def _blocks_bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from agent_grants_test._bff_env.__wrapped__(monkeypatch)
    reset_block_cache()


def _client(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant: Any = _TENANT_A,
    platform_admin: bool | None = None,
    principal_kind: str | None = None,
    sub: str = _OP_A,
) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(tenant),
        tenant_role=role.value,
        platform_admin=platform_admin,
        principal_kind=principal_kind,
    )
    session_id = _seed_session_sync(tenant_id=tenant, access_token=token, operator_sub=sub)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)


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
    assert response.status_code == 204, response.text
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
    assert blocked.status_code == 204, blocked.text
    assert blocked.headers["HX-Redirect"] == _PAGE
    assert _BLOCKED in listed.text and "typed reason" in listed.text
    assert unblocked.status_code == 204
    assert again.status_code == 404
    assert other_tenant.status_code == 404
    assert _block_rows() == [("in-b", str(_TENANT_B), "reason b")]
    (block_row,) = _audit_payloads("principal.block")
    assert block_row["blocked_sub"] == _BLOCKED and block_row["reason"] == "typed reason"
    (unblock_row,) = _audit_payloads("principal.unblock")
    assert unblock_row["unblocked_sub"] == _BLOCKED


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
