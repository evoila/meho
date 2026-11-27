# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The unblock confirm of the console removes only the blocks its page showed."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, update

from meho_backplane.auth import principal_block
from meho_backplane.auth.principal_block import BlockedPerson, reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PrincipalBlock
from tests.test_ui_agent_grants import _TENANT_A, _TENANT_B, _bff_env, _csrf_headers
from tests.test_ui_principal_blocks import (
    _UNBLOCK,
    _audit_payloads,
    _block_rows,
    _client,
    _confirmed,
    _seed_blocks,
    _shown,
    _tenants,
)

_LOGIN = "late-person"
_CHANGED = "data-unblock-changed"


@pytest.fixture(autouse=True)
def _blocks_bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _bff_env.__wrapped__(monkeypatch)
    reset_block_cache()


def _remove_block(sub: str, scope: str) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                delete(PrincipalBlock).where(
                    PrincipalBlock.sub == sub, PrincipalBlock.scope == scope
                )
            )

    asyncio.run(_do())


def _set_again(sub: str, scope: str, reason: str, blocked_by: str) -> None:
    """Remove the block of *sub* in *scope* and set it again, as another admin would."""
    _remove_block(sub, scope)

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                PrincipalBlock(
                    sub=sub,
                    scope=scope,
                    tenant_id=None if scope == "*" else _TENANT_A,
                    reason=reason,
                    blocked_by_sub=blocked_by,
                    blocked_at=datetime.now(UTC),
                )
            )

    asyncio.run(_do())


def _open(client: Any, csrf: str, sub: str = _LOGIN) -> Any:
    return client.post(_UNBLOCK, data={"sub": sub}, headers=_csrf_headers(csrf))


def _confirm(client: Any, csrf: str, page: Any, sub: str = _LOGIN) -> Any:
    return client.post(_UNBLOCK, data=_confirmed(sub, _shown(page)), headers=_csrf_headers(csrf))


