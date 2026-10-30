# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Step 1 of a vCenter install: ``vmware.composite.vcenter.appliance.deploy`` (#4394).

Imports the vCenter appliance file (OVA) from a content library of an
existing vCenter onto a cluster or host, with the size, the disk mode, the
network settings and the host name of the new appliance. ``dangerous`` with
``requires_approval=True``. It runs on the **existing** vCenter target.

It uses the import path of ``vmware.composite.vm.import_from_library``: MEHO
reads the OVF descriptor and the disks from the content library (a download
session) and streams each disk to the ``HttpNfcLease`` of
``ResourcePool.ImportVApp`` (:mod:`..ovf_import`). The handler runs until the
copy and the clean-up are done, so callers use MEHO's async mode
(``async: true``).

What it sends as OVF properties (``OvfManager.CreateImportSpec``
``propertyMapping``): only the settings the appliance file marks as user
configurable, the seven network settings (``guestinfo.cis.appliance.net.*``,
with the host name as ``net.pnid``) and the root password
(``guestinfo.cis.appliance.root.passwd``). vCenter does not pass hidden
settings to the appliance, so the SSO domain and the setup itself are step 2
(``vmware.composite.vcenter.install``). When vCenter warns that it did not use
a sent setting, the answer names it in ``settings_not_used``.

The root password comes from Vault (``root_password_secret_ref``) and is
never returned. It is in the ``CreateImportSpec`` and ``ImportVApp`` bodies
only. A vendor fault text that holds it is replaced. The op is pinned as
``credential_write``, so the flight recorder keeps no request body and the
dispatcher keeps no vendor text of a failed call.

Clean-up: the download session is cancelled in a ``finally`` block, also after
an error. A failed copy aborts the lease, and vCenter then removes the
half-made VM. When the MEHO process dies during the copy, nothing runs the
clean-up: vCenter ends the download session and the lease after their own
idle timeouts (a few minutes without a keep-alive) and removes the half-made
VM with the lease. A finished import whose power-on did not run leaves a
powered-off VM with the given name; a new run with the same name then fails
in ``ImportVApp`` (duplicate name), so delete or rename that VM first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest import library_download
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import (
    _IMPORT_STATUS_TO_ENVELOPE,
    _SUB_OPS_VM_IMPORT_FROM_LIBRARY,
    _VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY,
    _import_vapp_gate,
    _power_on_deployed_vm,
    _power_vm_op_id,
    _refuse_resume_without_state,
    _resolve_deploy_library_item,
    _vsphere_fault_detail,
)
from meho_backplane.connectors.vmware_rest.ovf_import import (
    ImportPlacement,
    LeaseImportResult,
    import_ovf_from_source,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    hide_secrets,
    read_secret_param,
)
from meho_backplane.operations._preview import register_preview_builder
from meho_backplane.operations.composite import composite_resume_state

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "APPLIANCE_DEPLOY_OP_ID",
    "vcenter_appliance_deploy_composite",
    "vcenter_appliance_deploy_preview",
]

APPLIANCE_DEPLOY_OP_ID: Final = "vmware.composite.vcenter.appliance.deploy"

# The same vendor calls as vm.import_from_library: the item find, the download
# session, the power-on, and the vim import and lease calls.
_SUB_OPS_VCENTER_APPLIANCE_DEPLOY: tuple[str, ...] = _SUB_OPS_VM_IMPORT_FROM_LIBRARY
_VIM_SUB_OPS_VCENTER_APPLIANCE_DEPLOY: tuple[str, ...] = _VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY

#: Seconds between two lease progress calls and download-session keep-alives
#: during the copy (module-global so a test can shrink it).
_HEARTBEAT_INTERVAL = 30.0

#: The OVF network name of the vCenter appliance file.
DEFAULT_OVF_NETWORK_NAME: Final = "Network 1"

#: The user-configurable OVF properties of the vCenter appliance file that MEHO sets.
_P: Final = "guestinfo.cis.appliance."
PROP_IP_FAMILY: Final = _P + "net.addr.family"
PROP_MODE: Final = _P + "net.mode"
PROP_ADDRESS: Final = _P + "net.addr"
PROP_PREFIX: Final = _P + "net.prefix"
PROP_GATEWAY: Final = _P + "net.gateway"
PROP_DNS_SERVERS: Final = _P + "net.dns.servers"
PROP_HOST_NAME: Final = _P + "net.pnid"
PROP_ROOT_PASSWORD: Final = _P + "root.passwd"

