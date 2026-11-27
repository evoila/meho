# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: an unblock holds the rows it read until it ends.

An unblock reads the blocks of a login with ``SELECT ... FOR UPDATE``
(:func:`meho_backplane.auth.principal_block._read_blocks`) and removes
only the rows it read. While it runs, a second writer of the same rows
waits:

* a second database session that sets a block again waits, and the
  confirm removes the rows it read and compared with the shown set;
* a second unblock of the same login waits, then finds no block and
  answers ``404 principal_not_blocked``;
* a block of the same login (console or REST) waits, and is written when
  the unblock ends.

SQLite (the unit-suite driver) has no row lock, so only a real Postgres
can show this. Each test runs the production app with every middleware.
The test holds the first unblock right after its read, starts the second
writer, and goes on only when Postgres shows that writer waiting for a
lock.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from httpx import ASGITransport
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth import principal_block
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.auth.principal_block import BlockedPerson, reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, PrincipalBlock
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

from .conftest import (
    DOCKER_AVAILABLE,
    SKIP_REASON,
    pg_engine,  # noqa: F401 — pytest-discovered fixture
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON),
]

#: The tenants ``pg_engine`` seeds on entry.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_LOGIN = "person-in-two-unblocks"
_PAGE = "/ui/blocked-people"
_UNBLOCK = f"{_PAGE}/unblock"
_REST = "/api/v1/principals/blocked"
_WAIT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def _console_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    get_settings.cache_clear()
    clear_jwks_cache()
    clear_discovery_cache()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_block_cache()


@pytest.fixture
async def _key(pg_engine: None) -> AsyncIterator[Any]:  # noqa: F811 — the fixture above
    key = make_rsa_keypair("kid-unblock-row-lock")
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(PrincipalBlock))
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        yield key


def _token(key: Any, sub: str, *, platform_admin: bool | None = None) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=platform_admin,
    )


@asynccontextmanager
async def _rest_client(key: Any, sub: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        headers={"Authorization": f"Bearer {_token(key, sub)}"},
    ) as client:
        yield client


