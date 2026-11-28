# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Names and mounts that a Vault operation puts into a Vault path.

Many Vault operations put a value from the caller into the address of the
request, for example ``auth/<mount>/users/<username>``. Such a value must
stay one part of that address:

* a **name** (:data:`NAME`) is one part: no ``/``, not ``.`` or ``..``,
  none of ``?``, ``#``, ``%``, ``\\``, no control character;
* a **mount** (:data:`MOUNT`) is one or more parts split on ``/`` (for
  example ``auth/team-a``): no empty part, no ``.`` or ``..`` part, and the
  same refused characters. A ``/`` at the start or end is dropped.

:data:`VAULT_PATH_PARAMS` is the one list of these values: for each
operation, the parameters that go into a Vault path and their kind. Each
operation in it gets the params check :func:`check_path_params`, so the
dispatcher refuses a bad value as ``invalid_params`` before an approval
request is stored and before anything is sent. The handlers read the value
with :func:`path_param`, which applies the same rule again.

:data:`VAULT_NAME_PARAMS_NOT_IN_PATH` lists the parameters that look like a
name but never go into a path as they are: Vault gets them in the request
body, or they are a KV address that the tenant folder check
(:func:`~meho_backplane.connectors.vault.tenant_scope.enforce_tenant_scope`)
already checks. A guard test reads both lists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from meho_backplane.operations._validate import (
    ParamsCheck,
    params_problem,
    register_params_check,
)

__all__ = [
    "MOUNT",
    "MOUNT_PATTERN",
    "NAME",
    "NAME_PATTERN",
    "VAULT_NAME_PARAMS_NOT_IN_PATH",
    "VAULT_PATH_PARAMS",
    "PathKind",
    "check_path_params",
    "path_param",
    "path_part_problem",
]

PathKind = Literal["name", "mount"]

NAME: Final[PathKind] = "name"
MOUNT: Final[PathKind] = "mount"

#: Characters a name or a mount never holds. ``%`` starts an encoded
#: character, ``?`` and ``#`` end the path part of an address, and a
#: backslash can act as a separator.
_REFUSED_CHARACTERS: Final[frozenset[str]] = frozenset("?#%\\")

#: The schema form of the name rule (``pattern`` of the parameter).
NAME_PATTERN: Final[str] = r"^(?=.*\S)(?!\s*\.\.?\s*$)[^/?#%\\\x00-\x1f\x7f]+$"

#: The schema form of the mount rule (``pattern`` of the parameter).
MOUNT_PATTERN: Final[str] = (
    r"^(?=.*[^\s/])(?!(?:.*/)?\s*\.\.?\s*(?:/.*)?$)(?!.*//)[^?#%\\\x00-\x1f\x7f]+$"
)

_EXPECTED: Final[dict[PathKind, str]] = {
    NAME: "one plain name: no '/', not '.' or '..', no '?', '#', '%', '\\' or control character",
    MOUNT: (
        "a plain mount path such as 'userpass' or 'auth/team-a': no empty, '.' or '..' "
        "part, no '?', '#', '%', '\\' or control character"
    ),
}

#: op_id -> {parameter: kind} for every parameter that goes into a Vault path.
VAULT_PATH_PARAMS: Final[dict[str, dict[str, PathKind]]] = {
    "vault.auth.userpass.list": {"mount": MOUNT},
    "vault.auth.userpass.read": {"username": NAME, "mount": MOUNT},
    "vault.auth.userpass.write": {"username": NAME, "mount": MOUNT},
    "vault.auth.userpass.update_password": {"username": NAME, "mount": MOUNT},
    "vault.auth.userpass.delete": {"username": NAME, "mount": MOUNT},
    "vault.auth.userpass.disable": {"username": NAME, "mount": MOUNT},
    "vault.auth.userpass.enable": {"username": NAME, "mount": MOUNT},
    "vault.auth.approle.list": {"mount": MOUNT},
    "vault.auth.approle.read": {"role_name": NAME, "mount": MOUNT},
    "vault.auth.approle.write": {"role_name": NAME, "mount": MOUNT},
    "vault.auth.approle.delete": {"role_name": NAME, "mount": MOUNT},
    "vault.auth.approle.generate_secret_id": {"role_name": NAME, "mount": MOUNT},
    "vault.auth.jwt.config.read": {"mount": MOUNT},
    "vault.auth.jwt.role.list": {"mount": MOUNT},
    "vault.auth.jwt.role.read": {"role_name": NAME, "mount": MOUNT},
    "vault.auth.jwt.role.write": {"role_name": NAME, "mount": MOUNT},
    "vault.identity.entity.read": {"entity_id": NAME},
    "vault.identity.group.read": {"name": NAME},
    "vault.identity.group.delete": {"name": NAME},
    "vault.identity.oidc.key.read": {"name": NAME},
    "vault.identity.oidc.key.delete": {"name": NAME},
    "vault.identity.oidc.role.read": {"name": NAME},
    "vault.identity.oidc.role.delete": {"name": NAME},
    "vault.token.role.read": {"role_name": NAME},
    "vault.token.role.write": {"role_name": NAME},
    "vault.token.create_and_store": {"role_name": NAME},
    "vault.sys.policy.read": {"name": NAME},
    "vault.sys.policy.write": {"name": NAME},
    "vault.sys.policy.delete": {"name": NAME},
    "vault.sys.auth.enable": {"path": NAME},
    "vault.sys.auth.tune": {"path": NAME},
    "vault.sys.mounts.enable": {"path": NAME},
    "vault.sys.mounts.tune": {"path": NAME},
}

