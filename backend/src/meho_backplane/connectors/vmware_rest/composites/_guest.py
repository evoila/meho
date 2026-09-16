# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing guest-operations channel module
# (#3100 / #3255); one cohesive unit (auth + manager resolution + the six
# guest.* handlers), already over the soft limit on main before this change.

"""Governed guest-operations channel handlers (``vmware.composite.vm.guest.*``, #3100 / #3255).

A governed way to reach *inside* an arbitrary VM's guest OS -- list
processes, read environment variables, inspect guest network state, read
and write files, and run a program -- riding **VMware Tools guest
operations** (the vim ``GuestOperationsManager`` family) over the existing
VI-JSON write seam
(:meth:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector._post_vmomi_json`),
the same substrate the #2890 mutating-vmomi wave used. No ``pyvmomi``, no
SSH, no in-guest agent of MEHO's own.

Design (see ``docs/codebase/connectors-vmware-rest-guest-ops.md``):

* **Guest credentials via ``secret_ref`` only, never in params.** Guest
  OS credentials are read from the target's Vault secret under the
  operator's identity
  (:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  with the ``guest_username`` / ``guest_password`` fields) -- exactly as
  the connector already reads the vCenter service account from the same
  secret. The credential lives only as the ephemeral
  ``NamePasswordAuthentication`` object the vim request body carries; it
  never enters params, a result, an audit row, a preview, or a log line.
* **Structured sub-ops, not freeform shell.** Each op is a discrete
  typed verb. ``program.run`` *does* run an arbitrary program
  (``StartProgramInGuest``, #3255 -- the freeform-exec tier #3100
  deferred, now lifted), but through an explicit ``program_path`` /
  ``arguments`` / ``env`` contract with the same ``dangerous`` /
  ``requires_approval`` governance as the file write, not an open
  ``run(cmd)`` shell endpoint. Its ``arguments`` / ``env`` values may
  carry secrets and are kept off the governed decision surfaces (sub-op
  gate params, park preview, result, audit-row hash, logs) and clamped to
  aggregate-only on broadcast via the ``_CREDENTIAL_WRITE_OPS`` pin; the
  resume-bound ``ApprovalRequest.params`` and flight-recorder spans still
  carry them, so operators must not pass bare secrets there -- the same
  characteristic as ``file.write``'s ``content`` (see the guest-ops doc's
  safety model).
* **Read/write split.** ``process.list`` / ``env.read`` / ``net.show`` /
  ``file.read`` are ``safety_level="safe"`` reads; ``file.write`` and
  ``program.run`` are the ``dangerous`` / ``requires_approval`` writes,
  gated through the same #2254 :func:`enforce_subop_policy` seam the other
  write composites use.

Guest-operations manager MoRefs
-------------------------------

The sub-managers (``GuestProcessManager`` / ``GuestFileManager``) are
reached at their own MoIds. This codebase has no ``RetrieveServiceContent``
path and no verified literal for these singletons, so rather than
hard-code four unverified MoIds, the sub-manager MoRef is resolved
*dynamically* from the top-level ``GuestOperationsManager`` (a single
overridable default MoId, ``"guestOperationsManager"``) via a
``RetrievePropertiesEx`` read of its ``processManager`` / ``fileManager``
properties -- the same property-read seam the cluster-DRS composite uses.
An operator whose deployment names the top manager differently overrides
``guest_ops_manager_moid``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "guest_env_read_composite",
    "guest_file_read_composite",
    "guest_file_write_composite",
    "guest_net_show_composite",
    "guest_process_list_composite",
    "guest_program_run_composite",
]

# --- governance facts (mirror the write-composite substrate, #2254) ---------
_CONNECTOR_ID = "vmware-rest-9.0"
_WRITE_SAFETY_LEVEL = "dangerous"
_WRITE_REQUIRES_APPROVAL = False

# --- Vault secret fields carrying the guest OS credentials ------------------
#: The guest OS credential fields the target's Vault secret must carry,
#: distinct from the ``username`` / ``password`` the connector reads for
#: the vCenter session. Resolved under the operator's identity; never in
#: params.
_GUEST_CRED_FIELDS: tuple[str, str] = ("guest_username", "guest_password")

# --- vim singletons + method paths (canonical METHOD:/path op_ids) ----------
#: PropertyCollector singleton MoId (literal, as in ``_read.py``).
_PROPERTY_COLLECTOR_MOID = "propertyCollector"
#: Default top-level GuestOperationsManager MoId. Overridable per call
#: (``guest_ops_manager_moid``); unverified-live, so the override exists.
_DEFAULT_GUEST_OPS_MANAGER_MOID = "guestOperationsManager"

_OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
_OP_LIST_PROCESSES = "POST:/GuestProcessManager/{moId}/ListProcessesInGuest"
_OP_READ_ENV = "POST:/GuestProcessManager/{moId}/ReadEnvironmentVariableInGuest"
_OP_START_PROGRAM = "POST:/GuestProcessManager/{moId}/StartProgramInGuest"
_OP_FILE_TRANSFER_FROM = "POST:/GuestFileManager/{moId}/InitiateFileTransferFromGuest"
_OP_FILE_TRANSFER_TO = "POST:/GuestFileManager/{moId}/InitiateFileTransferToGuest"

# --- vim type names + property paths ----------------------------------------
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_GUEST_OPS_MANAGER_MO_TYPE = "GuestOperationsManager"
_NAME_PASSWORD_AUTH_TYPE = "NamePasswordAuthentication"
_GUEST_FILE_ATTRIBUTES_TYPE = "GuestFileAttributes"
_GUEST_PROGRAM_SPEC_TYPE = "GuestProgramSpec"
_PROP_PROCESS_MANAGER = "processManager"
_PROP_FILE_MANAGER = "fileManager"
_PROP_GUEST_NET = "guest.net"
_PROP_GUEST_IP_STACK = "guest.ipStack"

#: Cap on the number of processes returned inline before the list is
#: JSONFlux-handled by the dispatcher.
_DEFAULT_MAX_PROCESSES = 200

#: ``file.read`` inline-fetch byte caps. A caller-supplied ``max_inline_bytes``
#: is clamped into ``[1, _FILE_READ_HARD_CAP_BYTES]``; absent, the default
#: budget applies. The schema also enforces these bounds declaratively, so the
#: clamp is defence-in-depth (a direct handler call / a schema drift).
_FILE_READ_DEFAULT_MAX_BYTES = 1024 * 1024  # 1 MiB
_FILE_READ_HARD_CAP_BYTES = 8 * 1024 * 1024  # 8 MiB
#: Cap (bytes) on the transfer-host error body read into a non-2xx refusal
#: message. A non-2xx transfer GET is refused with a *clean* connector error
#: naming the status + a bounded snippet of the body -- never the raw
#: :class:`httpx.HTTPStatusError` (whose ``__str__`` embeds the one-time
#: transfer URL/token) and never an unread streamed response (which would make
#: the dispatcher's downstream ``.text`` / ``.json`` raise
#: :exc:`httpx.ResponseNotRead` and escape the never-raises contract). Read via
#: ``aiter_raw`` so a lying/compressed error body cannot balloon past this cap.
_TRANSFER_ERROR_SNIPPET_BYTES = 4096
#: Chunk width (base64 characters per ``content_lines`` row) for binary content.
#: Wider than RFC 2045's 76-char MIME wrap so a large binary spilling to a
#: result handle produces far fewer rows to page (an 8 MiB file is ~2.7k rows
#: here vs ~147k at width 76) -- the rows are not independently meaningful (the
#: agent concatenates them all before one b64decode), so a wide row is pure
#: paging economy. Held under ~5000 so a default ``result_query`` page
#: (``limit=50``) of a base64 handle stays comfortably inside the 256 KiB
#: ``result_query_max_output_bytes`` budget (50 * 4096 ~= 205 KB + envelope),
#: rather than tripping the recoverable output-too-large / lower-your-limit
#: path on the very first default page. A multiple of 4 keeps each row a whole
#: base64 quantum.
_BASE64_LINE_WIDTH = 4096

#: Default wall-clock ceiling (seconds) for the ``guest.program.run``
#: exit-code poll. Matches VMware Tools' ~5-minute retention of a finished
#: process's exit code (``StartProgramInGuest`` doc: "its exit code and end
#: time will be available for 5 minutes after completion") -- polling past
#: that window risks the exit info ageing out.
_DEFAULT_RUN_TIMEOUT_SECONDS = 300

#: Interval (seconds) between ``ListProcessesInGuest`` polls while waiting
#: for a started program's exit code. Well inside the ~5-minute exit-info
#: window, so a fast-finishing process is still observed with its exit code.
_RUN_POLL_INTERVAL_SECONDS = 2.0


def _unwrap_envelope(payload: Any) -> Any:
    """Strip a legacy ``{"value": X}`` envelope; bare payloads pass through."""
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def _name_password_auth(username: str, password: str) -> dict[str, Any]:
    """Build the ephemeral ``NamePasswordAuthentication`` vim object.

    ``interactiveSession`` is required by the pinned spec and is ``False``
    for programmatic guest ops (no interactive desktop session). This dict
    is the only place the guest credential materialises; it is never
    logged, echoed, or persisted.
    """
    return {
        "_typeName": _NAME_PASSWORD_AUTH_TYPE,
        "interactiveSession": False,
        "username": username,
        "password": password,
    }


async def _guest_auth(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> dict[str, Any]:
    """Resolve the guest OS credential from the target's ``secret_ref``.

    Reads the ``guest_username`` / ``guest_password`` fields off the same
    KV-v2 secret the connector reads the vCenter session credential from,
    under the operator's identity. Raises
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
    when the secret is unset or missing a guest field -- the dispatcher
    wraps it ``connector_error`` for the caller.
    """
    del connector  # credentials resolve off the target, not the session
    creds = await load_basic_credentials(target, operator, fields=_GUEST_CRED_FIELDS)
    return _name_password_auth(creds["guest_username"], creds["guest_password"])


async def _resolve_guest_manager_moid(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    top_moid: str,
    property_name: str,
) -> str:
    """Resolve a guest sub-manager MoId off the top GuestOperationsManager.

    One ``RetrievePropertiesEx`` read of ``property_name``
    (``processManager`` / ``fileManager``) on
    ``GuestOperationsManager(top_moid)``, returning the sub-manager
    MoRef's ``value``. Raises :class:`RuntimeError` when the property is
    absent or not a MoRef (a deployment whose top MoId differs from the
    default -- the caller's ``guest_ops_manager_moid`` override is the fix).
    """
    _method, _, path_template = _OP_RETRIEVE_PROPERTIES.partition(":")
    path = path_template.format(moId=_PROPERTY_COLLECTOR_MOID)
    result = await connector._post_vmomi_json(
        target,
        path,
        operator=operator,
        json=retrieve_properties_body(_GUEST_OPS_MANAGER_MO_TYPE, [top_moid], [property_name]),
    )
    moref = _single_prop(result, property_name)
    moid = moref.get("value") if isinstance(moref, dict) else None
    if not isinstance(moid, str) or not moid:
        raise RuntimeError(
            f"could not resolve GuestOperationsManager.{property_name} off "
            f"{top_moid!r}; pass guest_ops_manager_moid if this deployment names "
            "the guest-operations manager differently"
        )
    return moid


def _single_prop(retrieve_result: Any, name: str) -> Any:
    """Pull one property's ``val`` off a single-object RetrievePropertiesEx result."""
    payload = _unwrap_envelope(retrieve_result)
    objects = payload.get("objects") if isinstance(payload, dict) else None
    if not isinstance(objects, list) or not objects:
        return None
    first = objects[0]
    prop_set = first.get("propSet") if isinstance(first, dict) else None
    for prop in prop_set or []:
        if isinstance(prop, dict) and prop.get("name") == name:
            return unwrap_vim_value(prop.get("val"))
    return None


async def _vm_guest_method(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    manager_moid: str,
    body: dict[str, Any],
) -> Any:
    """POST one vim guest-ops method on the resolved sub-manager MoId."""
    _method, _, path_template = op_id.partition(":")
    path = path_template.format(moId=manager_moid)
    return await connector._post_vmomi_json(target, path, operator=operator, json=body)


# ===========================================================================
# Reads (safe / no approval)
# ===========================================================================


async def guest_process_list_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List processes running in a VM's guest OS via ``ListProcessesInGuest``.

    Op-id: ``vmware.composite.vm.guest.process.list``. Resolves the
    GuestProcessManager MoRef, authenticates with the guest credential
    from ``secret_ref``, and returns the (capped) process list -- the
    exec-status shape (name / pid / owner / cmdLine / startTime / exitCode).
    Set-shaped, so the dispatcher JSONFlux-wraps it over threshold.
    """
    vm_moid = params["vm"]
    top_moid = params.get("guest_ops_manager_moid", _DEFAULT_GUEST_OPS_MANAGER_MOID)
    max_processes = int(params.get("max_processes", _DEFAULT_MAX_PROCESSES))
    auth = await _guest_auth(connector, target, operator)
    manager_moid = await _resolve_guest_manager_moid(
        connector, target, operator, top_moid=top_moid, property_name=_PROP_PROCESS_MANAGER
    )
    raw = await _vm_guest_method(
        connector,
        target,
        operator,
        op_id=_OP_LIST_PROCESSES,
        manager_moid=manager_moid,
        body={"vm": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, vm_moid), "auth": auth},
    )
    processes = _unwrap_envelope(raw)
    if not isinstance(processes, list):
        raise RuntimeError(
            f"guest.process.list: expected list from {_OP_LIST_PROCESSES!r}, "
            f"got {type(processes).__name__}"
        )
    capped = processes[:max_processes]
    return {
        "vm": vm_moid,
        "process_manager_moid": manager_moid,
        "processes": capped,
        "count": len(capped),
        "max_processes_applied": max_processes,
    }


async def guest_env_read_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Read guest environment variables via ``ReadEnvironmentVariableInGuest``.

    Op-id: ``vmware.composite.vm.guest.env.read``. Returns the guest
    environment as a list of ``NAME=value`` strings (the whole environment
    when ``names`` is omitted, else just the requested names). Set-shaped.
    """
    vm_moid = params["vm"]
    top_moid = params.get("guest_ops_manager_moid", _DEFAULT_GUEST_OPS_MANAGER_MOID)
    names = params.get("names")
    auth = await _guest_auth(connector, target, operator)
    manager_moid = await _resolve_guest_manager_moid(
        connector, target, operator, top_moid=top_moid, property_name=_PROP_PROCESS_MANAGER
    )
    body: dict[str, Any] = {"vm": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, vm_moid), "auth": auth}
    if names:
        body["names"] = list(names)
    raw = await _vm_guest_method(
        connector,
        target,
        operator,
        op_id=_OP_READ_ENV,
        manager_moid=manager_moid,
        body=body,
    )
    variables = _unwrap_envelope(raw)
    if not isinstance(variables, list):
        raise RuntimeError(
            f"guest.env.read: expected list from {_OP_READ_ENV!r}, got {type(variables).__name__}"
        )
    return {
        "vm": vm_moid,
        "process_manager_moid": manager_moid,
        "variables": variables,
        "count": len(variables),
    }


