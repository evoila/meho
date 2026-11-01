# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``srm.storage_adapter.install`` -- install a storage replication adapter (SRA).

An SRA is the storage vendor's plug-in that lets SRM drive array-based
replication. It comes as an archive (``.tar.gz``) and runs as a container
on the appliance. MEHO does the whole install inside its own call:

1. ``list_before`` -- the adapters on the appliance. The vendor answers
   ``GET /storage-replication-adapters`` with a task; MEHO reads the task
   until it ends.
2. ``download`` -- MEHO fetches the archive into a temporary folder (name
   prefix :data:`TEMP_PREFIX`, file ending ``.meho-partial`` until the
   check passed). No redirect is followed, so no credential can travel to
   another host; the address passes the same screen as a target address.
3. ``check_sha256`` -- the archive's SHA-256 must equal ``sha256``; else
   nothing is uploaded.
4. ``upload`` -- ``POST /storage-replication-adapters`` with the archive
   as ``multipart/form-data`` (form field ``file``).
5. ``list_after`` -- the adapters again; the new one is the answer.

While the call runs, MEHO reads ``GET /session`` of the setup API once a
minute, so the vendor session does not expire during a long download.
The temporary folder is removed in a ``finally`` block: after success,
after an error and on cancel. When the MEHO process dies, the folder
stays; the next run removes such folders when they are older than
:data:`LEFTOVER_AGE_SECONDS`, and the vendor session ends by its own
timeout. A broken upload leaves nothing on the appliance that MEHO knows
of: the answer reads the list again and says whether an adapter was
installed.

The archive URL comes in one of two ways. ``archive_url`` is a plain
URL with no query part. A signed URL keeps its token in the query part,
and the parameters of a call are shown in the public preview, the
approval and the broadcast; so a signed URL is read from Vault instead:
``archive_url_secret_ref`` names a secret whose field ``url`` holds it,
read under the operator's identity. The answer, the preview and the logs
carry the URL without its query part.

The operation is ``destructive``: only a person runs it, after
``preview_operation``. The preview builder reads the appliance and its
adapters and checks that the archive URL answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog

from meho_backplane.connectors._shared.pinned_transport import build_pinned_async_transport
from meho_backplane.connectors.srm.appliance import setup_api_for
from meho_backplane.connectors.srm.setup_api import (
    SetupApi,
    SetupApiError,
    Steps,
    parse_ref,
    wait_for_task,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.targets.ssrf_guard import TargetDestinationBlockedError, screen_and_resolve

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike

__all__ = [
    "LEFTOVER_AGE_SECONDS",
    "OP_ID",
    "TEMP_PREFIX",
    "storage_adapter_install",
    "storage_adapter_install_preview",
]

_log = structlog.get_logger(__name__)

OP_ID: Final = "srm.storage_adapter.install"
TEMP_PREFIX: Final = "meho-srm-sra-"
PARTIAL_SUFFIX: Final = ".meho-partial"
LEFTOVER_AGE_SECONDS: Final = 6 * 3600
MAX_ARCHIVE_BYTES: Final = 4 * 1024**3
_CHUNK: Final = 1024 * 1024
_SHA256: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_NAME: Final = re.compile(r"[^A-Za-z0-9._-]")
_STEPS: Final[tuple[str, ...]] = ("list_before", "download", "check_sha256", "upload", "list_after")

#: Module globals so a test can shrink them.
KEEP_ALIVE_SECONDS: float = 60.0
LIST_TIMEOUT_SECONDS: float = 300.0
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)
_UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=1800.0, pool=10.0)

_ADAPTER_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "vendor",
    "version",
    "image_id",
    "repo_tags",
    "error",
)


class _ArchiveError(RuntimeError):
    """The archive could not be fetched or checked. The message has no URL query."""


def _archive_url(raw: Any, *, allow_query: bool) -> tuple[httpx.URL, str]:
    """Check an archive URL; return it and the form that answers and logs show.

    The messages never carry the value: the URL may be a signed one.
    """
    if not isinstance(raw, str):
        raise ValueError("must be an http or https URL")
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http or https URL with a host")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("must not carry a user name, password or fragment")
    if parts.query and not allow_query:
        raise ValueError(
            "must have no query part: a signed URL keeps its token there, and the parameters "
            "are shown in the preview, the approval and the broadcast; put the full URL in "
            "a Vault secret (field url) and give archive_url_secret_ref instead"
        )
    shown = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return httpx.URL(raw.strip()), shown


