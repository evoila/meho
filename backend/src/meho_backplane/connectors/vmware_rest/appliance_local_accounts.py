# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The local accounts of the vCenter appliance: read them, set a password, set the expiry rules.

A local account is an operating-system account of the vCenter appliance
itself, for example ``root``. ``root`` is the login for the appliance
console, for SSH and for the management page on port 5480. When its
password expires, these logins stop working until someone sets a new one.

The four typed operations (their metadata is in
:mod:`.typed_ops_appliance_local_accounts`) call the appliance REST API on
the connector's own session, so they need no spec ingest:

* ``GET /appliance/local-accounts`` -- the user names;
* ``GET /appliance/local-accounts/{username}`` -- one account;
* ``PATCH /appliance/local-accounts/{username}`` -- a change. The body is
  the vendor ``UpdateConfig`` at the top level (no ``config`` wrapper), as
  the pinned 8.0 and 9.0 ``vcenter.yaml`` files declare it for ``/api``.

The new password is never an operation parameter; how it stays secret is in
:mod:`.appliance_local_account_password`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
import structlog

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.appliance_local_account_password import (
    PASSWORD_SET_OP_ID,
    password_refusal,
    read_new_password,
    vapi_error,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "ACCOUNT_FIELDS",
    "DAYS_MAX",
    "DAYS_MIN",
    "GET_OP_ID",
    "LIST_OP_ID",
    "PASSWORD_SET_OP_ID",
    "POLICY_DAY_FIELDS",
    "POLICY_FIELDS",
    "POLICY_FLAG_FIELDS",
    "POLICY_SET_OP_ID",
    "USERNAME_MAX_LENGTH",
    "USERNAME_PATTERN",
    "VENDOR_OP_IDS",
    "account_row",
    "expiry_status",
    "local_account_get_impl",
    "local_account_list_impl",
    "local_account_password_set_impl",
    "local_account_policy_set_impl",
    "policy_body",
]

_log = structlog.get_logger(__name__)

LIST_OP_ID: Final = "vmware.appliance.local_account.list"
GET_OP_ID: Final = "vmware.appliance.local_account.get"
POLICY_SET_OP_ID: Final = "vmware.appliance.local_account.policy.set"

_LIST_PATH: Final = "/appliance/local-accounts"
_ACCOUNT_PATH: Final = "/appliance/local-accounts/{username}"

#: The vendor calls, in the ``METHOD:/path`` form the reconcile lane checks.
VENDOR_OP_IDS: Final[frozenset[str]] = frozenset(
    {f"GET:{_LIST_PATH}", f"GET:{_ACCOUNT_PATH}", f"PATCH:{_ACCOUNT_PATH}"}
)

#: Letters, digits, ``.``, ``@`` and ``-``, starting and ending with a letter or
#: a digit: the rule vCenter uses for local user names. It also keeps ``.`` and
#: ``..`` out of the URL path.
USERNAME_PATTERN: Final = r"^[A-Za-z0-9](?:[A-Za-z0-9.@-]*[A-Za-z0-9])?$"
USERNAME_MAX_LENGTH: Final = 64
_USERNAME_RE: Final = re.compile(USERNAME_PATTERN)

#: The expiry fields of the vendor ``UpdateConfig``. The parameters have the
#: vendor names, so the body is the parameters that the call sets.
POLICY_FLAG_FIELDS: Final = ("password_expires", "inactive_after_password_expiration")
POLICY_DAY_FIELDS: Final = (
    "max_days_between_password_change",
    "min_days_between_password_change",
    "warn_days_before_password_expiration",
    "days_after_password_expiration",
)
POLICY_FIELDS: Final = POLICY_FLAG_FIELDS + POLICY_DAY_FIELDS

#: The day range vCenter accepts in its password policy: -1 to 99999.
DAYS_MIN: Final = -1
DAYS_MAX: Final = 99999

_DAY_FIELDS_READ: Final = (
    "max_days_between_password_change",
    "min_days_between_password_change",
    "warn_days_before_password_expiration",
)