async def guest_net_show_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Show VMware Tools-reported guest network state (no guest credentials).

    Op-id: ``vmware.composite.vm.guest.net.show``. Reads the VM's
    ``guest.net`` (``GuestNicInfo`` -- per-NIC IPs / MAC / connected) and
    ``guest.ipStack`` (``GuestStackInfo`` -- routes / DNS / default
    gateways) via ``RetrievePropertiesEx``. This is Tools-*reported* state
    on the VM object, so it needs **no** in-guest authentication and runs
    nothing inside the guest -- it serves the "read ``ip addr`` / routes"
    diagnosis half without a guest login.
    """
    vm_moid = params["vm"]
    result = await connector._post_vmomi_json(
        target,
        _OP_RETRIEVE_PROPERTIES.partition(":")[2].format(moId=_PROPERTY_COLLECTOR_MOID),
        operator=operator,
        json=retrieve_properties_body(
            _VIRTUAL_MACHINE_MO_TYPE, [vm_moid], [_PROP_GUEST_NET, _PROP_GUEST_IP_STACK]
        ),
    )
    nics = _single_prop(result, _PROP_GUEST_NET)
    ip_stacks = _single_prop(result, _PROP_GUEST_IP_STACK)
    return {
        "vm": vm_moid,
        "nics": nics if isinstance(nics, list) else [],
        "ip_stacks": ip_stacks if isinstance(ip_stacks, list) else [],
    }


async def guest_file_read_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Initiate a guest file read via ``InitiateFileTransferFromGuest``.

    Op-id: ``vmware.composite.vm.guest.file.read``. Returns the
    ``FileTransferInformation`` the guest file manager issues -- the file
    ``size``, POSIX/Windows ``attributes``, and a one-time transfer ``url`` --
    for ``guest_path``.

    ``fetch_content`` (default ``False``) selects the shape:

    * **false** -- returns the transfer metadata + one-time ``url`` and
      ``content_fetch="deferred"`` (byte-identical to the read's first
      increment): existence + size + attributes without MEHO proxying bytes.
    * **true** -- MEHO GETs the one-time transfer URL server-side over the
      same pooled TLS client the write path uses (:func:`_get_guest_file_bytes`)
      and returns the bytes as set-shaped ``content_lines`` (utf-8 text lines
      when the whole file decodes cleanly, else wide base64 chunks for binary /
      non-decodable content; flagged by ``content_encoding``). Large content is
      JSONFlux-wrapped into a result handle by the dispatcher (drill in via
      ``result_query``). A file larger than ``max_inline_bytes`` (default 1 MiB,
      hard cap 8 MiB) is **refused** (not truncated) with an error naming the
      size -- before any GET when the guest-reported size is known, and, when it
      is not, by :func:`_get_guest_file_bytes`'s streamed running byte-budget
      (which aborts the read mid-body rather than buffering an unbounded
      payload). The one-time URL and its token are never returned or logged on
      this path (the result omits ``url``).

    Safety tier is ``caution`` (not ``safe``): with ``fetch_content=true`` the
    op returns arbitrary guest file bytes read as the (often privileged)
    in-guest login with no allow-list, so it auto-parks for agent / service
    principals (a human seat still executes it immediately). The tier is
    op-level, so a metadata-only ``fetch_content=false`` read parks for agents
    too.
    """
    vm_moid = params["vm"]
    guest_path = params["guest_path"]
    top_moid = params.get("guest_ops_manager_moid", _DEFAULT_GUEST_OPS_MANAGER_MOID)
    fetch_content = bool(params.get("fetch_content", False))
    max_inline_bytes = _bounded_max_inline_bytes(params.get("max_inline_bytes"))
    auth = await _guest_auth(connector, target, operator)
    manager_moid = await _resolve_guest_manager_moid(
        connector, target, operator, top_moid=top_moid, property_name=_PROP_FILE_MANAGER
    )
    raw = await _vm_guest_method(
        connector,
        target,
        operator,
        op_id=_OP_FILE_TRANSFER_FROM,
        manager_moid=manager_moid,
        body={
            "vm": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, vm_moid),
            "auth": auth,
            "guestFilePath": guest_path,
        },
    )
    info = _unwrap_envelope(raw)
    info = info if isinstance(info, dict) else {}
    size = unwrap_vim_value(info.get("size"))
    size_bytes = size if isinstance(size, int) and not isinstance(size, bool) else None

    if not fetch_content:
        return {
            "vm": vm_moid,
            "file_manager_moid": manager_moid,
            "guest_path": guest_path,
            "url": unwrap_vim_value(info.get("url")),
            "size_bytes": size_bytes,
            "attributes": unwrap_vim_value(info.get("attributes")),
            "content_fetch": "deferred",
        }

    url = unwrap_vim_value(info.get("url"))
    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"guest.file.read: {_OP_FILE_TRANSFER_FROM!r} returned no transfer URL "
            f"for {guest_path!r} on vm {vm_moid!r}"
        )
    # Refuse before egress when the guest-reported size is known and over cap.
    if size_bytes is not None and size_bytes > max_inline_bytes:
        raise _file_too_large(size_bytes, max_inline_bytes)
    content = await _get_guest_file_bytes(connector, target, url=url, max_bytes=max_inline_bytes)
    encoding, content_lines = _shape_guest_file_content(content)
    return {
        "vm": vm_moid,
        "file_manager_moid": manager_moid,
        "guest_path": guest_path,
        "size_bytes": size_bytes,
        "attributes": unwrap_vim_value(info.get("attributes")),
        "content_fetch": "inline",
        "content_encoding": encoding,
        "content_lines": content_lines,
    }