async def _resolve_archive(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> tuple[httpx.URL, str]:
    """The archive URL from ``archive_url`` or from Vault, and the form answers show."""
    direct = params.get("archive_url")
    ref = params.get("archive_url_secret_ref")
    if (direct is None) == (ref is None):
        raise ValueError("give exactly one of archive_url and archive_url_secret_ref")
    if ref is None:
        try:
            return _archive_url(direct, allow_query=False)
        except ValueError as exc:
            raise ValueError(f"archive_url {exc}") from None
    data = await connector.setup_secret_loader(
        target, operator, "archive_url_secret_ref", parse_ref(params, "archive_url_secret_ref")
    )
    try:
        return _archive_url(data.get("url"), allow_query=True)
    except ValueError as exc:
        raise ValueError(f"archive_url_secret_ref: the secret's field url {exc}") from None


def _expected_sha256(params: dict[str, Any]) -> str:
    raw = params.get("sha256")
    if not isinstance(raw, str) or not _SHA256.match(raw.strip()):
        raise ValueError("sha256 must be the archive's SHA-256: 64 hex digits")
    return raw.strip().lower()


def _archive_name(url: httpx.URL) -> str:
    base = _SAFE_NAME.sub("_", Path(url.path).name)
    return base if base and base not in (".", "..") else "adapter.tar.gz"


def _adapter_rows(task: dict[str, Any]) -> list[dict[str, Any]]:
    result = task.get("result")
    rows = result.get("list") if isinstance(result, dict) else result
    return [
        {key: row.get(key) for key in _ADAPTER_FIELDS}
        for row in rows or []
        if isinstance(row, dict)
    ]


async def list_adapters(api: SetupApi) -> list[dict[str, Any]]:
    """The adapters on the appliance (a vendor task, read until it ends)."""
    task = await api.call("GET", "/storage-replication-adapters")
    return _adapter_rows(await wait_for_task(api, task, timeout_seconds=LIST_TIMEOUT_SECONDS))


def _new_adapters(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen = {(row.get("id"), row.get("image_id")) for row in before}
    return [row for row in after if (row.get("id"), row.get("image_id")) not in seen]


def remove_leftovers(now: float | None = None) -> list[str]:
    """Remove temporary folders of earlier runs older than :data:`LEFTOVER_AGE_SECONDS`."""
    removed: list[str] = []
    cutoff = (now if now is not None else time.time()) - LEFTOVER_AGE_SECONDS
    root = Path(tempfile.gettempdir())
    for path in root.glob(f"{TEMP_PREFIX}*"):
        with contextlib.suppress(OSError):
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed.append(path.name)
    return removed


def _archive_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_DOWNLOAD_TIMEOUT,
        follow_redirects=False,
        transport=build_pinned_async_transport(screen_and_resolve),
    )


def _refuse_redirect(resp: httpx.Response) -> None:
    if not resp.is_redirect:
        return
    location = resp.headers.get("location", "")
    try:
        other = resp.url.join(location).host != resp.url.host
    except httpx.InvalidURL:
        other = True
    where = "to another host" if other else "on the same host"
    raise _ArchiveError(
        f"the archive URL answered with a redirect {where} (HTTP {resp.status_code}); "
        "MEHO follows no redirect, give the final URL"
    )


async def _download(url: httpx.URL, dest: Path) -> tuple[int, str]:
    """Stream the archive to *dest*; return its size and SHA-256."""
    hasher = hashlib.sha256()
    size = 0
    try:
        async with _archive_client() as client:
            request = client.build_request("GET", url, headers={"Accept-Encoding": "identity"})
            resp = await client.send(request, stream=True)
            try:
                _refuse_redirect(resp)
                if not resp.is_success:
                    raise _ArchiveError(f"the archive URL answered HTTP {resp.status_code}")
                with dest.open("wb") as handle:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise _ArchiveError("the archive passes the size limit of 4 GiB")
                        hasher.update(chunk)
                        handle.write(chunk)
            finally:
                await resp.aclose()
    except httpx.HTTPError as exc:
        # The class name only: the text can carry the full URL.
        raise _ArchiveError(f"the archive download failed ({type(exc).__name__})") from None
    except TargetDestinationBlockedError as exc:
        raise _ArchiveError(f"the archive address is not allowed: {exc}") from None
    return size, hasher.hexdigest()


async def _keep_alive(api: SetupApi) -> None:
    while True:
        await asyncio.sleep(KEEP_ALIVE_SECONDS)
        try:
            await api.call("GET", "/session")
        except Exception as exc:  # best effort; a dead session fails the upload itself
            _log.warning("srm_sra_keep_alive_failed", error_class=type(exc).__name__)


async def _upload(api: SetupApi, path: Path, name: str) -> None:
    with path.open("rb") as handle:
        await api.send(
            "POST",
            "/storage-replication-adapters",
            files={"file": (name, handle, "application/gzip")},
            timeout=_UPLOAD_TIMEOUT,
        )


async def _list_after_failure(api: SetupApi, before: list[dict[str, Any]] | None) -> bool | None:
    """After a failed upload: was an adapter installed anyway? ``None`` when unknown."""
    if before is None:
        return False
    try:
        return bool(_new_adapters(before, await list_adapters(api)))
    except SetupApiError:
        return None


async def _run(
    api: SetupApi, steps: Steps, url: httpx.URL, workdir: Path, answer: dict[str, Any]
) -> None:
    archive = answer["archive"]
    steps.start("list_before")
    before = await list_adapters(api)
    answer["adapters_before"] = before
    steps.done("list_before", count=len(before))
    steps.start("download")
    partial = workdir / f"{archive['name']}{PARTIAL_SUFFIX}"
    archive["size"], archive["sha256"] = await _download(url, partial)
    steps.done("download", size=archive["size"])
    steps.start("check_sha256")
    if archive["sha256"] != archive["expected_sha256"]:
        raise _ArchiveError(
            f"the archive has SHA-256 {archive['sha256']}, not the expected "
            f"{archive['expected_sha256']}; nothing was uploaded"
        )
    final = partial.with_name(archive["name"])
    partial.rename(final)
    steps.done("check_sha256")
    steps.start("upload")
    try:
        await _upload(api, final, archive["name"])
    except SetupApiError:
        answer["installed"] = await _list_after_failure(api, before)
        raise
    steps.done("upload")
    steps.start("list_after")
    answer["installed"] = None  # uploaded; unknown until the list is read
    after = await list_adapters(api)
    new = _new_adapters(before, after)
    steps.done("list_after", count=len(after))
    answer.update(
        outcome="installed",
        installed=bool(new),
        adapter=new[0] if new else None,
        adapters_after=after,
    )
    if not new:
        answer["note"] = "the appliance took the upload, but its list shows no new adapter"


async def storage_adapter_install(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.storage_adapter.install``; see the module docstring."""
    url, shown = await _resolve_archive(connector, operator, target, params)
    expected = _expected_sha256(params)
    api = setup_api_for(connector, operator, target, params)
    steps = Steps(_STEPS)
    answer: dict[str, Any] = {
        "outcome": "failed",
        "installed": False,
        "adapter": None,
        "archive": {
            "name": _archive_name(url),
            "source": shown,
            "size": None,
            "sha256": None,
            "expected_sha256": expected,
        },
        "adapters_before": None,
        "adapters_after": None,
    }
    leftovers = await asyncio.to_thread(remove_leftovers)
    workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=TEMP_PREFIX))
    heartbeat = asyncio.create_task(_keep_alive(api))
    try:
        await _run(api, steps, url, workdir, answer)
    except (SetupApiError, _ArchiveError, OSError) as exc:
        steps.fail(str(exc))
        _log.warning("srm_sra_install_failed", target=target.name, source=shown, error=str(exc))
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await asyncio.to_thread(shutil.rmtree, workdir, True)
    answer["steps"] = steps.as_list()
    answer["cleanup"] = {
        "temporary_folder_removed": not os.path.exists(workdir),
        "leftovers_removed": leftovers,
    }
    return answer


async def _archive_check(url: httpx.URL) -> dict[str, Any]:
    """``HEAD`` the archive URL. Refuse when it is gone; note other answers."""
    try:
        async with _archive_client() as client:
            resp = await client.head(url)
    except httpx.HTTPError as exc:
        raise ValueError(f"the archive URL does not answer ({type(exc).__name__})") from None
    except TargetDestinationBlockedError as exc:
        raise ValueError(f"the archive address is not allowed: {exc}") from None
    if resp.status_code in (404, 410):
        raise ValueError(f"the archive URL answers HTTP {resp.status_code}: there is no archive")
    try:
        _refuse_redirect(resp)
    except _ArchiveError as exc:
        raise ValueError(str(exc)) from None
    length = resp.headers.get("content-length", "")
    return {
        "http_status": resp.status_code,
        "size": int(length) if length.isdigit() else None,
    }


async def storage_adapter_install_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder: the appliance, its adapters and the archive, read before the park.

    Raises (and so refuses the park) when the appliance's setup API cannot
    be read or the archive URL answers 404 or 410.
    """
    from meho_backplane.connectors.srm.connector import SrmConnector

    connector = ctx.connector_instance
    if not isinstance(connector, SrmConnector):
        raise ValueError("the preview needs the srm connector")
    url, shown = await _resolve_archive(connector, ctx.operator, ctx.target, ctx.params)
    expected = _expected_sha256(ctx.params)
    api = setup_api_for(connector, ctx.operator, ctx.target, ctx.params)
    info = await api.call("GET", "/appliance")
    adapters = await list_adapters(api)
    archive = await _archive_check(url)
    return {
        "blast_radius": {
            "object": {
                "kind": "srm_appliance",
                "hostname": info.get("hostname"),
                "appliance_version": info.get("appliance_version"),
                "archive": {"source": shown, "sha256": expected, "size": archive["size"]},
            },
            "children": [{"kind": "storage_replication_adapter", **row} for row in adapters],
            "irreversibility": (
                "The adapter runs as a container on the appliance. MEHO has no operation "
                "that removes it; removing an adapter stops the array-based replication "
                "it drives and needs a person in the appliance's own interface. The "
                "adapters listed under children are already on the appliance."
            ),
        },
        "archive_check": archive,
    }


register_preview_builder(OP_ID, storage_adapter_install_preview)
