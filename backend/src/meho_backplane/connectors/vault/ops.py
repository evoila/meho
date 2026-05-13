# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault op-map — per-operation handlers for VaultConnector.

Op-id namespace (v0.2): ``vault.kv.<verb>``.

Future ops (``vault.kv.write``, ``vault.kv.list``, ``vault.policy.read``,
``vault.transit.encrypt``) are intentionally out of scope for T5 — the
acceptance criteria specify only ``vault.kv.read`` as required.

Each handler follows the signature
``async (target, params) -> OperationResult`` so :class:`OP_MAP` is a
uniform lookup table with no per-op dispatch logic in the connector itself.

The ``_auth_vault`` module reference is used throughout so that the test
seam (``monkeypatch.setattr(vault_module, "_build_client", fake)`` and
``monkeypatch.setattr(vault_module, "vault_client_for_operator", fake)``)
applies transparently. Binding the helpers by name (``from ... import
_build_client``) would break the monkeypatch because the local name would
still point at the original object.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors.schemas import OperationResult

__all__ = ["OP_MAP"]


async def vault_kv_read(target: Any, params: dict[str, Any]) -> OperationResult:
    """Read a KV v2 secret from Vault via OIDC-forwarded operator JWT.

    Op-id: ``vault.kv.read``
    Param: ``path`` (str, required) — the KV v2 secret path relative to
    the mount root, e.g. ``"meho/test/federation"``.

    Returns ``OperationResult.result`` as the secret's data dict on success.
    On failure distinguishes two phases via ``extras["phase"]``:

    * ``"login"`` — OIDC-login or network failure before any secret was read.
      Caller should treat as ``vault.reachable=False``.
    * ``"read"``  — login succeeded but the KV read raised (permission, path
      missing, malformed payload, etc.). Caller treats as ``read_ok=False``.

    The ``extras["version"]`` key carries the KV v2 metadata version on
    success, allowing callers to surface ``detail="version=N"`` without
    re-parsing the raw hvac payload.
    """
    path = params.get("path")
    if not isinstance(path, str) or not path.strip():
        return OperationResult(
            status="error",
            op_id="vault.kv.read",
            error="path must be a non-empty string",
            duration_ms=0.0,
        )

    start = time.monotonic()

    def _elapsed() -> float:
        return (time.monotonic() - start) * 1000.0

    try:
        # vault_client_for_operator is accessed via the module reference so that
        # test monkeypatches on vault_module._build_client propagate through the
        # call chain. The target object is duck-typed: vault_client_for_operator
        # only accesses target.raw_jwt, which VaultTarget provides.
        async with _auth_vault.vault_client_for_operator(target) as client:
            try:
                secret_payload = await asyncio.to_thread(
                    client.secrets.kv.v2.read_secret_version,
                    path=path,
                    raise_on_deleted_version=False,
                )
                # Structural unwrap — raises KeyError/TypeError on a malformed
                # hvac payload so the caller sees a "read" phase error rather
                # than a successful operation with a None result.
                data = secret_payload["data"]
                secret_data = data["data"]
                metadata = data["metadata"]
                version = metadata.get("version")
                extras: dict[str, Any] = {"version": version} if version is not None else {}
                return OperationResult(
                    status="ok",
                    op_id="vault.kv.read",
                    result=secret_data,
                    duration_ms=_elapsed(),
                    extras=extras,
                )
            except Exception as exc:
                return OperationResult(
                    status="error",
                    op_id="vault.kv.read",
                    error=f"read_failed: {type(exc).__name__}",
                    duration_ms=_elapsed(),
                    extras={"exc_type": type(exc).__name__, "phase": "read"},
                )
    except VaultClientError as exc:
        return OperationResult(
            status="error",
            op_id="vault.kv.read",
            error=f"vault_client_error: {type(exc).__name__}",
            duration_ms=_elapsed(),
            extras={"exc_type": type(exc).__name__, "phase": "login"},
        )


OP_MAP: dict[str, Callable[..., Awaitable[OperationResult]]] = {
    "vault.kv.read": vault_kv_read,
}