# ===========================================================================
# Write (dangerous / requires approval)
# ===========================================================================


async def guest_file_write_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Write a file into a VM's guest OS via ``InitiateFileTransferToGuest``.

    Op-id: ``vmware.composite.vm.guest.file.write``. The one gated write
    of this increment: ``dangerous`` / ``requires_approval``. Flow:

    1. **Gate first** through the #2254 :func:`enforce_subop_policy` seam
       with the ``InitiateFileTransferToGuest`` governance op_id (the
       ``{moId}`` placeholder key, so a grant matches regardless of the
       resolved manager). A parked / denied gate returns the
       :class:`OperationResult` verbatim and **nothing else runs** -- no
       guest credential is resolved, no transfer URL is minted, no bytes
       are PUT.
    2. On a cleared gate, resolve the guest credential (from
       ``secret_ref``) + the GuestFileManager MoRef, then
       ``InitiateFileTransferToGuest`` mints a one-time PUT URL and the
       ``content`` bytes are PUT to it directly (the vim API's two-step
       design -- bytes never transit the vim channel).

    ``content`` is UTF-8 text (the common config-repair case). The gate's
    ``proposed_effect`` preview echoes path + byte size + overwrite only,
    never the content (:mod:`._write_preview`).
    """
    vm_moid = params["vm"]
    guest_path = params["guest_path"]
    content = params["content"]
    overwrite = bool(params.get("overwrite", False))
    top_moid = params.get("guest_ops_manager_moid", _DEFAULT_GUEST_OPS_MANAGER_MOID)
    content_bytes = content.encode("utf-8")

    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_OP_FILE_TRANSFER_TO,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params={"vm": vm_moid, "guest_path": guest_path, "overwrite": overwrite},
    )
    if gate is not None:
        return gate

    auth = await _guest_auth(connector, target, operator)
    manager_moid = await _resolve_guest_manager_moid(
        connector, target, operator, top_moid=top_moid, property_name=_PROP_FILE_MANAGER
    )
    raw_url = await _vm_guest_method(
        connector,
        target,
        operator,
        op_id=_OP_FILE_TRANSFER_TO,
        manager_moid=manager_moid,
        body={
            "vm": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, vm_moid),
            "auth": auth,
            "guestFilePath": guest_path,
            "fileAttributes": {"_typeName": _GUEST_FILE_ATTRIBUTES_TYPE},
            "fileSize": len(content_bytes),
            "overwrite": overwrite,
        },
    )
    url = unwrap_vim_value(_unwrap_envelope(raw_url))
    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"guest.file.write: {_OP_FILE_TRANSFER_TO!r} returned no transfer URL "
            f"for {guest_path!r} on vm {vm_moid!r}"
        )
    await _put_guest_file_bytes(connector, target, url=url, content=content_bytes)
    return {
        "status": "written",
        "vm": vm_moid,
        "file_manager_moid": manager_moid,
        "guest_path": guest_path,
        "size_bytes": len(content_bytes),
        "overwrite": overwrite,
    }


def _resolve_transfer_url(url: str, target: Any) -> str:
    """Replace a literal ``*`` transfer-URL host with the target's host.

    ``InitiateFileTransferToGuest`` can return a URL whose host is the
    literal ``*`` placeholder meaning "the server you called" (the VMware
    guest-ops convention). Substitute the target's host so the PUT reaches
    a routable endpoint; a concrete host (e.g. an ESXi management address)
    is left untouched.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.hostname != "*":
        return url
    netloc = f"{target.host}:{parts.port}" if parts.port else str(target.host)
    return urlunsplit(parts._replace(netloc=netloc))


