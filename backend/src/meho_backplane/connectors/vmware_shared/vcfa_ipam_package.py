# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``<ns>.ipam_package.import``: import an IPAM provider package (a zip) from a URL.

An IPAM provider package holds code that the vendor runs at every IP
allocation of the IPAM integrations that use it. So the op is
``destructive``: only a person runs it, after a preview.

MEHO does this work inside its own call. The steps:

1. **download** — ``GET`` the package URL with MEHO's own client: HTTPS only,
   no user info, query or fragment in the URL, the host passes the same
   address screen as a target host, no redirect is followed, and no
   credential is ever sent. The bytes go to a temporary file named
   ``meho-ipam-package-*.zip.part``; MEHO counts the size and the SHA-256.
2. **check** — the file must be a zip. When the call gives
   ``expected_sha256``, it must match. MEHO reads ``registration.yaml`` in
   the zip for the provider name and version: when the vendor already has
   that provider version, MEHO stops with ``unchanged`` (safe to run twice).
3. **upload** — the vendor takes the package over the tus resumable-upload
   protocol (read from the vendor's own web app; no published API file lists
   these calls): ``POST .../packages/import`` with ``Upload-Length`` answers
   ``201`` and a ``Location``; then one ``PATCH`` per chunk of 9 MiB. The
   vendor drops an unfinished upload after a few minutes.
4. **import** — ``POST .../packages/import`` with ``{"bundleId": <upload id>}``.
5. **verify** — read the providers again.

The temporary file is removed in a ``finally`` block, also after an error. If
the MEHO process dies, the next run of this op removes files with the MEHO
name that are older than one hour; the vendor drops its unfinished upload by
itself. The answer never carries the full package URL: only its scheme,
host and path.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import os
import tempfile
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from meho_backplane.connectors._shared.pinned_transport import build_pinned_async_transport
from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    IPAM_PACKAGE_IMPORT_PATH,
    IPAM_PROVIDERS_PATH,
    Calls,
    Steps,
    content_rows,
    vendor_text,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.targets.ssrf_guard import TargetDestinationBlockedError, screen_and_resolve

__all__ = [
    "IMPORT_OP_IDS",
    "TEMP_PREFIX",
    "PackageError",
    "ipam_package_import",
    "ipam_package_import_preview",
]

#: The import op on each connector; both get the preview builder.
IMPORT_OP_IDS: Final[tuple[str, ...]] = (
    "vcfa.tenant.ipam_package.import",
    "vcfa.vra8.ipam_package.import",
)
#: The name start of MEHO's temporary package files.
TEMP_PREFIX: Final[str] = "meho-ipam-package-"
#: The largest package MEHO downloads.
MAX_PACKAGE_BYTES: Final[int] = 512 * 1024 * 1024
#: The size of one upload chunk (the vendor's web app uses 9 MB).
UPLOAD_CHUNK_BYTES: int = 9 * 1024 * 1024
#: A temporary file older than this is a leftover of a run that died.
_STALE_SECONDS: Final[int] = 3600
_TUS: Final[dict[str, str]] = {"Tus-Resumable": "1.0.0"}
_TIMEOUT: Final = httpx.Timeout(60.0, connect=15.0)


class PackageError(RuntimeError):
    """A step of the import stopped. The message names no credential and no full URL."""


def package_url(raw: Any) -> httpx.URL:
    """Check the package URL: HTTPS with a host, no user info, query or fragment."""
    parts = urlsplit(raw if isinstance(raw, str) else "")
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("package_url must be an https:// URL with a host; nothing was sent")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            "package_url must not carry user info, a query or a fragment: MEHO sends no "
            "credential to it. Put the package where it can be read without a login."
        )
    return httpx.URL(raw)


def shown_url(url: httpx.URL) -> str:
    """The URL without anything but scheme, host, port and path."""
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=False,
        transport=build_pinned_async_transport(screen_and_resolve),
    )


def remove_leftovers(folder: str | None = None) -> list[str]:
    """Remove MEHO package files older than one hour (left by a run that died)."""
    removed: list[str] = []
    base = Path(folder or tempfile.gettempdir())
    now = time.time()
    for path in base.glob(f"{TEMP_PREFIX}*"):
        with contextlib.suppress(OSError):
            if now - path.stat().st_mtime > _STALE_SECONDS:
                path.unlink()
                removed.append(path.name)
    return removed


@dataclass
class _Package:
    path: Path
    file_name: str
    size: int = 0
    sha256: str = ""
    provider_name: str | None = None
    provider_version: str | None = None


