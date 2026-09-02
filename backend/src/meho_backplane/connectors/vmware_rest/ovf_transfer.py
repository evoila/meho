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

**Device-host trust model (#3284).** Each disk PUT streams to an *absolute*
per-device ESXi upload URL the authenticated vCenter returns in the lease
info -- not to ``target.host``. The pooled client dials that absolute URL,
so the per-target SSRF host re-screen (keyed to ``target.host``,
:mod:`meho_backplane.connectors.adapters.http`) does **not** run for the
device host, and that bypass stays: screening a private ESXi host would
wrongly block a legitimate import. The replacement control is
**certificate thumbprint pinning**. vCenter attests each device host by
returning its ``HttpNfcLeaseDeviceUrl.sslThumbprint`` (a SHA-1 hash of the
DER certificate, colon-separated hex -- the same vim thumbprint convention
``HostConnectSpec.sslThumbprint`` documents); :func:`verify_device_thumbprint`
opens a pre-flight handshake to the device host and refuses to stream a
byte unless the presented certificate matches that attestation. When the
field is **empty** -- which the vim spec sanctions ("Empty if no SSL
thumbprint is available or needed") -- pinning is skipped and the transfer
falls back to the pooled client's existing TLS trust (fail-open on an
absent attestation; see :func:`verify_device_thumbprint`).

The pin is a *pre-flight* handshake, separate from the PUT connection, run
per-disk immediately before its upload: ``httpx`` builds its client on a
stdlib :class:`ssl.SSLContext`, which exposes no per-connection fingerprint
callback, so the presented certificate cannot be pinned inside the PUT
itself. The residual window is one disk's worth of time between the verified
handshake and the pooled client's connect to the same ``host:port`` -- far
smaller than the un-pinned status quo this replaces, and the practical
alternative short of abandoning the pooled client (and its retry / redirect /
flight-recorder plumbing) for a hand-rolled socket upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import socket
import ssl
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
    "DeviceThumbprintError",
    "OvfSource",
    "OvfSourceFile",
    "ProgressTracker",
    "TransferEntry",
    "lease_progress",
    "plan_transfers",
    "substitute_wildcard_host",
    "transfer_all",
    "verify_device_thumbprint",
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

# Connect + handshake budget for the pre-flight thumbprint verification. A
# dead device host fails the check fast rather than pinning a worker thread;
# the disk stream that follows has its own (uncapped) timeout.
_THUMBPRINT_HANDSHAKE_TIMEOUT = 10.0

# Digest algorithm keyed by the expected thumbprint's hex length. vim
# ``sslThumbprint`` is SHA-1 (40 hex chars) by convention
# (``HostConnectSpec.sslThumbprint``: "always computed using the SHA1
# hash"); a 64-char value is accepted as SHA-256 for forward-compatibility.
# An expected value of any other length matches no digest and so fails
# closed.
_THUMBPRINT_ALGORITHMS = {40: "sha1", 64: "sha256"}


class DeviceThumbprintError(Exception):
    """The device host's certificate did not match the lease attestation (#3284).

    Raised by :func:`verify_device_thumbprint` when a lease device URL
    carries a non-empty ``sslThumbprint`` but the certificate the host
    presents does not hash to it -- or the pre-flight handshake could not
    obtain a certificate to compare. Either way the disk is **not**
    streamed; :func:`~meho_backplane.connectors.vmware_rest.ovf_import`
    catches this before ``httpx.HTTPError`` and aborts the lease, so a
    tampered or redirected device URL never receives a byte.
    """


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
    """One planned disk upload: source file path -> lease device URL.

    ``ssl_thumbprint`` carries the lease's per-device
    ``HttpNfcLeaseDeviceUrl.sslThumbprint`` attestation (``None`` / empty
    when vCenter supplied none); :func:`verify_device_thumbprint` pins the
    device host's certificate to it before the PUT streams (#3284).
    """

    device_id: str
    path: str
    url: str
    size: int
    is_disk: bool
    ssl_thumbprint: str | None = None


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
    substituted with *connect_host*. Each device URL's ``sslThumbprint`` is
    captured onto the entry so the transfer can pin the device host's
    certificate (#3284). A file with no matching device URL is skipped (the
    lease did not request its upload, e.g. a shared parent disk).
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
        thumbprint = matched.get("sslThumbprint")
        plan.append(
            TransferEntry(
                device_id=device_id,
                path=str(item.get("path", device_id)),
                url=substitute_wildcard_host(url, connect_host),
                size=int(size) if isinstance(size, int) and not isinstance(size, bool) else 0,
                is_disk=bool(matched.get("disk")),
                ssl_thumbprint=thumbprint if isinstance(thumbprint, str) else None,
            )
        )
    return plan


def _normalize_thumbprint(raw: str) -> str:
    """Reduce a vim thumbprint to bare lowercase hex for comparison.

    vCenter formats ``sslThumbprint`` as colon-separated hex
    (``A1:B2:...``); strip the colons and any surrounding whitespace and
    lowercase so the comparison is insensitive to separators and case.
    """
    return raw.replace(":", "").replace(" ", "").strip().lower()


def _cert_thumbprint(cert_der: bytes, expected_hex_len: int) -> str:
    """Hash *cert_der* with the algorithm implied by *expected_hex_len*.

    The lease attestation's length selects the digest (40 hex → SHA-1, the
    vim default; 64 → SHA-256), so a matching cert produces an identical
    bare-hex string. An unrecognised length falls back to SHA-1, whose
    40-char digest cannot equal a differently-sized expected value, so the
    comparison fails closed.
    """
    algorithm = _THUMBPRINT_ALGORITHMS.get(expected_hex_len, "sha1")
    return hashlib.new(algorithm, cert_der).hexdigest()


def _fetch_peer_cert_der(host: str, port: int, timeout: float) -> bytes | None:
    """Return the DER certificate *host:port* presents on a TLS handshake.

    Verification is **off** (``CERT_NONE``) on purpose: ESXi device hosts
    serve self-signed certificates, so the identity check is the thumbprint
    pin, not chain validation. ``check_hostname`` is cleared before
    ``verify_mode`` (assigning ``CERT_NONE`` while hostname checking is on
    raises on CPython). Runs off the event loop via
    :func:`asyncio.to_thread`. Returns ``None`` only if the peer sent no
    certificate (an anonymous cipher), which the caller treats as
    unverifiable.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # NOSONAR(S4830) — thumbprint pinning is the identity check; ESXi device hosts are self-signed (module docstring)  # noqa: E501  # fmt: skip
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # NOSONAR(S4830) — same justification; the pin authenticates the endpoint  # noqa: E501  # fmt: skip
    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        cert = tls.getpeercert(binary_form=True)
    return cert


async def verify_device_thumbprint(url: str, expected: str | None) -> None:
    """Pin the device host's certificate to the lease *expected* thumbprint.

    Fail-**open** on an absent attestation: an empty / ``None`` *expected*
    means vCenter supplied no thumbprint, which the vim spec explicitly
    sanctions ("Empty if no SSL thumbprint is available or needed"), so the
    transfer proceeds under the pooled client's existing TLS trust rather
    than being blocked -- a conformant lease that omits the field must still
    import. Fail-**closed** on a present attestation: open a pre-flight
    handshake to the device host, hash the presented certificate, and raise
    :exc:`DeviceThumbprintError` unless it matches. A handshake that cannot
    obtain a certificate to compare is also a closed failure -- the disk is
    never streamed to an endpoint whose identity the lease attested but that
    could not be confirmed.
    """
    normalized_expected = _normalize_thumbprint(expected) if expected else ""
    if not normalized_expected:
        return
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise DeviceThumbprintError(f"device URL {url!r} has no host to pin a thumbprint against")
    port = parts.port or 443
    try:
        cert_der = await asyncio.to_thread(
            _fetch_peer_cert_der, host, port, _THUMBPRINT_HANDSHAKE_TIMEOUT
        )
    except OSError as exc:
        raise DeviceThumbprintError(
            f"could not complete the TLS handshake to device host {host}:{port} to verify "
            f"its certificate against the lease sslThumbprint: {exc}"
        ) from exc
    if not cert_der:
        raise DeviceThumbprintError(
            f"device host {host}:{port} presented no certificate to pin against the "
            f"lease sslThumbprint"
        )
    presented = _cert_thumbprint(cert_der, len(normalized_expected))
    if presented != normalized_expected:
        raise DeviceThumbprintError(
            f"device host {host}:{port} certificate thumbprint {presented} does not match the "
            f"lease-provided sslThumbprint {normalized_expected}"
        )


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

    Returns the byte count uploaded. Before a single byte flows, the device
    host's certificate is pinned to the lease-provided ``sslThumbprint``
    (#3284): :func:`verify_device_thumbprint` raises
    :exc:`DeviceThumbprintError` on a mismatch, so a tampered or redirected
    device URL is refused pre-stream and the caller aborts the lease. The
    stream is wrapped so the shared :class:`ProgressTracker` advances as
    bytes flow -- that is what the heartbeat reports. Uses the connector's
    pooled client (its TLS trust config) with the ESXi NFC device URL as an
    absolute URL; the lease token embedded in the URL authorizes the PUT.
    ``Content-Length`` is the source file's own size (not the descriptor's
    declared size), sent explicitly so the NFC endpoint gets a sized -- not
    chunked -- body.
    """
    await verify_device_thumbprint(entry.url, entry.ssl_thumbprint)
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
