# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.migrate_cross_vcenter`` -- move a VM to another vCenter.

Advanced cross vCenter vMotion: ``VirtualMachine.RelocateVM_Task`` on the
source vCenter, with a ``ServiceLocator`` of the other vCenter in the
relocate spec. The other vCenter is its own MEHO target. MEHO reads its
address and its certificate hash (``extras.ssl_thumbprint``) from that
target and its login from Vault, never from the call.

Checks before the start (nothing is sent when one fails):

1. Every moid has the right shape; the other target exists, is a vCenter,
   is not the call's own target, and has a SHA-1 ``ssl_thumbprint``.
2. The VM exists; a move of it that still runs is returned
   (``already_running``) -- a help, not a lock.
3. The certificate the other vCenter shows matches the thumbprint
   (``certificate_mismatch`` when not).
4. MEHO reads the other vCenter's login from Vault and logs in to read its
   ``instanceUuid`` (``destination_login_failed`` /
   ``destination_unreachable`` when that fails).
5. The host, pool, datastore, folder and networks exist in the other
   vCenter (``not_found`` lists the missing ones).

A vendor job: the answer says ``accepted`` with the vCenter task id, and
``vmware.task.info`` follows it. One vendor call, so there is no partial
result.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    moref_values,
    read_many,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vm_cross_vcenter_schemas import (
    NETWORK_MOID_PATTERN,
    RESOURCE_POOL_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_MOID_PATTERN,
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    unwrap_vim_value,
    vim_moref,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VM_MIGRATE_CROSS_VCENTER_OP_ID",
    "placement",
    "presented_certificate",
    "resolve_destination",
    "vm_migrate_cross_vcenter_composite",
]

_log = structlog.get_logger(__name__)

VM_MIGRATE_CROSS_VCENTER_OP_ID: Final = "vmware.composite.vm.migrate_cross_vcenter"
TASK_INFO_OP_ID: Final = "vmware.task.info"
_OP_RELOCATE_VM_TASK: Final = "POST:/VirtualMachine/{moId}/RelocateVM_Task"
_OP_RETRIEVE_SERVICE_CONTENT: Final = "POST:/ServiceInstance/{moId}/RetrieveServiceContent"
#: vi-json sub-op manifest: the reads, then the one write.
_VIM_SUB_OPS_VM_MIGRATE_CROSS_VCENTER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RETRIEVE_SERVICE_CONTENT,
    _OP_RELOCATE_VM_TASK,
)
_SERVICE_CONTENT_PATH: Final = "/ServiceInstance/ServiceInstance/RetrieveServiceContent"
#: Served without a login on every vCenter; the read only opens a TLS connection.
_SERVICE_VERSIONS_PATH: Final = "/sdk/vimServiceVersions.xml"
#: ``TaskInfo.descriptionId`` of a move (``<Type>.<method>``).
_RELOCATE_DESCRIPTION_ID: Final = "VirtualMachine.relocate"
_RUNNING_TASK_STATES: Final = frozenset({"queued", "running"})
#: The key in the other target's ``extras`` that holds its certificate hash.
THUMBPRINT_EXTRA: Final = "ssl_thumbprint"
_VMWARE_PRODUCT: Final = "vmware"
_VM_PROPS: Final = ["name", "config.hardware.device", "recentTask"]
#: (param, managed object type, moid pattern, relocate spec field).
_PLACEMENT: Final = (
    ("host", "HostSystem", HOST_MOID_PATTERN, "host"),
    ("resource_pool", "ResourcePool", RESOURCE_POOL_MOID_PATTERN, "pool"),
    ("datastore", "Datastore", DATASTORE_MOID_PATTERN, "datastore"),
    ("folder", "Folder", VM_FOLDER_MOID_PATTERN, "folder"),
)
_NIC_BACKING_PREFIX: Final = "VirtualEthernetCard"
_DV_PORT_BACKING_TYPE: Final = "VirtualEthernetCardDistributedVirtualPortBackingInfo"
_STANDARD_NETWORK_BACKING_TYPE: Final = "VirtualEthernetCardNetworkBackingInfo"
_DV_PORT_CONNECTION_TYPE: Final = "DistributedVirtualSwitchPortConnection"
_TARGET_CONTEXT_KEYS: Final = ("target_id", "target_name", "audit_target_id")


