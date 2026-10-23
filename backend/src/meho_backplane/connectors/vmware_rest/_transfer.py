# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Copy files from a vCenter download to a destination URL over HTTP PUT (#4343).

One helper for the vCenter operations that copy files out of vCenter: the
content library export, the host configuration backup, and the datastore file
export. The operation that calls it owns the vCenter side (it opens the
download session, or makes a temporary library item) and hands over:

* the source files: name, download URL and the expected size when known;
* the destination: a base URL that accepts HTTP PUT, and a Vault reference to
  its token;
* a keep-alive callback (keeps the vCenter download session open during a
  long copy) and a clean-up callback (ends the session, deletes a temporary
  library item);
* optionally a callback that lists and removes MEHO's leftovers from an
  earlier run.

What the helper does, one file at a time:

1. ``HEAD`` the final destination path. ``401`` / ``403`` stops the run (not
   allowed to write). A file that is already there stops the run unless
   ``overwrite`` is set.
2. ``HEAD`` ``<name>.meho-partial``. A partial file from an earlier run is
   deleted. When the destination does not support ``DELETE`` (``405`` /
   ``501``), the new ``PUT`` replaces it. Any other error answer to this check
   or to the delete stops the run before the upload.
3. ``GET`` the vCenter download as a stream and ``PUT`` it to
   ``<name>.meho-partial``. Chunks are at most 8 MiB; nothing holds a whole
   file in memory. The helper counts bytes and computes SHA-256 as the bytes
   pass. It stops when a file or the total passes its limit, or when the byte
   count does not match the expected size.
4. ``MOVE`` (WebDAV rename) the partial file to its final name. When the
   destination does not support ``MOVE`` (``405`` / ``501``), the file keeps
   its temporary name and the result row says ``partial``.

Before it touches the destination, the helper checks every name. A name must
be a safe path, may appear only once, and may not use the ending
``.meho-partial``: MEHO keeps that ending for its temporary files, and a file
called ``disk.meho-partial`` would be removed as a leftover when ``disk`` is
copied after it.

The clean-up callback runs in a ``finally`` block, so it runs on success, on
error and on cancel. It cannot run when the MEHO process dies. What is left
then, and how it goes away:

* the vCenter download session: it expires after its own timeout;
* a temporary library item or session name: it starts with
  :data:`MEHO_NAME_PREFIX` (see :func:`meho_temp_name`), and the next run of
  the same operation lists and removes it through ``remove_leftovers``;
* a ``.meho-partial`` file at the destination: the next run removes or
  replaces it. It never looks like a complete file.

Credentials never cross a redirect. The helper follows no redirect at all: a
``3xx`` from the source or the destination stops the run, and the message says
whether it pointed to another host. The destination token goes only to URLs
built from the destination base URL. The destination host passes the same
address screen as a target host (``targets.ssrf_guard``). No result row, error message or log
event carries a URL with a credential: rows carry the destination path only,
and messages carry the file name and the HTTP status, never the vCenter
download URL. An ``httpx`` transport error is reported by its class name only,
because its text and its request can carry the full URL. ``httpx`` itself logs
one line per request with the URL; the production logging
(:mod:`meho_backplane.logging`) cuts that URL down to its scheme and host.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import structlog

from meho_backplane.connectors._shared.pinned_transport import build_pinned_async_transport
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.targets.ssrf_guard import TargetDestinationBlockedError, screen_and_resolve

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from meho_backplane.auth.operator import Operator

__all__ = [
    "CHUNK_BYTES",
    "MEHO_NAME_PREFIX",
    "PARTIAL_SUFFIX",
    "Destination",
    "FileResult",
    "SourceFile",
    "TransferError",
    "TransferResult",
    "copy_files",
    "is_meho_temp_name",
    "meho_temp_name",
]

_log = structlog.get_logger(__name__)

CHUNK_BYTES = 8 * 1024 * 1024
PARTIAL_SUFFIX = ".meho-partial"
MEHO_NAME_PREFIX = "meho-transfer-"

DEFAULT_MAX_FILE_BYTES = 256 * 1024**3
DEFAULT_MAX_TOTAL_BYTES = 1024**4

# Module-global so tests can shrink it. vCenter ends an idle download session
# after a few minutes; one call a minute keeps it open.
_KEEP_ALIVE_INTERVAL = 60.0

# A large file copy runs for a long time, but a single read or write that
# waits five minutes means the connection is dead.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

# The destination does not support the WebDAV method (MOVE or DELETE).
_METHOD_NOT_SUPPORTED = frozenset({405, 501})
_NO_WRITE_ACCESS = frozenset({401, 403})

FileStatus = Literal["done", "partial", "failed"]


class _TargetLike(Protocol):
    """Shape of the vCenter target this helper reads (name and host, for logs)."""

    name: str
    host: str


