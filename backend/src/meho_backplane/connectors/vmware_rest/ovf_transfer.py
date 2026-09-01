# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Streaming disk transfer for the ``HttpNfcLease`` OVF import (#3229).

The net-new machinery the deploy-window-decoupling fix is built on: given a
lease's per-device upload URLs and an :class:`OvfSource`, stream each disk
straight from the source to its device URL with a background
``HttpNfcLeaseProgress`` heartbeat. Nothing here buffers a whole disk -- the
source's ``aiter_bytes`` chunks flow directly into the ``httpx`` PUT body, so
a multi-GB transfer is bounded only by its own duration, never by a single
HTTP read-timeout (the #3176 failure mode). The heartbeat both keeps the
lease + source session alive and surfaces percent progress.

The control-plane vim calls (``CreateImportSpec`` / ``ImportVApp`` / lease
poll / ``Complete`` / ``Abort``) live in :mod:`.ovf_import_control`; the
orchestration in :mod:`.ovf_import`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "OvfSource",
    "OvfSourceFile",
    "ProgressTracker",
    "TransferEntry",
    "lease_progress",
    "plan_transfers",
    "substitute_wildcard_host",
    "transfer_all",
]

# Content-Type the ESXi NFC endpoint expects for a stream-optimized VMDK PUT;
# non-disk file backings (ISO, nvram) upload as opaque octets.
_STREAM_VMDK_CONTENT_TYPE = "application/x-vnd.vmware-streamVmdk"
_OCTET_STREAM_CONTENT_TYPE = "application/octet-stream"

# The lease PUT legitimately outlasts the connector's 30s client default (a
# multi-GB disk copy), so read/write are uncapped; connect/pool stay fast so a
# dead ESXi host still fails fast.
_DISK_UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0)

# Default heartbeat cadence (module-global so tests can shrink it).
_PROGRESS_HEARTBEAT_INTERVAL = 30.0


class OvfSourceFile(Protocol):
    """One openable source file (a disk) with its total byte size."""

    size: int

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        """Yield the file's bytes in transfer-sized chunks."""
        ...


class OvfSource(Protocol):
    """The OVF byte source the import engine streams from.

    :mod:`.library_download` implements this over the content-library
    download-session REST flow; tests supply an in-memory fake. ``open_disk``
    is an async context manager so the streaming read is torn down
    deterministically after each disk uploads.
    """

    async def read_descriptor(self) -> str:
        """Return the OVF descriptor XML text (small; buffered whole)."""
        ...

    def open_disk(self, name: str) -> contextlib.AbstractAsyncContextManager[OvfSourceFile]:
        """Open the named disk file for streaming (async context manager)."""
        ...

    async def keep_alive(self) -> None:
        """Refresh the source-side session so a long transfer does not expire it."""
        ...

    async def aclose(self) -> None:
        """Release the source (cancel/delete the download session)."""
        ...


@dataclass
class TransferEntry:
    """One planned disk upload: source file path -> lease device URL."""

    device_id: str
    path: str
    url: str
    size: int
    is_disk: bool