async def _put_guest_file_bytes(
    connector: VmwareRestConnector, target: Any, *, url: str, content: bytes
) -> None:
    """PUT ``content`` to a guest-transfer ``url`` on the pooled target client.

    Uses the target's pooled, TLS-configured client
    (:meth:`~meho_backplane.connectors.adapters.http.HttpConnector._http_client`,
    the precedent the unauthenticated version-discovery GET uses) -- the
    transfer ticket rides the URL, so no ``auth_headers`` are attached.
    The target's ``tls_server_name`` SNI extension is deliberately **not**
    forwarded: it names the vCenter cert, whereas a guest-transfer URL may
    resolve to a different host (an ESXi host); a ``verify_tls=false``
    target (the common lab case) is unaffected either way. A non-2xx PUT
    raises for the caller to surface.
    """
    resolved = _resolve_transfer_url(url, target)
    client = await connector._http_client(target)
    response = await client.put(resolved, content=content)
    response.raise_for_status()


def _bounded_max_inline_bytes(raw: Any) -> int:
    """Validate + clamp the inline-fetch byte budget into ``[1, hard cap]``.

    ``None`` selects the default budget. The schema already bounds the param
    declaratively (``minimum:1`` / ``maximum:8388608``); this is defence in
    depth for a direct handler call or a schema drift -- a sub-1 value is a
    caller error (raise), an over-cap value clamps to the hard cap.
    """
    if raw is None:
        return _FILE_READ_DEFAULT_MAX_BYTES
    value = int(raw)
    if value < 1:
        raise ValueError("max_inline_bytes must be >= 1")
    return min(value, _FILE_READ_HARD_CAP_BYTES)