class _StopError(Exception):
    """A check failed: answer *status* with *guidance*; nothing is sent."""

    def __init__(
        self,
        status: str,
        guidance: str,
        missing: list[str] | None = None,
        *,
        task: str | None = None,
        vm_name: str | None = None,
    ) -> None:
        super().__init__(guidance)
        self.status = status
        self.guidance = guidance
        self.missing = missing
        self.task = task
        self.vm_name = vm_name


def _answer(
    status: str,
    params: dict[str, Any],
    guidance: str,
    *,
    vm_name: str | None = None,
    task: str | None = None,
    requested: dict[str, Any] | None = None,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "vm": str(params.get("vm")),
        "vm_name": vm_name,
        "destination_target": str(params.get("destination_target")),
        "task": task,
        "status_read": None
        if task is None
        else {"op_id": TASK_INFO_OP_ID, "params": {"task": task}},
        "requested": requested if requested is not None else placement(params),
        "missing": missing or [],
        "guidance": guidance,
    }


def placement(params: dict[str, Any]) -> dict[str, Any]:
    return {name: params[name] for name, *_rest in _PLACEMENT if isinstance(params.get(name), str)}


def _shape_problem(params: dict[str, Any]) -> str | None:
    """The first input of the wrong shape, else ``None``."""
    checks: list[tuple[Any, str, str]] = [(params.get("vm"), VM_MOID_PATTERN, "vm")]
    checks += [(params.get(name), pattern, name) for name, _t, pattern, _f in _PLACEMENT]
    if params.get("network") is not None:
        checks.append((params["network"], NETWORK_MOID_PATTERN, "network"))
    for nic in params.get("nics") or []:
        checks.append((nic.get("network"), NETWORK_MOID_PATTERN, "nics[].network"))
    for value, pattern, label in checks:
        problem = moid_problem(value, pattern, label)
        if problem is not None:
            return problem
    name = params.get("destination_target")
    if not isinstance(name, str) or not name.strip():
        return "destination_target must be the name of the other vCenter's MEHO target"
    return None


def normalize_thumbprint(raw: Any) -> str | None:
    """A SHA-1 certificate hash as 40 lower-case hex digits, else ``None``."""
    if not isinstance(raw, str):
        return None
    digits = raw.replace(":", "").replace(" ", "").strip().lower()
    if len(digits) != 40 or any(c not in "0123456789abcdef" for c in digits):
        return None
    return digits


def _colon_form(digits: str) -> str:
    """``AB:CD:...``, the thumbprint form vCenter shows and takes."""
    return ":".join(digits[i : i + 2] for i in range(0, len(digits), 2)).upper()


