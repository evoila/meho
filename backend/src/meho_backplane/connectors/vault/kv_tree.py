# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Walk a KV v2 folder and list every secret under it, also in sub-folders.

Used by ``vault.kv.list`` with ``recursive=true`` and by
``vault.kv.folder.delete``. Vault's list endpoint
(``LIST /v1/<mount>/metadata/<path>``) returns only the names directly
below one folder, with sub-folders marked by a trailing ``/``. This module
lists each sub-folder in turn and returns the secret paths relative to the
start folder. It reads names only, never a secret value.

The walk stops with :class:`VaultFolderTooLargeError` as soon as it finds
more secrets than ``max_keys`` (the ``vault_kv_folder_max_keys`` setting),
so a huge folder cannot make one call run for a long time.
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

from meho_backplane.settings import get_settings

__all__ = [
    "DEFAULT_FOLDER_MAX_KEYS",
    "VaultFolderTooLargeError",
    "folder_max_keys",
    "list_kv_tree",
]

#: Default of the ``vault_kv_folder_max_keys`` setting.
DEFAULT_FOLDER_MAX_KEYS = 500


class VaultFolderTooLargeError(Exception):
    """A KV v2 folder holds more secrets than the ``vault_kv_folder_max_keys`` setting allows.

    The message names the folder and the limit, never a secret value. The
    caller should pick a smaller folder.
    """


def folder_max_keys() -> int:
    """Return the most secrets one folder call may walk or delete."""
    return int(get_settings().vault_kv_folder_max_keys)


async def _list_names(client: Any, *, mount: str, path: str) -> list[str]:
    """List the names directly below *path*. A folder with nothing in it gives ``[]``.

    Vault answers a list of an empty or missing folder with 404, which hvac
    raises as ``InvalidPath``. Any other error (for example ``Forbidden``)
    is raised unchanged.
    """
    try:
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.list_secrets,
            path=path,
            mount_point=mount,
        )
    except hvac.exceptions.InvalidPath:
        return []
    return [str(name) for name in payload["data"]["keys"]]


async def list_kv_tree(client: Any, *, mount: str, path: str, max_keys: int) -> list[str]:
    """Return every secret path under the folder *path*, relative to it, sorted.

    *path* is the folder relative to the mount, without leading or trailing
    ``/`` (an empty string means the mount root). A secret ``a/b`` in the
    sub-folder ``a/`` comes back as ``"a/b"``. Folder entries are not
    returned. Raises :class:`VaultFolderTooLargeError` when the folder holds
    more than *max_keys* secrets.
    """
    found: list[str] = []
    pending: list[str] = [""]
    while pending:
        relative_folder = pending.pop()
        list_path = f"{path}/{relative_folder}".strip("/")
        for name in await _list_names(client, mount=mount, path=list_path):
            relative = f"{relative_folder}{name}"
            if name.endswith("/"):
                pending.append(relative)
                continue
            found.append(relative)
            if len(found) > max_keys:
                raise VaultFolderTooLargeError(
                    f"The folder {mount}/{path} holds more than {max_keys} secrets, "
                    "the most one call may handle (setting vault_kv_folder_max_keys). "
                    "Pick a smaller folder."
                )
    return sorted(found)