def _file_too_large(size_bytes: int, max_bytes: int) -> RuntimeError:
    """Build the over-cap refusal error -- names the size + cap, never the URL.

    The dispatcher maps a :class:`RuntimeError` to ``connector_error`` with the
    message intact (as the other guest-handler guards do), so the caller sees
    an actionable size + remediation.
    """
    return RuntimeError(
        f"guest.file.read: file is {size_bytes} bytes, over the "
        f"{max_bytes}-byte inline-fetch cap (max_inline_bytes; hard cap "
        f"{_FILE_READ_HARD_CAP_BYTES}). Re-read without fetch_content for the "
        f"transfer handle, or raise max_inline_bytes."
    )


def _transfer_get_failed(status_code: int, snippet: str) -> RuntimeError:
    """Build the non-2xx transfer-GET refusal -- names the status + a body snippet.

    Deliberately a **clean** :class:`RuntimeError` carrying only the HTTP status
    and a bounded body snippet, never the raw :class:`httpx.HTTPStatusError`
    (whose ``__str__`` embeds the one-time transfer URL including its
    ``?token=`` ticket) and never the transfer URL itself. The dispatcher maps
    a :class:`RuntimeError` to ``connector_error`` with the message intact and
    writes the synchronous error-audit row, so the caller sees an actionable
    status without the ticket leaking into the envelope, a log line, or a
    traceback. It is raised outside any ``except`` block, so nothing chains an
    ``HTTPStatusError`` (or its ticket-bearing ``__str__``) as ``__context__``.
    """
    detail = f" ({snippet})" if snippet else ""
    return RuntimeError(
        f"guest.file.read: transfer host returned HTTP {status_code} for the "
        f"one-time file-transfer GET{detail}. The one-time ticket may have "
        f"expired or been consumed; re-read to mint a fresh transfer URL."
    )


