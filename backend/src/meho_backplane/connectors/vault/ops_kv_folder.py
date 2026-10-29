# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.kv.folder.delete``: soft-delete many KV v2 secrets under one folder, with one approval.

Before this op, ``vault.kv.delete`` was the only way to delete a secret, one
secret per call and one approval per call. Cleaning up a folder of 190
secrets took 190 approvals.

How it works
============

* The caller names a folder (``mount`` + ``path``) and the exact list of
  secrets to delete (``keys``, paths relative to the folder). The list comes
  from ``vault.kv.list`` with ``recursive=true``.
* The ``keys`` list is part of the params. The approval card shows the full
  list and the count (:func:`_folder_delete_preview`), and the approval is
  bound to these params. So the call can only ever delete what the approver
  saw. A secret added to the folder later is not on the list, so it is left
  alone and reported as ``not_on_list``.
* "Delete" is the same soft delete as ``vault.kv.delete``: every version
  that is not yet deleted or destroyed is marked deleted. Vault keeps the
  data, so an undelete brings it back. Safe to run twice: a secret with no
  live version left is reported as ``already_deleted``.
* A version is live until its ``deletion_time`` has passed. Vault also puts
  a time in that field for a deletion it has only scheduled
  (``delete_version_after``); such a version is still live, so this op
  deletes it now (:func:`_live_versions`).

Guards
======

* The mount root and an empty path are refused (``path`` must hold at least
  one character that is not ``/`` or a space).
* ``keys`` may hold at most ``vault_kv_folder_max_keys`` entries (default
  500). The param schema carries this limit, so a longer list is refused
  before the call waits for approval. When the call runs, a folder that now
  holds more secrets than the limit is refused and nothing is deleted.
* A permission check runs before the call waits for approval
  (:func:`_folder_delete_preflight`). It walks the folder first, the same
  walk the delete does, to find every sub-folder the delete will list,
  also one no listed key is in. The identity needs ``list`` on the
  metadata path of the folder and of every one of these sub-folders, and,
  for every listed key, ``read`` on its metadata path and ``update`` on
  its delete path. Vault rights can differ per key and per sub-folder, so
  every path is checked, in one ``sys/capabilities-self`` call. When Vault
  would refuse one of them, MEHO refuses the call and asks no one to
  approve it.

