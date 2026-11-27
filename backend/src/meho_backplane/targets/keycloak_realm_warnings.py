# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Warn when a Keycloak target manages a realm that other logins depend on.

A Keycloak target may manage its own admin realm, or the realm MEHO itself
signs in with. MEHO allows both, but never silently:
:func:`keycloak_realm_warnings` gives one line for each case. Create and
update of a target answer with these lines, and the approval card of a
write on such a target shows them (:func:`target_warnings`, stamped on the
card by the dispatcher).

MEHO's own sign-in realm comes from its settings: the realm in
``keycloak_issuer_url`` and in ``keycloak_admin_url``
(:func:`backplane_login_realms`).

A probe made while the switch ``KEYCLOAK_ANSWER_REALM`` is on can save the
line :data:`REALM_NAME_DIFFERS` in the probe's ``extras``.
:func:`shown_fingerprint` leaves it out of a saved probe while the switch is
off, so a target read shows it only while the switch is on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from meho_backplane.settings import get_settings, parse_bool_env
from meho_backplane.targets.required_extras import KEYCLOAK_PRODUCT, realm_name_problem

__all__ = [
    "REALM_NAME_DIFFERS",
    "answer_realm_on",
    "backplane_login_realms",
    "keycloak_realm_warnings",
    "realm_from_url",
    "shown_fingerprint",
    "target_managed_realm",
    "target_warnings",
]

#: The admin realm a Keycloak target logs in to when it names none.
_DEFAULT_ADMIN_REALM = "master"

#: The probe ``extras`` key that says Keycloak answered with another realm name.
REALM_NAME_DIFFERS: Final = "realm_name_differs"


def answer_realm_on() -> bool:
    """Return the state of the ``KEYCLOAK_ANSWER_REALM`` switch.

    Read from the env name, with the same parser as
    :attr:`~meho_backplane.settings.Settings.keycloak_answer_realm`.
    """
    return parse_bool_env(os.environ.get("KEYCLOAK_ANSWER_REALM"))


def shown_fingerprint(fingerprint: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Return a saved probe as a target read shows it.

    While the switch is off, the probe's ``extras`` lose
    :data:`REALM_NAME_DIFFERS`. Every other probe is returned as it is.
    """
    if fingerprint is None or answer_realm_on():
        return fingerprint
    extras = fingerprint.get("extras")
    if not isinstance(extras, Mapping) or REALM_NAME_DIFFERS not in extras:
        return fingerprint
    kept = {key: value for key, value in extras.items() if key != REALM_NAME_DIFFERS}
    return {**fingerprint, "extras": kept}


def realm_from_url(url: str) -> str | None:
    """Return the realm name in a Keycloak issuer or admin URL.

    The realm is the path segment after the last ``realms`` segment, so
    ``https://kc/realms/meho`` and ``https://kc/admin/realms/meho`` both give
    ``meho``. A URL with no realm gives ``None``.
    """
    segments = [unquote(s) for s in urlsplit(url).path.split("/") if s]
    realm = None
    for index in range(len(segments) - 1):
        if segments[index] == "realms":
            realm = segments[index + 1]
    return realm


def backplane_login_realms() -> set[str]:
    """Return the realms this backplane's own login uses."""
    settings = get_settings()
    realms: set[str] = set()
    for url in (str(settings.keycloak_issuer_url), settings.keycloak_admin_url):
        if not url:
            continue
        realm = realm_from_url(url)
        if realm is not None:
            realms.add(realm)
    return realms


def keycloak_realm_warnings(product: str | None, extras: Any) -> list[str]:
    """The warnings for a Keycloak target's managed realm; empty when there are none.

    Other products, and a target whose realms do not pass the realm check,
    get no warning: that check refuses them on its own.
    """
    if not isinstance(product, str) or product.strip().lower() != KEYCLOAK_PRODUCT:
        return []
    if not isinstance(extras, Mapping):
        return []
    managed = extras.get("managed_realm")
    if realm_name_problem(managed) is not None:
        return []
    admin = extras.get("admin_realm") or _DEFAULT_ADMIN_REALM
    wanted = str(managed).casefold()
    warnings: list[str] = []
    if isinstance(admin, str) and wanted == admin.casefold():
        warnings.append(
            f"The managed realm {managed!r} is the admin realm of this Keycloak. "
            "MEHO writes on this target change the realm the target logs in to."
        )
    if wanted in {name.casefold() for name in backplane_login_realms()}:
        warnings.append(
            f"The managed realm {managed!r} is the realm MEHO itself signs in with. "
            "MEHO writes on this target change MEHO's own sign-in."
        )
    return warnings


def target_warnings(target: Any) -> list[str]:
    """The warnings for *target* (a row or a target model); empty when there are none."""
    return keycloak_realm_warnings(
        getattr(target, "product", None), getattr(target, "extras", None)
    )


def target_managed_realm(target: Any) -> str | None:
    """The managed realm of a Keycloak *target*, for the approval card; ``None`` otherwise.

    A target of another product, or a Keycloak target whose managed realm
    does not pass the realm check, gives ``None``: the card shows no realm.
    """
    product = getattr(target, "product", None)
    extras = getattr(target, "extras", None)
    if not isinstance(product, str) or product.strip().lower() != KEYCLOAK_PRODUCT:
        return None
    if not isinstance(extras, Mapping):
        return None
    managed = extras.get("managed_realm")
    if realm_name_problem(managed) is not None:
        return None
    return str(managed)