def _transfer_content_encoding_refused(content_encoding: str) -> RuntimeError:
    """Build the refusal for a transfer response carrying a transport encoding.

    Guest files come from vCenter / ESXi, which do not transport-compress the
    bytes (the file *is* the payload), so any ``Content-Encoding`` other than
    ``identity`` is unexpected and rejected. httpx auto-decodes on the response
    ``Content-Encoding`` regardless of the request ``Accept-Encoding``, so a
    single ``gzip`` network read could balloon its decoded output far past the
    byte budget before the between-yields running total can trip -- a
    decompression-bomb amplification. Refusing a content-encoded transfer
    response (combined with the ``aiter_raw`` wire-byte budget) keeps
    ``raw == decoded`` so the decoded content the agent receives is bounded by
    ``max_inline_bytes`` and no amplification is possible.
    """
    return RuntimeError(
        f"guest.file.read: refusing a transfer response with "
        f"Content-Encoding: {content_encoding!r}. Guest file transfers carry "
        f"identity-encoded bytes; a transport encoding on this response is "
        f"unexpected and is refused to bound decoded size against a "
        f"decompression bomb."
    )


async def _bounded_error_snippet(response: httpx.Response) -> str:
    """Read at most :data:`_TRANSFER_ERROR_SNIPPET_BYTES` of a non-2xx body.

    Streams the raw wire bytes (``aiter_raw`` -- never a decoded/unbounded
    ``aread``) and stops the instant the cap is reached, so a hostile transfer
    host cannot make the error path buffer an unbounded body. Decoded lossily
    (``errors="replace"``) for a diagnostic snippet; the body may be a vendor
    HTML/JSON error page.
    """
    collected = bytearray()
    async for chunk in response.aiter_raw():
        collected.extend(chunk)
        if len(collected) >= _TRANSFER_ERROR_SNIPPET_BYTES:
            break
    snippet = bytes(collected[:_TRANSFER_ERROR_SNIPPET_BYTES])
    return snippet.decode("utf-8", errors="replace").strip()


async def _get_guest_file_bytes(
    connector: VmwareRestConnector, target: Any, *, url: str, max_bytes: int
) -> bytes:
    """GET the guest-transfer ``url`` server-side on the pooled target client.

    Symmetric to :func:`_put_guest_file_bytes`: resolves the ``*`` placeholder
    host to the target's and uses the target's pooled, TLS-configured client
    (the transfer ticket rides the URL, so no ``auth_headers`` are attached).

    The body is read **streamed with a running byte budget over the raw wire
    bytes** (the shape of the shared transport's
    :func:`~meho_backplane.connectors.adapters.http._read_capped_json_response`),
    so at most ``max_bytes`` (plus one trailing chunk) is ever held in memory:

    * a ``Content-Length`` that already exceeds ``max_bytes`` is
      **fast-rejected before a single body byte is read**, and
    * the streamed running total over :meth:`~httpx.Response.aiter_raw` (the
      *wire* bytes, same units as ``Content-Length``) **aborts the read the
      instant** the accumulated bytes exceed ``max_bytes``.

    The request sends ``Accept-Encoding: identity`` and the response is
    **refused if it carries any transport ``Content-Encoding``** other than
    ``identity``. Guest files come from vCenter / ESXi, which do not
    transport-compress them, so a transport encoding is unexpected -- and httpx
    auto-decodes on the response ``Content-Encoding`` regardless of the request
    ``Accept-Encoding``, so a ``gzip`` bomb could balloon one decoded network
    read past ``max_bytes`` before the between-yields running total could trip.
    Refusing a content-encoded response and counting the budget over
    ``aiter_raw`` keeps ``raw == decoded``, so the decoded content the agent
    receives is bounded by ``max_bytes`` and no amplification is possible.

    This bounds memory even when the transfer host / guest under-reports or
    omits ``FileTransferInformation.size`` (the pre-fetch size guard cannot
    bound egress then) *and* omits or understates ``Content-Length`` (a chunked
    or lying transfer host) -- a non-streaming ``client.get`` would materialise
    the whole body into memory first and could OOM the pod before the cap ever
    fired.

    A non-2xx transfer response (an expired / consumed one-time ticket ->
    404 / 403, a transfer-host 5xx, or a refused cross-origin 3xx) is turned
    into a **clean** connector error (:func:`_transfer_get_failed`) carrying the
    status and a bounded body snippet -- never the raw
    :class:`httpx.HTTPStatusError` with an unread, then-closed streamed response
    (which would make the dispatcher's downstream ``.text`` / ``.json`` raise
    :exc:`httpx.ResponseNotRead` and escape the never-raises contract) and never
    the transfer URL. The URL (and its ``api_key`` / token query) is never
    logged.
    """
    resolved = _resolve_transfer_url(url, target)
    client = await connector._http_client(target)
    async with client.stream(
        "GET", resolved, headers={"Accept-Encoding": "identity"}
    ) as response:
        if not response.is_success:
            # Read a BOUNDED error snippet off the still-unread stream and raise
            # a clean connector error. Do NOT call response.raise_for_status()
            # here: it would raise an httpx.HTTPStatusError carrying an unread,
            # about-to-be-closed streamed body, and the dispatcher's downstream
            # error enrichment (.text / .json) would then raise
            # httpx.ResponseNotRead and escape -- no structured result, no
            # error-audit row.
            raise _transfer_get_failed(
                response.status_code, await _bounded_error_snippet(response)
            )
        # Refuse a transport Content-Encoding (see _transfer_content_encoding_refused):
        # with identity enforced, the aiter_raw wire budget below bounds exactly
        # what the agent receives, immune to a gzip decompression bomb.
        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding and content_encoding != "identity":
            raise _transfer_content_encoding_refused(content_encoding)
        # Fast-reject an honest oversized body on its declared length, before
        # reading a byte. A missing / malformed header falls through to the
        # streaming running-total guard below (the real backstop against an
        # absent or understated length).
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = None
            if length is not None and length > max_bytes:
                raise _file_too_large(length, max_bytes)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_raw():
            total += len(chunk)
            if total > max_bytes:
                # Abort mid-read: the accumulated body already exceeds the cap,
                # so exiting the stream context closes the connection without
                # buffering the rest of the (possibly unbounded) body.
                raise _file_too_large(total, max_bytes)
            chunks.append(chunk)
        return b"".join(chunks)