@asynccontextmanager
async def _console_client(
    key: Any, sub: str, *, platform_admin: bool | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in console session of *sub*, with its CSRF token."""
    async with get_sessionmaker()() as session, session.begin():
        web_session = await create_session(
            session,
            operator_sub=sub,
            tenant_id=_TENANT_A,
            access_token=_token(key, sub, platform_admin=platform_admin),
            refresh_token="refresh-token-plaintext",
            lifetime=timedelta(hours=1),
        )
    csrf = mint_csrf_token(str(web_session.id))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
        headers={"X-CSRF-Token": csrf, "HX-Request": "true"},
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, str(web_session.id))
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        yield client


async def _shown(client: httpx.AsyncClient, sub: str) -> str:
    """Open the confirm page of *sub* and return the set of blocks its form carries."""
    page = await client.post(_UNBLOCK, data={"sub": sub})
    assert page.status_code == 200, page.text
    match = re.search(r'name="shown" value="([0-9a-f]+)"', page.text)
    assert match is not None, page.text
    return match.group(1)


async def _seed(*rows: tuple[str, str]) -> None:
    """Insert ``(scope, reason)`` blocks of :data:`_LOGIN`."""
    async with get_sessionmaker()() as session, session.begin():
        for scope, reason in rows:
            session.add(
                PrincipalBlock(
                    sub=_LOGIN,
                    scope=scope,
                    tenant_id=None if scope == "*" else uuid.UUID(scope),
                    reason=reason,
                    blocked_by_sub="first-admin",
                    blocked_at=datetime.now(UTC),
                )
            )


async def _rows() -> list[tuple[str, str, str]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(PrincipalBlock))).scalars().all()
    return sorted((r.scope, r.reason, r.blocked_by_sub) for r in rows)


async def _audit(*op_ids: str) -> list[AuditLog]:
    """The block and unblock audit rows of :data:`_LOGIN`, in the order they were written.

    The request records of the routes carry the same ``op_id`` but name no
    login, so they are left out.
    """
    async with get_sessionmaker()() as session:
        query = select(AuditLog).order_by(AuditLog.occurred_at)
        rows = (await session.execute(query)).scalars().all()
    return [
        r
        for r in rows
        if r.payload.get("op_id") in op_ids
        and _LOGIN in (r.payload.get("blocked_sub"), r.payload.get("unblocked_sub"))
    ]


async def _someone_waits_for_a_lock() -> bool:
    """True once Postgres shows another connection waiting for a lock."""
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
        "AND pid <> pg_backend_pid()"
    )
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        async with get_sessionmaker()() as session:
            if (await session.execute(query)).scalar_one() > 0:
                return True
        await asyncio.sleep(0.05)
    return False


class _HeldUnblock:
    """Hold the first unblock right after its read, while a second writer starts.

    The second writer runs as a task. The unblock goes on once Postgres
    shows a connection waiting for a lock, or after :data:`_WAIT_SECONDS`.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.second: asyncio.Future[Any] | None = None
        self.second_waited = False
        self._start: Callable[[], Awaitable[Any]] | None = None
        read_blocks = principal_block._read_blocks

        async def _read_and_hold(session: AsyncSession, sub: str) -> list[BlockedPerson]:
            blocks = await read_blocks(session, sub)
            start, self._start = self._start, None
            if start is not None:
                self.second = asyncio.ensure_future(start())
                self.second_waited = await _someone_waits_for_a_lock()
            return blocks

        monkeypatch.setattr(principal_block, "_read_blocks", _read_and_hold)

    def during_the_next_unblock(self, start: Callable[[], Awaitable[Any]]) -> None:
        self._start = start

    async def second_result(self) -> Any:
        assert self.second is not None, "the unblock never read the blocks"
        return await asyncio.wait_for(self.second, _WAIT_SECONDS)


async def test_a_console_confirm_removes_the_rows_it_read_while_a_second_session_waits(
    _key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed((str(_TENANT_A), "reason a"), (str(_TENANT_B), "reason b"))
    held = _HeldUnblock(monkeypatch)

    async def _set_again() -> int:
        async with get_sessionmaker()() as other, other.begin():
            result = await other.execute(
                update(PrincipalBlock)
                .where(PrincipalBlock.sub == _LOGIN)
                .values(reason="set again", blocked_by_sub="second-admin")
            )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async with _console_client(_key, "platform-admin", platform_admin=True) as console:
        shown = await _shown(console, _LOGIN)
        held.during_the_next_unblock(_set_again)
        confirmed = await console.post(
            _UNBLOCK, data={"sub": _LOGIN, "confirm": "yes", "shown": shown}
        )
    set_again = await held.second_result()

    assert held.second_waited, "the second session did not wait for the row lock"
    assert confirmed.status_code == 303, confirmed.text
    assert set_again == 0
    assert await _rows() == []
    (row,) = await _audit("principal.unblock")
    assert row.operator_sub == "platform-admin"
    assert row.payload["removed_blocks"] == [
        {"scope": str(_TENANT_A), "reason": "reason a"},
        {"scope": str(_TENANT_B), "reason": "reason b"},
    ]


async def test_of_two_rest_unblocks_at_the_same_moment_one_removes_and_one_is_404(
    _key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed((str(_TENANT_A), "reason a"))
    held = _HeldUnblock(monkeypatch)
    async with _rest_client(_key, "admin-one") as one, _rest_client(_key, "admin-two") as two:
        held.during_the_next_unblock(lambda: two.delete(f"{_REST}/{_LOGIN}"))
        first = await one.delete(f"{_REST}/{_LOGIN}")
        second = await held.second_result()

    assert held.second_waited, "the second unblock did not wait for the row lock"
    assert first.status_code == 200, first.text
    assert first.json() == {"sub": _LOGIN, "removed": 1}
    assert second.status_code == 404, second.text
    assert second.json()["detail"] == "principal_not_blocked"
    assert await _rows() == []
    (row,) = await _audit("principal.unblock")
    assert row.operator_sub == "admin-one"
    assert row.payload["removed_blocks"] == [{"scope": str(_TENANT_A), "reason": "reason a"}]


def _block_after_unblock(rows: list[AuditLog], *, unblocked_by: str, blocked_by: str) -> None:
    """One unblock row naming the old block, then one block row with the new reason."""
    assert [(r.payload["op_id"], r.operator_sub) for r in rows] == [
        ("principal.unblock", unblocked_by),
        ("principal.block", blocked_by),
    ]
    assert rows[0].payload["removed_blocks"] == [{"scope": str(_TENANT_A), "reason": "old reason"}]
    assert rows[1].payload["reason"] == "new reason"


async def test_a_console_block_during_a_console_unblock_waits_and_is_written(
    _key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed((str(_TENANT_A), "old reason"))
    held = _HeldUnblock(monkeypatch)
    async with (
        _console_client(_key, "admin-one") as one,
        _console_client(_key, "admin-two") as two,
    ):
        shown = await _shown(one, _LOGIN)
        held.during_the_next_unblock(
            lambda: two.post(_PAGE, data={"sub": _LOGIN, "reason": "new reason"})
        )
        unblocked = await one.post(_UNBLOCK, data={"sub": _LOGIN, "confirm": "yes", "shown": shown})
        blocked = await held.second_result()

    assert held.second_waited, "the block did not wait for the unblock"
    assert unblocked.status_code == 303, unblocked.text
    assert blocked.status_code == 303, blocked.text
    assert await _rows() == [(str(_TENANT_A), "new reason", "admin-two")]
    _block_after_unblock(
        await _audit("principal.unblock", "principal.block"),
        unblocked_by="admin-one",
        blocked_by="admin-two",
    )


async def test_a_rest_block_during_a_rest_unblock_waits_and_is_written(
    _key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed((str(_TENANT_A), "old reason"))
    held = _HeldUnblock(monkeypatch)
    async with _rest_client(_key, "admin-one") as one, _rest_client(_key, "admin-two") as two:
        held.during_the_next_unblock(
            lambda: two.post(_REST, json={"sub": _LOGIN, "reason": "new reason"})
        )
        unblocked = await one.delete(f"{_REST}/{_LOGIN}")
        blocked = await held.second_result()

    assert held.second_waited, "the block did not wait for the unblock"
    assert unblocked.status_code == 200, unblocked.text
    assert blocked.status_code == 201, blocked.text
    assert blocked.json()["reason"] == "new reason"
    assert await _rows() == [(str(_TENANT_A), "new reason", "admin-two")]
    _block_after_unblock(
        await _audit("principal.unblock", "principal.block"),
        unblocked_by="admin-one",
        blocked_by="admin-two",
    )
