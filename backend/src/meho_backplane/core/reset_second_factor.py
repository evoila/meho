# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reset the second factor of one local account (#4746).

``sudo meho-host reset-second-factor <user>`` runs this module in the core
pod with ``kubectl exec``::

    python -m meho_backplane.core.reset_second_factor --by <linux user> --host <host> -- <user>

It is the way back in for a person of realm ``meho`` who lost the one-time
code app and the recovery codes. It logs in to the Keycloak Admin REST API
as ``meho-admin`` with the client secret the pod already has
(``KEYCLOAK_ADMIN_CLIENT_SECRET``), so the secret never leaves the cluster
and is never printed. Then it:

1. finds the user by exact user name (Keycloak ignores case in user names);
2. checks that it can reach the MEHO database, before it changes anything;
3. deletes the user's credentials of type ``otp`` and
   ``recovery-authn-codes``;
4. adds the required actions ``UPDATE_PASSWORD`` and ``CONFIGURE_TOTP``, so
   the next login asks for a new password and a new code app;
5. writes one ``audit_log`` row: the Linux user, the host, the Keycloak
   user, what was removed and the time. The row is written also when step
   3 or 4 fails part-way, and it then says so.

Exit code ``0`` when all is done and the audit row is written. Exit code
``3`` on any failure, after one ``[FAIL]`` line; ``meho-host`` turns it into
its own exit code 1. Exit code ``2`` for a wrong command line. See
docs/codebase/linux-server-meho-host.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import text

from meho_backplane.core.keycloak_api import REALM, Admin, HttpAdmin, RealmStageError
from meho_backplane.db.engine import dispose_engine, get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.memory.audit import INTERNAL_METHOD
from meho_backplane.settings import get_settings

__all__ = [
    "AUDIT_PATH",
    "EXIT_DONE",
    "EXIT_FAILED",
    "NEXT_LOGIN_ACTIONS",
    "OPERATOR_SUB",
    "SECOND_FACTOR_TYPES",
    "Reset",
    "main",
    "reset_second_factor",
]

EXIT_DONE = 0
EXIT_FAILED = 3
#: The credential types of the second factor: the one-time code and the
#: recovery codes (Keycloak 26.3 and later).
SECOND_FACTOR_TYPES = ("otp", "recovery-authn-codes")
_TYPE_WORDS = {"otp": "the one-time code", "recovery-authn-codes": "the recovery codes"}
NEXT_LOGIN_ACTIONS = ("UPDATE_PASSWORD", "CONFIGURE_TOTP")
OPERATOR_SUB = "system:meho-host"
AUDIT_PATH = "meho-host.reset-second-factor"
_USERS = f"/admin/realms/{REALM}/users"
_HTTP_TIMEOUT_S = 10.0


class ResetError(Exception):
    """A failure, as one plain sentence with no secret in it."""


@dataclass
class Reset:
    """What one run found and changed. The audit row is made from it."""

    username: str
    user_id: str = ""
    tenant_id: uuid.UUID | None = None
    removed: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.required_actions)


def _say(line: str) -> None:
    print(line, flush=True)


def _fail(problem: str, fix: str) -> int:
    print(f"[FAIL] {problem} Fix: {fix}", file=sys.stderr, flush=True)
    return EXIT_FAILED


async def _get(admin: Admin, path: str, what: str, **params: str) -> Any:
    try:
        reply = await admin.call("GET", path, params=params or None)
    except RealmStageError as exc:
        raise ResetError(f"{what}: {exc}") from None
    if reply.status != 200:
        raise ResetError(f"{what}: Keycloak answered HTTP {reply.status}")
    return reply.data