def _shape_guest_file_content(content: bytes) -> tuple[str, list[str]]:
    """Return ``(encoding, lines)`` -- set-shaped so the reducer can spill.

    UTF-8-decodable bytes decode and split on newline (the trailing empty token
    dropped, exactly as ``k8s.logs`` shapes its ``lines``); ``encoding="utf-8"``.
    That split is line-oriented, not byte-reversible: a trailing newline is
    dropped and a CRLF file keeps a dangling ``\\r`` -- callers needing the exact
    bytes use the base64 path. Non-decodable (binary) bytes base64-encode and
    wrap at :data:`_BASE64_LINE_WIDTH` chars per row; ``encoding="base64"``,
    reassembled byte-exactly as ``base64.b64decode("".join(lines))``. A strict
    ``decode`` cleanly detects binary and routes it to base64 rather than lossily
    rendering a binary file as replacement-char "text".
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        b64 = base64.b64encode(content).decode("ascii")
        lines = [b64[i : i + _BASE64_LINE_WIDTH] for i in range(0, len(b64), _BASE64_LINE_WIDTH)]
        return "base64", lines
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return "utf-8", lines


async def guest_program_run_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Run a program in a VM's guest OS via ``StartProgramInGuest`` (#3255).

    Op-id: ``vmware.composite.vm.guest.program.run``. The freeform in-guest
    program-execution tier #3100 deliberately deferred, now lifted:
    ``dangerous`` / ``requires_approval``, same governance shape as
    ``guest.file.write``. Flow:

    1. **Gate first** through the #2254 :func:`enforce_subop_policy` seam with
       the ``StartProgramInGuest`` governance op_id. The gate params carry
       only ``vm`` / ``program_path`` / ``working_directory`` -- never
       ``arguments`` or ``env``, whose values may embed secrets (a password
       on a command line, a token in an env var) and are redacted from every
       durable surface. A parked / denied gate returns the
       :class:`OperationResult` verbatim and nothing else runs -- no guest
       credential is resolved, no program is started.
    2. On a cleared gate, resolve the guest credential (from ``secret_ref``)
       and the GuestProcessManager MoRef, then ``StartProgramInGuest`` starts
       the program and returns its PID. It is fire-and-forget: **no output is
       captured** (vim's only exec primitive returns a PID, not stdout).
    3. When ``wait`` is true, :func:`_poll_for_exit` polls
       ``ListProcessesInGuest`` (filtered to the PID) every
       :data:`_RUN_POLL_INTERVAL_SECONDS` until the exit code is available or
       ``timeout_seconds`` elapses. VMware Tools keeps a finished process's
       exit code listable for only ~5 minutes after completion (and not
       across a Tools restart), so a process no longer listed before an exit
       code is seen yields ``status='exit_unknown'`` rather than a hang.

    ``arguments`` / ``env`` never enter the result, the gate params, the
    approval preview
    (:func:`~meho_backplane.connectors.vmware_rest.composites._write_preview._guest_program_run_preview`),
    or a log line -- only the PID, exit code, and start/end times surface.
    """
    vm_moid = params["vm"]
    program_path = params["program_path"]
    arguments = params.get("arguments", "")
    working_directory = params.get("working_directory")
    env = params.get("env")
    wait = bool(params.get("wait", False))
    timeout_seconds = int(params.get("timeout_seconds", _DEFAULT_RUN_TIMEOUT_SECONDS))
    top_moid = params.get("guest_ops_manager_moid", _DEFAULT_GUEST_OPS_MANAGER_MOID)

    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_OP_START_PROGRAM,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params=_run_gate_params(vm_moid, program_path, working_directory),
    )
    if gate is not None:
        return gate

    manager_moid, auth, pid = await _start_guest_program(
        connector,
        target,
        operator,
        vm_moid=vm_moid,
        program_path=program_path,
        arguments=arguments,
        working_directory=working_directory,
        env=env,
        top_moid=top_moid,
    )
    result: dict[str, Any] = {
        "status": "started",
        "vm": vm_moid,
        "process_manager_moid": manager_moid,
        "program_path": program_path,
        "pid": pid,
        "exit_code": None,
        "start_time": None,
        "end_time": None,
        "wait": wait,
    }
    if not wait:
        return result

    result.update(
        await _poll_for_exit(
            connector,
            target,
            operator,
            manager_moid=manager_moid,
            vm_moid=vm_moid,
            auth=auth,
            pid=pid,
            timeout_seconds=timeout_seconds,
        )
    )
    return result


