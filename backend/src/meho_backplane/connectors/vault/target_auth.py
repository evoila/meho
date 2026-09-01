# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-target Vault auth resolution — role + auth-mount off the ``Target`` (#3274).

The vault connector is **JWT-federated**: the operator's runtime Keycloak JWT
is forwarded to Vault's JWT/OIDC auth method under a **role**, and that role's
Vault policy *is* the ACL for the dispatch. There is no stored per-target
credential — a vault ``Target``'s ``secret_ref`` stays ``NULL`` — so the only
per-target auth datum the connector reads is the role name (and, optionally,
the auth-method mount the role lives on).

Contract (frozen; lab-verified in ``claude-rdc-hetzner-dc#2814`` / PR
``#2815``):

* ``extras["vault_role"]`` (string) — when present and the target's product is
  in the vault family, :func:`~meho_backplane.auth.vault.vault_client_for_operator`
  logs in under this role instead of ``settings.vault_oidc_role``. Absent →
  byte-for-byte today's behaviour. This is the load-bearing selector that
  routes a governed teardown's ``vault.kv.delete`` under a dedicated narrow
  role (``meho-teardown``) without widening the shared ``meho-mcp`` identity.
* ``extras["vault_mount"]`` (string, optional) — the JWT auth-method mount
  path the role lives on (e.g. ``jwt-meho``); absent,
  ``settings.vault_oidc_mount_path`` stands.
* The Vault **address** stays global (``settings.vault_addr``) — there is one
  Vault. Only role + auth-mount are per-target.
* ``version`` is **not** consulted here: the live teardown target ships
  ``version=null`` and resolves to the connector through its wildcard
  registration (G0.15-T6 #1215). Role resolution keys on ``product`` +
  ``extras`` alone, so a null-version target is fully supported.

Fail-closed is enforced downstream, not here: this module only *selects* the
role. A selected role Vault denies surfaces
:class:`~meho_backplane.auth.vault.VaultRoleDeniedError` from the login with no
fallback to the settings-global role. A blank / whitespace / non-string extras
value is treated as *absent* (no override → settings default), mirroring how a
blank ``VAULT_CHECK_RUNNER_ROLE`` normalises to ``None`` in settings — an empty
string does not *name* a role, so it is not a denial, just no selection.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, NamedTuple

import hvac

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.targets.schemas import Target

__all__ = [
    "VAULT_MOUNT_EXTRAS_KEY",
    "VAULT_ROLE_EXTRAS_KEY",
    "VaultTargetAuth",
    "resolve_vault_target_auth",
    "vault_client_for_target",
]

#: Reserved ``Target.extras`` key naming the per-target Vault JWT role.
VAULT_ROLE_EXTRAS_KEY = "vault_role"
#: Reserved ``Target.extras`` key naming the JWT auth-method mount path.
VAULT_MOUNT_EXTRAS_KEY = "vault_mount"

#: Products whose targets may carry a per-target Vault role. The connector
#: registers under ``product="vault"``; the gate keeps a stray
#: ``extras["vault_role"]`` on a non-vault target from ever selecting a role.
_VAULT_PRODUCT_FAMILY = frozenset({"vault"})


class VaultTargetAuth(NamedTuple):
    """The per-target Vault auth overrides resolved off a ``Target``.

    Both fields are ``None`` when the target carries no override (or is
    ``None`` / not a vault-family product), in which case
    :func:`~meho_backplane.auth.vault.vault_client_for_operator` falls back to
    the settings-global role + auth mount byte-for-byte.
    """

    role: str | None
    mount_path: str | None


#: The no-override singleton — returned for a ``None`` target, a non-vault
#: product, or a target whose extras name neither override.
_NO_OVERRIDE = VaultTargetAuth(role=None, mount_path=None)


def _extras_string(extras: Mapping[str, Any], key: str) -> str | None:
    """Return a non-empty stripped string extras value, or ``None``.

    A missing key, a non-string value, or a blank / whitespace-only string all
    resolve to ``None`` ("no override"), matching the blank-role normalisation
    the settings layer applies to ``VAULT_CHECK_RUNNER_ROLE``.
    """
    value = extras.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_vault_target_auth(target: Target | None) -> VaultTargetAuth:
    """Resolve the per-target Vault role + auth mount off *target*'s extras.

    Returns :data:`_NO_OVERRIDE` (both fields ``None``) unless *target* is a
    vault-family product carrying ``extras["vault_role"]`` /
    ``extras["vault_mount"]``. The product gate is defence-in-depth: only the
    vault connector's own handlers call this, and they only run for a target
    the resolver matched to the vault connector, but gating on the product
    keeps a non-vault target's stray extras key from ever selecting a role.

    Attribute access is duck-typed via :func:`getattr`: the handlers pass
    ``target: Any`` and the dispatcher/resolver contract admits any
    target-shaped object — a real :class:`Target` (``extras`` is a dict), an
    ORM row (``extras`` may be SQL ``NULL`` → ``None``), or a minimal
    duck-typed descriptor that carries no ``extras`` at all. Any of those
    that does not present a ``Mapping`` ``extras`` simply names no override,
    so the login falls back to the settings-global role — never a crash.
    """
    if getattr(target, "product", None) not in _VAULT_PRODUCT_FAMILY:
        return _NO_OVERRIDE
    extras = getattr(target, "extras", None)
    if not isinstance(extras, Mapping):
        return _NO_OVERRIDE
    return VaultTargetAuth(
        role=_extras_string(extras, VAULT_ROLE_EXTRAS_KEY),
        mount_path=_extras_string(extras, VAULT_MOUNT_EXTRAS_KEY),
    )


def vault_client_for_target(
    operator: Operator, target: Target | None
) -> AbstractAsyncContextManager[hvac.Client]:
    """Open a Vault client bound to *operator* under *target*'s resolved role.

    Thin convenience over
    :func:`~meho_backplane.auth.vault.vault_client_for_operator` that resolves
    the per-target role + auth mount (:func:`resolve_vault_target_auth`) and
    forwards them. Every vault KV/auth handler routes through here so the
    per-target selection lives in one place.

    The call is qualified through the ``_auth_vault`` module reference (not a
    direct symbol import) so the existing test seams —
    ``monkeypatch.setattr(vault_module, "vault_client_for_operator", fake)``
    and the ``_build_client`` fake — propagate to this call site unchanged.
    """
    resolved = resolve_vault_target_auth(target)
    return _auth_vault.vault_client_for_operator(
        operator, role=resolved.role, mount_path=resolved.mount_path
    )