The result and the approval card carry key paths only, never a secret value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import hvac.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.kv_tree import (
    DEFAULT_FOLDER_MAX_KEYS,
    folder_max_keys,
    list_kv_folders,
    list_kv_tree,
)
from meho_backplane.connectors.vault.ops import (
    _DEFAULT_KV_MOUNT,
    _MOUNT_PROPERTY,
    _extract_mount_and_path,
    vault_capabilities_preflight,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.connectors.vault.tenant_scope import enforce_tenant_scope
from meho_backplane.operations._preview import (
    PreviewContext,
    register_permission_preflight,
    register_preview_builder,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "FOLDER_DELETE_OP_ID",
    "VaultFolderKeyError",
    "register_vault_kv_folder_operations",
    "vault_kv_folder_delete",
    "vault_kv_folder_delete_parameter_schema",
]

FOLDER_DELETE_OP_ID = "vault.kv.folder.delete"

#: A key is a path relative to the folder: no leading, trailing or double
#: ``/``. The handler also refuses ``.`` and ``..`` segments.
_KEY_PATTERN = "^[^/]+(/[^/]+)*$"


class VaultFolderKeyError(Exception):
    """A ``vault.kv.folder.delete`` call named a folder or key that is not allowed.

    Raised before any Vault call: the folder is the mount root, or a key
    holds a ``.`` or ``..`` segment, or the list is longer than the limit.
    """


def vault_kv_folder_delete_parameter_schema(max_keys: int) -> dict[str, Any]:
    """Return the param schema, with *max_keys* as the longest allowed ``keys`` list."""
    return {
        "type": "object",
        "properties": {
            "mount": _MOUNT_PROPERTY,
            "path": {
                "type": "string",
                "minLength": 1,
                "pattern": "[^/\\s]",
                "description": (
                    "The folder, relative to the mount, without the mount name. "
                    "Example: 'tenants/acme/runs'. The mount root is refused."
                ),
            },
            "keys": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_keys,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "pattern": _KEY_PATTERN,
                    "description": "One secret path relative to the folder, e.g. 'run1/token'.",
                },
                "description": (
                    "The exact secrets to delete, as paths relative to the folder. "
                    "Get them from vault.kv.list with recursive=true. Only these "
                    f"are deleted. At most {max_keys} entries."
                ),
            },
        },
        "required": ["path", "keys"],
        "additionalProperties": False,
        "examples": [{"path": "tenants/acme/runs", "keys": ["run1/token", "run2/token"]}],
    }


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": {"type": "string", "description": "The KV v2 mount."},
        "path": {"type": "string", "description": "The folder, relative to the mount."},
        "listed": {"type": "integer", "description": "How many keys the call listed."},
        "deleted": {"type": "integer", "description": "Keys soft-deleted by this call."},
        "already_deleted": {
            "type": "integer",
            "description": (
                "Listed keys that had no live version left. Nothing was sent. A "
                "version whose deletion is only scheduled still counts as live."
            ),
        },
        "not_found": {
            "type": "integer",
            "description": "Listed keys that are no longer in the folder. Nothing was sent.",
        },
        "failed": {"type": "integer", "description": "Listed keys whose delete failed."},
        "not_on_list": {
            "type": "integer",
            "description": "Secrets in the folder that were not listed. They were left alone.",
        },
        "results": {
            "type": "array",
            "description": "One row per key.",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Path relative to the folder."},
                    "status": {
                        "type": "string",
                        "enum": [
                            "deleted",
                            "already_deleted",
                            "not_found",
                            "failed",
                            "not_on_list",
                        ],
                        "description": "What happened to this key.",
                    },
                    "versions": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "The versions soft-deleted (status 'deleted' only).",
                    },
                    "error": {
                        "type": "string",
                        "description": "The error type (status 'failed' only).",
                    },
                },
            },
        },
    },
    "required": [
        "mount",
        "path",
        "listed",
        "deleted",
        "already_deleted",
        "not_found",
        "failed",
        "not_on_list",
        "results",
    ],
}

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Delete many KV v2 secrets under one folder with one approval, for "
        "example when a lab or a tenant is removed. It is the same soft delete "
        "as vault.kv.delete: every live version is marked deleted, and Vault can "
        "undelete it. Only the keys you list are deleted."
    ),
    "preconditions": (
        "First call vault.kv.list with recursive=true on the folder and pass the "
        "returned keys as 'keys'. A second person must approve; the approval card "
        "shows every key path and the count. The Vault role needs 'list' and "
        "'read' on <mount>/metadata/<folder>/... and 'update' on "
        "<mount>/delete/<folder>/...; MEHO checks this for every listed key and "
        "every sub-folder of the folder before it asks for approval and refuses "
        "with error_code 'write_capability_denied' when a right is missing on "
        "one of them."
    ),
    "parameter_hints": {
        "mount": "Optional. KV v2 mount point; defaults to 'secret'.",
        "path": "Required. The folder relative to the mount. Not the mount root.",
        "keys": (
            "Required. Secret paths relative to the folder, from vault.kv.list "
            "with recursive=true. At most the vault_kv_folder_max_keys setting "
            "(default 500); split a bigger folder into sub-folders."
        ),
    },
    "output_shape": (
        "{'mount', 'path', 'listed', 'deleted', 'already_deleted', 'not_found', "
        "'failed', 'not_on_list', 'results': [{'key', 'status', 'versions'?, "
        "'error'?}]}. 'not_on_list' rows are secrets in the folder that were not "
        "listed; they are left alone. Never returns secret values."
    ),
    "next_step": (
        "Check 'failed' and 'not_on_list'. Run again with the same keys to retry "
        "failed ones (already deleted keys are skipped). To delete 'not_on_list' "
        "secrets, make a new call that lists them."
    ),
}