async def _start_guest_program(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm_moid: str,
    program_path: str,
    arguments: str,
    working_directory: Any,
    env: Any,
    top_moid: str,
) -> tuple[str, dict[str, Any], int]:
    """Resolve guest creds + the GuestProcessManager, then ``StartProgramInGuest``.

    Returns ``(manager_moid, auth, pid)`` -- the resolved GuestProcessManager
    MoId, the ephemeral guest auth object (reused by the exit-code poll), and
    the started process's PID. Raises :class:`RuntimeError` if the vim call
    returns no usable PID.
    """
    auth = await _guest_auth(connector, target, operator)
    manager_moid = await _resolve_guest_manager_moid(
        connector, target, operator, top_moid=top_moid, property_name=_PROP_PROCESS_MANAGER
    )
    raw_pid = await _vm_guest_method(
        connector,
        target,
        operator,
        op_id=_OP_START_PROGRAM,
        manager_moid=manager_moid,
        body={
            "vm": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, vm_moid),
            "auth": auth,
            "spec": _guest_program_spec(program_path, arguments, working_directory, env),
        },
    )
    pid = _coerce_int(_unwrap_envelope(raw_pid))
    if pid is None:
        raise RuntimeError(
            f"guest.program.run: {_OP_START_PROGRAM!r} returned no PID for "
            f"{program_path!r} on vm {vm_moid!r}"
        )
    return manager_moid, auth, pid


def _run_gate_params(vm_moid: str, program_path: str, working_directory: Any) -> dict[str, Any]:
    """Build the redaction-safe gate/preview params for ``guest.program.run``.

    Carries the program identity + working directory only. ``arguments`` and
    ``env`` are deliberately excluded so their (possibly secret) values never
    reach the sub-op policy gate, the durable approval row, or the audit
    preview -- mirroring ``guest.file.write`` excluding ``content``.
    """
    gate_params: dict[str, Any] = {"vm": vm_moid, "program_path": program_path}
    if working_directory is not None:
        gate_params["working_directory"] = working_directory
    return gate_params


def _guest_program_spec(
    program_path: str, arguments: str, working_directory: Any, env: Any
) -> dict[str, Any]:
    """Build the vim ``GuestProgramSpec`` body for ``StartProgramInGuest``.

    ``programPath`` + ``arguments`` are required by the pinned spec
    (``arguments`` defaults to an empty string). ``envVariables`` is the
    ``NAME=value`` array vim expects; per the vim contract it **replaces**
    the guest's whole environment rather than augmenting it.
    """
    spec: dict[str, Any] = {
        "_typeName": _GUEST_PROGRAM_SPEC_TYPE,
        "programPath": program_path,
        "arguments": arguments,
    }
    if working_directory is not None:
        spec["workingDirectory"] = working_directory
    if env:
        spec["envVariables"] = [f"{name}={value}" for name, value in env.items()]
    return spec


async def _poll_for_exit(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    manager_moid: str,
    vm_moid: str,
    auth: dict[str, Any],
    pid: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Poll ``ListProcessesInGuest`` for ``pid``'s exit code until it exits or times out.

    Returns the completion fields to merge into the result (``status`` +
    ``exit_code`` / ``start_time`` / ``end_time``). Terminal cases:

    * the process reports an ``exitCode`` (an int, including ``0``) ->
      ``'exited'`` with the code + start/end times;
    * ``timeout_seconds`` elapses while the process is still running ->
      ``'timeout'`` (``exit_code`` null);
    * the process is no longer listable before an exit code is seen -- it
      finished and its exit info aged out of VMware Tools' ~5-minute window,
      or Tools restarted -> ``'exit_unknown'`` (``exit_code`` null).

    The loop is bounded by wall-clock ``timeout_seconds``: it always polls at
    least once and never blocks indefinitely on a process that neither exits
    nor disappears.
    """
    deadline = time.monotonic() + timeout_seconds
    body = {
        "vm": vim_moref(_VIRTUAL_MACHINE_MO_TYPE, vm_moid),
        "auth": auth,
        "pids": [pid],
    }
    seen_running = False
    while True:
        raw = await _vm_guest_method(
            connector,
            target,
            operator,
            op_id=_OP_LIST_PROCESSES,
            manager_moid=manager_moid,
            body=body,
        )
        info = _process_info_for_pid(_unwrap_envelope(raw), pid)
        if info is not None:
            exit_code = _coerce_int(info.get("exitCode"))
            if exit_code is not None:
                return {
                    "status": "exited",
                    "exit_code": exit_code,
                    "start_time": _as_str(info.get("startTime")),
                    "end_time": _as_str(info.get("endTime")),
                }
            seen_running = True
        elif seen_running:
            # Was listable, now gone before an exit code surfaced: the exit
            # info aged out of the ~5-minute window (or Tools restarted).
            return _exit_unknown()
        if time.monotonic() >= deadline:
            return {"status": "timeout", "exit_code": None} if seen_running else _exit_unknown()
        await asyncio.sleep(_RUN_POLL_INTERVAL_SECONDS)


def _exit_unknown() -> dict[str, Any]:
    """Completion fields for a process whose exit code could not be determined."""
    return {"status": "exit_unknown", "exit_code": None, "start_time": None, "end_time": None}


def _process_info_for_pid(payload: Any, pid: int) -> dict[str, Any] | None:
    """Return the ``GuestProcessInfo`` entry matching ``pid``, or ``None``."""
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if isinstance(entry, dict) and _coerce_int(entry.get("pid")) == pid:
            return entry
    return None


def _coerce_int(value: Any) -> int | None:
    """Unwrap a vim-boxed value to an ``int`` (PID / exit code), else ``None``.

    ``bool`` is excluded (``isinstance(True, int)`` is truthy in Python) and a
    numeric string is accepted -- VI-JSON may box an ``int64`` as a string.
    """
    unwrapped = unwrap_vim_value(value)
    if isinstance(unwrapped, bool):
        return None
    if isinstance(unwrapped, int):
        return unwrapped
    if isinstance(unwrapped, str) and unwrapped.lstrip("-").isdigit():
        return int(unwrapped)
    return None


def _as_str(value: Any) -> str | None:
    """Unwrap a vim-boxed value to a ``str`` (a timestamp), else ``None``."""
    unwrapped = unwrap_vim_value(value)
    return unwrapped if isinstance(unwrapped, str) else None
