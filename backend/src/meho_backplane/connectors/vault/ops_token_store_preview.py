# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval preview and permission preflight for ``vault.token.create_and_store``.

Both are wired at import time (the handler module imports this one), like
the other Vault write previews.

* The **preview** shows the approver the token settings (role, policies,
  TTLs, number of uses) and the destination. The op makes a secret, so the
  generic parameter echo is off for it; this builder is its only preview.
  It never holds a token: the token does not exist yet at approval time.
* The **preflight** asks Vault (``sys/capabilities-self`` under this
  target's Vault role, the role the token is made with) whether the role
  may make the token, and for a ``vault_kv`` store whether it may write
  the KV path. When it may not, MEHO does not ask for approval
  (``write_capability_denied``). The answer holds capability names only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault import ops as _kv_ops
from meho_backplane.connectors.vault.ops_token_store_schemas import DEFAULT_KV_FIELD
from meho_backplane.operations._preview import (
    PreviewContext,
    register_permission_preflight,
    register_preview_builder,
)

__all__ = [
    "TOKEN_STORE_OP",
    "kv_destination",
    "token_create_auth_path",
    "vault_token_create_and_store_preflight",
]

TOKEN_STORE_OP: Final = "vault.token.create_and_store"
_DEFAULT_KV_MOUNT: Final = "secret"


def token_create_auth_path(role_name: str | None) -> str:
    """The path Vault checks for making the token."""
    return f"auth/token/create/{role_name}" if role_name else "auth/token/create-orphan"


def kv_destination(store: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(mount, path, field)`` of a ``vault_kv`` store, defaults applied."""
    return (
        str(store.get("mount") or _DEFAULT_KV_MOUNT).strip(),
        str(store["path"]).strip(),
        str(store.get("field") or DEFAULT_KV_FIELD).strip(),
    )


async def vault_token_create_and_store_preflight(
    operator: Operator, params: dict[str, Any], target: Any = None
) -> dict[str, Any] | None:
    """Ask Vault whether this target's role may make the token (and write the KV path).

    ``sys/capabilities-self`` under the target's role must grant ``update``
    on :func:`token_create_auth_path`. For a ``vault_kv`` store it must also
    grant what ``vault.kv.put`` and ``vault.kv.patch`` need on
    ``<mount>/data/<path>``. Returns the first check that says the call will
    be denied, else the token check; ``None`` when no probe answered.
    """
    role_name = params.get("role_name")
    create_check = await _kv_ops.vault_capability_preflight(
        operator,
        TOKEN_STORE_OP,
        token_create_auth_path(role_name if isinstance(role_name, str) else None),
        frozenset({"update"}),
        target,
    )
    checks = [create_check]
    store = params.get("store")
    if isinstance(store, Mapping) and isinstance(store.get("vault_kv"), Mapping):
        mount, path, _field = kv_destination(store["vault_kv"])
        required = (
            _kv_ops.VAULT_KV_WRITE_CAPABILITIES["vault.kv.put"]
            | _kv_ops.VAULT_KV_WRITE_CAPABILITIES["vault.kv.patch"]
        )
        checks.append(
            await _kv_ops.vault_capability_preflight(
                operator,
                TOKEN_STORE_OP,
                _kv_ops.vault_kv_write_target_path("vault.kv.put", {"mount": mount, "path": path}),
                required,
                target,
            )
        )
    answered = [check for check in checks if check is not None]
    denied = [check for check in answered if check.get("will_be_denied") is True]
    if denied:
        return denied[0]
    return answered[0] if answered else None


async def _preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    return await vault_token_create_and_store_preflight(ctx.operator, ctx.params, ctx.target)


def _store_view(store: Mapping[str, Any]) -> dict[str, Any] | None:
    kv = store.get("vault_kv")
    if isinstance(kv, Mapping) and isinstance(kv.get("path"), str):
        mount, path, field_name = kv_destination(kv)
        return {"vault_kv": {"mount": mount, "path": path, "field": field_name}}
    k8s = store.get("k8s_secret")
    if isinstance(k8s, Mapping):
        return {"k8s_secret": {k: k8s.get(k) for k in ("target", "namespace", "name", "key")}}
    return None


async def _preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver reads: the token settings and the destination, never a token."""
    p = ctx.params
    store = p.get("store")
    view = _store_view(store) if isinstance(store, Mapping) else None
    if view is None:
        return None
    role_name = p.get("role_name")
    return {
        "action": "create_and_store_token",
        "role_name": role_name,
        "vault_path": token_create_auth_path(role_name if isinstance(role_name, str) else None),
        **{k: p.get(k) for k in ("policies", "ttl", "explicit_max_ttl", "ttl_period", "num_uses")},
        "store": view,
        "token_value_returned": False,
    }


register_preview_builder(TOKEN_STORE_OP, _preview)
register_permission_preflight(TOKEN_STORE_OP, _preflight)
