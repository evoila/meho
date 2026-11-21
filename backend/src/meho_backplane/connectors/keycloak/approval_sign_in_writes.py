# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Keycloak writes that can change the sign-in MEHO asks for before an approval.

MEHO accepts an approve on a token of its approval sign-in: the token's
client, its sign-in level (``acr``, ``amr``) and its sign-in time
(``auth_time``) (:mod:`meho_backplane.auth.approval_sign_in`). In MEHO's
own login realm these requests can change that sign-in, so they never run
alone in the web console and the login that asked never approves them
(:mod:`meho_backplane.operations.approval_sign_in_writes`):

* ``keycloak.client.update`` of a sign-in client
  (:func:`sign_in_client_ids`): the clients of the CLI approval and the web
  approval, and the client of the web console login;
* ``keycloak.protocol_mapper.create`` on a sign-in client, or of a mapper
  that puts a sign-in claim (:data:`SIGN_IN_CLAIMS`) into a token or is a
  sign-in level mapper (:data:`SIGN_IN_MAPPER_TYPES`);
* ``keycloak.client_scope.protocol_mapper.update`` of such a mapper, or one
  that would become such a mapper;
* ``keycloak.realm.update`` that sets a key outside :data:`HARMLESS_REALM_KEYS`
  (the realm holds the level map and the login flows).

The checks read the params and the approval card. When neither says which
client, mapper or keys the write changes, the check passes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.operations.approval_sign_in_writes import register_approval_sign_in_check
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "HARMLESS_REALM_KEYS",
    "SIGN_IN_CLAIMS",
    "SIGN_IN_MAPPER_TYPES",
    "register_approval_sign_in_checks",
    "sign_in_client_ids",
]

#: The token claims MEHO reads to accept an approve (lower case).
SIGN_IN_CLAIMS: Final[frozenset[str]] = frozenset({"acr", "amr", "auth_time", "azp"})

#: The mapper types that put the sign-in level into a token (lower case).
SIGN_IN_MAPPER_TYPES: Final[frozenset[str]] = frozenset({"oidc-acr-mapper", "oidc-amr-mapper"})

#: The keys of a realm update that do not change how a sign-in works.
HARMLESS_REALM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "displayName",
        "displayNameHtml",
        "smtpServer",
        "eventsEnabled",
        "eventsExpiration",
        "eventsListeners",
        "enabledEventTypes",
        "adminEventsEnabled",
        "adminEventsDetailsEnabled",
        "internationalizationEnabled",
        "supportedLocales",
        "defaultLocale",
    }
)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def sign_in_client_ids() -> frozenset[str]:
    """The client ids (lower case) whose tokens prove a sign-in for an approve."""
    settings = get_settings()
    names = (
        settings.keycloak_cli_approve_client_id,
        settings.ui_keycloak_approve_client_id,
        settings.ui_keycloak_client_id,
    )
    return frozenset(name.casefold() for name in map(_text, names) if name is not None)


def _preview(request: ApprovalRequest) -> dict[str, Any]:
    preview = (request.proposed_effect or {}).get("preview")
    return preview if isinstance(preview, dict) else {}


def _representation(request: ApprovalRequest) -> dict[str, Any]:
    """The representation the write sends: from the params, else from the card.

    A request whose params hold a credential keeps no params on the row, so
    the approve reads the card's copy (values hidden, keys kept).
    """
    params = request.params if isinstance(request.params, dict) else {}
    preview = _preview(request)
    for representation in (
        params.get("representation"),
        preview.get("representation"),
        preview.get("new_values"),
        preview.get("new_mapper"),
    ):
        if isinstance(representation, dict) and representation:
            return representation
    return {}


def _on_sign_in_client(request: ApprovalRequest) -> bool:
    """True when the card names a sign-in client, or names no client."""
    client_id = _text(_preview(request).get("client_id"))
    return client_id is None or client_id.casefold() in sign_in_client_ids()


def _sign_in_claim(config: Any) -> bool:
    claim = _text(config.get("claim.name")) if isinstance(config, dict) else None
    return claim is not None and claim.casefold() in SIGN_IN_CLAIMS


def _sign_in_mapper_type(value: Any) -> bool:
    kind = _text(value)
    return kind is not None and kind.casefold() in SIGN_IN_MAPPER_TYPES


def _client_update(request: ApprovalRequest) -> bool:
    return _on_sign_in_client(request)


def _protocol_mapper_create(request: ApprovalRequest) -> bool:
    mapper = _representation(request)
    return (
        _on_sign_in_client(request)
        or _sign_in_mapper_type(mapper.get("protocolMapper"))
        or _sign_in_claim(mapper.get("config"))
    )


def _scope_mapper_update(request: ApprovalRequest) -> bool:
    preview = _preview(request)
    current = preview.get("current")
    if preview.get("current_available") is not True or not isinstance(current, dict):
        return True
    return (
        _sign_in_mapper_type(current.get("protocolMapper"))
        or _sign_in_claim({"claim.name": current.get("claim_name")})
        or _sign_in_claim(_representation(request).get("config"))
    )


def _realm_update(request: ApprovalRequest) -> bool:
    representation = _representation(request)
    return not representation or not set(representation) <= HARMLESS_REALM_KEYS


def register_approval_sign_in_checks() -> None:
    """Register the check of each Keycloak write that can change the approval sign-in."""
    for op_id, check in (
        ("keycloak.client.update", _client_update),
        ("keycloak.protocol_mapper.create", _protocol_mapper_create),
        ("keycloak.client_scope.protocol_mapper.update", _scope_mapper_update),
        ("keycloak.realm.update", _realm_update),
    ):
        register_approval_sign_in_check(op_id, check)