async def resolve_destination(operator: Operator, name: str) -> Any:
    """The caller's target named *name*, tenant-scoped; :class:`_StopError` when there is none.

    The target resolver binds the target id and name into the log context
    for the audit row; they are put back so this call's audit row stays on
    the source vCenter.
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import (
        AmbiguousTargetError,
        TargetNotFoundError,
        resolve_target,
    )

    bound = structlog.contextvars.get_contextvars()
    saved = {key: bound[key] for key in _TARGET_CONTEXT_KEYS if key in bound}
    try:
        async with get_sessionmaker()() as session:
            return await resolve_target(session, operator.tenant_id, name.strip())
    except TargetNotFoundError:
        raise _StopError("not_found", f"no target {name!r} in your tenant (list_targets)") from None
    except AmbiguousTargetError:
        raise _StopError(
            "invalid_request", f"{name!r} names more than one target; use the target's name"
        ) from None
    finally:
        structlog.contextvars.unbind_contextvars(*_TARGET_CONTEXT_KEYS)
        structlog.contextvars.bind_contextvars(**saved)


async def presented_certificate(connector: VmwareRestConnector, target: Any) -> bytes:
    """The DER certificate *target* shows on MEHO's own connection to it.

    One GET of the version file, which vCenter serves without a login, on the
    pooled client of the target (its address checks and TLS settings apply).
    The certificate comes from the TLS connection of that answer (the httpx
    ``network_stream`` extension). Transport errors propagate.
    """
    client = await connector._http_client(target)
    extensions = connector._request_extensions(target)
    async with client.stream("GET", _SERVICE_VERSIONS_PATH, extensions=extensions) as response:
        stream = response.extensions.get("network_stream")
        ssl_object = stream.get_extra_info("ssl_object") if stream is not None else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    if not der:
        raise _StopError(
            "destination_unreachable",
            "the other vCenter showed no TLS certificate on MEHO's connection. Nothing was sent.",
        )
    return bytes(der)


def _destination_problem(dest: Any, source: Any, connector: VmwareRestConnector) -> str | None:
    name = getattr(dest, "name", None)
    if getattr(dest, "product", None) != _VMWARE_PRODUCT or connector._target_is_esxi(dest):
        return f"target {name!r} is not a vCenter target"
    same_id = getattr(source, "id", None) is not None and getattr(source, "id", None) == dest.id
    if same_id or getattr(source, "name", None) == name:
        return f"target {name!r} is this call's own vCenter; use vm.migrate inside one vCenter"
    return None


async def _check_certificate(connector: VmwareRestConnector, dest: Any, thumbprint: str) -> None:
    try:
        der = await presented_certificate(connector, dest)
    except httpx.HTTPError as exc:
        raise _StopError(
            "destination_unreachable",
            f"MEHO could not reach target {dest.name!r} to check its certificate "
            f"({type(exc).__name__}). Nothing was sent.",
        ) from exc
    if hashlib.new("sha1", der, usedforsecurity=True).hexdigest() != thumbprint:
        raise _StopError(
            "certificate_mismatch",
            f"target {dest.name!r} shows a certificate whose SHA-1 hash is not the "
            f"{THUMBPRINT_EXTRA} of the target. Check the certificate and fix the target's "
            f"{THUMBPRINT_EXTRA}. Nothing was sent.",
        )


async def _login(
    connector: VmwareRestConnector, dest: Any, operator: Operator
) -> tuple[dict[str, str], str]:
    """``(login, instanceUuid)`` of the other vCenter: a Vault read, then a logged-in read."""
    try:
        creds = await connector._session_loader(dest, operator)
        raw = await connector._post_vmomi_json(
            dest, _SERVICE_CONTENT_PATH, operator=operator, json={}
        )
    except httpx.TransportError as exc:
        raise _StopError(
            "destination_unreachable",
            f"MEHO could not reach target {dest.name!r} ({type(exc).__name__}). Nothing was sent.",
        ) from exc
    except Exception as exc:
        raise _StopError(
            "destination_login_failed",
            f"MEHO could not read the Vault login of target {dest.name!r} or log in with it "
            f"({type(exc).__name__}). Check the target's secret_ref. Nothing was sent.",
        ) from exc
    content = unwrap_vim_value(raw)
    about = content.get("about") if isinstance(content, dict) else None
    uuid = about.get("instanceUuid") if isinstance(about, dict) else None
    username, password = creds.get("username"), creds.get("password")
    if not isinstance(uuid, str) or not uuid:
        raise _StopError("destination_login_failed", f"target {dest.name!r} gave no instanceUuid")
    if not isinstance(username, str) or not isinstance(password, str):
        raise _StopError(
            "destination_login_failed", f"the Vault login of target {dest.name!r} is incomplete"
        )
    return {"username": username, "password": password}, uuid


def _nic_devices(devices: Any) -> dict[int, dict[str, Any]]:
    """``{device key: device}`` of the VM's network cards."""
    out: dict[int, dict[str, Any]] = {}
    for device in devices if isinstance(devices, list) else []:
        backing = device.get("backing") if isinstance(device, dict) else None
        backing_type = backing.get(VIM_TYPE_NAME_KEY) if isinstance(backing, dict) else None
        key = device.get("key") if isinstance(device, dict) else None
        if isinstance(key, int) and str(backing_type or "").startswith(_NIC_BACKING_PREFIX):
            out[key] = device
    return out


def _nic_networks(params: dict[str, Any], nics: dict[int, Any]) -> dict[int, str]:
    """``{nic key: destination network moid}`` for every network card of the VM."""
    override = {int(n["nic_key"]): str(n["network"]) for n in params.get("nics") or []}
    unknown = sorted(set(override) - set(nics))
    if unknown:
        raise _StopError(
            "invalid_request",
            f"the VM has no network card with key {unknown}; vmware.vm.hardware.show lists "
            "the keys. Nothing was sent.",
        )
    default = params.get("network")
    without = sorted(key for key in nics if key not in override and not isinstance(default, str))
    if without:
        raise _StopError(
            "invalid_request",
            f"network card(s) {without} need a network in the other vCenter: pass network "
            "or nics. Nothing was sent.",
        )
    return {key: override.get(key, str(default)) for key in nics}