@dataclass(frozen=True)
class SourceFile:
    """One file to copy: its name at the destination, its download URL, its size."""

    name: str
    url: str
    size: int | None = None


@dataclass(frozen=True)
class Destination:
    """The destination base URL and where its token lives in Vault.

    ``token_ref`` is a KV-v2 path; ``token_key`` is the field inside it. The
    token is sent as ``Authorization: Bearer <token>``, only to URLs under
    ``base_url``.
    """

    base_url: str
    token_ref: str
    token_key: str = "token"
    token_mount: str = DEFAULT_KV_MOUNT


@dataclass
class FileResult:
    """One result row: what was copied, and where."""

    name: str
    size: int
    sha256: str | None
    destination_path: str
    status: FileStatus


@dataclass
class TransferResult:
    """All result rows, plus the leftovers of an earlier run that were removed."""

    files: list[FileResult]
    leftovers_removed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the result as plain JSON-ready data."""
        return {
            "files": [asdict(row) for row in self.files],
            "leftovers_removed": list(self.leftovers_removed),
        }


class TransferError(RuntimeError):
    """The copy stopped. ``files`` holds the rows up to and including the failed file.

    The message names the file and the reason. It never carries a URL with a
    credential.
    """

    def __init__(self, message: str, *, files: list[FileResult] | None = None) -> None:
        super().__init__(message)
        self.files: list[FileResult] = files or []


def meho_temp_name(label: str) -> str:
    """Return a unique name for a temporary vCenter object MEHO makes.

    The prefix lets the next run find and remove the object when MEHO died
    before its clean-up ran.
    """
    return f"{MEHO_NAME_PREFIX}{label}-{uuid.uuid4().hex[:12]}"


def is_meho_temp_name(name: str) -> bool:
    """Say whether *name* was made by :func:`meho_temp_name`."""
    return name.startswith(MEHO_NAME_PREFIX)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _base_url(raw: str) -> httpx.URL:
    """Check the destination base URL: HTTPS, a host, no user info, no query."""
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise TransferError("the destination base URL must be an https:// URL with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise TransferError(
            "the destination base URL must not carry user info, a query or a fragment; "
            "put the token in Vault and pass its reference"
        )
    path = parts.path if parts.path.endswith("/") else f"{parts.path}/"
    return httpx.URL(urlunsplit((parts.scheme, parts.netloc, path, "", "")))


def _quoted_name(name: str) -> str:
    """Percent-encode each part of *name*; refuse empty, ``.`` and ``..`` parts."""
    segments = name.split("/")
    if any(seg in ("", ".", "..") for seg in segments) or any(ord(ch) < 32 for ch in name):
        raise TransferError(f"file name {name!r} is not a safe destination path")
    return "/".join(quote(seg, safe="") for seg in segments)


def _refuse_redirect(resp: httpx.Response, what: str) -> None:
    """Stop on any ``3xx``. Say whether it pointed to another host."""
    if not resp.is_redirect:
        return
    try:
        other_host = _origin(resp.url.join(resp.headers.get("location", ""))) != _origin(resp.url)
    except httpx.InvalidURL:
        other_host = True
    if other_host:
        raise TransferError(
            f"{what} redirected to another host (HTTP {resp.status_code}); MEHO does not "
            "follow it and sent no credential there"
        )
    raise TransferError(
        f"{what} answered with a redirect (HTTP {resp.status_code}); MEHO does not follow "
        "redirects, give the final URL instead"
    )


def _http_failure(name: str, exc: httpx.HTTPError) -> TransferError:
    # The class name only: the text and ``exc.request`` can carry the full URL.
    return TransferError(f"{name}: the connection failed ({type(exc).__name__})")


class _Dest:
    """The destination side: builds URLs under the base and sends the token."""

    def __init__(self, client: httpx.AsyncClient, base: httpx.URL, token: str) -> None:
        self._client = client
        self._base = base
        self._auth = {"Authorization": f"Bearer {token}"}

    def url(self, name: str) -> httpx.URL:
        return self._base.join(_quoted_name(name))

    async def send(
        self,
        method: str,
        url: httpx.URL,
        *,
        headers: Mapping[str, str] | None = None,
        content: AsyncIterator[bytes] | None = None,
    ) -> httpx.Response:
        if _origin(url) != _origin(self._base):
            raise TransferError("a destination URL left the destination host")
        request = self._client.build_request(
            method, url, headers={**self._auth, **(headers or {})}, content=content
        )
        resp = await self._client.send(request, follow_redirects=False)
        _refuse_redirect(resp, "the destination")
        return resp


async def _prepare_destination(
    dest: _Dest, name: str, *, overwrite: bool, leftovers: list[str]
) -> None:
    """Check write access and that the file is not there; remove an old partial file."""
    final = await dest.send("HEAD", dest.url(name))
    if final.status_code in _NO_WRITE_ACCESS:
        raise TransferError(
            f"{name}: MEHO may not write at the destination (HTTP {final.status_code})"
        )
    if final.is_success and not overwrite:
        raise TransferError(
            f"{name}: the file is already at the destination; set overwrite to replace it"
        )
    if not final.is_success and final.status_code != 404:
        raise TransferError(
            f"{name}: the destination answered HTTP {final.status_code} to the check"
        )
    partial_url = dest.url(name + PARTIAL_SUFFIX)
    partial = await dest.send("HEAD", partial_url)
    if partial.status_code == 404:
        return
    if not partial.is_success:
        raise TransferError(
            f"{name}: the destination answered HTTP {partial.status_code} to the check of "
            "the temporary file"
        )
    removed = await dest.send("DELETE", partial_url)
    if removed.is_success or removed.status_code == 404:
        leftovers.append(partial_url.path)
    elif removed.status_code not in _METHOD_NOT_SUPPORTED:
        # Without DELETE support, the PUT below replaces the file. Any other
        # error means the destination is not ready for the upload.
        raise TransferError(
            f"{name}: the destination could not delete the temporary file "
            f"(HTTP {removed.status_code})"
        )


async def _open_source(
    client: httpx.AsyncClient, src: SourceFile, headers: Mapping[str, str]
) -> httpx.Response:
    # Ask for the raw bytes: with gzip, Content-Length is the compressed size
    # while the copy counts and sends the unpacked bytes.
    request_headers = httpx.Headers(headers)
    request_headers["Accept-Encoding"] = "identity"
    request = client.build_request("GET", src.url, headers=request_headers, timeout=_TIMEOUT)
    resp = await client.send(request, stream=True, follow_redirects=False)
    try:
        _refuse_redirect(resp, f"{src.name}: the vCenter download")
        if not resp.is_success:
            raise TransferError(
                f"{src.name}: the vCenter download answered HTTP {resp.status_code}"
            )
    except TransferError:
        await resp.aclose()
        raise
    return resp


def _expected_size(src: SourceFile, resp: httpx.Response) -> int | None:
    if src.size is not None:
        return src.size
    length = resp.headers.get("content-length", "")
    return int(length) if length.isdigit() else None


async def _copy_one(
    dest: _Dest,
    src: SourceFile,
    row: FileResult,
    *,
    source_client: httpx.AsyncClient,
    source_headers: Mapping[str, str],
    budget: int,
    overwrite: bool,
) -> None:
    """Stream one file to ``<name>.meho-partial``, then rename it; fill in *row*."""
    resp = await _open_source(source_client, src, source_headers)
    try:
        expected = _expected_size(src, resp)
        if expected is not None and expected > budget:
            raise TransferError(f"{src.name}: the file ({expected} bytes) passes the size limit")
        hasher = hashlib.sha256()

        async def body() -> AsyncIterator[bytes]:
            async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                row.size += len(chunk)
                if row.size > budget:
                    raise TransferError(f"{src.name}: the copy passed the size limit")
                if expected is not None and row.size > expected:
                    raise TransferError(
                        f"{src.name}: the source sent more than the expected {expected} bytes"
                    )
                hasher.update(chunk)
                yield chunk
            if expected is not None and row.size != expected:
                raise TransferError(
                    f"{src.name}: the source sent {row.size} bytes, expected {expected}"
                )

        headers = {"Content-Type": "application/octet-stream"}
        if expected is not None:
            headers["Content-Length"] = str(expected)
        partial_url = dest.url(src.name + PARTIAL_SUFFIX)
        row.destination_path = partial_url.path
        put = await dest.send("PUT", partial_url, headers=headers, content=body())
    finally:
        await resp.aclose()
    if not put.is_success:
        raise TransferError(
            f"{src.name}: the destination answered HTTP {put.status_code} {put.reason_phrase}"
        )
    row.sha256 = hasher.hexdigest()
    await _rename(dest, src.name, row, overwrite=overwrite)


async def _rename(dest: _Dest, name: str, row: FileResult, *, overwrite: bool) -> None:
    final_url = dest.url(name)
    moved = await dest.send(
        "MOVE",
        dest.url(name + PARTIAL_SUFFIX),
        headers={"Destination": str(final_url), "Overwrite": "T" if overwrite else "F"},
    )
    if moved.is_success:
        row.destination_path = final_url.path
        row.status = "done"
    elif moved.status_code in _METHOD_NOT_SUPPORTED:
        row.status = "partial"
    else:
        raise TransferError(
            f"{name}: the destination could not rename the file (HTTP {moved.status_code})"
        )


def _check_names(files: Sequence[SourceFile]) -> None:
    """Refuse an unsafe name, a name MEHO keeps for its temporary files, and a repeat.

    ``<name>.meho-partial`` is the temporary name of ``<name>``. A file called
    that would be deleted as a leftover when ``<name>`` is copied after it.
    """
    seen: set[str] = set()
    for src in files:
        _quoted_name(src.name)
        if any(seg.endswith(PARTIAL_SUFFIX) for seg in src.name.split("/")):
            raise TransferError(
                f"file name {src.name!r} uses the ending {PARTIAL_SUFFIX!r}, which MEHO keeps "
                "for its temporary files"
            )
        if src.name in seen:
            raise TransferError(f"file name {src.name!r} is listed twice")
        seen.add(src.name)


def _check_planned_sizes(files: Sequence[SourceFile], max_file: int, max_total: int) -> None:
    for src in files:
        if src.size is not None and src.size > max_file:
            raise TransferError(f"{src.name}: the file ({src.size} bytes) passes the size limit")
    known = sum(src.size for src in files if src.size is not None)
    if known > max_total:
        raise TransferError(f"the files ({known} bytes) pass the total size limit")


async def _read_token(destination: Destination, operator: Operator, target: _TargetLike) -> str:
    ref = _TokenRef(name=target.name, host=target.host, secret_ref=destination.token_ref)
    data = await load_param_secret_data(
        ref, operator, param="destination_token_ref", mount=destination.token_mount
    )
    value = data.get(destination.token_key)
    token = strip_credential_value(value) if isinstance(value, str) else ""
    if not token:
        raise TransferError(
            f"the destination token secret has no value under key {destination.token_key!r}"
        )
    return token


@dataclass
class _TokenRef:
    name: str
    host: str
    secret_ref: str | None


async def _keep_alive_loop(keep_alive: Callable[[], Awaitable[None]]) -> None:
    while True:
        await asyncio.sleep(_KEEP_ALIVE_INTERVAL)
        try:
            await keep_alive()
        except Exception as exc:  # best effort; a dead session fails the copy itself
            _log.warning("vcenter_transfer_keep_alive_failed", error_class=type(exc).__name__)


async def _run_cleanup(cleanup: Callable[[], Awaitable[None]]) -> None:
    try:
        await cleanup()
    except Exception as exc:  # must not hide the copy result or its error
        _log.warning("vcenter_transfer_cleanup_failed", error_class=type(exc).__name__)


async def copy_files(
    files: Sequence[SourceFile],
    destination: Destination,
    *,
    operator: Operator,
    target: _TargetLike,
    source_client: httpx.AsyncClient,
    source_headers: Mapping[str, str],
    keep_alive: Callable[[], Awaitable[None]],
    cleanup: Callable[[], Awaitable[None]],
    remove_leftovers: Callable[[], Awaitable[list[str]]] | None = None,
    overwrite: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> TransferResult:
    """Copy *files* one at a time to *destination*; see the module docstring.

    *source_client* is the connector's pooled client (its vCenter TLS trust);
    *source_headers* carry the vCenter session. The helper builds its own
    destination client with the address screen. Raises :class:`TransferError`
    with the rows so far when the copy stops. *cleanup* always runs.
    """
    rows: list[FileResult] = []
    result = TransferResult(files=rows)
    heartbeat = asyncio.create_task(_keep_alive_loop(keep_alive))
    client = httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=False,
        transport=build_pinned_async_transport(screen_and_resolve),
    )
    try:
        base = _base_url(destination.base_url)
        _check_names(files)
        _check_planned_sizes(files, max_file_bytes, max_total_bytes)
        dest = _Dest(client, base, await _read_token(destination, operator, target))
        if remove_leftovers is not None:
            result.leftovers_removed.extend(await remove_leftovers())
        total = 0
        for src in files:
            row = FileResult(src.name, 0, None, dest.url(src.name).path, "failed")
            rows.append(row)
            budget = min(max_file_bytes, max_total_bytes - total)
            try:
                await _prepare_destination(
                    dest, src.name, overwrite=overwrite, leftovers=result.leftovers_removed
                )
                await _copy_one(
                    dest,
                    src,
                    row,
                    source_client=source_client,
                    source_headers=source_headers,
                    budget=budget,
                    overwrite=overwrite,
                )
            except httpx.HTTPError as exc:
                raise _http_failure(src.name, exc) from None
            except TargetDestinationBlockedError as exc:
                raise TransferError(f"{src.name}: {exc}") from None
            total += row.size
            _log.info(
                "vcenter_transfer_file_copied",
                target=target.name,
                file=row.name,
                size=row.size,
                destination_path=row.destination_path,
                status=row.status,
            )
    except TransferError as exc:
        exc.files = rows
        _log.warning("vcenter_transfer_failed", target=target.name, error=str(exc))
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await client.aclose()
        await _run_cleanup(cleanup)
    return result