async def find_user(admin: Admin, username: str) -> dict[str, Any] | None:
    """The full user with this user name, or ``None`` when there is none."""
    found = await _get(
        admin, _USERS, f"cannot look up user {username}", username=username, exact="true"
    )
    if not isinstance(found, list):
        raise ResetError(f"cannot look up user {username}: the answer is not a list")
    wanted = username.strip().lower()
    users = [
        u for u in found if isinstance(u, dict) and str(u.get("username", "")).lower() == wanted
    ]
    if not users:
        return None
    if len(users) > 1 or not users[0].get("id"):
        raise ResetError(f"Keycloak gave no single user for the name {username}")
    path = f"{_USERS}/{quote(str(users[0]['id']), safe='')}"
    user = await _get(admin, path, f"cannot read user {username}")
    if not isinstance(user, dict) or user.get("id") != users[0]["id"]:
        raise ResetError(f"cannot read user {username}: the answer is not that user")
    return user


async def _tenant_of(admin: Admin, user_path: str, username: str) -> uuid.UUID | None:
    """The one tenant of the user's groups (``meho_tenant`` is ``<uuid>:<name>:<role>``).

    ``None`` when the groups name no tenant or more than one.
    """
    groups = await _get(
        admin,
        f"{user_path}/groups",
        f"cannot read the groups of {username}",
        briefRepresentation="false",
    )
    tenants: set[uuid.UUID] = set()
    for group in groups if isinstance(groups, list) else []:
        attributes = group.get("attributes") if isinstance(group, dict) else None
        for value in (attributes or {}).get("meho_tenant") or []:
            try:
                tenants.add(uuid.UUID(str(value).split(":", 1)[0]))
            except ValueError:
                continue
    return tenants.pop() if len(tenants) == 1 else None


async def _change(admin: Admin, user: dict[str, Any], result: Reset) -> None:
    """Delete the second factor and set the next-login actions; record each step in *result*."""
    user_path = f"{_USERS}/{quote(result.user_id, safe='')}"
    credentials = await _get(
        admin, f"{user_path}/credentials", f"cannot read the credentials of {result.username}"
    )
    for credential in credentials if isinstance(credentials, list) else []:
        kind = credential.get("type") if isinstance(credential, dict) else None
        if kind not in SECOND_FACTOR_TYPES or not credential.get("id"):
            continue
        try:
            reply = await admin.call(
                "DELETE", f"{user_path}/credentials/{quote(str(credential['id']), safe='')}"
            )
        except RealmStageError as exc:
            raise ResetError(f"cannot remove {_TYPE_WORDS[kind]}: {exc}") from None
        if reply.status not in (204, 404):
            raise ResetError(
                f"cannot remove {_TYPE_WORDS[kind]}: Keycloak answered HTTP {reply.status}"
            )
        if reply.status == 204:
            result.removed.append(kind)
    current = [str(a) for a in user.get("requiredActions") or []]
    wanted = current + [a for a in NEXT_LOGIN_ACTIONS if a not in current]
    # PUT replaces the whole list, so the user read above goes back with the new list.
    try:
        reply = await admin.call("PUT", user_path, json={**user, "requiredActions": wanted})
    except RealmStageError as exc:
        raise ResetError(f"cannot set what the next login asks for: {exc}") from None
    if reply.status != 204:
        raise ResetError(
            f"cannot set what the next login asks for: Keycloak answered HTTP {reply.status}"
        )
    result.required_actions = list(NEXT_LOGIN_ACTIONS)


async def reset_second_factor(
    admin: Admin,
    username: str,
    *,
    check_database: Callable[[], Awaitable[None]],
) -> Reset | None:
    """Do steps 1 to 4 of the module docstring. ``None`` when the user is unknown.

    A failure in step 3 or 4 is kept in :attr:`Reset.error`, so the caller
    still writes the audit row of what changed.
    """
    user = await find_user(admin, username)
    if user is None:
        return None
    result = Reset(username=str(user.get("username") or username), user_id=str(user["id"]))
    user_path = f"{_USERS}/{quote(result.user_id, safe='')}"
    result.tenant_id = await _tenant_of(admin, user_path, result.username)
    try:
        await check_database()
    except Exception as exc:
        raise ResetError(
            "cannot reach the MEHO database, so no audit row could be written: "
            f"{type(exc).__name__}"
        ) from None
    try:
        await _change(admin, user, result)
    except ResetError as exc:
        result.error = str(exc)
    return result


async def _check_database() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(text("SELECT 1"))


