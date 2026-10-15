# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-merge-write for the Keycloak update ops (#3941).

``keycloak.client.update`` and ``keycloak.realm.update`` take a *partial*
representation. Keycloak's ``PUT`` on a client or a realm is not a real
partial update: for some fields that are missing from the body it writes
its own default. Examples from the Keycloak 26.3 server code:

* Client ``PUT`` turns authorization services off when
  ``authorizationServicesEnabled`` is missing
  (``ClientResource.updateAuthorizationSettings``). When ``webOrigins`` is
  missing, Keycloak builds it from the redirect URIs
  (``RepresentationToModel.defaultWebOrigins``).
* Realm ``PUT`` resets the OAuth 2.0 device-code lifespan and polling
  interval, and the CIBA and PAR settings, when they are missing
  (``DefaultExportImportManager.updateRealm``). A realm ``attributes`` map
  in the body also removes every realm attribute it does not list.

So an update that only sends ``{"enabled": false}`` could change settings
the caller never asked for. This module makes the update safe:

1. Read the current object (``GET``).
2. Put the caller's fields on top (:func:`merge_representation`).
3. Write the full object back (``PUT``).
4. Read the object again and compare it with step 1. Any field that changed
   but was not in the caller's request is reported by name
   (:func:`unrequested_changes`). Values are never reported, so a client
   ``secret`` or an SMTP password never reaches the op result.

Because maps merge key by key, an update can add or change a key inside
a map such as ``attributes`` or ``smtpServer``, but it cannot remove one.
Sending ``null`` for a key does not remove it either: Keycloak stores the
null. To empty a list, send ``[]``.

The read-back is best effort. When it fails or returns something that is
not an object, the write has already happened, so the op still succeeds
and says ``read_back_checked: false``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.http import ResponseTooLargeError
from meho_backplane.connectors.keycloak.connector import KeycloakAdminTokenError

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["merge_representation", "read_merge_write", "unrequested_changes"]


def merge_representation(current: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    """Put the caller's fields on top of the current representation.

    A field the caller sends replaces the current value. A map field (for
    example ``attributes`` or ``smtpServer``) is merged key by key, so
    sending one attribute does not drop the others. Lists such as
    ``redirectUris`` or ``protocolMappers`` are replaced as a whole when the
    caller sends them, and kept as they are when the caller does not.
    """
    merged = dict(current)
    for key, value in requested.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = {**old, **value}
        else:
            merged[key] = value
    return merged


def _canonical(value: Any) -> Any:
    """Return a form of *value* that ignores list order and dict key order.

    Keycloak stores many list fields (redirect URIs, web origins, protocol
    mappers) as sets, so two reads can return the same items in a different
    order. Sorting lists by their JSON text makes such reads compare equal.
    """
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def unrequested_changes(
    before: dict[str, Any], after: dict[str, Any], requested: dict[str, Any]
) -> list[str]:
    """Name the fields that changed between *before* and *after* but were not requested.

    Top-level fields the caller did not send are compared as a whole. For a
    map field the caller did send (for example ``attributes``), the keys
    inside it that the caller did not send are compared one by one and
    reported as ``"<field>.<key>"``. Returns a sorted list of names, never
    values.
    """
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        sent = requested.get(key)
        if key not in requested:
            if _canonical(old) != _canonical(new):
                changed.append(key)
        elif isinstance(sent, dict) and isinstance(old, dict) and isinstance(new, dict):
            for sub in sorted((set(old) | set(new)) - set(sent)):
                if _canonical(old.get(sub)) != _canonical(new.get(sub)):
                    changed.append(f"{key}.{sub}")
    return changed


async def read_merge_write(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    path: str,
    requested: dict[str, Any],
    read_back_path: str | None = None,
) -> dict[str, Any]:
    """GET the object at *path*, merge *requested* on top, PUT it back, read it again.

    *read_back_path* is where the object lives after the write; it differs
    from *path* only when the update renames the object (a realm rename).
    Returns ``{changed_fields, unrequested_changes, read_back_checked}``:
    the fields the caller sent, the fields that changed without being sent,
    and whether the read-back ran.
    """
    before = await connector._get_admin_json(target, path, operator=operator)
    merged = merge_representation(before, requested)
    await connector._write_admin(
        target, "PUT", path, operator=operator, json=merged, idempotent_conflict=False
    )
    outcome: dict[str, Any] = {
        "changed_fields": sorted(requested),
        "unrequested_changes": [],
        "read_back_checked": False,
    }
    try:
        after = await connector._get_admin_json(target, read_back_path or path, operator=operator)
    except (
        httpx.HTTPError,
        ResponseTooLargeError,
        OSError,
        ValueError,
        KeycloakAdminTokenError,
        CredentialsReadError,
    ):
        # The PUT already succeeded; a failed check must not report the
        # write as failed. The caller sees read_back_checked=false.
        return outcome
    if not isinstance(after, dict):
        return outcome
    outcome["read_back_checked"] = True
    outcome["unrequested_changes"] = unrequested_changes(before, after, requested)
    return outcome