async def _download(url: httpx.URL, package: _Package) -> None:
    digest = hashlib.sha256()
    try:
        async with _client() as client, client.stream("GET", url) as resp:
            if resp.is_redirect:
                raise PackageError(
                    f"the package URL answered with a redirect (HTTP {resp.status_code}); MEHO "
                    "follows no redirect. Give the final URL."
                )
            if resp.is_error:
                raise PackageError(f"the package URL answered HTTP {resp.status_code}")
            with package.path.open("wb") as out:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    package.size += len(chunk)
                    if package.size > MAX_PACKAGE_BYTES:
                        raise PackageError(f"the package is larger than {MAX_PACKAGE_BYTES} bytes")
                    digest.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
    except TargetDestinationBlockedError as exc:
        raise PackageError(f"the package host is not allowed: {exc}") from None
    except httpx.TransportError as exc:
        raise PackageError(f"the package URL did not answer ({type(exc).__name__})") from None
    package.sha256 = digest.hexdigest()


def _read_registration(package: _Package) -> None:
    """Check the zip; read the provider name and version from ``registration.yaml``."""
    if not zipfile.is_zipfile(package.path):
        raise PackageError("the downloaded file is not a zip file")
    with zipfile.ZipFile(package.path) as archive:
        names = sorted(
            (n for n in archive.namelist() if n.rsplit("/", 1)[-1] == "registration.yaml"),
            key=lambda n: n.count("/"),
        )
        if not names:
            return
        data = yaml.safe_load(io.TextIOWrapper(archive.open(names[0]), encoding="utf-8"))
    if isinstance(data, Mapping):
        package.provider_name = str(data["name"]) if data.get("name") else None
        package.provider_version = str(data["version"]) if data.get("version") else None


def provider_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The flat view of one IPAM provider."""
    return {
        "id": row.get("id") or row.get("providerId"),
        "name": row.get("name") or row.get("providerName"),
        "version": row.get("version") or row.get("providerVersion"),
    }


async def list_providers(calls: Calls) -> list[dict[str, Any]]:
    return [provider_row(row) for row in content_rows(await calls.json("GET", IPAM_PROVIDERS_PATH))]


def _upload_path(location: str) -> str:
    """The path of the upload; refuses a ``Location`` outside the import path."""
    path = urlsplit(location).path
    if not path.startswith(f"{IPAM_PACKAGE_IMPORT_PATH}/") or ".." in path.split("/"):
        raise PackageError("the vendor answered the upload start with an unexpected Location")
    return path


async def _upload(calls: Calls, package: _Package) -> str:
    """Send the file over tus; return the upload id."""
    metadata = ",".join(
        f"{key} {base64.b64encode(value.encode()).decode()}"
        for key, value in (("filename", package.file_name), ("filetype", "application/zip"))
    )
    created = await calls.send(
        "POST",
        IPAM_PACKAGE_IMPORT_PATH,
        headers={**_TUS, "Upload-Length": str(package.size), "Upload-Metadata": metadata},
        content=b"",
    )
    path = _upload_path(created.headers.get("location", ""))
    offset = 0
    with package.path.open("rb") as source:
        while offset < package.size:
            chunk = await asyncio.to_thread(source.read, UPLOAD_CHUNK_BYTES)
            try:
                resp = await calls.send(
                    "PATCH",
                    path,
                    headers={
                        **_TUS,
                        "Upload-Offset": str(offset),
                        "Content-Type": "application/offset+octet-stream",
                    },
                    content=chunk,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {404, 410}:
                    raise PackageError(
                        "the vendor dropped the upload (it keeps an unfinished upload only a "
                        "few minutes). Run the import again."
                    ) from None
                raise
            offset = int(resp.headers.get("upload-offset", offset + len(chunk)))
    return path.rsplit("/", 1)[-1]


def _answer(status: str, guidance: str, steps: Steps, **extra: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {"status": status, "package": None, "provider": None}
    answer.update(extra)
    answer["steps"] = steps.rows()
    answer["guidance"] = guidance
    return answer


async def ipam_package_import(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<ns>.ipam_package.import``; see the module text for the steps."""
    url = package_url(params.get("package_url"))
    steps = Steps("download", "check", "upload", "import", "verify")
    leftovers = await asyncio.to_thread(remove_leftovers)
    fd, raw_path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".zip.part")
    os.close(fd)
    package = _Package(path=Path(raw_path), file_name=url.path.rsplit("/", 1)[-1] or "package.zip")
    try:
        answer = await _run(calls, params, url, package, steps, {"leftovers_removed": leftovers})
    finally:
        with contextlib.suppress(OSError):
            package.path.unlink()
    answer["temp_file_removed"] = not package.path.exists()
    return answer


