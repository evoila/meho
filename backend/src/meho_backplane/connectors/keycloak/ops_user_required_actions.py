# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One-time code (OTP) set-up for Keycloak users.

Two ops:

``keycloak.user.required_actions.update`` (write, waits for approval)
    Adds or removes named required actions on one user. The usual use is
    ``CONFIGURE_TOTP``: Keycloak then makes the user set up a one-time code
    app at the next login, and after that asks for a code at every login.

``keycloak.user.otp_status`` (safe read)
    Lists users with their required actions and whether each one has a
    one-time code set up. It reads only the credential *type* of each user.
    It never returns a credential value, label or other credential field.

How the write works
===================

Keycloak's ``PUT /users/{id}`` replaces the whole ``requiredActions`` list.
So the op reads the user first, builds the new list (other actions stay),
and writes that same read back with the new list, with the read-merge-write
step the update ops use
(:func:`~meho_backplane.connectors.keycloak.ops_write_merge.read_merge_write`).
The list is built from the read that is written, so no second read can
bring in actions the new list does not have.
Each name to add must be an **enabled** required action of the realm
(``GET .../authentication/required-actions``). An unknown or disabled name
is refused before anything is written, and the error lists the allowed names.
When the list would not change, nothing is written.

After approval, the op acts on the user id shown on the approval card. It
does not look the username up again, so a name that moved to another
account while the request waited cannot redirect the write. A card with no
user id is refused.

A request that gives both ``id`` and ``username`` must name one user: the
username stored in Keycloak for that id must match. Otherwise the request
is refused before it waits for approval, and again when the approved
request runs. Nothing is written. The card and the result show the
username stored in Keycloak whenever the user could be read.

When the realm is the one MEHO's own sign-in uses and an added action sets
up a passkey, the card says that the next sign-in can add a new passkey.

Why a new read, not ``keycloak.user.list``
==========================================

Keycloak's user list answers a short form by default. It has no
``requiredActions`` and no credential information. ``otp_status`` asks for
the full form and then reads each user's credential types, one request per
user, so ``max`` is capped. A user whose credentials cannot be read gets
``has_otp: null`` (unknown), never ``false``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _REQUIRED_ACTIONS_PATH,
    _USER_CREDENTIALS_PATH,
    _USER_PATH,
    _USERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_read import KeycloakOp
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
    get_or_none,
)
from meho_backplane.connectors.keycloak.ops_write_merge import read_merge_write
from meho_backplane.connectors.keycloak.ops_write_preview import _managed_realm, _opt_str
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations._park_check import register_park_check
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)
from meho_backplane.targets.keycloak_realm_warnings import (
    backplane_login_realms as _backplane_login_realms,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "OTP_STATUS_OP_ID",
    "REQUIRED_ACTIONS_UPDATE_OP_ID",
    "USER_REQUIRED_ACTION_OPS",
    "keycloak_user_otp_status",
    "keycloak_user_required_actions_update",
]

REQUIRED_ACTIONS_UPDATE_OP_ID = "keycloak.user.required_actions.update"
OTP_STATUS_OP_ID = "keycloak.user.otp_status"

#: The required action that makes a user set up a one-time code app.
_OTP_REQUIRED_ACTION = "CONFIGURE_TOTP"
#: Credential type Keycloak uses for a one-time code.
_OTP_CREDENTIAL_TYPE = "otp"
#: Required actions that let the next sign-in add a passkey.
_PASSKEY_REQUIRED_ACTIONS = frozenset({"webauthn-register", "webauthn-register-passwordless"})
_PASSKEY_WARNING = "this lets the next sign-in to this account add a new passkey"
_OTHER_USER_REASON = (
    f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: 'id' and 'username' belong to different users. "
    "Nothing was changed. Send the request with one user."
)

_DEFAULT_MAX_USERS = 100
_MAX_USERS = 500
#: How many credential reads run at the same time.
_CREDENTIAL_READ_CONCURRENCY = 8
_UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "pattern": _UUID_PATTERN, "description": "User internal UUID."},
        "username": {
            "type": "string",
            "description": (
                "Username (resolved to the UUID if id is absent). With id, it must be "
                "the username of that user."
            ),
        },
        "add": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 20,
            "description": "Required action names to add, for example ['CONFIGURE_TOTP'].",
        },
        "remove": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 20,
            "description": "Required action names to remove.",
        },
    },
    "allOf": [
        {"anyOf": [{"required": ["id"]}, {"required": ["username"]}]},
        {"anyOf": [{"required": ["add"]}, {"required": ["remove"]}]},
    ],
    "additionalProperties": False,
    "examples": [{"username": "alice", "add": ["CONFIGURE_TOTP"]}],
}

_UPDATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "username": {"type": ["string", "null"]},
        "realm": {"type": "string"},
        "changed": {"type": "boolean", "description": "False when nothing had to be written."},
        "added": {"type": "array", "items": {"type": "string"}},
        "removed": {"type": "array", "items": {"type": "string"}},
        "required_actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The user's required actions after the call.",
        },
        "unrequested_changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Fields that changed without being asked for (names only).",
        },
        "read_back_checked": {"type": "boolean"},
    },
    "required": ["id", "realm", "changed", "added", "removed", "required_actions"],
}

_OTP_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {
            "type": "string",
            "description": "Keycloak's username filter (a substring match).",
        },
        "max": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_USERS,
            "description": (
                f"How many users to check (default {_DEFAULT_MAX_USERS}, most {_MAX_USERS}). "
                "One credential read runs per user."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{"username": "adm", "max": 50}],
}

_OTP_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "username": {"type": ["string", "null"]},
                    "enabled": {"type": ["boolean", "null"]},
                    "required_actions": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "Null when Keycloak did not return the list.",
                    },
                    "has_otp": {
                        "type": ["boolean", "null"],
                        "description": "Null means unknown: the credentials could not be read.",
                    },
                    "otp_setup_pending": {
                        "type": ["boolean", "null"],
                        "description": "True when CONFIGURE_TOTP is set on the user.",
                    },
                },
                "required": ["id", "has_otp"],
            },
        },
        "total": {"type": "integer"},
        "unknown": {"type": "integer", "description": "Rows whose has_otp is null."},
        "truncated": {
            "type": "boolean",
            "description": "True when more users exist than max. Narrow with username.",
        },
        "skipped_no_id": {
            "type": "integer",
            "description": "User rows left out because Keycloak gave them no id.",
        },
    },
    "required": ["rows", "total", "unknown", "truncated", "skipped_no_id"],
}

_WHEN_TO_USE_UPDATE = (
    "Use to make a user set up a one-time code (OTP) at the next login: add "
    "``CONFIGURE_TOTP`` with ``keycloak.user.required_actions.update``. Other "
    "required actions of the user stay. A second person must approve."
)

USER_REQUIRED_ACTION_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=REQUIRED_ACTIONS_UPDATE_OP_ID,
        handler_attr="user_required_actions_update",
        summary="Add or remove required actions on one Keycloak user (approval-gated).",
        description=(
            "Adds or removes named required actions on one user; other actions "
            "stay. A name to add must be an enabled action of the realm, else it "
            "is refused and the error lists the allowed names. Add "
            "``CONFIGURE_TOTP`` for a one-time code at next login. "
            "requires_approval=True."
        ),
        parameter_schema=_UPDATE_SCHEMA,
        response_schema=_UPDATE_RESPONSE_SCHEMA,
        group_key="user_write",
        tags=("write", "user", "otp", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_UPDATE,
            "when_to_call": (
                "To make a user set up a one-time code (OTP) at the next login, or "
                "to take a pending required action off a user."
            ),
            "preconditions": (
                "The user exists in the target's realm, and each name to add is an "
                "enabled required action of that realm. A second person must approve."
            ),
            "parameter_hints": {
                "id": "User internal UUID (from keycloak.user.list).",
                "username": (
                    "Username; resolved to the UUID when id is absent. With id, it must "
                    "name the same user, else the request is refused."
                ),
                "add": "Names such as ['CONFIGURE_TOTP'] (upper case, as Keycloak spells them).",
                "remove": "Names to take off the user.",
            },
            "output_shape": (
                "``{id, username, realm, changed, added, removed, required_actions, "
                "unrequested_changes, read_back_checked}``. No credential data."
            ),
            "next_step": "Run keycloak.user.otp_status to see who has a one-time code.",
        },
    ),
    KeycloakOp(
        op_id=OTP_STATUS_OP_ID,
        handler_attr="user_otp_status",
        summary="Show each user's required actions and whether a one-time code is set up.",
        description=(
            "Lists users with their required actions, and ``has_otp`` from the "
            "credential types of each user (one read per user, capped by ``max``). "
            "``has_otp`` is null when a user's credentials could not be read. "
            "``truncated`` is true when more users exist than ``max``. Never "
            "returns a credential value."
        ),
        parameter_schema=_OTP_STATUS_SCHEMA,
        response_schema=_OTP_STATUS_RESPONSE_SCHEMA,
        group_key="user",
        tags=("read-only", "user", "otp", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "To check which users have a one-time code set up, or which still "
                "have CONFIGURE_TOTP pending."
            ),
            "when_to_use": (
                "To check which users have a one-time code set up, or which still "
                "have CONFIGURE_TOTP pending, for example after "
                "keycloak.user.required_actions.update."
            ),
            "parameter_hints": {
                "username": "Optional filter; Keycloak matches it as a substring.",
                "max": f"Users to check (default {_DEFAULT_MAX_USERS}, most {_MAX_USERS}).",
            },
            "output_shape": (
                "``{rows: [{id, username, enabled, required_actions, has_otp, "
                "otp_setup_pending}], total, unknown, truncated, skipped_no_id}``."
            ),
            "next_step": (
                "When truncated is true, narrow with username. A null has_otp means "
                "unknown: do not read it as no code."
            ),
        },
    ),
)