def substitute_wildcard_host(url: str, connect_host: str) -> str:
    """Replace a ``*`` device-URL host with the host the client connected to.

    A ``HttpNfcLeaseDeviceUrl.url`` may return ``*`` for the host when vCenter
    cannot determine a reachable name (NAT / proxy / multihomed); the client
    substitutes the host it used to reach the server (the
    ``HttpNfcLeaseDeviceUrl.url`` spec contract). A concrete host is untouched.
    """
    parts = urlsplit(url)
    if parts.hostname != "*":
        return url
    userinfo = f"{parts.username}@" if parts.username else ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{userinfo}{connect_host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def plan_transfers(
    file_items: list[dict[str, Any]], device_urls: list[dict[str, Any]], connect_host: str
) -> list[TransferEntry]:
    """Match each ``OvfFileItem`` to its lease ``deviceUrl`` by import key.

    ``HttpNfcLeaseDeviceUrl.importKey`` equals the ``OvfFileItem.deviceId``
    (the name-based device id from the ImportSpec); a ``*`` URL host is
    substituted with *connect_host*. A file with no matching device URL is
    skipped (the lease did not request its upload, e.g. a shared parent disk).
    """
    by_key: dict[str, dict[str, Any]] = {}
    for durl in device_urls:
        if not isinstance(durl, dict):
            continue
        key = durl.get("importKey") or durl.get("key")
        if isinstance(key, str):
            by_key[key] = durl
    plan: list[TransferEntry] = []
    for item in file_items:
        device_id = item.get("deviceId") if isinstance(item, dict) else None
        matched = by_key.get(device_id) if isinstance(device_id, str) else None
        url = matched.get("url") if isinstance(matched, dict) else None
        if not isinstance(device_id, str) or not isinstance(url, str) or matched is None:
            continue
        size = item.get("size")
        plan.append(
            TransferEntry(
                device_id=device_id,
                path=str(item.get("path", device_id)),
                url=substitute_wildcard_host(url, connect_host),
                size=int(size) if isinstance(size, int) and not isinstance(size, bool) else 0,
                is_disk=bool(matched.get("disk")),
            )
        )
    return plan


class ProgressTracker:
    """Shared byte counter -> lease percent, updated as disks stream."""

    def __init__(self, total_bytes: int) -> None:
        self._total = max(total_bytes, 0)
        self.sent = 0

    def add(self, chunk: int) -> None:
        """Advance the sent-byte counter as a chunk flows to the wire."""
        self.sent += chunk

    def percent(self) -> int:
        """Completion as an integer 0-100 (0 when the total size is unknown)."""
        if self._total <= 0:
            return 0
        return min(100, int(self.sent * 100 / self._total))


async def lease_progress(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    percent: int,
) -> None:
    """Send one ``HttpNfcLeaseProgress`` (keeps the lease alive + reports percent)."""
    path = f"/HttpNfcLease/{lease_moid}/HttpNfcLeaseProgress"
    await connector._post_vmomi_json(target, path, operator=operator, json={"percent": percent})


async def _heartbeat(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    tracker: ProgressTracker,
    source: OvfSource,
    interval: float,
) -> None:
    """Background loop: refresh the lease + source session on a fixed cadence.

    Runs concurrently with the disk stream so a transfer that outlasts the
    lease / download-session timeout keeps both alive (AC1) and reports
    percent (AC2). Cancelled by :func:`transfer_all` once every disk uploads.
    A transient progress/keep-alive fault is swallowed -- the heartbeat is
    best-effort; a real upload fault surfaces on the transfer path itself.
    """
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(httpx.HTTPError):
            await lease_progress(
                connector, target, operator, lease_moid=lease_moid, percent=tracker.percent()
            )
        with contextlib.suppress(Exception):
            await source.keep_alive()


async def _upload_one(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    *,
    entry: TransferEntry,
    source: OvfSource,
    tracker: ProgressTracker,
) -> int:
    """Stream one disk from the source straight to its lease device URL.

    Returns the byte count uploaded. The stream is wrapped so the shared
    :class:`ProgressTracker` advances as bytes flow -- that is what the
    heartbeat reports. Uses the connector's pooled client (its TLS trust
    config) with the ESXi NFC device URL as an absolute URL; the lease token
    embedded in the URL authorizes the PUT. ``Content-Length`` is the source
    file's own size (not the descriptor's declared size), sent explicitly so
    the NFC endpoint gets a sized -- not chunked -- body.
    """
    client = await connector._http_client(target)
    content_type = _STREAM_VMDK_CONTENT_TYPE if entry.is_disk else _OCTET_STREAM_CONTENT_TYPE
    async with source.open_disk(entry.path) as handle:
        size = handle.size

        async def _counted() -> AsyncIterator[bytes]:
            async for chunk in handle.aiter_bytes():
                tracker.add(len(chunk))
                yield chunk

        headers = {"Content-Type": content_type, "Content-Length": str(size)}
        resp = await client.put(
            entry.url, content=_counted(), headers=headers, timeout=_DISK_UPLOAD_TIMEOUT
        )
        resp.raise_for_status()
    return size


async def transfer_all(
    connector: VmwareRestConnector,
    target: VsphereTargetLike,
    operator: Operator,
    *,
    lease_moid: str,
    plan: list[TransferEntry],
    source: OvfSource,
    heartbeat_interval: float = _PROGRESS_HEARTBEAT_INTERVAL,
) -> list[dict[str, Any]]:
    """Upload every planned disk with a concurrent progress heartbeat.

    Files upload **in order** (a later file may patch an earlier one per the
    OVF spec). Returns the per-disk manifest. Any upload failure propagates to
    the caller, which aborts the lease so vCenter tears down the half-import.
    The heartbeat task is always cancelled + awaited on exit.
    """
    total = sum(e.size for e in plan)
    tracker = ProgressTracker(total)
    manifest: list[dict[str, Any]] = []
    hb = asyncio.create_task(
        _heartbeat(
            connector,
            target,
            operator,
            lease_moid=lease_moid,
            tracker=tracker,
            source=source,
            interval=heartbeat_interval,
        )
    )
    try:
        for entry in plan:
            sent = await _upload_one(connector, target, entry=entry, source=source, tracker=tracker)
            manifest.append({"path": entry.path, "device_id": entry.device_id, "size_bytes": sent})
        # Final 100% progress so the server records completion before Complete.
        await lease_progress(connector, target, operator, lease_moid=lease_moid, percent=100)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
    return manifest