def _package_view(url: httpx.URL, package: _Package) -> dict[str, Any]:
    return {
        "file_name": package.file_name,
        "size": package.size,
        "sha256": package.sha256 or None,
        "source": shown_url(url),
        "target": IPAM_PACKAGE_IMPORT_PATH,
        "provider_name": package.provider_name,
        "provider_version": package.provider_version,
    }


async def _run(
    calls: Calls,
    params: Mapping[str, Any],
    url: httpx.URL,
    package: _Package,
    steps: Steps,
    extra: dict[str, Any],
) -> dict[str, Any]:
    step = "download"
    try:
        await _download(url, package)
        steps.done(step)
        step = "check"
        _read_registration(package)
        expected = str(params.get("expected_sha256") or "").lower()
        if expected and expected != package.sha256:
            raise PackageError("the SHA-256 of the package does not match expected_sha256")
        before = await list_providers(calls)
        steps.done(step)
        extra["package"] = _package_view(url, package)
        same = [
            p
            for p in before
            if package.provider_name
            and p["name"] == package.provider_name
            and p["version"] == package.provider_version
        ]
        if same:
            return _answer(
                "unchanged",
                "The vendor already has this provider version. Nothing was uploaded.",
                steps,
                provider=same[0],
                **extra,
            )
        step = "upload"
        upload_id = await _upload(calls, package)
        steps.done(step)
        step = "import"
        resp = await calls.send(
            "POST", IPAM_PACKAGE_IMPORT_PATH, headers=_TUS, json={"bundleId": upload_id}
        )
        imported = resp.json() if resp.content else {}
        steps.done(step)
        step = "verify"
        provider = provider_row(imported if isinstance(imported, Mapping) else {})
        after = await list_providers(calls)
        found = next((p for p in after if p["id"] and p["id"] == provider["id"]), None)
        if found:
            steps.done(step)
        else:
            steps.failed(step, "the new provider is not in the provider list")
    except PackageError as exc:
        steps.failed(step, str(exc))
    except httpx.HTTPStatusError as exc:
        steps.failed(step, vendor_text(exc))
    except httpx.TransportError as exc:
        steps.unknown(step, f"the system did not answer ({type(exc).__name__})")
    else:
        return _answer(
            "imported",
            "The provider is imported. Use its id as integration_properties.providerId of an "
            "IPAM integration.",
            steps,
            provider=found or provider,
            **extra,
        )
    extra.setdefault("package", _package_view(url, package))
    return _answer(
        "failed",
        "The import stopped; see the steps. MEHO removed its temporary file; the vendor drops "
        "an unfinished upload by itself. Read the providers, then run the import again.",
        steps,
        **extra,
    )


async def ipam_package_import_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``blast_radius`` of an import: the package and the providers it joins.

    Returns ``None`` without a connector. Raises :exc:`LookupError` when no
    package is at the URL (``404`` / ``410``) or the URL redirects: MEHO then
    refuses to queue the import.
    """
    connector: Any = ctx.connector_instance
    if connector is None or ctx.target is None:
        return None
    url = package_url(ctx.params.get("package_url"))
    async with _client() as client:
        try:
            head = await client.head(url)
        except (httpx.TransportError, TargetDestinationBlockedError) as exc:
            raise LookupError(f"the package URL cannot be read ({type(exc).__name__})") from None
    if head.status_code in {404, 410} or head.is_redirect:
        raise LookupError(f"no package at the URL (HTTP {head.status_code})")
    ns = ctx.descriptor.op_id.rsplit(".ipam_package.import", 1)[0]
    calls = Calls(connector=connector, operator=ctx.operator, target=ctx.target, ns=ns)
    providers = await list_providers(calls)
    return {
        "blast_radius": {
            "object": {
                "kind": "ipam_provider_package",
                "source": shown_url(url),
                "size": head.headers.get("content-length"),
            },
            "children": [{"kind": "ipam_provider", **row} for row in providers],
            "irreversibility": "partly_reversible",
            "note": (
                "the package adds provider code that runs at every IP allocation of the IPAM "
                "integrations that use it; what that code did cannot be undone. MEHO does not "
                "remove an imported provider"
            ),
            "match_count": 1,
        }
    }


for _op_id in IMPORT_OP_IDS:
    register_preview_builder(_op_id, ipam_package_import_preview)