async def _network_backing(
    connector: VmwareRestConnector, dest: Any, operator: Operator, network: str
) -> dict[str, Any] | None:
    """The NIC backing of *network* in the other vCenter; ``None`` when it does not exist."""
    if network.startswith("dvportgroup-"):
        props = await read_object(
            connector,
            dest,
            operator,
            mo_type="DistributedVirtualPortgroup",
            moid=network,
            props=["key", "config.distributedVirtualSwitch"],
        )
        if props is None:
            return None
        switch = moref_values([props.get("config.distributedVirtualSwitch")])
        key = props.get("key")
        switch_props = (
            await read_object(
                connector, dest, operator, mo_type=switch[0][0], moid=switch[0][1], props=["uuid"]
            )
            if switch
            else None
        )
        uuid = (switch_props or {}).get("uuid")
        if not isinstance(key, str) or not isinstance(uuid, str):
            raise RuntimeError(f"port group {network!r} has no key or switch uuid")
        port = {
            VIM_TYPE_NAME_KEY: _DV_PORT_CONNECTION_TYPE,
            "switchUuid": uuid,
            "portgroupKey": key,
        }
        return {VIM_TYPE_NAME_KEY: _DV_PORT_BACKING_TYPE, "port": port}
    props = await read_object(
        connector, dest, operator, mo_type="Network", moid=network, props=["name"]
    )
    if props is None:
        return None
    return {
        VIM_TYPE_NAME_KEY: _STANDARD_NETWORK_BACKING_TYPE,
        "deviceName": str(props.get("name") or ""),
        "network": vim_moref("Network", network),
    }