#: ``storage_size`` -> the suffix of the appliance file's deployment option id.
_STORAGE_SUFFIX: Final[dict[str, str]] = {
    "default": "",
    "large": "-lstorage",
    "xlarge": "-xlstorage",
}


def deployment_option(params: dict[str, Any]) -> str:
    """The OVF deployment option id: the size plus the storage size (``small-lstorage``)."""
    suffix = _STORAGE_SUFFIX.get(str(params.get("storage_size") or "default"), "")
    return f"{params['size']}{suffix}"


def appliance_properties(params: dict[str, Any], root_password: str) -> dict[str, str]:
    """The OVF properties MEHO sends: the network settings, the host name, the root password."""
    network = params["network"]
    props = {
        PROP_IP_FAMILY: str(network.get("ip_family") or "ipv4"),
        PROP_MODE: str(network["mode"]),
        PROP_HOST_NAME: str(params["host_name"]),
    }
    if network["mode"] == "static":
        props[PROP_ADDRESS] = str(network["address"])
        props[PROP_PREFIX] = str(network["prefix"])
        props[PROP_GATEWAY] = str(network["gateway"])
    dns = network.get("dns_servers")
    if isinstance(dns, list) and dns:
        props[PROP_DNS_SERVERS] = ",".join(str(server) for server in dns)
    props[PROP_ROOT_PASSWORD] = root_password
    return props


def _placement(params: dict[str, Any], root_password: str) -> ImportPlacement:
    ovf_network = str(params.get("ovf_network_name") or DEFAULT_OVF_NETWORK_NAME)
    return ImportPlacement(
        resource_pool=str(params["resource_pool"]),
        datastore=str(params["datastore"]),
        entity_name=str(params["name"]),
        host=params.get("host"),
        folder=params.get("folder"),
        network_mappings={ovf_network: str(params["port_group"])},
        ovf_properties=appliance_properties(params, root_password),
        disk_provisioning=str(params.get("disk_mode") or "thin"),
        deployment_option=deployment_option(params),
    )


def _safe_issues(issues: list[dict[str, Any]], secrets: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{**entry, "message": hide_secrets(entry.get("message"), secrets)} for entry in issues]


def _settings_not_used(issues: list[dict[str, Any]], sent: list[str]) -> list[str]:
    """The sent property ids that a vCenter warning names (vCenter did not use them)."""
    texts = [
        str(entry.get("message") or "") for entry in issues if entry.get("severity") == "warning"
    ]
    return [prop for prop in sent if any(prop in text for text in texts)]


def _envelope(
    params: dict[str, Any],
    *,
    status: str,
    secrets: tuple[str, ...],
    item_id: str | None = None,
    result: LeaseImportResult | None = None,
    issues: list[dict[str, Any]] | None = None,
    candidates: list[str] | None = None,
) -> dict[str, Any]:
    sent = sorted(appliance_properties(params, "")) if "network" in params else []
    found = _safe_issues(list(result.issues) if result else list(issues or []), secrets)
    return {
        "status": status,
        "vm_id": result.vm_id if result else None,
        "resource_type": result.resource_type if result else None,
        "library_item_id": item_id,
        "name": params.get("name"),
        "deployment_option": deployment_option(params) if "size" in params else None,
        "powered_on": False,
        "settings_sent": sent,
        "settings_not_used": _settings_not_used(found, sent),
        "issues": found,
        "transfer": list(result.transfer) if result else [],
        "candidates": candidates,
    }


def _resumed_envelope() -> dict[str, Any] | None:
    """The envelope a waiting power-on of this op saved, during its resume."""
    saved = composite_resume_state(APPLIANCE_DEPLOY_OP_ID)
    imported = saved.get("imported") if saved is not None else None
    if not isinstance(imported, dict) or not isinstance(imported.get("vm_id"), str):
        return None
    return imported


async def _power_on(
    connector: VmwareRestConnector, target: Any, operator: Operator, envelope: dict[str, Any]
) -> dict[str, Any]:
    """Start the imported appliance VM, so it boots and waits for step 2."""
    powered_on, power_issue = await _power_on_deployed_vm(
        connector, target, operator, str(envelope["vm_id"]), resume_state={"imported": envelope}
    )
    finished = {**envelope, "powered_on": powered_on}
    if power_issue is not None:
        finished["issues"] = [*envelope["issues"], power_issue]
    return finished


