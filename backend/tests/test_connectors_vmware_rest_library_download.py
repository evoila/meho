# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the content-library download-session OVF source (#3229).

Exercises :class:`LibraryDownloadSource` against a stateful fake connector
(no live vCenter): create the session, list files, prepare + poll to
``PREPARED``, buffer the OVF descriptor, stream a disk with its size taken
from the download ``Content-Length``, keep-alive, and cancel on close.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.vmware_rest.library_download import (
    LibraryDownloadError,
    LibraryDownloadSource,
)


class _Target:
    host = "vcenter.lab"
    name = "vc-1"


class _Operator:
    raw_jwt = "jwt"


class _GetResp:
    def __init__(self, *, text: str = "", data: bytes = b"", headers: dict[str, str] | None = None):
        self.text = text
        self._data = data
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk: int | None = None) -> Any:
        yield self._data


class _StreamCtx:
    def __init__(self, resp: _GetResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _GetResp:
        return self._resp

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _SourceClient:
    def __init__(self, descriptor: str, disk: bytes) -> None:
        self._descriptor = descriptor
        self._disk = disk

    async def get(self, uri: str, *, headers: dict[str, str]) -> _GetResp:
        return _GetResp(text=self._descriptor)

    def stream(self, method: str, uri: str, *, headers: dict[str, str]) -> _StreamCtx:
        return _StreamCtx(
            _GetResp(data=self._disk, headers={"content-length": str(len(self._disk))})
        )


class _SourceConnector:
    """Stateful fake: a file becomes PREPARED once ``?action=prepare`` is posted."""

    def __init__(
        self,
        *,
        files: list[str],
        descriptor: str = "<Envelope/>",
        disk: bytes = b"DISKDATA",
        prepare_status: dict[str, str] | None = None,
    ) -> None:
        self._files = files
        self._prepared: set[str] = set()
        self._client = _SourceClient(descriptor, disk)
        self._prepare_status = prepare_status or {}
        self.session_created = False
        self.canceled = False
        self.keep_alives = 0
        self.posts: list[str] = []

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return path

    async def _post_json(self, target: Any, path: str, *, operator: Any, json: Any = None) -> Any:
        self.posts.append(path)
        if path == "/content/library/item/download-session":
            self.session_created = True
            return "dl-1"
        if path.endswith("/file?action=prepare"):
            self._prepared.add(json["file_name"])
            return {"name": json["file_name"], "status": "PREPARE_REQUESTED"}
        if path.endswith("?action=keep-alive"):
            self.keep_alives += 1
            return {}
        if path.endswith("?action=cancel"):
            self.canceled = True
            return {}
        return {}

    async def _get_json(self, target: Any, path: str, *, operator: Any, params: Any = None) -> Any:
        out: list[dict[str, Any]] = []
        for name in self._files:
            forced = self._prepare_status.get(name)
            if forced:
                out.append({"name": name, "status": forced})
            elif name in self._prepared:
                out.append(
                    {
                        "name": name,
                        "status": "PREPARED",
                        "download_endpoint": {"uri": f"https://vcenter.lab/dl/{name}"},
                    }
                )
            else:
                out.append({"name": name, "status": "UNPREPARED"})
        return out

    async def _http_client(self, target: Any) -> _SourceClient:
        return self._client

    async def auth_headers(self, target: Any, operator: Any) -> dict[str, str]:
        return {}


def _source(conn: _SourceConnector) -> LibraryDownloadSource:
    return LibraryDownloadSource(
        connector=conn,  # type: ignore[arg-type]
        target=_Target(),
        operator=_Operator(),  # type: ignore[arg-type]
        library_item_id="item-1",
    )


async def test_read_descriptor_creates_session_prepares_and_buffers() -> None:
    conn = _SourceConnector(
        files=["app.ovf", "app-disk1.vmdk"], descriptor="<Envelope>ovf</Envelope>"
    )
    source = _source(conn)
    text = await source.read_descriptor()
    assert text == "<Envelope>ovf</Envelope>"
    assert conn.session_created
    assert "/content/library/item/download-session/dl-1/file?action=prepare" in conn.posts


async def test_open_disk_streams_bytes_with_content_length_size() -> None:
    conn = _SourceConnector(files=["app.ovf", "app-disk1.vmdk"], disk=b"0123456789")
    source = _source(conn)
    async with source.open_disk("app-disk1.vmdk") as handle:
        assert handle.size == 10  # from the download Content-Length
        collected = b"".join([chunk async for chunk in handle.aiter_bytes()])
    assert collected == b"0123456789"


async def test_missing_descriptor_raises() -> None:
    conn = _SourceConnector(files=["only-a-disk.vmdk"])
    source = _source(conn)
    with pytest.raises(LibraryDownloadError, match=r"no \.ovf descriptor"):
        await source.read_descriptor()


async def test_prepare_error_status_raises() -> None:
    conn = _SourceConnector(files=["app.ovf"], prepare_status={"app.ovf": "ERROR"})
    source = _source(conn)
    with pytest.raises(LibraryDownloadError, match="failed to prepare"):
        await source.read_descriptor()


async def test_keep_alive_and_aclose_are_session_scoped() -> None:
    conn = _SourceConnector(files=["app.ovf"])
    source = _source(conn)
    # No session yet: keep_alive / aclose are no-ops (nothing to refresh/cancel).
    await source.keep_alive()
    await source.aclose()
    assert conn.keep_alives == 0
    assert not conn.canceled
    # Establish a session, then keep-alive + cancel act on it.
    await source.read_descriptor()
    await source.keep_alive()
    await source.aclose()
    assert conn.keep_alives == 1
    assert conn.canceled


async def test_create_returning_no_id_raises() -> None:
    class _NoIdConnector(_SourceConnector):
        async def _post_json(
            self, target: Any, path: str, *, operator: Any, json: Any = None
        ) -> Any:
            if path == "/content/library/item/download-session":
                return ""
            return await super()._post_json(target, path, operator=operator, json=json)

    source = _source(_NoIdConnector(files=["app.ovf"]))
    with pytest.raises(LibraryDownloadError, match="no id"):
        await source.read_descriptor()