async def _destination_changes(
    connector: VmwareRestConnector,
    dest: Any,
    operator: Operator,
    params: dict[str, Any],
    nic_networks: dict[int, str],
    nics: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check every target object in the other vCenter; the NIC edits of the move."""
    missing: list[str] = []
    for name, mo_type, _pattern, _field in _PLACEMENT:
        found = await read_object(
            connector, dest, operator, mo_type=mo_type, moid=params[name], props=["name"]
        )
        if found is None:
            missing.append(f"{name} {params[name]}")
    backings: dict[str, dict[str, Any]] = {}
    for network in sorted(set(nic_networks.values())):
        backing = await _network_backing(connector, dest, operator, network)
        if backing is None:
            missing.append(f"network {network}")
        else:
            backings[network] = backing
    if missing:
        raise _StopError(
            "not_found",
            f"target {dest.name!r} has no {', '.join(missing)}. Read the moids in the other "
            "vCenter. Nothing was sent.",
            missing,
        )
    return [
        {
            VIM_TYPE_NAME_KEY: "VirtualDeviceConfigSpec",
            "operation": "edit",
            "device": {**nics[key], "backing": backings[network]},
        }
        for key, network in sorted(nic_networks.items())
    ]


async def _running_move(
    connector: VmwareRestConnector, target: Any, operator: Operator, recent: Any
) -> str | None:
    """The id of a move of the VM that still runs, else ``None`` (a help, not a lock)."""
    task_ids = [moid for kind, moid in moref_values(recent) if kind in ("", "Task")]
    if not task_ids:
        return None
    infos, _complete = await read_many(
        connector, target, operator, mo_type="Task", moids=task_ids, props=["info"]
    )
    for task_id in task_ids:
        info = infos.get(task_id, {}).get("info")
        if (
            isinstance(info, dict)
            and info.get("descriptionId") == _RELOCATE_DESCRIPTION_ID
            and info.get("state") in _RUNNING_TASK_STATES
        ):
            return task_id
    return None


def _service_locator(
    dest: Any, uuid: str, login: dict[str, str], thumbprint: str
) -> dict[str, Any]:
    host = getattr(dest, "fqdn", None) or dest.host
    port = getattr(dest, "port", None)
    url = f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"
    return {
        VIM_TYPE_NAME_KEY: "ServiceLocator",
        # vCenter takes the upper-case form; AboutInfo gives it in lower case.
        "instanceUuid": uuid.upper(),
        "url": url,
        "credential": {VIM_TYPE_NAME_KEY: "ServiceLocatorNamePassword", **login},
        "sslThumbprint": _colon_form(thumbprint),
    }


async def _prepare(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """The checks before the start: ``(RelocateVM_Task body, requested, VM name)``.

    Raises :class:`_StopError` with the answer of a failed check, also for a move
    of the VM that still runs (``already_running`` with its task id).
    """
    problem = _shape_problem(params)
    if problem is not None:
        raise _StopError("invalid_request", f"{problem}. Nothing was sent.")
    dest = await resolve_destination(operator, params["destination_target"])
    problem = _destination_problem(dest, target, connector)
    if problem is not None:
        raise _StopError("invalid_request", f"{problem}. Nothing was sent.")
    thumbprint = normalize_thumbprint((getattr(dest, "extras", None) or {}).get(THUMBPRINT_EXTRA))
    if thumbprint is None:
        raise _StopError(
            "invalid_request",
            f"target {dest.name!r} needs {THUMBPRINT_EXTRA} in its extras: the SHA-1 hash of "
            "its certificate (40 hex digits, colons allowed). Nothing was sent.",
        )
    vm = params["vm"]
    props = await read_object(
        connector, target, operator, mo_type="VirtualMachine", moid=vm, props=_VM_PROPS
    )
    if props is None:
        raise _StopError("not_found", f"no VM {vm!r}; vmware.vm.info finds the moid from the name")
    vm_name = props.get("name") if isinstance(props.get("name"), str) else None
    nics = _nic_devices(props.get("config.hardware.device"))
    nic_networks = _nic_networks(params, nics)
    requested = {**placement(params), "networks": {str(k): v for k, v in nic_networks.items()}}
    running = await _running_move(connector, target, operator, props.get("recentTask"))
    if running is not None:
        raise _StopError(
            "already_running",
            f"a move of VM {vm!r} ({running}) still runs. Nothing new was started. Follow it "
            "with vmware.task.info.",
            task=running,
            vm_name=vm_name,
        )
    await _check_certificate(connector, dest, thumbprint)
    login, uuid = await _login(connector, dest, operator)
    device_change = await _destination_changes(
        connector, dest, operator, params, nic_networks, nics
    )
    spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: "VirtualMachineRelocateSpec",
        "service": _service_locator(dest, uuid, login, thumbprint),
    }
    for name, mo_type, _pattern, field in _PLACEMENT:
        spec[field] = vim_moref(mo_type, params[name])
    if device_change:
        spec["deviceChange"] = device_change
    body: dict[str, Any] = {"spec": spec}
    if isinstance(params.get("priority"), str):
        body["priority"] = params["priority"]
    return body, requested, vm_name


async def vm_migrate_cross_vcenter_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Move one VM to another vCenter (``vmware.composite.vm.migrate_cross_vcenter``).

    Runs the checks of the module docstring, then sends ``RelocateVM_Task``
    through the governed vim seam (a parked or denied gate is returned as
    is). The gate sees the placement only, never the login. Answers
    ``accepted`` with the task id and the status read; no waiting.
    """
    try:
        body, requested, vm_name = await _prepare(connector, target, operator, params)
    except _StopError as stop:
        return _answer(
            stop.status,
            params,
            stop.guidance,
            vm_name=stop.vm_name,
            task=stop.task,
            missing=stop.missing,
        )
    vm = params["vm"]
    gate, payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RELOCATE_VM_TASK,
        vmomi_path=f"/VirtualMachine/{vm}/RelocateVM_Task",
        body=body,
        params={"vm": vm, "destination_target": params["destination_target"], **requested},
    )
    if gate is not None:
        return gate
    refs = moref_values([_unwrap_value(payload)])
    if not refs:
        raise RuntimeError("RelocateVM_Task returned no task reference")
    task = refs[0][1]
    _log.info("vm_migrate_cross_vcenter_accepted", vm=vm, task=task)
    guidance = (
        f"vCenter accepted the move task {task}; the move is not done yet. Read "
        "vmware.task.info on this target with this task id until it ends, then find the VM "
        "in the other vCenter. Do not start again."
    )
    return _answer("accepted", params, guidance, vm_name=vm_name, task=task, requested=requested)