async def _import(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    item_id: str,
    placement: ImportPlacement,
    secrets: tuple[str, ...],
) -> LeaseImportResult | OperationResult:
    """Run the lease import; always cancel the download session afterwards."""
    source = library_download.LibraryDownloadSource(
        connector=connector, target=target, operator=operator, library_item_id=item_id
    )
    try:
        return await import_ovf_from_source(
            connector=connector,
            target=target,
            operator=operator,
            source=source,
            placement=placement,
            gate=_import_vapp_gate(connector, target, operator),
            heartbeat_interval=_HEARTBEAT_INTERVAL,
        )
    except httpx.HTTPError as exc:
        detail = hide_secrets(_vsphere_fault_detail(exc), secrets)
        return LeaseImportResult(
            status="import_error",
            issues=[issue("import", "error", f"the import faulted: {detail}")],
        )
    except RuntimeError as exc:  # LibraryDownloadError and the control-plane checks
        detail = hide_secrets(str(exc), secrets)
        return LeaseImportResult(
            status="import_error",
            issues=[issue("import", "error", f"the import failed: {detail}")],
        )
    finally:
        await source.aclose()


async def vcenter_appliance_deploy_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Import the vCenter appliance file onto a cluster or host and power it on.

    Op-id: ``vmware.composite.vcenter.appliance.deploy``. Reads the root
    password from Vault, resolves the library item, imports it with the
    appliance settings, and (``power_on``, default true) starts the VM. A
    power-on that waits for approval saves the import's answer: approving it
    only starts the VM and does not import again.
    """
    imported = _resumed_envelope()
    refusal = _refuse_resume_without_state(
        composite_op_id=APPLIANCE_DEPLOY_OP_ID,
        write_ops=frozenset({_power_vm_op_id("start")}),
        has_state=imported is not None,
        nothing_done="no appliance was imported",
        remediation="Nothing ran. Power on the appliance VM named in the approval request.",
    )
    if refusal is not None:
        return refusal
    if imported is not None:
        return await _power_on(connector, target, operator, imported)

    secrets = (await read_secret_param(operator, target, params, prefix="root_password"),)
    try:
        item_id, failure = await _resolve_deploy_library_item(
            connector=connector, target=target, operator=operator, params=params
        )
    except httpx.HTTPError as exc:
        message = f"content-library lookup faulted: {_vsphere_fault_detail(exc)}"
        return _envelope(
            params,
            status="resolve_error",
            secrets=secrets,
            issues=[issue("resolve", "error", message)],
        )
    if failure is not None:
        return _envelope(
            params,
            status=str(failure.get("status")),
            secrets=secrets,
            issues=failure.get("issues"),
            candidates=failure.get("candidates"),
        )
    assert item_id is not None  # failure is None => the item resolved

    result = await _import(
        connector,
        target,
        operator,
        item_id=item_id,
        placement=_placement(params, secrets[0]),
        secrets=secrets,
    )
    if isinstance(result, OperationResult):  # the ImportVApp gate parked or denied
        return result
    status = _IMPORT_STATUS_TO_ENVELOPE.get(result.status, "deploy_error")
    envelope = _envelope(params, status=status, secrets=secrets, item_id=item_id, result=result)
    if status == "deployed" and result.vm_id and bool(params.get("power_on", True)):
        return await _power_on(connector, target, operator, envelope)
    return envelope


async def vcenter_appliance_deploy_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver sees: where the appliance goes and its settings, never the password."""
    params = ctx.params
    network = params.get("network")
    if not isinstance(network, dict) or not isinstance(params.get("size"), str):
        return None
    return {
        "action": "import the vCenter appliance file and power on the new appliance",
        "library_item": params.get("library_item") or params.get("library_item_name"),
        "name": params.get("name"),
        "resource_pool": params.get("resource_pool"),
        "datastore": params.get("datastore"),
        "host": params.get("host"),
        "port_group": params.get("port_group"),
        "deployment_option": deployment_option(params),
        "disk_mode": params.get("disk_mode") or "thin",
        "host_name": params.get("host_name"),
        "network": dict(network),
        "root_password_source": "vault",
        "root_password_secret_ref": params.get("root_password_secret_ref"),
        "power_on": bool(params.get("power_on", True)),
    }


register_preview_builder(APPLIANCE_DEPLOY_OP_ID, vcenter_appliance_deploy_preview)