async def write_audit_row(result: Reset, *, by: str, host: str, duration_ms: float) -> uuid.UUID:
    """Commit the one audit row of a run that changed something."""
    audit_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=audit_id,
                event_class=AuditEventClass.OPERATION_EXECUTION,
                occurred_at=datetime.now(UTC),
                operator_sub=OPERATOR_SUB,
                tenant_id=result.tenant_id,
                method=INTERNAL_METHOD,
                path=AUDIT_PATH,
                status_code=500 if result.error else 200,
                duration_ms=Decimal(str(round(duration_ms, 3))),
                payload={
                    "op_id": AUDIT_PATH,
                    "linux_user": by,
                    "host": host,
                    "keycloak_user": result.username,
                    "keycloak_user_id": result.user_id,
                    "removed_credentials": result.removed,
                    "required_actions_set": result.required_actions,
                    "result": "error" if result.error else "ok",
                    **({"error": result.error} if result.error else {}),
                },
            )
        )
        await session.commit()
    return audit_id


def _report(result: Reset) -> None:
    for kind in SECOND_FACTOR_TYPES:
        if kind in result.removed:
            _say(f"[ok]   removed {_TYPE_WORDS[kind]} of {result.username}")
    if not result.removed:
        _say(f"[note] {result.username} had no one-time code and no recovery codes.")
    if result.required_actions:
        _say(
            f"[ok]   the next login of {result.username} asks for a new password and a new "
            "one-time code app, and gives twelve new recovery codes"
        )


async def _run(admin: Admin, args: argparse.Namespace, secret: str, client_id: str) -> int:
    started = time.monotonic()
    try:
        if not await admin.login(REALM, client_id, secret):
            return _fail(
                f"Keycloak refused the login of {client_id}. Nothing was changed.", _SUPPORT
            )
        result = await reset_second_factor(admin, args.user, check_database=_check_database)
    except (ResetError, RealmStageError) as exc:
        return _fail(f"{exc}. Nothing was changed.", _SUPPORT)
    if result is None:
        return _fail(
            f"there is no user {args.user} in realm {REALM}. Nothing was changed.",
            "check the user name, for example in the Keycloak admin console, "
            "and run the command again.",
        )
    if not result.changed:
        return _fail(f"{result.error}. Nothing was changed.", _SUPPORT)
    _report(result)
    try:
        audit_id = await write_audit_row(
            result, by=args.by, host=args.host, duration_ms=(time.monotonic() - started) * 1000
        )
    except Exception as exc:
        return _fail(
            "the changes above were made, but the audit row could not be written "
            f"({type(exc).__name__}).",
            f"run the command again, so the audit holds the reset. Then {_SUPPORT}",
        )
    _say(
        f"[ok]   wrote audit row {audit_id}: {args.by} on {args.host} "
        f"reset the second factor of {result.username}"
    )
    if result.error:
        return _fail(
            f"{result.error}. The audit row lists what was changed.",
            f"run the command again. If it fails again, {_SUPPORT}",
        )
    return EXIT_DONE


_SUPPORT = "run 'meho-host support-bundle' and send the file to support."


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    suffix = f"/admin/realms/{REALM}"
    admin_url = settings.keycloak_admin_url.rstrip("/")
    secret = settings.keycloak_admin_client_secret
    if not admin_url.endswith(suffix) or not secret or not settings.keycloak_admin_client_id:
        return _fail(
            f"the MEHO core has no Keycloak admin login for realm {REALM}. Nothing was changed.",
            _SUPPORT,
        )
    try:
        async with httpx.AsyncClient(
            base_url=admin_url[: -len(suffix)], timeout=_HTTP_TIMEOUT_S
        ) as http:
            return await _run(HttpAdmin(http), args, secret, settings.keycloak_admin_client_id)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meho_backplane.core.reset_second_factor")
    parser.add_argument("--by", required=True, help="the Linux user who ran meho-host")
    parser.add_argument("--host", required=True, help="the host name of the machine")
    parser.add_argument("user", help="the user name in realm meho")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not args.user.strip():
        print("the user name is empty", file=sys.stderr)
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
