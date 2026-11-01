# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.kv.compare``: check that copies of one secret hold the same value.

Many systems keep one secret in more than one place: two Vault paths, or a
Vault path and a Kubernetes Secret. After a change the operator must prove
that every copy matches. This op answers that question without showing the
value.

How it works
============

* Each copy is read with the caller's own Vault login (the same role and
  the same tenant guard as ``vault.kv.read``).
* The value under ``key`` becomes a :class:`StoredValue`: its kind (text,
  or any other JSON value) and its bytes. Two copies match only when the
  kind and the bytes match, compared by :func:`hmac.compare_digest`
  (constant time). So the string ``"123"`` never matches the number
  ``123``, and ``"null"`` never matches ``null``.
* Only the first copy's value is kept while the others are read; every
  other value is dropped as soon as it is compared.
* The answer holds ``same`` and, per copy, ``found``, ``matches_first``,
  the Vault ``version`` and a ``reason`` when the copy was not read. It
  never holds a value or a hash: a hash of a short password can be guessed
  offline.

A copy the caller may not read (Vault answers 403, or the path is outside
the caller's tenant) answers ``found: false, reason: "forbidden"``. The
other copies are still compared. A login failure or an unreachable Vault
still fails the whole call, because then no copy can be read.

:func:`read_kv_copy` and :func:`compare_copies` are shared with
``k8s.secret.compare_to_ref``, which compares a Kubernetes Secret key with
one Vault copy.

The op id is on ``broadcast.events._CREDENTIAL_READ_OPS``: the backplane
reads secret values to answer it, so the flight recorder never stores a
vendor body for it and the broadcast feed shows only the outcome.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import dataclass
from typing import Any, Literal

import hvac.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops import (
    _MOUNT_PROPERTY,
    _PATH_PROPERTY,
    _extract_mount_and_path,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.connectors.vault.tenant_scope import (
    VaultTenantScopeError,
    enforce_tenant_scope,
)

__all__ = [
    "KV_COMPARE_OP_SPEC",
    "REASONS",
    "VAULT_KV_REF_SCHEMA",
    "StoredValue",
    "compare_copies",
    "read_kv_copy",
    "text_value",
    "values_match",
    "vault_kv_compare",
]

_OP_ID = "vault.kv.compare"

#: Why a copy was not read. ``forbidden``: the caller may not read it.
#: ``not_found``: no secret at the path (or its latest version is deleted).
#: ``key_not_found``: the secret exists but has no such key.
REASONS = ("forbidden", "not_found", "key_not_found")

#: One Vault copy: ``{path, key, mount?}``. Shared with
#: ``k8s.secret.compare_to_ref``.
VAULT_KV_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One Vault copy of the secret.",
    "properties": {
        "mount": _MOUNT_PROPERTY,
        "path": _PATH_PROPERTY,
        "key": {
            "type": "string",
            "minLength": 1,
            "description": "The key inside the secret whose value is compared.",
        },
    },
    "required": ["path", "key"],
    "additionalProperties": False,
}

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paths": {
            "type": "array",
            "minItems": 2,
            "maxItems": 10,
            "items": VAULT_KV_REF_SCHEMA,
            "description": (
                "The copies to compare, 2 to 10. The first one is the reference: "
                "every other copy is compared with it."
            ),
        },
    },
    "required": ["paths"],
    "additionalProperties": False,
    "examples": [
        {
            "paths": [
                {"path": "apps/db", "key": "password"},
                {"path": "apps/db-replica", "key": "password"},
            ]
        },
        {
            "paths": [
                {"mount": "kv", "path": "keycloak/admin", "key": "password"},
                {"path": "helm/keycloak", "key": "admin-password"},
                {"path": "backup/keycloak", "key": "admin-password"},
            ]
        },
    ],
}

_COPY_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": {"type": "string"},
        "path": {"type": "string"},
        "key": {"type": "string"},
        "found": {"type": "boolean"},
        "matches_first": {"type": "boolean"},
        "version": {"type": ["integer", "null"]},
        "reason": {"type": ["string", "null"], "enum": [*REASONS, None]},
    },
    "required": ["mount", "path", "key", "found", "matches_first", "version", "reason"],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same": {
            "type": "boolean",
            "description": "True only when every copy was found and all values match.",
        },
        "copies": {"type": "array", "items": _COPY_RESULT_SCHEMA},
    },
    "required": ["same", "copies"],
    "additionalProperties": False,
}

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Check that two or more Vault copies of one secret hold the same value, for "
        "example after a password change. The value never reaches you: the answer "
        "says only 'same' or 'different' per copy. Do not use vault.kv.read for this. "
        "To compare a Kubernetes Secret with a Vault copy, use k8s.secret.compare_to_ref."
    ),
    "parameter_hints": {
        "paths": "2 to 10 items {path, key, mount?}. The first item is the reference.",
    },
    "output_shape": (
        "{same, copies: [{mount, path, key, found, matches_first, version, reason}]}. "
        "reason is 'forbidden', 'not_found' or 'key_not_found' when found is false, "
        "else null. Never a value, never a hash."
    ),
    "next_step": (
        "If same is false, fix the copy with matches_first false (vault.kv.put, "
        "vault.kv.patch or secret.move) and compare again. A 'forbidden' copy needs a "
        "caller whose Vault role may read it."
    ),
    # Keep the verdict inline if the copies list is ever stored behind a
    # handle; the reducer copies no scalar of a credential-class op by itself.
    "result_scalars": {"keys": ["same"]},
}