def test_a_block_added_after_the_first_click_is_shown_and_removed_only_after_a_new_confirm() -> (
    None
):
    _tenants()
    _seed_blocks((_LOGIN, str(_TENANT_A), "reason a"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        first = _open(client, csrf)
        _seed_blocks((_LOGIN, str(_TENANT_B), "reason b"))
        stale = _confirm(client, csrf, first)
        rows_after_stale = _block_rows()
        audit_after_stale = _audit_payloads("principal.unblock")
        fresh = _confirm(client, csrf, stale)
    finally:
        mock.stop()
    assert "reason a" in first.text and "reason b" not in first.text
    assert stale.status_code == 200, stale.text
    assert _CHANGED in stale.text
    assert "reason a" in stale.text and "reason b" in stale.text
    assert rows_after_stale == [
        (_LOGIN, str(_TENANT_A), "reason a"),
        (_LOGIN, str(_TENANT_B), "reason b"),
    ]
    assert audit_after_stale == []
    assert fresh.status_code == 303, fresh.text
    assert _block_rows() == []
    (row,) = _audit_payloads("principal.unblock")
    assert sorted(row["scopes"]) == sorted([str(_TENANT_A), str(_TENANT_B)])


def test_a_block_removed_after_the_first_click_leaves_the_other_block_until_a_new_confirm() -> None:
    _tenants()
    _seed_blocks((_LOGIN, str(_TENANT_A), "reason a"), (_LOGIN, str(_TENANT_B), "reason b"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        first = _open(client, csrf)
        _remove_block(_LOGIN, str(_TENANT_B))
        stale = _confirm(client, csrf, first)
        rows_after_stale = _block_rows()
        fresh = _confirm(client, csrf, stale)
    finally:
        mock.stop()
    assert "reason a" in first.text and "reason b" in first.text
    assert stale.status_code == 200, stale.text
    assert _CHANGED in stale.text
    assert "reason a" in stale.text and "reason b" not in stale.text
    assert rows_after_stale == [(_LOGIN, str(_TENANT_A), "reason a")]
    assert fresh.status_code == 303, fresh.text
    assert _block_rows() == []
    (row,) = _audit_payloads("principal.unblock")
    assert row["scopes"] == [str(_TENANT_A)]


def test_an_unchanged_set_is_removed_by_the_first_confirm_and_named_in_the_audit_row() -> None:
    _tenants()
    _seed_blocks((_LOGIN, str(_TENANT_A), "reason a"), (_LOGIN, str(_TENANT_B), "reason b"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        first = _open(client, csrf)
        confirmed = _confirm(client, csrf, first)
    finally:
        mock.stop()
    assert first.status_code == 200, first.text
    assert _CHANGED not in first.text
    assert confirmed.status_code == 303, confirmed.text
    assert _block_rows() == []
    (row,) = _audit_payloads("principal.unblock")
    assert row["removed_blocks"] == sorted(
        [
            {"scope": str(_TENANT_A), "reason": "reason a"},
            {"scope": str(_TENANT_B), "reason": "reason b"},
        ],
        key=lambda b: b["scope"],
    )


@pytest.mark.parametrize(
    ("reason", "blocked_by"),
    [("new reason", "someone"), ("reason a", "another-admin")],
)
def test_a_tenant_admin_sees_a_block_set_again_and_is_asked_again(
    reason: str, blocked_by: str
) -> None:
    _tenants()
    _seed_blocks((_LOGIN, str(_TENANT_A), "reason a"))
    client, mock, csrf = _client()
    try:
        first = _open(client, csrf)
        _set_again(_LOGIN, str(_TENANT_A), reason, blocked_by)
        stale = _confirm(client, csrf, first)
        rows_after_stale = _block_rows()
        audit_after_stale = _audit_payloads("principal.unblock")
        fresh = _confirm(client, csrf, stale)
    finally:
        mock.stop()
    assert stale.status_code == 200, stale.text
    assert _CHANGED in stale.text
    assert reason in stale.text and blocked_by in stale.text
    assert "removes the block of this login in this tenant" in stale.text
    assert rows_after_stale == [(_LOGIN, str(_TENANT_A), reason)]
    assert audit_after_stale == []
    assert fresh.status_code == 303, fresh.text
    assert _block_rows() == []
    (row,) = _audit_payloads("principal.unblock")
    assert row["removed_blocks"] == [{"scope": str(_TENANT_A), "reason": reason}]


@pytest.mark.parametrize("shown", ["", "0" * 64, "not-a-set"])
def test_a_confirm_with_a_set_that_matches_no_blocks_removes_nothing(shown: str) -> None:
    _tenants()
    _seed_blocks((_LOGIN, str(_TENANT_A), "reason a"))
    client, mock, csrf = _client(platform_admin=True)
    try:
        forged = client.post(_UNBLOCK, data=_confirmed(_LOGIN, shown), headers=_csrf_headers(csrf))
        unknown = client.post(
            _UNBLOCK, data=_confirmed("nobody", shown), headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert forged.status_code == 200, forged.text
    assert _CHANGED in forged.text
    assert unknown.status_code == 404
    assert _block_rows() == [(_LOGIN, str(_TENANT_A), "reason a")]
    assert _audit_payloads("principal.unblock") == []


def test_a_block_set_again_during_the_confirm_stays_and_the_page_shows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second database session sets the block again after the confirm read it."""
    _tenants()
    _seed_blocks((_LOGIN, str(_TENANT_A), "reason a"))
    read_blocks = principal_block._read_blocks

    async def _read_then_set_again(session: Any, sub: str) -> list[BlockedPerson]:
        blocks = await read_blocks(session, sub)
        async with get_sessionmaker()() as other, other.begin():
            await other.execute(
                update(PrincipalBlock)
                .where(PrincipalBlock.sub == sub)
                .values(reason="set again", blocked_by_sub="another-admin")
            )
        return blocks

    client, mock, csrf = _client(platform_admin=True)
    try:
        first = _open(client, csrf)
        monkeypatch.setattr(principal_block, "_read_blocks", _read_then_set_again)
        confirm = _confirm(client, csrf, first)
    finally:
        mock.stop()
    assert confirm.status_code == 200, confirm.text
    assert _CHANGED in confirm.text
    assert "set again" in confirm.text and "another-admin" in confirm.text
    assert _block_rows() == [(_LOGIN, str(_TENANT_A), "set again")]
    assert _audit_payloads("principal.unblock") == []