def _names(value: Any) -> list[str]:
    """Return the distinct, trimmed names of a list param, in order."""
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        name = item.strip() if isinstance(item, str) else ""
        if name and name not in seen:
            seen.append(name)
    return seen


def _apply(current: list[str], add: list[str], remove: list[str]) -> list[str]:
    """Return *current* without *remove*, then with the new *add* names at the end."""
    kept = [name for name in current if name not in remove]
    return kept + [name for name in add if name not in kept]


def _string_list(value: Any) -> list[str] | None:
    """Return *value* when it is a list of strings, else ``None``."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


async def _enabled_required_actions(
    self: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> list[str]:
    """Aliases of the realm's enabled required actions (sorted)."""
    rows = await self._get_admin_list(
        target,
        fill_path(_REQUIRED_ACTIONS_PATH, {"realm": realm}),
        operator=operator,
        strict=True,
    )
    return sorted(
        str(row["alias"])
        for row in rows
        if isinstance(row, dict) and row.get("enabled") is True and row.get("alias")
    )


def _parse_change(params: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the ``(add, remove)`` name lists, or raise :exc:`ValueError` for a bad request."""
    add = _names(params.get("add"))
    remove = _names(params.get("remove"))
    if not add and not remove:
        raise ValueError(f"{REQUIRED_ACTIONS_UPDATE_OP_ID} needs 'add' or 'remove'")
    both = [name for name in add if name in remove]
    if both:
        raise ValueError(f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: in both add and remove: {both}")
    return add, remove


def _approved_user_id() -> str | None:
    """The user id on the approval card, during the approved run of this op.

    ``None`` when the call is not the approved run of a request. An approved
    run whose card shows no user id is refused: MEHO cannot tell which user
    the approver saw, and looking the username up again could find another
    account.
    """
    preview = approved_preview(REQUIRED_ACTIONS_UPDATE_OP_ID)
    if preview is None:
        return None
    uuid = _opt_str(preview.get("id"))
    if uuid is None:
        raise ValueError(
            f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: the approval card shows no user id, so MEHO "
            "cannot tell which user was approved. Nothing was changed. Send a new request."
        )
    return uuid


def _same_user(params: dict[str, Any], user: dict[str, Any]) -> bool:
    """Whether the request's ``username`` (if any) is the username stored for *user*."""
    asked = _opt_str(params.get("username"))
    if asked is None:
        return True
    stored = _opt_str(user.get("username"))
    return stored is not None and stored.casefold() == asked.casefold()


async def _read_user(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Resolve the user of the request. Returns ``(uuid, path, user representation)``.

    Raises :exc:`KeycloakUserNotFoundError` for a user that does not exist,
    and :exc:`ValueError` when the request's username is not this user's.
    """
    realm = resolve_realm_config(target).managed_realm
    uuid = _approved_user_id()
    username = _opt_str(params.get("username"))
    if uuid is None:
        uuid = _opt_str(params.get("id"))
    if uuid is None:
        if username is None:
            raise ValueError(f"{REQUIRED_ACTIONS_UPDATE_OP_ID} requires either 'id' or 'username'")
        uuid = await self._find_user_uuid(target, realm, username, operator=operator)
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: no user with username={username!r} "
                f"in realm {realm!r}"
            )
    path = fill_path(_USER_PATH, {"realm": realm, "user-id": uuid})
    user = await get_or_none(self, target, path, operator)
    if user is None:
        raise KeycloakUserNotFoundError(
            f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: no user with id={uuid!r} in realm {realm!r}"
        )
    if not _same_user(params, user):
        raise ValueError(_OTHER_USER_REASON)
    return uuid, path, user


async def _refuse_unusable_names(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    add: list[str],
) -> None:
    """Raise :exc:`ValueError` unless every name in *add* is an enabled required action."""
    realm = resolve_realm_config(target).managed_realm
    allowed = await _enabled_required_actions(self, operator, target, realm)
    unknown = [name for name in add if name not in allowed]
    if unknown:
        raise ValueError(
            f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: {unknown} "
            f"{'is' if len(unknown) == 1 else 'are'} not enabled required actions "
            f"in realm {realm!r}. Allowed names: {allowed}"
        )


async def keycloak_user_required_actions_update(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Add or remove required actions on one user.

    Op-id: ``keycloak.user.required_actions.update``. Raises
    :exc:`ValueError` for no change asked, a name in both lists, or an
    added name that is not an enabled required action of the realm.
    Raises :exc:`KeycloakUserNotFoundError` for a user that does not exist.
    """
    realm = resolve_realm_config(target).managed_realm
    add, remove = _parse_change(params)
    if add:
        await _refuse_unusable_names(self, operator, target, add)
    uuid, path, user = await _read_user(self, operator, target, params)
    current = _string_list(user.get("requiredActions"))
    if current is None:
        raise KeycloakUnexpectedResponseError(
            f"user {uuid!r} came back without a usable requiredActions list; nothing was written"
        )
    resulting = _apply(current, add, remove)
    result: dict[str, Any] = {
        "id": uuid,
        "username": _opt_str(user.get("username")) or _opt_str(params.get("username")),
        "realm": realm,
        "changed": resulting != current,
        "added": [name for name in add if name not in current],
        "removed": [name for name in remove if name in current],
        "required_actions": resulting,
        "unrequested_changes": [],
        "read_back_checked": False,
    }
    if resulting == current:
        return result
    outcome = await read_merge_write(
        self, operator, target, path=path, requested={"requiredActions": resulting}, before=user
    )
    result["unrequested_changes"] = outcome["unrequested_changes"]
    result["read_back_checked"] = outcome["read_back_checked"]
    return result


def _credentials_path(realm: str, user_id: str) -> str:
    return fill_path(_USER_CREDENTIALS_PATH, {"realm": realm, "user-id": user_id})


async def _has_otp(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    path: str,
    gate: asyncio.Semaphore,
) -> bool | None:
    """Whether the user has a one-time code; ``None`` when it cannot be read.

    Reads only the ``type`` of each credential. A 401 is raised, so the
    dispatcher can renew the admin login.
    """
    async with gate:
        try:
            rows = await self._get_admin_list(target, path, operator=operator, strict=True)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise
            return None
        except (httpx.HTTPError, ValueError):
            return None
    return any(isinstance(row, dict) and row.get("type") == _OTP_CREDENTIAL_TYPE for row in rows)


def _status_row(user: dict[str, Any], has_otp: bool | None) -> dict[str, Any]:
    """One answer row: the identity fields, the required actions and the OTP flags."""
    row: dict[str, Any] = {key: user.get(key) for key in ("id", "username", "enabled")}
    actions = _string_list(user.get("requiredActions"))
    row["required_actions"] = actions
    row["has_otp"] = has_otp
    row["otp_setup_pending"] = None if actions is None else _OTP_REQUIRED_ACTION in actions
    return row


async def keycloak_user_otp_status(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Show each user's required actions and whether a one-time code is set up.

    Op-id: ``keycloak.user.otp_status``. Returns
    ``{rows, total, unknown, truncated, skipped_no_id}``. A row's
    ``has_otp`` is ``null`` when that user's credentials could not be read.
    ``skipped_no_id`` counts user rows left out because they had no id. No
    credential value is ever returned.
    """
    realm = resolve_realm_config(target).managed_realm
    asked = params.get("max")
    limit = asked if isinstance(asked, int) and 0 < asked <= _MAX_USERS else _DEFAULT_MAX_USERS
    # One more than the limit, so a longer list is visible as ``truncated``.
    query: dict[str, Any] = {"briefRepresentation": "false", "max": limit + 1}
    username = _opt_str(params.get("username"))
    if username is not None:
        query["username"] = username
    users = await self._get_admin_list(
        target,
        fill_path(_USERS_PATH, {"realm": realm}),
        operator=operator,
        params=query,
        strict=True,
    )
    truncated = len(users) > limit
    kept = [
        redact_secret_fields(user)
        for user in users[:limit]
        if isinstance(user, dict) and isinstance(user.get("id"), str) and user["id"]
    ]
    skipped = len(users[:limit]) - len(kept)
    gate = asyncio.Semaphore(_CREDENTIAL_READ_CONCURRENCY)
    flags = await asyncio.gather(
        *(
            _has_otp(self, operator, target, _credentials_path(realm, user["id"]), gate)
            for user in kept
        )
    )
    rows = [_status_row(user, has_otp) for user, has_otp in zip(kept, flags, strict=True)]
    return {
        "rows": rows,
        "total": len(rows),
        "unknown": sum(1 for row in rows if row["has_otp"] is None),
        "truncated": truncated,
        "skipped_no_id": skipped,
    }


async def _required_actions_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card: the user, the realm, and what is added or removed (with before and after).

    The before and after come from one live read of the user, and the card
    then shows the username stored in Keycloak. When the user cannot be read
    (no live connector, an unknown name, a fault), the card still shows the
    request and says ``current_available: false``.
    """
    add = _names(ctx.params.get("add"))
    remove = _names(ctx.params.get("remove"))
    preview: dict[str, Any] = {
        "resource": "user_required_actions",
        "username": _opt_str(ctx.params.get("username")),
        "id": _opt_str(ctx.params.get("id")),
        "realm": _managed_realm(ctx),
        "add": add,
        "remove": remove,
        "current_available": False,
    }
    if _adds_passkey_on_login_realm(preview["realm"], add):
        preview["warning"] = _PASSKEY_WARNING
    connector = ctx.connector_instance
    target = ctx.target
    if connector is None or target is None:
        return preview
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    if not isinstance(connector, KeycloakConnector):
        return preview
    try:
        realm = resolve_realm_config(target).managed_realm
        uuid = preview["id"]
        if uuid is None and preview["username"] is not None:
            uuid = await connector._find_user_uuid(
                target, realm, preview["username"], operator=ctx.operator
            )
        if uuid is None:
            return preview
        user = await get_or_none(
            connector,
            target,
            fill_path(_USER_PATH, {"realm": realm, "user-id": uuid}),
            ctx.operator,
        )
    except Exception:
        return preview
    current = _string_list(user.get("requiredActions")) if user is not None else None
    if user is None or current is None:
        return preview
    preview["id"] = uuid
    preview["username"] = _opt_str(user.get("username")) or preview["username"]
    preview["current_available"] = True
    preview["current_required_actions"] = current
    preview["resulting_required_actions"] = _apply(current, add, remove)
    return preview


def _adds_passkey_on_login_realm(realm: str | None, add: list[str]) -> bool:
    """Whether *add* sets up a passkey in the realm MEHO's own sign-in uses."""
    if realm is None or not _PASSKEY_REQUIRED_ACTIONS.intersection(add):
        return False
    return realm.casefold() in {name.casefold() for name in _backplane_login_realms()}


async def _refuse_id_of_another_user(ctx: PreviewContext) -> str | None:
    """Refuse a request whose ``id`` and ``username`` name different users.

    Runs before the request waits for approval. A user id that does not
    exist is let through: the run then fails with "not found". MEHO cannot
    check without a live Keycloak connector, so it refuses then.
    """
    uuid = _opt_str(ctx.params.get("id"))
    if uuid is None or _opt_str(ctx.params.get("username")) is None:
        return None
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if not isinstance(connector, KeycloakConnector) or ctx.target is None:
        return (
            f"{REQUIRED_ACTIONS_UPDATE_OP_ID}: MEHO could not check that 'id' and "
            "'username' name the same user. Send only one of them."
        )
    realm = resolve_realm_config(ctx.target).managed_realm
    path = fill_path(_USER_PATH, {"realm": realm, "user-id": uuid})
    user = await get_or_none(connector, ctx.target, path, ctx.operator)
    if user is None or _same_user(ctx.params, user):
        return None
    return _OTHER_USER_REASON


register_park_check(REQUIRED_ACTIONS_UPDATE_OP_ID, _refuse_id_of_another_user)
register_preview_builder(REQUIRED_ACTIONS_UPDATE_OP_ID, _required_actions_update_preview)
