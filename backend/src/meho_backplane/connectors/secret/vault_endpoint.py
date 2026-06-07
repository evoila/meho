# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault KV-v2 :class:`SecretEndpoint` adapter — the first broker pair.

Registered under kind ``"vault"`` (see :func:`register_secret_endpoint`
at module import), this adapter is both a **source** (``read_secret``)
and a **sink** (``write_secret``) so the broker's first move pair is
vault-kv → vault-kv. Sibling tasks add further sink kinds (e.g. keycloak,
#1578) under the same :class:`SecretEndpoint` contract.

Ref grammar
===========

The store-specific ``ref`` addresses a KV-v2 secret as::

    [<mount>/]<path>#<field>

* ``<path>`` is the KV-v2 logical path (relative to the mount, the same
  shape hvac's ``read_secret_version`` / ``create_or_update_secret``
  ``path=`` expects — Vault inserts the ``/data/`` segment itself).
* ``#<field>`` (required) selects which field of the secret's data dict
  carries the value to move. The broker moves a single field, not a
  whole secret body, because the move is value-oriented (a password, a
  token) — naming the field is what keeps the SHA-256 / length in the
  response meaningful.
* A leading mount segment is **not** parsed out of the path here: the
  ``ref`` is forwarded to hvac's ``path=`` and the mount defaults to
  ``"secret"`` (the deployment KV-v2 mount, mirroring
  :data:`meho_backplane.connectors._shared.vault_creds.DEFAULT_KV_MOUNT`).
  A non-default mount is a per-adapter concern a sibling task can add via
  a richer ref grammar without touching the broker handler.

No secret in logs
=================

The adapter's structlog events carry only the ``path`` and the ``field``
*name* — never the field's value, and never the bytes carried by the
:class:`SecretMaterial`. The value lives only inside the
:class:`SecretMaterial` between the source read and the sink write.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    strip_credential_value,
)
from meho_backplane.connectors.secret.endpoints import (
    SecretMaterial,
    register_secret_endpoint,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "VaultKvSecretEndpoint",
    "VaultSecretRefError",
]

_log = structlog.get_logger(__name__)


class VaultSecretRefError(ValueError):
    """A vault-kv ``ref`` is malformed or addresses a missing field.

    Raised for a ref without the required ``#<field>`` fragment, an
    empty path/field, a KV-v2 read whose data dict lacks the named
    field, or a malformed hvac payload. A :class:`ValueError` so the
    dispatcher's ``connector_error`` branch surfaces
    ``exception_class="VaultSecretRefError"``. The message names the
    path and field, never the value.
    """


def _parse_vault_ref(ref: str) -> tuple[str, str]:
    """Split ``"<path>#<field>"`` into ``(path, field)``.

    The split is on the **last** ``#`` so a path may itself contain a
    ``#`` (uncommon but legal in a KV-v2 key). Both halves must be
    non-empty after the split.
    """
    path, sep, field = ref.rpartition("#")
    if not sep or not path or not field:
        raise VaultSecretRefError(
            f"malformed vault secret ref {ref!r}: expected '<path>#<field>' "
            "selecting one KV-v2 field (e.g. 'secret/db/prod#password')"
        )
    return path.strip(), field.strip()


class VaultKvSecretEndpoint:
    """A vault-kv source+sink endpoint addressing one KV-v2 field.

    Constructed per move from the parsed ``ref``. Both methods open an
    operator-scoped Vault client via
    :func:`~meho_backplane.auth.vault.vault_client_for_operator` (JWT/OIDC
    login under the operator's own token, revoked on exit), so the move
    runs inside the operator's authorization envelope.
    """

    def __init__(self, ref: str, *, mount: str = DEFAULT_KV_MOUNT) -> None:
        self._path, self._field = _parse_vault_ref(ref)
        self._mount = mount

    async def read_secret(self, operator: Operator) -> SecretMaterial:
        """Read the addressed field and wrap its value in a :class:`SecretMaterial`.

        Reads via ``client.secrets.kv.v2.read_secret_version``, performs
        the KV-v2 ``data.data`` double-unwrap (the same shape
        ``vault/ops.py`` and ``vault_creds._structural_unwrap`` use), and
        selects the named field. The value is coerced + whitespace-
        stripped by :func:`strip_credential_value` (a trailing newline is
        the single most common secret-storage artifact) before it is
        hashed and forwarded, so source and sink agree byte-for-byte.
        """
        _log.debug("secret_broker.vault.read", path=self._path, field=self._field)
        async with _auth_vault.vault_client_for_operator(operator) as client:
            payload = await asyncio.to_thread(
                client.secrets.kv.v2.read_secret_version,
                path=self._path,
                mount_point=self._mount,
                raise_on_deleted_version=False,
            )
        secret_data = _structural_unwrap(payload, path=self._path)
        if self._field not in secret_data:
            raise VaultSecretRefError(
                f"vault KV-v2 secret at {self._path!r} has no field {self._field!r}"
            )
        return SecretMaterial(strip_credential_value(secret_data[self._field]))

    async def write_secret(self, operator: Operator, material: SecretMaterial) -> None:
        """Write *material* into the addressed field as a new KV-v2 version.

        KV-v2 replaces the latest version wholesale (no server-side
        merge), so the write sends a single-field secret body
        ``{<field>: <value>}``. The value is decoded back to ``str`` from
        the material's bytes — KV-v2 stores JSON string values, and the
        source read coerced via :func:`strip_credential_value`, so the
        round-trip is lossless for the credential strings the broker
        moves. ``cas`` is omitted (unconditional write); a check-and-set
        guard is a policy-task concern (#1579), not the mechanism.
        """
        _log.debug("secret_broker.vault.write", path=self._path, field=self._field)
        body: dict[str, str] = {self._field: material.value.decode("utf-8")}
        async with _auth_vault.vault_client_for_operator(operator) as client:
            await asyncio.to_thread(
                client.secrets.kv.v2.create_or_update_secret,
                path=self._path,
                secret=body,
                cas=None,
                mount_point=self._mount,
            )


def _structural_unwrap(payload: object, *, path: str) -> dict[str, Any]:
    """Unwrap hvac's ``read_secret_version`` payload to the secret data dict.

    KV-v2's GET returns ``{"data": {"data": {<secret kv>}, "metadata":
    {...}}}``; the secret content is the nested ``data["data"]``. A
    malformed payload (missing either level) raises
    :class:`VaultSecretRefError` naming the path rather than a bare
    ``KeyError`` deep inside hvac's response — mirroring
    ``vault_creds._structural_unwrap``.
    """
    outer = payload.get("data") if isinstance(payload, dict) else None
    secret_data = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(secret_data, dict):
        raise VaultSecretRefError(
            f"vault KV-v2 read for {path!r} returned a malformed payload: "
            "expected a nested 'data.data' object holding the secret fields"
        )
    return secret_data


# Register the vault-kv adapter under kind ``"vault"`` at import time. The
# package ``__init__`` imports this module so the registration lands
# before the lifespan runs the move op's registrar.
register_secret_endpoint("vault", VaultKvSecretEndpoint)