_KV_ADDRESS: Final[frozenset[str]] = frozenset({"mount", "path"})

#: op_id -> parameters that look like a name but never go into a Vault path
#: as they are: Vault gets them in the request body, or they are a KV
#: address that the tenant folder check already checks.
VAULT_NAME_PARAMS_NOT_IN_PATH: Final[dict[str, frozenset[str]]] = {
    "vault.auth.userpass.write": frozenset({"password_secret_mount", "password_secret_ref"}),
    "vault.identity.entity.write": frozenset({"name", "entity_id"}),
    "vault.identity.entity_alias.write": frozenset(
        {"name", "canonical_id", "mount_accessor", "alias_id"}
    ),
    "vault.identity.group.write": frozenset({"name", "group_id"}),
    "vault.token.create": frozenset({"display_name"}),
    "vault.token.create_and_store": frozenset({"display_name"}),
    "vault.token.lookup_accessor": frozenset({"accessor"}),
    "vault.token.revoke_accessor": frozenset({"accessor"}),
    "vault.kv.read": _KV_ADDRESS,
    "vault.kv.list": _KV_ADDRESS,
    "vault.kv.versions": _KV_ADDRESS,
    "vault.kv.put": _KV_ADDRESS,
    "vault.kv.patch": _KV_ADDRESS,
    "vault.kv.delete": _KV_ADDRESS,
    "vault.kv.metadata.delete": _KV_ADDRESS,
    "vault.kv.generate": _KV_ADDRESS,
    "vault.kv.folder.delete": _KV_ADDRESS,
}


def path_part_problem(value: object, kind: PathKind) -> str | None:
    """Say what is wrong with *value* as a path value of *kind*, or ``None``.

    The value is checked as the handler sends it: without the spaces around
    it, and for a mount without a ``/`` at the start or end.
    """
    if not isinstance(value, str):
        return "it is not a string"
    text = value.strip()
    if kind == MOUNT:
        text = text.strip("/")
    if not text:
        return "it is empty"
    if any(char in _REFUSED_CHARACTERS or ord(char) < 0x20 or char == "\x7f" for char in text):
        return "it holds a refused character"
    parts = text.split("/")
    if kind == NAME and len(parts) > 1:
        return "it holds a '/'"
    if any(part.strip() in ("", ".", "..") for part in parts):
        return "it has an empty, '.' or '..' part"
    return None


def path_param(
    params: Mapping[str, Any], name: str, kind: PathKind, *, default: str | None = None
) -> str:
    """Return the path value *name* of *params*, cleaned; raise ``ValueError`` when it is bad.

    A missing, ``None`` or empty value takes *default* when one is given.
    The answer has no spaces around it, and a mount has no ``/`` at the
    start or end. The message names the parameter, never the value.
    """
    value = params.get(name)
    if default is not None and (value is None or value == ""):
        value = default
    problem = path_part_problem(value, kind)
    if problem is not None:
        raise ValueError(f"{name} must be {_EXPECTED[kind]}; {problem}")
    text = str(value).strip()
    return text.strip("/") if kind == MOUNT else text


def check_path_params(op_id: str) -> ParamsCheck:
    """The params check of *op_id*: the first path value that is not plain."""
    fields = VAULT_PATH_PARAMS[op_id]

    def check(params: dict[str, Any]) -> dict[str, Any] | None:
        for name, kind in fields.items():
            value = params.get(name)
            if value is None or value == "":
                continue
            problem = path_part_problem(value, kind)
            if problem is not None:
                return params_problem(
                    name,
                    _EXPECTED[kind],
                    f"Give {name} as {_EXPECTED[kind]}; {problem}.",
                )
        return None

    return check


for _op_id in VAULT_PATH_PARAMS:
    register_params_check(_op_id, check_path_params(_op_id))
