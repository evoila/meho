# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One user, as Keycloak stores it, for every Keycloak user write.

A user write names its user by ``id`` (the user's internal UUID) or by
``username``. A caller may send both. The two must then belong to the same
user. This module is the one place that checks it, for these ops:
``keycloak.user.disable``, ``keycloak.user.enable``,
``keycloak.user.reset_password``, ``keycloak.user.logout``,
``keycloak.user.delete``, ``keycloak.user.required_actions.update``,
``keycloak.role_mapping.assign``, ``keycloak.group.member.add`` and
``keycloak.group.member.remove`` (these two name the user as ``user_id``).
``keycloak.user.delete`` keeps its own approval card and lookup, and uses
:func:`card_user_id` and :func:`check_card_username` for its approved run.

Three pieces, used at three moments:

* :func:`refuse_other_user_check` runs before the request waits for
  approval. An ``id`` and a ``username`` of different users are refused
  and no approval request is stored.
* :func:`with_stored_user` wraps the approval card builder. The card shows
  the username stored in Keycloak for the user id. A username the caller
  sent is never shown as the user's name: when the user cannot be read, the
  card shows it as ``requested_username``.
* :func:`read_user_for_write` runs when the call runs. In the approved run
  it acts on the user id on the card, not on a new lookup of the name. It
  reads the user and refuses the run when the sent username is not the
  stored one (for example the user was renamed after the request).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import _USER_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import get_or_none
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._park_check import ParkCheck, register_park_check
from meho_backplane.operations._preview import (
    PreviewBuilder,
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "UserOfWrite",
    "card_user_id",
    "check_card_username",
    "other_user_reason",
    "read_user_for_write",
    "refuse_other_user_check",
    "register_user_write",
    "user_matches_username",
    "with_stored_user",
]


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


@dataclass(frozen=True)
class UserOfWrite:
    """The user a write acts on: the id, the user's path and the stored user."""

    uuid: str
    path: str
    user: dict[str, Any]

    @property
    def username(self) -> str | None:
        """The username stored in Keycloak."""
        return _opt_str(self.user.get("username"))


def other_user_reason(op_id: str, id_key: str = "id") -> str:
    """The refusal text for an id and a username of different users."""
    return (
        f"{op_id}: '{id_key}' and 'username' belong to different users. "
        "Nothing was changed. Send the request with one user."
    )


def user_matches_username(params: dict[str, Any], user: dict[str, Any]) -> bool:
    """Whether the request's ``username`` (if any) is the username stored for *user*.

    Case does not count, as in Keycloak.
    """
    asked = _opt_str(params.get("username"))
    if asked is None:
        return True
    stored = _opt_str(user.get("username"))
    return stored is not None and stored.casefold() == asked.casefold()


def card_user_id(op_id: str, card: dict[str, Any], id_key: str = "id") -> str:
    """The user id on an approval card; refuse a card that shows none.

    Without the id, MEHO cannot tell which user the approver saw, and looking
    the username up again could find another account.
    """
    uuid = _opt_str(card.get(id_key))
    if uuid is None:
        raise ValueError(
            f"{op_id}: the approval card shows no user id, so MEHO cannot tell which "
            "user was approved. Nothing was changed. Send a new request."
        )
    return uuid


def check_card_username(op_id: str, card: dict[str, Any], user: dict[str, Any]) -> None:
    """Refuse the run when *user*'s stored username is not the one on the card.

    For example, the user was renamed after the approval.
    """
    if _opt_str(card.get("username")) is None or not user_matches_username(card, user):
        raise ValueError(
            f"{op_id}: the user's name in Keycloak is not the name on the approval "
            "card. Nothing was changed. Send a new request."
        )


def _approved_user_id(op_id: str, id_key: str) -> str | None:
    """The user id on the approval card, during the approved run of this op.

    ``None`` when the call is not the approved run of a request. An approved
    run whose card shows no user id is refused (:func:`card_user_id`).
    """
    preview = approved_preview(op_id)
    if preview is None:
        return None
    return card_user_id(op_id, preview, id_key)


async def read_user_for_write(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
    id_key: str = "id",
) -> UserOfWrite:
    """Find the user of a write and read it from Keycloak.

    Raises :exc:`KeycloakUserNotFoundError` for a user that does not exist,
    and :exc:`ValueError` when the request names no user, or its ``username``
    is not the one stored for the user.
    """
    # Imported late: ``ops_write`` imports this module for its handlers.
    from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError

    realm = resolve_realm_config(target).managed_realm
    uuid = _approved_user_id(op_id, id_key) or _opt_str(params.get(id_key))
    username = _opt_str(params.get("username"))
    if uuid is None:
        if username is None:
            raise ValueError(f"{op_id} requires either '{id_key}' (UUID) or 'username'")
        uuid = await connector._find_user_uuid(target, realm, username, operator=operator)
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"{op_id}: no user with username={username!r} in realm {realm!r}"
            )
    path = fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(uuid)})
    user = await get_or_none(connector, target, path, operator)
    if user is None:
        raise KeycloakUserNotFoundError(f"{op_id}: no user with id={uuid!r} in realm {realm!r}")
    if not user_matches_username(params, user):
        raise ValueError(other_user_reason(op_id, id_key))
    return UserOfWrite(uuid=uuid, path=path, user=user)


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    # Imported late: the operations package must not import a connector at load.
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


