# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Remove one stored credential of a user: ``keycloak.user.credential.delete``.

Example: a lost passkey, or a one-time code app on a lost phone. The other
credentials of the user stay.

Pass the user (``id`` or exact ``username``) and either ``credential_id`` or
``credential_type`` (for example ``otp`` or ``webauthn-passwordless``). A
type removes every credential of that type that the user has when the call
runs. A call that matches nothing is an error that names the types the user
has.

How a call runs:

1. Read ``GET .../users/{id}/credentials``. In the approved run of a
   request, the matches must be the credentials the approval card lists
   under ``will_remove``. When they differ, nothing is deleted
   (:class:`KeycloakCredentialsChangedError`).
2. ``DELETE .../users/{id}/credentials/{credentialId}`` for each match.
   When a delete fails after some ran, the error says which credentials are
   already removed.
3. Read the list again.

What may leave this module
==========================

Keycloak's credential list holds ``secretData`` and ``credentialData``
(password hashes, one-time code seeds, passkey keys). They never leave the
handler. Every credential is cut down to an allow-list of four fields
(:data:`CREDENTIAL_FIELDS`: ``id``, ``type``, ``userLabel``,
``createdDate``), and a value of another type than text or number is
dropped. The allow-list is the only way a field gets into a result, a
preview or an audit row. There is no list of blocked fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.http import ResponseTooLargeError
from meho_backplane.connectors.keycloak._paths import (
    _USER_CREDENTIAL_PATH,
    _USER_CREDENTIALS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.connector import KeycloakAdminTokenError
from meho_backplane.connectors.keycloak.ops_write_account_common import (
    opt_str,
    raise_user_not_found_on_404,
    resolve_user,
)
from meho_backplane.connectors.keycloak.ops_write_grant import CREDENTIALS_CHANGED
from meho_backplane.connectors.keycloak.session import quote_segment
from meho_backplane.operations._preview import approved_preview

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "CREDENTIAL_FIELDS",
    "KeycloakCredentialDeleteError",
    "KeycloakCredentialNotFoundError",
    "KeycloakCredentialsChangedError",
    "allowed_credential",
    "check_approved_matches",
    "keycloak_user_credential_delete",
    "match_credentials",
    "read_credentials",
]

#: The only credential fields that may reach a result, a preview or an audit row.
CREDENTIAL_FIELDS: Final[tuple[str, ...]] = ("id", "type", "userLabel", "createdDate")


class KeycloakCredentialNotFoundError(Exception):
    """No stored credential of the user matches the request."""


class KeycloakCredentialDeleteError(Exception):
    """A delete failed after others of the same call had run."""


class KeycloakCredentialsChangedError(Exception):
    """The approved run matches other credentials than the approval card lists."""


def allowed_credential(row: Any) -> dict[str, Any]:
    """Cut one credential record down to :data:`CREDENTIAL_FIELDS`."""
    if not isinstance(row, dict):
        raise ValueError("Keycloak returned a credential entry that is not an object")
    out: dict[str, Any] = {}
    for field in CREDENTIAL_FIELDS:
        value = row.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            out[field] = value
    return out


async def read_credentials(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    realm: str,
    user_id: str,
) -> list[dict[str, Any]]:
    """Read the user's credentials, cut down to the allow-list. A bad answer raises."""
    rows = await connector._get_admin_list(
        target,
        fill_path(_USER_CREDENTIALS_PATH, {"realm": realm, "user-id": quote_segment(user_id)}),
        operator=operator,
        strict=True,
    )
    return [allowed_credential(row) for row in rows]


def match_credentials(
    credentials: list[dict[str, Any]], params: dict[str, Any], *, op_id: str
) -> list[dict[str, Any]]:
    """The credentials a request names. Raises when it names none, or names both ways."""
    credential_id = opt_str(params.get("credential_id"))
    credential_type = opt_str(params.get("credential_type"))
    if (credential_id is None) == (credential_type is None):
        raise ValueError(f"{op_id} requires exactly one of 'credential_id' or 'credential_type'")
    if credential_id is not None:
        found = [c for c in credentials if c.get("id") == credential_id]
        wanted = f"id {credential_id!r}"
    else:
        assert credential_type is not None
        found = [
            c
            for c in credentials
            if str(c.get("type", "")).casefold() == credential_type.casefold()
        ]
        wanted = f"type {credential_type!r}"
    if not found:
        types = sorted({str(c["type"]) for c in credentials if "type" in c})
        raise KeycloakCredentialNotFoundError(
            f"{op_id}: the user has no credential with {wanted}. The user has these types: {types}"
        )
    if any(not isinstance(c.get("id"), str) or not c["id"] for c in found):
        raise ValueError(f"{op_id}: Keycloak returned a matching credential without an id")
    return found


def check_approved_matches(matches: list[dict[str, Any]], *, op_id: str) -> None:
    """Refuse an approved run whose matches are not the credentials on the card.

    The card is the one the approver saw (:func:`approved_preview`), not the
    params of the call. On an ordinary run there is no card and nothing to
    compare. A card without a readable ``will_remove`` list is refused too.
    """
    card = approved_preview(op_id)
    if card is None:
        return
    rows = card.get("will_remove")
    approved = (
        {row.get("id") for row in rows if isinstance(row, dict)} if isinstance(rows, list) else None
    )
    if approved != {entry["id"] for entry in matches}:
        raise KeycloakCredentialsChangedError(CREDENTIALS_CHANGED)


async def keycloak_user_credential_delete(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Remove stored credentials of one user (``DELETE .../users/{id}/credentials/{id}``).

    Op-id: ``keycloak.user.credential.delete``. Returns the type, label and
    creation date of each removed and each remaining credential.
    """
    op_id = "keycloak.user.credential.delete"
    uuid, username, realm = await resolve_user(self, operator, target, params, op_id=op_id)
    try:
        current = await read_credentials(self, operator, target, realm=realm, user_id=uuid)
    except httpx.HTTPStatusError as exc:
        raise_user_not_found_on_404(exc, op_id=op_id, user_id=uuid, realm=realm)
        raise
    matches = match_credentials(current, params, op_id=op_id)
    check_approved_matches(matches, op_id=op_id)
    removed: list[dict[str, Any]] = []
    for entry in matches:
        path = fill_path(
            _USER_CREDENTIAL_PATH,
            {
                "realm": realm,
                "user-id": quote_segment(uuid),
                "credentialId": quote_segment(entry["id"]),
            },
        )
        try:
            await self._write_admin(
                target, "DELETE", path, operator=operator, idempotent_conflict=False
            )
        except httpx.HTTPError as exc:
            # Every failure after a delete ran (a 401 too) must name what is already gone.
            if not removed:
                raise
            status = (
                f" with HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else ""
            )
            raise KeycloakCredentialDeleteError(
                f"{op_id}: removed {len(removed)} of {len(matches)} credentials, then a "
                f"delete failed ({type(exc).__name__}{status}). Already removed ids: "
                f"{[item['id'] for item in removed]}"
            ) from exc
        removed.append(entry)
    outcome: dict[str, Any] = {
        "id": uuid,
        "username": username,
        "removed": removed,
        "remaining": None,
        "read_back_checked": False,
    }
    try:
        outcome["remaining"] = await read_credentials(
            self, operator, target, realm=realm, user_id=uuid
        )
        outcome["read_back_checked"] = True
    except (
        httpx.HTTPError,
        ResponseTooLargeError,
        OSError,
        ValueError,
        KeycloakAdminTokenError,
        CredentialsReadError,
    ):
        # The deletes already ran; a failed check must not report them as failed.
        pass
    return outcome