def _folder_and_keys(params: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Return ``(mount, folder, keys)``, or raise when the folder or a key is not allowed."""
    mount, path = _extract_mount_and_path(FOLDER_DELETE_OP_ID, params)
    folder = path.strip("/")
    if not folder:
        raise VaultFolderKeyError(
            f"{FOLDER_DELETE_OP_ID} refuses the mount root. Name a folder below the mount."
        )
    if any(segment in {"", ".", ".."} for segment in folder.split("/")):
        raise VaultFolderKeyError(
            f"{FOLDER_DELETE_OP_ID} path {folder!r} must not have empty, '.' or '..' parts."
        )
    keys = sorted({str(key) for key in params["keys"]})
    limit = folder_max_keys()
    if len(keys) > limit:
        raise VaultFolderKeyError(
            f"{FOLDER_DELETE_OP_ID} got {len(keys)} keys; at most {limit} are allowed "
            "(setting vault_kv_folder_max_keys). Split the folder."
        )
    for key in keys:
        segments = key.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise VaultFolderKeyError(
                f"{FOLDER_DELETE_OP_ID} key {key!r} must be a path relative to the "
                "folder, without empty, '.' or '..' parts."
            )
    return mount, folder, keys


def _deleted_at(meta: dict[str, Any], now: datetime) -> bool:
    """Say whether one version's metadata marks it deleted at *now*.

    Vault writes ``deletion_time`` both when a version is deleted and when a
    deletion is only scheduled (``delete_version_after``). Only a time that
    has passed means the version is gone. A ``deletion_time`` that cannot be
    parsed is treated as deleted, which only makes the op skip a version.
    """
    if meta.get("destroyed"):
        return True
    raw = meta.get("deletion_time")
    if not raw:
        return False
    try:
        deletion_time = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if deletion_time.tzinfo is None:
        deletion_time = deletion_time.replace(tzinfo=UTC)
    return deletion_time <= now


async def _live_versions(client: Any, *, mount: str, path: str, now: datetime) -> list[int] | None:
    """Return the versions not deleted or destroyed at *now*, or ``None`` when no secret exists.

    A version whose deletion is scheduled for later is still live.
    """
    try:
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_metadata,
            path=path,
            mount_point=mount,
        )
    except hvac.exceptions.InvalidPath:
        return None
    versions: dict[str, Any] = payload["data"]["versions"]
    return sorted(int(number) for number, meta in versions.items() if not _deleted_at(meta, now))


async def _delete_one(client: Any, *, mount: str, path: str, now: datetime) -> dict[str, Any]:
    """Soft-delete the versions of one secret that are live at *now* and say what happened."""
    try:
        live = await _live_versions(client, mount=mount, path=path, now=now)
        if live is None:
            return {"status": "not_found"}
        if not live:
            return {"status": "already_deleted"}
        await asyncio.to_thread(
            client.secrets.kv.v2.delete_secret_versions,
            path=path,
            versions=live,
            mount_point=mount,
        )
    except Exception as exc:  # one key's failure must not stop the others
        return {"status": "failed", "error": type(exc).__name__}
    return {"status": "deleted", "versions": live}


async def vault_kv_folder_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Soft-delete every listed secret under one KV v2 folder.

    Op-id: ``vault.kv.folder.delete``. ``safety_level=dangerous`` and
    ``requires_approval=True``. Lists the folder again when it runs, then
    soft-deletes only the listed keys that are still there. A secret in the
    folder that is not listed is reported as ``not_on_list`` and left alone.
    """
    mount, folder, keys = _folder_and_keys(params)

    # Mutating op: the read-only platform-path allow-list does not apply.
    # Every key is below the folder, so one check covers them all.
    enforce_tenant_scope(operator, mount=mount, path=folder, read_only=False)

    rows: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    async with vault_client_for_target(operator, target) as client:
        present = set(
            await list_kv_tree(client, mount=mount, path=folder, max_keys=folder_max_keys())
        )
        for key in keys:
            if key not in present:
                rows.append({"key": key, "status": "not_found"})
                continue
            outcome = await _delete_one(client, mount=mount, path=f"{folder}/{key}", now=now)
            rows.append({"key": key, **outcome})

    rows.extend({"key": key, "status": "not_on_list"} for key in sorted(present - set(keys)))
    counts = {
        status: sum(1 for row in rows if row["status"] == status)
        for status in ("deleted", "already_deleted", "not_found", "failed", "not_on_list")
    }
    return {"mount": mount, "path": folder, "listed": len(keys), **counts, "results": rows}


async def _folder_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval card for ``vault.kv.folder.delete``: every key path and the count.

    Pure: reads only the params, so ``preview_operation`` shows the agent the
    same block the approver reads. The params are what the approval binds, so
    this list is exactly what the call may delete.
    """
    mount = str(ctx.params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    folder = str(ctx.params.get("path", "")).strip().strip("/")
    raw_keys = ctx.params.get("keys")
    keys = sorted({str(key) for key in raw_keys}) if isinstance(raw_keys, list) else []
    return {
        "resource": "kv_folder",
        "mount": mount,
        "path": folder,
        "kv_version": 2,
        "semantics": "soft_delete",
        "key_count": len(keys),
        "key_paths": [f"{folder}/{key}" for key in keys],
        "note": (
            "Only these secrets are soft-deleted. A secret added to the folder "
            "later is left alone. Vault can undelete a soft-deleted secret."
        ),
    }


def _folder_delete_checks(
    mount: str, folder: str, keys: list[str], sub_folders: Sequence[str] = ()
) -> list[tuple[str, frozenset[str]]]:
    """The ``(path, rights)`` pairs the delete needs, as ``sys/capabilities-self`` names them.

    ``list`` on the folder's metadata path, on every sub-folder in
    *sub_folders* (the ones the walk found) and on every sub-folder a
    listed key is in, then ``read`` on each key's metadata path and
    ``update`` on each key's delete path.
    """
    folders: set[str] = {folder, *(f"{folder}/{sub}" for sub in sub_folders)}
    for key in keys:
        parts = key.split("/")
        for depth in range(1, len(parts)):
            folders.add(f"{folder}/{'/'.join(parts[:depth])}")
    checks: list[tuple[str, frozenset[str]]] = [
        (f"{mount}/metadata/{sub}", frozenset({"list"})) for sub in sorted(folders)
    ]
    for key in keys:
        checks.append((f"{mount}/metadata/{folder}/{key}", frozenset({"read"})))
        checks.append((f"{mount}/delete/{folder}/{key}", frozenset({"update"})))
    return checks


async def _folder_delete_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Check, before the call waits for approval, that Vault will allow the whole delete.

    Walks the folder (:func:`list_kv_folders`) to find every sub-folder the
    delete will list, builds the paths with :func:`_folder_delete_checks`
    and probes them all in one ``sys/capabilities-self`` call. A sub-folder
    the walk could not list is probed too, so the refusal names it. Returns
    the first check that fails, else the last check with the number of
    paths checked. Returns ``None`` when the walk or the probe itself fails
    (fail-soft, like the other KV writes; the run then refuses the call
    before it deletes anything).
    """
    mount = str(ctx.params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    folder = str(ctx.params.get("path", "")).strip().strip("/")
    raw_keys = ctx.params.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys or not folder:
        return None
    keys = sorted({str(key) for key in raw_keys})
    async with vault_client_for_target(ctx.operator, ctx.target) as client:
        sub_folders = await list_kv_folders(
            client, mount=mount, path=folder, max_keys=folder_max_keys()
        )
    return await vault_capabilities_preflight(
        ctx.operator,
        FOLDER_DELETE_OP_ID,
        _folder_delete_checks(mount, folder, keys, sub_folders),
        ctx.target,
    )


register_preview_builder(FOLDER_DELETE_OP_ID, _folder_delete_preview)
register_permission_preflight(FOLDER_DELETE_OP_ID, _folder_delete_preflight)


def _registration_max_keys() -> int:
    """The ``keys`` limit for the param schema, read when the op is registered.

    A running backplane always has its settings, so this is the
    ``vault_kv_folder_max_keys`` setting. A process without the required
    settings (some test sweeps register every op with no env) gets the
    setting's default. The handler checks the live setting again anyway.
    """
    try:
        return folder_max_keys()
    except KeyError:
        return DEFAULT_FOLDER_MAX_KEYS


async def register_vault_kv_folder_operations(
    *,
    when_to_use: str,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``vault.kv.folder.delete`` into the ``kv`` group.

    The ``keys`` limit in the param schema is read from the
    ``vault_kv_folder_max_keys`` setting here, at start-up.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        when_to_use=when_to_use,
        embedding_service=embedding_service,
        op_id=FOLDER_DELETE_OP_ID,
        handler=vault_kv_folder_delete,
        summary="Soft-delete the listed KV v2 secrets under one folder, with one approval.",
        description=(
            "Soft-deletes every live version of each listed secret under one KV v2 "
            "folder. Vault can undelete them. One approval covers the whole list; "
            "the card shows every key path. Secrets that are not listed are left "
            "alone and reported."
        ),
        parameter_schema=vault_kv_folder_delete_parameter_schema(_registration_max_keys()),
        response_schema=_RESPONSE_SCHEMA,
        tags=["write", "destructive", "reversible"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
    )
