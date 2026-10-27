# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The FortiGate target shape: device or VDOM, and the token in Vault.

One FortiGate gives several MEHO targets:

* a **device** target (``extras.scope = "device"``) for device-wide reads and
  changes: status, HA, VDOMs, licence, resources. Its token belongs to an
  administrator with global rights. FortiOS still needs a VDOM on a call
  (some releases return empty fields without one), so the connector sends the
  management VDOM: ``extras.management_vdom``, ``root`` when not set;
* one **VDOM** target per VDOM (``extras.scope = "vdom"`` and
  ``extras.vdom``), with a token limited to that VDOM.

The connector sends the target's VDOM on every call. No operation takes a
VDOM parameter, so an agent cannot reach another VDOM.

The token is read from the target's Vault secret (``secret_ref``), field
:data:`TOKEN_FIELD`, under the caller's identity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.fortios._errors import FortiOSScopeError, FortiOSTargetError

__all__ = [
    "DEFAULT_MANAGEMENT_VDOM",
    "SCOPE_DEVICE",
    "SCOPE_VDOM",
    "TOKEN_FIELD",
    "FortiOSTargetLike",
    "TargetScope",
    "load_token_from_vault",
    "require_scope",
    "resolve_scope",
]

SCOPE_DEVICE: Final[str] = "device"
SCOPE_VDOM: Final[str] = "vdom"
Scope = Literal["device", "vdom"]

#: The VDOM a device target sends when ``extras.management_vdom`` is not set.
DEFAULT_MANAGEMENT_VDOM: Final[str] = "root"

#: The Vault field that holds the API token.
TOKEN_FIELD: Final[str] = "api_token"

#: FortiOS VDOM names: letters, digits, ``-`` and ``_``, at most 31 characters.
_VDOM_NAME: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9_-]{1,31}\Z")


@runtime_checkable
class FortiOSTargetLike(Protocol):
    """The target fields the connector reads."""

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None
    extras: Mapping[str, Any]


@dataclass(frozen=True)
class TargetScope:
    """What a target is: the whole device or one VDOM, and the VDOM sent on calls."""

    kind: Scope
    vdom: str


def _vdom_name(target: Any, key: str, value: object) -> str:
    if not isinstance(value, str) or not _VDOM_NAME.match(value):
        raise FortiOSTargetError(
            f"FortiGate target {getattr(target, 'name', None)!r}: extras.{key} must be a "
            "VDOM name (letters, digits, '-' and '_', at most 31 characters)"
        )
    return value


def resolve_scope(target: Any) -> TargetScope:
    """Read the target kind from ``target.extras``; raise :class:`FortiOSTargetError`."""
    raw = getattr(target, "extras", None)
    extras: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    name = getattr(target, "name", None)
    scope = extras.get("scope")
    if scope == SCOPE_DEVICE:
        if "vdom" in extras:
            raise FortiOSTargetError(
                f"FortiGate target {name!r} is a device target, so it takes no extras.vdom. "
                "Set extras.management_vdom to change the VDOM a device call uses, or set "
                "extras.scope to 'vdom' for a VDOM target."
            )
        vdom = extras.get("management_vdom", DEFAULT_MANAGEMENT_VDOM)
        return TargetScope(kind="device", vdom=_vdom_name(target, "management_vdom", vdom))
    if scope == SCOPE_VDOM:
        return TargetScope(kind="vdom", vdom=_vdom_name(target, "vdom", extras.get("vdom")))
    raise FortiOSTargetError(
        f"FortiGate target {name!r}: extras.scope must be 'device' (device-wide reads and "
        "changes) or 'vdom' (one VDOM, named in extras.vdom); got "
        f"{scope!r}"
    )


def require_scope(target: Any, op_id: str, needed: Scope) -> TargetScope:
    """Return the target's scope when it is *needed*; else raise :class:`FortiOSScopeError`."""
    scope = resolve_scope(target)
    if scope.kind == needed:
        return scope
    name = getattr(target, "name", None)
    if needed == "device":
        raise FortiOSScopeError(
            f"{op_id} reads or changes the whole device, but target {name!r} is the VDOM "
            f"target of VDOM {scope.vdom!r}. Call it on the device target of this FortiGate."
        )
    raise FortiOSScopeError(
        f"{op_id} works inside one VDOM, but target {name!r} is a device target. Call it "
        "on the VDOM target of the VDOM you mean."
    )


async def load_token_from_vault(target: FortiOSTargetLike, operator: Operator) -> str:
    """Read the API token at ``target.secret_ref`` under the caller's identity."""
    data = await load_vault_secret_data(target, operator)
    raw = data.get(TOKEN_FIELD)
    token = strip_credential_value(raw) if raw is not None else ""
    if not token:
        raise VaultCredentialsReadError(
            f"FortiGate target {target.name!r}: the secret at secret_ref has no "
            f"{TOKEN_FIELD!r} field. Store the REST API administrator's token there."
        )
    return token
