# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A database error of the block table: the log line holds the step, the error type and code.

Each step of the block code that reads or writes the block table fails with
three faults: a database error whose statement values hold a made-up login
id and a made-up reason (or sign-in key), a refused connection, and a
time-out. The lines are read as the production log setup writes them,
after the request log context was merged into them. No made-up value is in
any line, no line of the call but the error line of the request holds a
traceback, and the line of the step holds the fields of the request log
context and its own step (``operation``), error type (``error_type``) and
error code (``error_code``). The call answers 503, or 500 when a block or
unblock write fails with an error that is not a database error, and when
the REST list cannot be read. The error that then leaves the app holds no
made-up value in its text, its repr or its chain.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import Select, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from meho_backplane.auth import principal_block
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import (
    BlockedPerson,
    PrincipalBlockUnavailableError,
    PrincipalBlockWriteError,
    block_principal,
    block_set_digest,
    reset_block_cache,
    unblock_principal,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PrincipalBlock
from meho_backplane.main import app
from meho_backplane.operations import approval_queue
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._production_log_lines import json_lines, production_logging, request_context_fields
from tests.test_ui_agent_grants import _bff_env, _csrf_headers, _seed_session_sync, _seed_tenant

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000db10c")
_CALLER = "admin-of-the-block-list"
_ROUTE = "/api/v1/principals/blocked"
_PAGE = "/ui/blocked-people"
_OWN_FIELDS = {"event", "level", "timestamp", "operation", "error_type", "error_code"}

#: Step -> (event, operation) of its log line.
_LINES = {
    "read_list": ("principal_block_read_failed", "read_list"),
    "read_blocks": ("principal_block_read_failed", "read_blocks"),
    "block": ("principal_block_write_failed", "block"),
    "unblock": ("principal_unblock_write_failed", "unblock"),
    "sign_in_lock": ("principal_unblock_sign_in_check_failed", "sign_in_check"),
    "sign_in_scan": ("principal_unblock_sign_in_check_failed", "sign_in_check"),
    "list": ("principal_block_list_read_failed", "list_blocked"),
}

#: The routes each step runs through, and the admin who calls them.
_CASES = [
    ("read_list", "rest_block", "platform"),
    ("read_blocks", "rest_unblock", "platform"),
    ("block", "rest_block", "platform"),
    ("block", "console_block", "platform"),
    ("unblock", "rest_unblock", "platform"),
    ("unblock", "console_unblock", "platform"),
    ("sign_in_lock", "rest_own_unblock", "platform"),
    ("sign_in_scan", "rest_own_unblock", "platform"),
    *[
        ("list", route, admin)
        for route in ("rest_list", "console_page", "console_unblock_list")
        for admin in ("platform", "tenant")
    ],
]

_WRITES = ("block", "unblock")

#: The lines that may hold a traceback: the error line of the request and
#: the line the web server writes for an error that leaves the app.
_ERROR_LINES = {"request_failed", "Exception in ASGI application\n"}


def _status(step: str, route: str, fault: str) -> int:
    """The status code of the call.

    A write fails with 500 on an error that is no database error, and the
    REST list fails with 500 on every error. Every other call answers 503.
    """
    if route == "rest_list" or (step in _WRITES and fault != "database"):
        return 500
    return 503


#: Fault -> the type name the log line names.
_ERROR_TYPES = {
    "database": "OperationalError",
    "refused": "ConnectionRefusedError",
    "timeout": "TimeoutError",
}

#: Fault -> the error code the log line names.
_ERROR_CODES = {
    "database": "57014",
    "refused": errno.errorcode[errno.ECONNREFUSED],
    "timeout": "none",
}


class _DriverError(Exception):
    """A driver error with a SQLSTATE, as the asyncpg driver gives one."""

    sqlstate = "57014"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "FRESH_SIGN_IN_MAX_AGE_SECONDS",
        "PER_ACTION_SIGN_IN_MAX_AGE_SECONDS",
        "KEYCLOAK_CLI_APPROVE_CLIENT_ID",
        "APPROVAL_SIGN_IN_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_block_cache()
    yield from _bff_env.__wrapped__(monkeypatch)
    reset_block_cache()


class _Recorder:
    """Keeps each error that leaves the app."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner
        self.errors: list[BaseException] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.inner(scope, receive, send)
        except Exception as exc:
            self.errors.append(exc)
            raise


def _fault(kind: str, login: str, value: str) -> Exception:
    if kind == "database":
        return OperationalError(
            "UPDATE principal_block SET reason=? WHERE sub=?",
            {"sub": login, "reason": value},
            _DriverError(f"no row for {login}: {value}"),
        )
    # The two other faults name the values too, so a chain to them shows.
    if kind == "refused":
        return ConnectionRefusedError(errno.ECONNREFUSED, f"Connection refused ({login} {value})")
    return TimeoutError(f"timed out ({login} {value})")


def _make_fail(step: str, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def _fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    if step == "read_list":
        monkeypatch.setattr(principal_block, "_load_entries", _fail)
    elif step == "read_blocks":
        execute = AsyncSession.execute

        async def _execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
            locks = getattr(statement, "_for_update_arg", None) is not None
            if isinstance(statement, Select) and locks and "principal_block" in str(statement):
                raise error
            return await execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", _execute)
    elif step == "list":
        execute = AsyncSession.execute

        async def _execute_list(
            self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            if isinstance(statement, Select) and "principal_block.blocked_at DESC" in str(
                statement
            ):
                raise error
            return await execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", _execute_list)
    elif step == "block":

        def _fail_now(*args: Any, **kwargs: Any) -> None:
            raise error

        monkeypatch.setattr(principal_block, "add_audit_row", _fail_now)
    elif step == "unblock":
        monkeypatch.setattr(principal_block, "_delete_read_blocks", _fail)
    elif step == "sign_in_lock":
        monkeypatch.setattr(approval_queue, "take_approval_lock", _fail)
    else:
        monkeypatch.setattr(principal_block, "earlier_sign_in_uses", _fail)


def _seed_block(sub: str, reason: str) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                PrincipalBlock(
                    sub=sub,
                    scope=str(_TENANT),
                    tenant_id=_TENANT,
                    reason=reason,
                    blocked_by_sub="another-admin",
                    blocked_at=datetime.now(UTC),
                )
            )

    asyncio.run(_do())


def _shown(sub: str) -> str:
    """The set of blocks of *sub* that the confirm page shows."""

    async def _do() -> str:
        async with get_sessionmaker()() as session:
            rows = (
                (await session.execute(select(PrincipalBlock).where(PrincipalBlock.sub == sub)))
                .scalars()
                .all()
            )
        return block_set_digest(
            BlockedPerson(r.sub, r.scope, r.reason, r.blocked_by_sub, r.blocked_at) for r in rows
        )

    return asyncio.run(_do())


def _token(key: Any, *, fresh: bool, platform: bool = True) -> str:
    claims: dict[str, Any] = {"platform_admin": True} if platform else {}
    if fresh:
        claims |= {"sid": f"sid-{uuid.uuid4().hex}", "auth_time": int(time.time())}
    return mint_token(
        key,
        sub=_CALLER,
        tenant_id=str(_TENANT),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        fresh_sign_in=fresh,
        extra_claims=claims,
    )


def _call(client: TestClient, key: Any, route: str, login: str, reason: str, admin: str) -> Any:
    platform = admin == "platform"
    if route.startswith("rest"):
        token = _token(key, fresh=route == "rest_own_unblock", platform=platform)
        headers = {"Authorization": f"Bearer {token}", "Meho-Tenant": str(_TENANT)}
        if route == "rest_list":
            return client.get(_ROUTE, headers=headers)
        if route == "rest_block":
            return client.post(_ROUTE, json={"sub": login, "reason": reason}, headers=headers)
        sub = _CALLER if route == "rest_own_unblock" else login
        return client.request("DELETE", f"{_ROUTE}/{sub}", json={"reason": reason}, headers=headers)
    session_id = _seed_session_sync(
        tenant_id=_TENANT,
        access_token=_token(key, fresh=False, platform=platform),
        operator_sub=_CALLER,
    )
    csrf = mint_csrf_token(str(session_id))
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    if route == "console_page":
        return client.get(_PAGE)
    if route == "console_unblock_list":
        return client.post(f"{_PAGE}/unblock", data={"sub": login}, headers=_csrf_headers(csrf))
    if route == "console_block":
        data = {"sub": login, "reason": reason}
        return client.post(_PAGE, data=data, headers=_csrf_headers(csrf))
    data = {"sub": login, "confirm": "yes", "shown": _shown(login)}
    return client.post(f"{_PAGE}/unblock", data=data, headers=_csrf_headers(csrf))


def _chain(error: BaseException) -> Iterator[BaseException]:
    seen: BaseException | None = error
    while seen is not None:
        yield seen
        seen = seen.__cause__ or seen.__context__


@pytest.mark.parametrize("fault", sorted(_ERROR_TYPES))
@pytest.mark.parametrize(("step", "route", "admin"), _CASES)
def test_a_database_error_of_the_block_table_logs_the_step_and_the_error_type_only(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    step: str,
    route: str,
    admin: str,
    fault: str,
) -> None:
    _seed_tenant(_TENANT, "block-table-errors")
    login = f"login-{uuid.uuid4().hex}"
    reason = f"moved to team {uuid.uuid4().hex}"
    sign_in_key = f"sign-in-{uuid.uuid4().hex}"
    made_up = (login, reason, sign_in_key)
    if route in ("rest_unblock", "console_unblock"):
        _seed_block(login, reason)
    if route == "rest_own_unblock":
        _seed_block(_CALLER, "own block")
    _make_fail(
        step, monkeypatch, _fault(fault, login, sign_in_key if "sign_in" in step else reason)
    )
    key = make_rsa_keypair(f"kid-{uuid.uuid4().hex[:8]}")
    recorder = _Recorder(app)
    capfd.readouterr()

    with production_logging(), respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        client = TestClient(recorder, raise_server_exceptions=False, follow_redirects=False)
        response = _call(client, key, route, login, reason, admin)
        for error in recorder.errors:
            # The line the web server writes for an error that leaves the app.
            logging.getLogger("uvicorn.error").error(
                "Exception in ASGI application\n", exc_info=error
            )
    out = capfd.readouterr().out

    status = _status(step, route, fault)
    assert response.status_code == status, response.text
    assert [value for value in made_up if value in out] == []
    lines = json_lines(out)
    with_traceback = [line["event"] for line in lines if "exception" in line]
    assert set(with_traceback) <= _ERROR_LINES, lines
    event, operation = _LINES[step]
    step_lines = [line for line in lines if line["event"] == event]
    assert len(step_lines) == 1, lines
    line = step_lines[0]
    assert set(line) == request_context_fields(lines) | _OWN_FIELDS
    assert line["level"] == "warning"
    assert line["operation"] == operation
    assert line["error_type"] == _ERROR_TYPES[fault]
    assert line["error_code"] == _ERROR_CODES[fault]
    if status == 500:
        assert len(recorder.errors) == 1
        for error in _chain(recorder.errors[0]):
            for value in made_up:
                assert value not in str(error)
                assert value not in repr(error)
    else:
        assert recorder.errors == []


@pytest.mark.parametrize("fault", sorted(_ERROR_TYPES))
@pytest.mark.parametrize("step", _WRITES)
def test_the_error_of_a_failed_block_or_unblock_write_has_the_error_type_only(
    monkeypatch: pytest.MonkeyPatch, step: str, fault: str
) -> None:
    _seed_tenant(_TENANT, "block-table-errors")
    login = f"login-{uuid.uuid4().hex}"
    reason = f"moved to team {uuid.uuid4().hex}"
    if step == "unblock":
        _seed_block(login, reason)
    _make_fail(step, monkeypatch, _fault(fault, login, reason))
    admin = Operator(
        sub=_CALLER,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )

    async def _write() -> None:
        if step == "block":
            await block_principal(admin, login, reason)
        else:
            await unblock_principal(admin, login, reason=reason)

    expected = PrincipalBlockUnavailableError if fault == "database" else PrincipalBlockWriteError
    with pytest.raises(expected) as raised:
        asyncio.run(_write())
    assert str(raised.value) == _ERROR_TYPES[fault]
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
