# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Content-library download-session OVF byte source for the lease import (#3229).

Implements :class:`.ovf_transfer.OvfSource` over the content-library
download-session REST flow, so the typed ``HttpNfcLease`` import
(:mod:`.ovf_import`) can read a library item's OVF descriptor + disk files
client-side and stream them to the lease. The flow (all served by the pinned
``vcenter.yaml``):

1. ``POST /content/library/item/download-session`` -- create a session for
   the library item (``{library_item_id}``); the 201 body is the session id.
2. ``GET .../download-session/{id}/file`` -- list the item's files
   (descriptor + disks) with their prepare status.
3. ``POST .../file?action=prepare`` (``{file_name}``) -- request a file, then
   poll the file list until its status is ``PREPARED`` and its
   ``download_endpoint.uri`` is set.
4. GET that URI -- the descriptor is buffered whole (small); each disk is
   streamed, its byte count taken from the response ``Content-Length`` (the
   ``File.Info.size`` is not guaranteed set until the download completes).
5. ``POST .../download-session/{id}?action=keep-alive`` -- refresh the session
   during a long transfer (folded into the lease heartbeat).
6. ``POST .../download-session/{id}?action=cancel`` on close.

The find-action resolution that turns a name into the item id stays in
:mod:`.composites._write` (shared with ``deploy_from_library``); this source
takes the resolved item id. The download endpoints are HTTPS links on the
authenticated vCenter, so the byte GETs carry the connector's session auth
headers.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.vmware_rest.typed_ops import _unwrap_value

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

# Canonical ``METHOD:/path`` op_ids (what the ingest parser emits from
# vcenter.yaml). ``_write`` re-exports these in its
# ``_SUB_OPS_VM_IMPORT_FROM_LIBRARY`` manifest; the write-body reconcile lane
# also introspects this module for its POST ops.
OP_DOWNLOAD_SESSION_CREATE = "POST:/content/library/item/download-session"
OP_DOWNLOAD_SESSION_LIST_FILES = (
    "GET:/content/library/item/download-session/{downloadSessionId}/file"
)
OP_DOWNLOAD_SESSION_PREPARE_FILE = (
    "POST:/content/library/item/download-session/{downloadSessionId}/file?action=prepare"
)
OP_DOWNLOAD_SESSION_KEEP_ALIVE = (
    "POST:/content/library/item/download-session/{downloadSessionId}?action=keep-alive"
)
OP_DOWNLOAD_SESSION_CANCEL = (
    "POST:/content/library/item/download-session/{downloadSessionId}?action=cancel"
)

# ``Content.Library.Item.Downloadsession.File.PrepareStatus`` enum values.
_PREPARE_STATUS_PREPARED = "PREPARED"
_PREPARE_STATUS_ERROR = "ERROR"

# OVF descriptor file extension (the item carries exactly one).
_OVF_DESCRIPTOR_SUFFIX = ".ovf"

# Default file-prepare wall-clock bound + cadence (module-global so tests zero
# them).
_PREPARE_TIMEOUT_SECONDS = 300.0
_PREPARE_POLL_INTERVAL = 2.0
# Streaming read chunk for the download GET -> lease PUT hop.
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class LibraryDownloadError(RuntimeError):
    """A download-session step failed in a way the import cannot recover from."""


class _DiskStream:
    """One in-flight disk download exposing ``size`` + ``aiter_bytes`` (OvfSourceFile).

    Wraps the connector client's streaming GET so the bytes flow straight into
    the lease PUT without buffering. The download URI is resolved on
    ``__aenter__`` (just-in-time, so an earlier disk can still be uploading);
    ``size`` is the download response's ``Content-Length`` -- authoritative for
    the lease PUT's own ``Content-Length``, since ``File.Info.size`` is not
    guaranteed set before the download completes.
    """

    def __init__(self, source: LibraryDownloadSource, name: str) -> None:
        self._source = source
        self._name = name
        self._stream_cm: Any = None
        self._response: httpx.Response | None = None
        self.size = 0

    async def __aenter__(self) -> _DiskStream:
        source = self._source
        uri = await source._resolve_download_uri(self._name)
        client = await source._connector._http_client(source._target)
        headers = await source._connector.auth_headers(source._target, source._operator)
        self._stream_cm = client.stream("GET", uri, headers=headers)
        self._response = await self._stream_cm.__aenter__()
        self._response.raise_for_status()
        length = self._response.headers.get("content-length")
        self.size = int(length) if length is not None and length.isdigit() else 0
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stream_cm is not None:
            await self._stream_cm.__aexit__(*exc)

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        assert self._response is not None  # entered
        return self._response.aiter_bytes(_DOWNLOAD_CHUNK_BYTES)


