# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed guest-operations channel handlers (``vmware.composite.vm.guest.*``, #3100).

A governed way to reach *inside* an arbitrary VM's guest OS -- list
processes, read environment variables, inspect guest network state, read
and write files -- riding **VMware Tools guest operations** (the vim
``GuestOperationsManager`` family) over the existing VI-JSON write seam
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
  typed verb. There is no ``run(cmd)`` -- freeform in-guest program
  execution (``StartProgramInGuest``) is a deliberately deferred tier.
* **Read/write split.** ``process.list`` / ``env.read`` / ``net.show`` /
  ``file.read`` are ``safety_level="safe"`` reads; ``file.write`` is the
  single ``dangerous`` / ``requires_approval`` write, gated through the
  same #2254 :func:`enforce_subop_policy` seam the other write composites
  use.

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

from typing import TYPE_CHECKING, Any

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
_OP_FILE_TRANSFER_FROM = "POST:/GuestFileManager/{moId}/InitiateFileTransferFromGuest"
_OP_FILE_TRANSFER_TO = "POST:/GuestFileManager/{moId}/InitiateFileTransferToGuest"

# --- vim type names + property paths ----------------------------------------
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_GUEST_OPS_MANAGER_MO_TYPE = "GuestOperationsManager"
_NAME_PASSWORD_AUTH_TYPE = "NamePasswordAuthentication"
_GUEST_FILE_ATTRIBUTES_TYPE = "GuestFileAttributes"
_PROP_PROCESS_MANAGER = "processManager"
_PROP_FILE_MANAGER = "fileManager"
_PROP_GUEST_NET = "guest.net"
_PROP_GUEST_IP_STACK = "guest.ipStack"

#: Cap on the number of processes returned inline before the list is
#: JSONFlux-handled by the dispatcher.
_DEFAULT_MAX_PROCESSES = 200


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
    ``size``, POSIX ``attributes``, and a one-time transfer ``url`` -- for
    ``guest_path``. Inline content retrieval (a raw GET of ``url``) is a
    deliberate follow-up (see the design note): the first increment returns
    the transfer handle so the file's existence, size, and attributes are
    known without MEHO proxying the bytes.
    """
    vm_moid = params["vm"]
    guest_path = params["guest_path"]
    top_moid = params.get("guest_ops_manager_moid", _DEFAULT_GUEST_OPS_MANAGER_MOID)
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
    return {
        "vm": vm_moid,
        "file_manager_moid": manager_moid,
        "guest_path": guest_path,
        "url": unwrap_vim_value(info.get("url")),
        "size_bytes": size if isinstance(size, int) and not isinstance(size, bool) else None,
        "attributes": unwrap_vim_value(info.get("attributes")),
        "content_fetch": "deferred",
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