def refuse_other_user_check(op_id: str, id_key: str = "id") -> ParkCheck:
    """The check before the wait for approval: an id and a username of one user.

    A user id that does not exist is let through: the run then fails with
    "not found". MEHO cannot check without a live Keycloak connector, so it
    refuses then.
    """

    async def _check(ctx: PreviewContext) -> str | None:
        uuid = _opt_str(ctx.params.get(id_key))
        if uuid is None or _opt_str(ctx.params.get("username")) is None:
            return None
        connector = _live_connector(ctx)
        if connector is None:
            return (
                f"{op_id}: MEHO could not check that '{id_key}' and 'username' name the "
                "same user. Send only one of them."
            )
        realm = resolve_realm_config(ctx.target).managed_realm
        path = fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(uuid)})
        user = await get_or_none(connector, ctx.target, path, ctx.operator)
        if user is None or user_matches_username(ctx.params, user):
            return None
        return other_user_reason(op_id, id_key)

    return _check


async def _read_for_card(ctx: PreviewContext, id_key: str) -> tuple[str, dict[str, Any]] | None:
    """The user's id and stored user for the card; ``None`` when it cannot be read."""
    connector = _live_connector(ctx)
    if connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        uuid = _opt_str(ctx.params.get(id_key))
        if uuid is None:
            username = _opt_str(ctx.params.get("username"))
            if username is None:
                return None
            uuid = await connector._find_user_uuid(
                ctx.target, realm, username, operator=ctx.operator
            )
            if uuid is None:
                return None
        user = await get_or_none(
            connector,
            ctx.target,
            fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(uuid)}),
            ctx.operator,
        )
    except Exception:
        return None
    return None if user is None else (uuid, user)


def with_stored_user(builder: PreviewBuilder, *, id_key: str = "id") -> PreviewBuilder:
    """Wrap an approval card builder so the card shows the user as Keycloak stores it.

    Read the user once. When it can be read, the card's ``id_key`` and
    ``username`` are the stored ones. When it cannot be read (no live
    connector, an unknown user, a fault), the card has no ``username``: the
    value the caller sent is shown as ``requested_username``. The card says
    which case it is with ``user_available``.
    """

    async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
        card = await builder(ctx)
        if card is None:
            return None
        card.pop("username", None)
        found = await _read_for_card(ctx, id_key)
        stored = _opt_str(found[1].get("username")) if found is not None else None
        if found is None or stored is None:
            card["user_available"] = False
            requested = _opt_str(ctx.params.get("username"))
            if requested is not None:
                card["requested_username"] = requested
            return card
        card[id_key] = found[0]
        card["username"] = stored
        card["user_available"] = True
        return card

    return _wrapped


def register_user_write(op_id: str, builder: PreviewBuilder, *, id_key: str = "id") -> None:
    """Register the card builder and the check before the wait for approval for *op_id*."""
    register_park_check(op_id, refuse_other_user_check(op_id, id_key))
    register_preview_builder(op_id, with_stored_user(builder, id_key=id_key))