#: The fields of one account, in output order (list rows add ``read_note``).
ACCOUNT_FIELDS: Final = (
    "username",
    "roles",
    "enabled",
    "has_password",
    "last_password_change",
    "password_expires_at",
    "password_expired",
    "days_until_password_expires",
    *_DAY_FIELDS_READ,
    "inactive_at",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """The current time in UTC. Tests replace it."""
    return datetime.now(UTC)


def _unwrap(payload: Any) -> Any:
    """The inner ``value`` of a legacy ``/rest`` answer, else the answer itself."""
    if isinstance(payload, dict) and set(payload) == {"value"}:
        return payload["value"]
    return payload


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _checked_username(params: Mapping[str, Any]) -> str:
    """Return ``params["username"]`` when it is a valid local user name.

    The parameter schema already refuses other names. This second check runs
    before any request, so no other text can reach the URL path.
    """
    username = params.get("username")
    if (
        not isinstance(username, str)
        or len(username) > USERNAME_MAX_LENGTH
        or _USERNAME_RE.fullmatch(username) is None
    ):
        raise ValueError(
            "username must be 1 to 64 letters, digits, '.', '@' or '-', and start and "
            "end with a letter or a digit"
        )
    return username


def _account_path(username: str) -> str:
    return _ACCOUNT_PATH.format(username=quote(username, safe="@"))


def _raise_if_not_found(exc: httpx.HTTPStatusError, username: str) -> None:
    """Raise the ``not_found`` error when vCenter answered 404 for *username*."""
    if exc.response.status_code == 404:
        raise ConnectorResourceNotFoundError(
            [username], f"vCenter has no local account named {username!r}"
        ) from None


def _failure_text(exc: Exception) -> str:
    """A short reason for a failed account read, for ``read_note``."""
    if isinstance(exc, httpx.HTTPStatusError):
        error_type, _ = vapi_error(exc.response)
        suffix = f" ({error_type})" if error_type else ""
        return f"HTTP {exc.response.status_code}{suffix}"
    if isinstance(exc, ConnectorResourceNotFoundError):
        return "vCenter does not have this account"
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Account rows
# ---------------------------------------------------------------------------


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def expiry_status(password_expires_at: str | None, now: datetime) -> dict[str, Any]:
    """Return ``password_expired`` and ``days_until_password_expires`` for one account.

    vCenter sends no expiry date when the password never expires: then
    ``password_expired`` is false and the day count is null. A date that does
    not parse gives null for both. The day count is whole days from *now*; it
    is negative after the expiry date.
    """
    if password_expires_at is None:
        return {"password_expired": False, "days_until_password_expires": None}
    expires_at = _parse_time(password_expires_at)
    if expires_at is None:
        return {"password_expired": None, "days_until_password_expires": None}
    days = math.floor((expires_at - now) / timedelta(days=1))
    return {"password_expired": expires_at <= now, "days_until_password_expires": days}


def account_row(username: str, info: Any, *, now: datetime) -> dict[str, Any]:
    """Project the vendor ``LocalAccounts.Info`` onto one account row.

    The vendor answer carries no password, only ``has_password``. Missing or
    malformed fields become null.
    """
    data = _unwrap(info)
    if not isinstance(data, Mapping):
        data = {}
    raw_roles = data.get("roles")
    roles = [r for r in raw_roles if isinstance(r, str)] if isinstance(raw_roles, list) else []
    expires_at = _text_or_none(data.get("password_expires_at"))
    row: dict[str, Any] = {
        "username": username,
        "roles": roles,
        "enabled": _bool_or_none(data.get("enabled")),
        "has_password": _bool_or_none(data.get("has_password")),
        "last_password_change": _text_or_none(data.get("last_password_change")),
        "password_expires_at": expires_at,
        **expiry_status(expires_at, now),
    }
    for field in _DAY_FIELDS_READ:
        row[field] = _int_or_none(data.get(field))
    row["inactive_at"] = _text_or_none(data.get("inactive_at"))
    return row


def _unread_row(username: str, note: str) -> dict[str, Any]:
    """A list row for an account whose read failed: every field null but the name."""
    row: dict[str, Any] = dict.fromkeys(ACCOUNT_FIELDS)
    row["username"] = username
    row["roles"] = []
    row["read_note"] = note
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _read_account(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    username: str,
) -> dict[str, Any]:
    """``GET /appliance/local-accounts/{username}`` as one account row; 404 is not_found."""
    path = await connector.mount_op_path(target, _account_path(username), operator)
    try:
        info = await connector._get_json(target, path, operator=operator)
    except httpx.HTTPStatusError as exc:
        _raise_if_not_found(exc, username)
        raise
    return account_row(username, info, now=_now())


async def _read_after(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    username: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the account after a change. Best effort: the change is already done."""
    try:
        return await _read_account(connector, operator, target, username), None
    except (httpx.HTTPError, ConnectorResourceNotFoundError) as exc:
        return None, (
            f"the change is done, but the read after it failed: {_failure_text(exc)}. "
            f"Read the account with {GET_OP_ID}."
        )


async def local_account_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vmware.appliance.local_account.list``: every local account, one row each.

    ``GET /appliance/local-accounts`` gives the user names only, so each name
    is read with ``GET /appliance/local-accounts/{username}``. A failed list
    read raises (a 403 means the service account lacks the appliance role). A
    failed account read gives a row with null fields and a ``read_note``.
    """
    del params  # the list takes no parameters
    path = await connector.mount_op_path(target, _LIST_PATH, operator)
    names = _unwrap(await connector._get_json(target, path, operator=operator))
    if not isinstance(names, list):
        raise RuntimeError(
            f"{LIST_OP_ID}: expected a list of user names from GET {_LIST_PATH}, "
            f"got {type(names).__name__}"
        )
    accounts: list[dict[str, Any]] = []
    for username in sorted({name for name in names if isinstance(name, str) and name}):
        try:
            row = await _read_account(connector, operator, target, username)
        except (httpx.HTTPError, ConnectorResourceNotFoundError) as exc:
            accounts.append(_unread_row(username, f"the read failed: {_failure_text(exc)}"))
            continue
        row["read_note"] = None
        accounts.append(row)
    _log.info("vmware_appliance_local_accounts_read", target=target.name, count=len(accounts))
    return {"accounts": accounts}


async def local_account_get_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vmware.appliance.local_account.get``: one account row; a missing account is not_found."""
    return await _read_account(connector, operator, target, _checked_username(params))


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------


async def local_account_password_set_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vmware.appliance.local_account.password.set``: one ``PATCH`` with the new password.

    Reads the password from Vault first, so a missing secret stops the call
    before vCenter is reached. After the change, one best-effort read gives the
    new expiry date. A 401 (the vCenter session ended) is raised unchanged, so
    the dispatcher logs in again and retries once.
    """
    username = _checked_username(params)
    password = await read_new_password(operator, target, params)
    path = await connector.mount_op_path(target, _account_path(username), operator)
    try:
        await connector._post_json(
            target, path, operator=operator, verb="PATCH", json={"password": password}
        )
    except httpx.HTTPStatusError as exc:
        _raise_if_not_found(exc, username)
        if exc.response.status_code == 401:
            raise
        raise password_refusal(exc, username=username, password=password) from None
    after, note = await _read_after(connector, operator, target, username)
    _log.info("vmware_appliance_local_account_password_set", target=target.name, username=username)
    return {
        "username": username,
        "password_set": True,
        "password_expires_at": after["password_expires_at"] if after else None,
        "read_note": note,
    }


def policy_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``UpdateConfig`` body: the expiry fields the call sets, and nothing else."""
    body: dict[str, Any] = {}
    for field in POLICY_FLAG_FIELDS:
        flag = params.get(field)
        if isinstance(flag, bool):
            body[field] = flag
    for field in POLICY_DAY_FIELDS:
        days = params.get(field)
        if isinstance(days, int) and not isinstance(days, bool):
            if not DAYS_MIN <= days <= DAYS_MAX:
                raise ValueError(f"{field} must be between {DAYS_MIN} and {DAYS_MAX}")
            body[field] = days
    if not body:
        raise ValueError(f"{POLICY_SET_OP_ID}: set at least one of {', '.join(POLICY_FIELDS)}")
    return body


async def local_account_policy_set_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vmware.appliance.local_account.policy.set``: read, ``PATCH`` the expiry fields, read.

    The first read finds a missing account (``not_found``) before any change
    and gives the values before. Only the fields the call sets are sent.
    """
    username = _checked_username(params)
    body = policy_body(params)
    before = await _read_account(connector, operator, target, username)
    path = await connector.mount_op_path(target, _account_path(username), operator)
    try:
        await connector._post_json(target, path, operator=operator, verb="PATCH", json=body)
    except httpx.HTTPStatusError as exc:
        _raise_if_not_found(exc, username)
        raise
    after, note = await _read_after(connector, operator, target, username)
    _log.info(
        "vmware_appliance_local_account_policy_set",
        target=target.name,
        username=username,
        fields=sorted(body),
    )
    return {
        "username": username,
        "requested": body,
        "before": before,
        "after": after,
        "read_note": note,
    }
