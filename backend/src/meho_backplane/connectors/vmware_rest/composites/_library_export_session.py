# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vCenter side of the content-library export: download session and clean-up.

Used by :mod:`._library_export`. :class:`DownloadSession` opens a download session
for one library item (with a ``Client-Token`` header), lists and prepares its
files, waits for their download URLs, keeps the session open and ends it.
:class:`ExportRun` holds what one export opened and closes it again
(:meth:`ExportRun.cleanup`): the session, then a temporary library item.
:func:`leftover_remover` deletes the temporary items of earlier runs that
died before their clean-up.

Every call rides the connector's own session. The download-session calls
change no vCenter object, so they run without the write check, like the
library import of :mod:`.library_download`. The temporary item delete
removes only an item this run made, or an old one with MEHO's name prefix.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest import library_download
from meho_backplane.connectors.vmware_rest._transfer import is_meho_temp_name
from meho_backplane.connectors.vmware_rest.composites._library_capture import (
    CLIENT_TOKEN_HEADER,
)
from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY_ITEM,
    get_or_none,
    id_problem,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _split_sub_op,
    _unwrap_value,
)

__all__ = [
    "TEMP_LABEL",
    "DownloadSession",
    "ExportRun",
    "SessionError",
    "leftover_remover",
    "vcenter_url",
]

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

OP_SESSION_CREATE: Final = library_download.OP_DOWNLOAD_SESSION_CREATE
OP_SESSION_FILES: Final = library_download.OP_DOWNLOAD_SESSION_LIST_FILES
OP_SESSION_PREPARE: Final = library_download.OP_DOWNLOAD_SESSION_PREPARE_FILE
OP_SESSION_KEEP_ALIVE: Final = library_download.OP_DOWNLOAD_SESSION_KEEP_ALIVE
#: The file read takes ``file_name`` as a query parameter; the vendor files
#: key the path with it, so the call id carries it too.
OP_SESSION_FILE: Final = (
    "GET:/content/library/item/download-session/{downloadSessionId}/file?file_name"
)
OP_SESSION_DELETE: Final = "DELETE:/content/library/item/download-session/{downloadSessionId}"
OP_LIST_ITEMS: Final = "GET:/content/library/item?library_id"
OP_DELETE_ITEM: Final = "DELETE:/content/library/item/{libraryItemId}"

_PREPARED: Final = "PREPARED"
_ERROR: Final = "ERROR"
TEMP_LABEL: Final = "ovf-export"
#: A temporary item younger than this may belong to a run that is still going.
_LEFTOVER_AGE: Final = timedelta(days=1)

# Module-global so tests can shrink them.
_PREPARE_TIMEOUT_SECONDS = 600.0
_PREPARE_POLL_SECONDS = 2.0
_KEEP_ALIVE_SECONDS = 60.0


class SessionError(RuntimeError):
    """A download-session step failed; the message names the file, never a URL."""


def _path(op_id: str, path_params: dict[str, Any]) -> tuple[str, str]:
    """``(method, path)`` of *op_id*. A bare query key (``?file_name``) only names
    the query parameter in the call id, so it is left out of the path."""
    base, sep, query = op_id.partition("?")
    method, path, _ = _split_sub_op(base if sep and "=" not in query else op_id, path_params)
    return method, path


@dataclass
class DownloadSession:
    """One vCenter download session for one library item."""

    connector: VmwareRestConnector
    target: Any
    operator: Operator
    item_id: str
    session_id: str | None = None
    client_token: str | None = None

    async def _send(self, op_id: str, body: dict[str, Any] | None, **kw: Any) -> Any:
        method, path = _path(op_id, {"downloadSessionId": self.session_id})
        mounted = await self.connector.mount_op_path(self.target, path, self.operator)
        return await self.connector._post_json(
            self.target, mounted, operator=self.operator, verb=method, json=body, **kw
        )

    async def _get(self, op_id: str, query: dict[str, Any] | None = None) -> Any:
        _method, path = _path(op_id, {"downloadSessionId": self.session_id})
        mounted = await self.connector.mount_op_path(self.target, path, self.operator)
        payload = await self.connector._get_json(
            self.target, mounted, operator=self.operator, params=query
        )
        return _unwrap_value(payload)

    async def open(self, client_token: str) -> None:
        self.client_token = client_token
        payload = await self._send(
            OP_SESSION_CREATE,
            {"library_item_id": self.item_id},
            extra_headers={CLIENT_TOKEN_HEADER: client_token},
        )
        session_id = _unwrap_value(payload)
        if not isinstance(session_id, str) or not session_id:
            raise SessionError("vCenter answered the download session create with no id")
        self.session_id = session_id

    async def file_names(self) -> list[str]:
        files = await self._get(OP_SESSION_FILES)
        rows = files if isinstance(files, list) else []
        return [row["name"] for row in rows if isinstance(row, dict) and row.get("name")]

    async def prepare(self, name: str) -> None:
        await self._send(OP_SESSION_PREPARE, {"file_name": name})

    async def keep_alive(self) -> None:
        if self.session_id is not None:
            await self._send(OP_SESSION_KEEP_ALIVE, {})

    async def delete(self) -> None:
        if self.session_id is not None:
            await self._send(OP_SESSION_DELETE, None)
            self.session_id = None

    async def download_urls(self, names: list[str]) -> dict[str, str]:
        """Wait until every file is ``PREPARED``; return its download URL by name."""
        urls: dict[str, str] = {}
        deadline = time.monotonic() + _PREPARE_TIMEOUT_SECONDS
        last_keep_alive = time.monotonic()
        while True:
            for name in names:
                if name in urls:
                    continue
                info = await self._get(OP_SESSION_FILE, {"file_name": name})
                info = info if isinstance(info, dict) else {}
                if info.get("status") == _ERROR:
                    raise SessionError(f"{name}: vCenter could not prepare the file")
                endpoint = info.get("download_endpoint")
                uri = endpoint.get("uri") if isinstance(endpoint, dict) else None
                if info.get("status") == _PREPARED and isinstance(uri, str) and uri:
                    urls[name] = uri
            if len(urls) == len(names):
                return urls
            if time.monotonic() >= deadline:
                raise SessionError("vCenter did not prepare the files in time")
            if time.monotonic() - last_keep_alive >= _KEEP_ALIVE_SECONDS:
                await self.keep_alive()
                last_keep_alive = time.monotonic()
            await asyncio.sleep(_PREPARE_POLL_SECONDS)


