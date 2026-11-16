# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change a user's details: ``keycloak.user.update``.

The op changes the user name, the email, the first name and the last name of
one user. Only the fields in ``changes`` are sent; every other field of the
user stays. Other user fields have their own ops (turn a user off or on,
ask for a one-time code setup).

How a call runs:

1. Find the user (``id``, or the exact current ``username``).
2. Read the user, put ``changes`` on top and write the whole user back
   (the same read, merge and write as ``keycloak.client.update``, see
   :mod:`~meho_backplane.connectors.keycloak.ops_write_merge`).
3. Read the user again and compare it with what was sent.

Keycloak does not always keep what it was sent. It keeps the old user name
when the realm does not allow user name changes (``editUsernameAllowed``)
and still answers with success. A realm user profile can also make a field
read-only. So the result names each field that did not change and says why
(``not_applied``). Keycloak stores the user name and the email in lower
case, so those two are compared without regard to case.

The result holds field names, never values. The op reads and returns no
credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.http import ResponseTooLargeError
from meho_backplane.connectors.keycloak._paths import _USER_PATH, fill_path
from meho_backplane.connectors.keycloak.connector import KeycloakAdminTokenError
from meho_backplane.connectors.keycloak.ops_write_account_common import (
    raise_user_not_found_on_404,
    resolve_user,
)
from meho_backplane.connectors.keycloak.ops_write_merge import (
    merge_representation,
    unrequested_changes,
)
from meho_backplane.connectors.keycloak.session import quote_segment

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["USER_DETAIL_FIELDS", "keycloak_user_update"]

#: The user fields this op may change.
USER_DETAIL_FIELDS: Final[tuple[str, ...]] = ("username", "email", "firstName", "lastName")

#: Fields that Keycloak stores in lower case.
_LOWERCASED: Final[frozenset[str]] = frozenset({"username", "email"})

_USERNAME_REASON: Final = (
    "Keycloak kept the old user name. The realm does not allow user name "
    "changes: set editUsernameAllowed on the realm (keycloak.realm.update) first."
)
_FIELD_REASON: Final = (
    "Keycloak kept the old value. The realm's user profile may not let this "
    "field be edited by an admin."
)


def _same(field: str, sent: str, now: Any) -> bool:
    if not isinstance(now, str):
        return False
    return now.casefold() == sent.casefold() if field in _LOWERCASED else now == sent


def _requested_changes(params: dict[str, Any]) -> dict[str, str]:
    raw = params.get("changes")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("keycloak.user.update requires 'changes' with at least one field")
    unknown = sorted(set(raw) - set(USER_DETAIL_FIELDS))
    if unknown:
        raise ValueError(
            f"keycloak.user.update cannot change {unknown}; allowed fields: "
            f"{list(USER_DETAIL_FIELDS)}"
        )
    bad = sorted(key for key, value in raw.items() if not isinstance(value, str))
    if bad:
        raise ValueError(f"keycloak.user.update needs text values; not text: {bad}")
    if "username" in raw and not raw["username"].strip():
        raise ValueError("keycloak.user.update: the new username must not be empty")
    return dict(raw)


async def keycloak_user_update(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Change user name, email, first name or last name (``PUT .../users/{id}``).

    Op-id: ``keycloak.user.update``. Keys on ``id`` or the current
    ``username``; the new values are in ``changes``.
    """
    op_id = "keycloak.user.update"
    changes = _requested_changes(params)
    uuid, username, realm = await resolve_user(self, operator, target, params, op_id=op_id)
    path = fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(uuid)})
    try:
        before = await self._get_admin_json(target, path, operator=operator)
        merged = merge_representation(before, changes)
        await self._write_admin(
            target, "PUT", path, operator=operator, json=merged, idempotent_conflict=False
        )
    except httpx.HTTPStatusError as exc:
        raise_user_not_found_on_404(exc, op_id=op_id, user_id=uuid, realm=realm)
        raise
    outcome: dict[str, Any] = {
        "id": uuid,
        "username": username,
        "requested_fields": sorted(changes),
        "applied_fields": None,
        "not_applied": None,
        "unrequested_changes": [],
        "read_back_checked": False,
    }
    try:
        after = await self._get_admin_json(target, path, operator=operator)
    except (
        httpx.HTTPError,
        ResponseTooLargeError,
        OSError,
        ValueError,
        KeycloakAdminTokenError,
        CredentialsReadError,
    ):
        # The PUT already ran; a failed check must not report the write as failed.
        return outcome
    if not isinstance(after, dict):
        return outcome
    applied = [field for field in sorted(changes) if _same(field, changes[field], after.get(field))]
    outcome.update(
        applied_fields=applied,
        not_applied=[
            {"field": field, "reason": _USERNAME_REASON if field == "username" else _FIELD_REASON}
            for field in sorted(set(changes) - set(applied))
        ],
        unrequested_changes=unrequested_changes(before, after, changes),
        read_back_checked=True,
    )
    return outcome
