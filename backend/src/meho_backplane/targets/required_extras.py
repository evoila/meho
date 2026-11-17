# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Settings that a target of one product must carry in its ``extras``.

A Keycloak target must name the realm it manages (``extras["managed_realm"]``).
MEHO has no built-in realm name. The Keycloak connector refuses to run on a
target without one, and a create or update of such a target is refused with
the same text.

A realm name, the managed realm and the optional ``extras["admin_realm"]``
alike, must be one plain name: :func:`realm_name_problem` says what is wrong
with a value. The same check runs at create, at update and before every
Keycloak call, and no value is trimmed: a name with spaces around it is
refused, not cleaned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "KEYCLOAK_PRODUCT",
    "REALM_NAME_MAX_LENGTH",
    "check_keycloak_managed_realm",
    "check_realm_name",
    "has_managed_realm",
    "keycloak_realm_problem",
    "managed_realm_missing_message",
    "realm_name_problem",
]

KEYCLOAK_PRODUCT: Final = "keycloak"

#: Keycloak keeps a realm name in a column of 255 characters.
REALM_NAME_MAX_LENGTH: Final = 255

#: Characters that would change a URL path, its query or its fragment.
_FORBIDDEN_CHARS: Final = frozenset("/\\?#%")


def realm_name_problem(value: Any) -> str | None:
    """Say why *value* is not a plain realm name, or ``None`` when it is one.

    A plain name is a non-empty string of at most
    :data:`REALM_NAME_MAX_LENGTH` characters, with no ``/``, ``\\``, ``?``,
    ``#`` or ``%``, no control character, no white space at the start or the
    end, and not made only of dots.
    """
    if not isinstance(value, str):
        return "it is not a string"
    if not value:
        return "it is empty"
    if len(value) > REALM_NAME_MAX_LENGTH:
        return f"it is longer than {REALM_NAME_MAX_LENGTH} characters"
    if value != value.strip():
        return "it has white space at the start or the end"
    if any(ch in _FORBIDDEN_CHARS for ch in value):
        return "it holds one of the characters / \\ ? # %"
    if any(not ch.isprintable() for ch in value):
        return "it holds a control character"
    if set(value) == {"."}:
        return "it is made only of dots"
    return None


def check_realm_name(value: Any, field: str) -> str:
    """Return *value* when it is a plain realm name, else raise :class:`ValueError`.

    *field* names the setting or parameter in the refusal text, for example
    ``extras.managed_realm`` or ``realm``.
    """
    problem = realm_name_problem(value)
    if problem is not None:
        raise ValueError(f"{field} {value!r} is not a plain realm name: {problem}.")
    return str(value)


def has_managed_realm(extras: Any) -> bool:
    """True when *extras* holds a ``managed_realm`` that is not empty or only spaces.

    The value may still not be a plain name; :func:`keycloak_realm_problem`
    checks that.
    """
    if not isinstance(extras, Mapping):
        return False
    value = extras.get("managed_realm")
    return isinstance(value, str) and bool(value.strip())


def managed_realm_missing_message(name: str | None = None) -> str:
    """The text for a Keycloak target that names no managed realm."""
    label = f"Keycloak target '{name}'" if name else "A Keycloak target"
    return (
        f"{label} names no managed realm. Set the realm MEHO manages on the target: "
        "pass `--extra managed_realm=<realm>` to `meho targets add`, or to "
        "`meho targets update <name>`. An update replaces the whole extras map, "
        "so repeat every extras key you want to keep (for example "
        "`--extra admin_realm=master --extra managed_realm=<realm>`)."
    )


def keycloak_realm_problem(extras: Any, name: str | None = None) -> str | None:
    """Say what is wrong with the realms of a Keycloak target, or ``None``.

    The managed realm must be set and be a plain name. The admin realm is
    optional (absent, ``None`` or empty means the default); when set, it
    must be a plain name too.
    """
    if not has_managed_realm(extras):
        return managed_realm_missing_message(name)
    label = f"Keycloak target '{name}': " if name else ""
    for key in ("managed_realm", "admin_realm"):
        value = extras.get(key)
        if key == "admin_realm" and value in (None, ""):
            continue
        problem = realm_name_problem(value)
        if problem is not None:
            return f"{label}extras.{key} {value!r} is not a plain realm name: {problem}."
    return None


def check_keycloak_managed_realm(product: str | None, extras: Any, name: str | None = None) -> None:
    """Raise :class:`ValueError` when a Keycloak target's realms are missing or not plain names."""
    if not isinstance(product, str) or product.strip().lower() != KEYCLOAK_PRODUCT:
        return
    problem = keycloak_realm_problem(extras, name)
    if problem is not None:
        raise ValueError(problem)