class LibraryDownloadSource:
    """Content-library download-session implementation of :class:`.ovf_transfer.OvfSource`.

    Lazily creates the download session on first use; :meth:`aclose` cancels
    it. Every REST call rides the connector's own authenticated session
    (``_post_json`` / ``_get_json``) -- these are content reads, so, like the
    ``?action=find`` resolution, they run un-gated (the governed write is the
    ``ImportVApp`` inside the engine).
    """

    def __init__(
        self,
        *,
        connector: VmwareRestConnector,
        target: VsphereTargetLike,
        operator: Operator,
        library_item_id: str,
    ) -> None:
        self._connector = connector
        self._target = target
        self._operator = operator
        self._item_id = library_item_id
        self._session_id: str | None = None

    async def _ensure_session(self) -> str:
        """Create the download session on first use; return its id."""
        if self._session_id is not None:
            return self._session_id
        _, _, path = OP_DOWNLOAD_SESSION_CREATE.partition(":")
        mounted = await self._connector.mount_op_path(self._target, path, self._operator)
        payload = await self._connector._post_json(
            self._target, mounted, operator=self._operator, json={"library_item_id": self._item_id}
        )
        session_id = _unwrap_value(payload)
        if not isinstance(session_id, str) or not session_id:
            raise LibraryDownloadError(f"download-session create returned no id (got {payload!r})")
        self._session_id = session_id
        return session_id

    async def _list_files(self) -> list[dict[str, Any]]:
        """List the session's files (name / status / download_endpoint)."""
        session_id = await self._ensure_session()
        path = OP_DOWNLOAD_SESSION_LIST_FILES.partition(":")[2].format(downloadSessionId=session_id)
        mounted = await self._connector.mount_op_path(self._target, path, self._operator)
        payload = await self._connector._get_json(self._target, mounted, operator=self._operator)
        files = _unwrap_value(payload)
        return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []

    async def _prepare(self, file_name: str) -> None:
        """POST ``?action=prepare`` for one file (idempotent server-side)."""
        session_id = await self._ensure_session()
        path = OP_DOWNLOAD_SESSION_PREPARE_FILE.partition(":")[2].format(
            downloadSessionId=session_id
        )
        mounted = await self._connector.mount_op_path(self._target, path, self._operator)
        await self._connector._post_json(
            self._target, mounted, operator=self._operator, json={"file_name": file_name}
        )

    async def _resolve_download_uri(self, file_name: str) -> str:
        """Prepare *file_name* and poll to ``PREPARED``; return its download URI."""
        await self._prepare(file_name)
        deadline = time.monotonic() + _PREPARE_TIMEOUT_SECONDS
        while True:
            info = _match_file(await self._list_files(), file_name)
            status = info.get("status") if info else None
            if status == _PREPARE_STATUS_PREPARED and info is not None:
                uri = _download_uri(info)
                if uri:
                    return uri
            if status == _PREPARE_STATUS_ERROR:
                raise LibraryDownloadError(
                    f"download-session file {file_name!r} failed to prepare (status=ERROR)"
                )
            if time.monotonic() >= deadline:
                raise LibraryDownloadError(
                    f"download-session file {file_name!r} not PREPARED within "
                    f"{int(_PREPARE_TIMEOUT_SECONDS)}s"
                )
            await asyncio.sleep(_PREPARE_POLL_INTERVAL)

    async def read_descriptor(self) -> str:
        """Buffer and return the item's OVF descriptor XML text."""
        files = await self._list_files()
        descriptor_name = _descriptor_name(files)
        if descriptor_name is None:
            raise LibraryDownloadError(
                f"library item {self._item_id!r} has no .ovf descriptor file"
            )
        uri = await self._resolve_download_uri(descriptor_name)
        client = await self._connector._http_client(self._target)
        headers = await self._connector.auth_headers(self._target, self._operator)
        resp = await client.get(uri, headers=headers)
        resp.raise_for_status()
        return resp.text

    def open_disk(self, name: str) -> _DiskStream:
        """Open the named disk for streaming (an async context manager)."""
        return _DiskStream(self, name)

    async def keep_alive(self) -> None:
        """Best-effort ``?action=keep-alive`` -- extends the active session."""
        if self._session_id is None:
            return
        path = OP_DOWNLOAD_SESSION_KEEP_ALIVE.partition(":")[2].format(
            downloadSessionId=self._session_id
        )
        with contextlib.suppress(Exception):
            mounted = await self._connector.mount_op_path(self._target, path, self._operator)
            await self._connector._post_json(
                self._target, mounted, operator=self._operator, json={}
            )

    async def aclose(self) -> None:
        """Best-effort ``?action=cancel`` -- releases the download session."""
        if self._session_id is None:
            return
        path = OP_DOWNLOAD_SESSION_CANCEL.partition(":")[2].format(
            downloadSessionId=self._session_id
        )
        with contextlib.suppress(Exception):
            mounted = await self._connector.mount_op_path(self._target, path, self._operator)
            await self._connector._post_json(
                self._target, mounted, operator=self._operator, json={}
            )
        self._session_id = None


def _match_file(files: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Return the file info whose ``name`` matches (exact, then basename)."""
    for info in files:
        if info.get("name") == name:
            return info
    base = name.rsplit("/", 1)[-1]
    for info in files:
        file_name = info.get("name")
        if isinstance(file_name, str) and file_name.rsplit("/", 1)[-1] == base:
            return info
    return None


def _descriptor_name(files: list[dict[str, Any]]) -> str | None:
    """Return the name of the item's ``.ovf`` descriptor file, if present."""
    for info in files:
        name = info.get("name")
        if isinstance(name, str) and name.lower().endswith(_OVF_DESCRIPTOR_SUFFIX):
            return name
    return None


def _download_uri(info: dict[str, Any]) -> str | None:
    """Pull ``download_endpoint.uri`` off a PREPARED file info."""
    endpoint = info.get("download_endpoint")
    if isinstance(endpoint, dict):
        uri = endpoint.get("uri")
        return uri if isinstance(uri, str) and uri else None
    return None