@dataclass(frozen=True, slots=True)
class StoredValue:
    """One stored secret value, ready to compare.

    ``kind`` is ``"text"`` for a Vault string or the raw bytes of a
    Kubernetes Secret, and ``"json"`` for any other JSON value in Vault
    (number, boolean, null, list, object). ``data`` holds the UTF-8 bytes
    of the text, or the canonical JSON text of the other value. Two values
    match only when both fields match, so a Vault string ``"123"`` and the
    number ``123`` are different, while a Kubernetes Secret holding
    ``123`` and a Vault string ``"123"`` are the same.
    """

    kind: Literal["text", "json"]
    data: bytes


def text_value(raw: bytes) -> StoredValue:
    """The :class:`StoredValue` of raw text bytes (a Kubernetes Secret key)."""
    return StoredValue("text", raw)


def _stored_value(value: Any) -> StoredValue:
    """The :class:`StoredValue` of one JSON value read from Vault."""
    if isinstance(value, str):
        return text_value(value.encode("utf-8"))
    return StoredValue(
        "json", json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def values_match(first: StoredValue | None, other: StoredValue | None) -> bool:
    """Constant-time equality of two values; a missing value never matches."""
    if first is None or other is None:
        return False
    return first.kind == other.kind and hmac.compare_digest(first.data, other.data)


def compare_copies(
    reads: list[tuple[dict[str, Any], StoredValue | None]],
) -> dict[str, Any]:
    """Build the ``{same, copies}`` answer from per-copy reports and values.

    The first entry is the reference. Each report gets ``matches_first``.
    The values are not part of the answer.
    """
    first = reads[0][1]
    copies = [{**report, "matches_first": values_match(first, value)} for report, value in reads]
    return {"same": all(copy["matches_first"] for copy in copies), "copies": copies}


async def read_kv_copy(
    client: Any, operator: Operator, ref: dict[str, Any], *, op_id: str
) -> tuple[dict[str, Any], StoredValue | None]:
    """Read one Vault copy; return its report and its value (or ``None``).

    The report has ``mount``, ``path``, ``key``, ``found``, ``version`` and
    ``reason``. A forbidden, missing or key-less copy returns ``None`` as
    the value and a ``reason``; it does not raise. A path that repeats the
    mount segment still raises :class:`VaultPathShapeError` (a caller
    mistake), and login or network errors still raise.
    """
    mount, path = _extract_mount_and_path(op_id, ref)
    key = str(ref["key"])
    report: dict[str, Any] = {
        "mount": mount,
        "path": path,
        "key": key,
        "found": False,
        "version": None,
        "reason": None,
    }
    try:
        enforce_tenant_scope(operator, mount=mount, path=path, read_only=True)
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version,
            path=path,
            mount_point=mount,
            raise_on_deleted_version=False,
        )
    except (VaultTenantScopeError, hvac.exceptions.Forbidden):
        return {**report, "reason": "forbidden"}, None
    except hvac.exceptions.InvalidPath:
        return {**report, "reason": "not_found"}, None

    data = payload["data"]
    report["version"] = (data.get("metadata") or {}).get("version")
    secret_data = data.get("data")
    if secret_data is None:
        return {**report, "reason": "not_found"}, None
    if key not in secret_data:
        return {**report, "reason": "key_not_found"}, None
    return {**report, "found": True}, _stored_value(secret_data[key])


async def vault_kv_compare(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Handler for ``vault.kv.compare``: compare 2 to 10 Vault copies of one secret."""
    async with vault_client_for_target(operator, target) as client:
        reads = [await read_kv_copy(client, operator, ref, op_id=_OP_ID) for ref in params["paths"]]
    answer = compare_copies(reads)
    del reads
    return answer


#: Registration spec, splatted into ``register_typed_operation`` by
#: :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
#: next to the other KV-v2 ops.
KV_COMPARE_OP_SPEC: dict[str, Any] = {
    "op_id": _OP_ID,
    "handler": vault_kv_compare,
    "summary": "Check that 2 to 10 Vault copies of one secret hold the same value.",
    "description": (
        "Reads the given key at each KV v2 path with the caller's own Vault login and "
        "compares the values with the first copy in constant time. Returns only "
        "same/different per copy, never a value or a hash. A copy the caller may not "
        "read answers found=false."
    ),
    "parameter_schema": _PARAMETER_SCHEMA,
    "response_schema": _RESPONSE_SCHEMA,
    "tags": ["read-only", "credential-read", "compare"],
    "safety_level": "safe",
    "requires_approval": False,
    "llm_instructions": _LLM_INSTRUCTIONS,
}