async def vcenter_url(connector: VmwareRestConnector, target: Any, uri: str) -> str:
    """The download URL on the target's own host and port: only its path is kept.

    So the vCenter session goes to no other host, whatever host vCenter names
    (an FQDN, an address, or ``*``).
    """
    client = await connector._http_client(target)
    return str(client.base_url.copy_with(raw_path=httpx.URL(uri).raw_path))


@dataclass
class ExportRun:
    """What one export opened, and the clean-up that closes it."""

    connector: VmwareRestConnector
    target: Any
    operator: Operator
    session: DownloadSession | None = None
    temp_item_id: str | None = None
    cleanup_state: dict[str, Any] = field(
        default_factory=lambda: {
            "download_session_deleted": None,
            "temporary_item_deleted": None,
            "note": None,
        }
    )
    _cleaned: bool = False

    async def cleanup(self) -> None:
        """End the session, then delete the temporary item. Runs once; never raises."""
        if self._cleaned:
            return
        self._cleaned = True
        notes: list[str] = []
        if self.session is not None and self.session.session_id is not None:
            try:
                await self.session.delete()
                self.cleanup_state["download_session_deleted"] = True
            except Exception as exc:  # the vendor timeout ends it; say so
                self.cleanup_state["download_session_deleted"] = False
                notes.append(f"the download session was not ended ({type(exc).__name__})")
        if self.temp_item_id is not None:
            try:
                await _delete_item(self.connector, self.target, self.operator, self.temp_item_id)
                self.cleanup_state["temporary_item_deleted"] = True
            except Exception as exc:  # the next run removes it; say so
                self.cleanup_state["temporary_item_deleted"] = False
                notes.append(
                    f"the temporary item {self.temp_item_id} was not deleted "
                    f"({type(exc).__name__}); delete it, or the next export removes it after a day"
                )
        self.cleanup_state["note"] = "; ".join(notes) or None


async def _delete_item(
    connector: VmwareRestConnector, target: Any, operator: Operator, item_id: str
) -> None:
    """Delete one item MEHO made. A gone item counts as deleted."""
    _method, path = _path(OP_DELETE_ITEM, {"libraryItemId": item_id})
    mounted = await connector.mount_op_path(target, path, operator)
    try:
        await connector._post_json(target, mounted, operator=operator, verb="DELETE", json=None)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise


def _created_before(model: dict[str, Any], limit: datetime) -> bool:
    raw = model.get("creation_time")
    try:
        created = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created < limit


def leftover_remover(run: ExportRun, library_id: str) -> Any:
    """The ``remove_leftovers`` callback: old MEHO export items of *library_id*."""

    async def remove() -> list[str]:
        _method, path = _path(OP_LIST_ITEMS, {})
        mounted = await run.connector.mount_op_path(run.target, path, run.operator)
        ids = _unwrap_value(
            await run.connector._get_json(
                run.target, mounted, operator=run.operator, params={"library_id": library_id}
            )
        )
        limit = datetime.now(UTC) - _LEFTOVER_AGE
        removed: list[str] = []
        for item_id in ids if isinstance(ids, list) else []:
            if item_id == run.temp_item_id or id_problem(item_id, "item id") is not None:
                continue
            model = await get_or_none(
                run.connector,
                run.target,
                run.operator,
                OP_GET_LIBRARY_ITEM,
                {"libraryItemId": item_id},
            )
            name = str((model or {}).get("name") or "")
            if not (model and is_meho_temp_name(name) and TEMP_LABEL in name):
                continue
            if not _created_before(model, limit):
                continue
            try:
                await _delete_item(run.connector, run.target, run.operator, item_id)
            except httpx.HTTPError:
                continue
            removed.append(f"library item {name} ({item_id})")
        return removed

    return remove
