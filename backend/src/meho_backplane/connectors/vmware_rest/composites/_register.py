# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``register_vmware_composite_operations`` -- registrar for the vmware-rest composites.

Module-level async function called from the lifespan-driven
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
after the registrar list is populated by the
``meho_backplane.connectors.vmware_rest.composites`` package's
``__init__`` (which appends this function via
:func:`register_typed_op_registrar`).

Where the rows come from
------------------------

Each composite keeps its registration rows in its own module, as
``COMPOSITES``: a tuple of
:class:`~meho_backplane.connectors.vmware_rest.composites._spec.CompositeSpec`.
:func:`collect_composites` imports every module of this package at
start-up, collects the rows and sorts them by op id. So a new composite
is a new module; no shared list in this file changes. A row also carries
its preview builder (registered here, at import) and, when the composite
is not on the vmware-rest-8.0 catalog, the reason why
(:data:`COMPOSITES_NOT_ON_8_0` is derived from the rows).

The per-group "when to use" texts stay hand-written here: they are text
for the model, one per group, not one per composite.

Mixed safety posture
--------------------

Read composites pass ``safety_level="safe"`` + ``requires_approval=False``
-- overrides of the ``dangerous`` / ``True`` defaults of
:func:`register_composite_operation`. A read can still be ``caution``: for
example ``vm.guest.file.read`` (#3720) can fetch any guest file as the
in-guest login, so it waits for approval for agent / service principals
while a human runs it at once. Write composites are all
``requires_approval=True``, at the level ``dangerous``, ``caution`` (a
change that is easy to undo) or ``destructive`` (a delete; see
``docs/decisions/governed-delete-operations.md``).
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from types import ModuleType

from meho_backplane.connectors.vmware_rest._content_library_schemas import (
    CONTENT_LIBRARY_GROUP_KEY,
    CONTENT_LIBRARY_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.alarm_state import (
    ALARMS_GROUP_KEY,
    ALARMS_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    register_vmware_governed_subops,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory import (
    register_migration_inventory_dispatch_surface,
)
from meho_backplane.connectors.vmware_rest.composites._migration_inventory_schemas import (
    MIGRATION_GROUP_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.host_access import (
    HOST_ACCESS_GROUP_KEY,
    HOST_ACCESS_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.host_security import (
    HOST_SECURITY_GROUP_KEY,
    HOST_SECURITY_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.license_common import (
    LICENSE_GROUP_KEY,
    LICENSE_WHEN_TO_USE,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    INSTALL_GROUP_KEY,
    INSTALL_WHEN_TO_USE,
)
from meho_backplane.operations._preview import register_preview_builder
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "COMPOSITES_NOT_ON_8_0",
    "collect_composites",
    "register_vmware_composite_operations",
    "register_vmware_composite_operations_8_0",
]


# Natural-key shorthand. Every composite registers against
# ``(product="vmware", version="9.0", impl_id="vmware-rest")`` -- the
# same triple :class:`VmwareRestConnector` advertises -- so the
# dispatcher's ``connector_id="vmware-rest-9.0"`` lookup resolves
# every composite alongside the ingested ops.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"

#: The second versioned catalog of the same implementation
#: (``connector_id="vmware-rest-8.0"``, served by ``VmwareRest80Connector``).
#: The resolver sends a fingerprinted 8.0.x target there, so the composites
#: are registered there too (#3789). The handlers already pick the right
#: path per vCenter release (VI-JSON for writes REST cannot do, the 8.0.x
#: arms in ``_write.py``), so the same handler serves both catalogs.
_VERSION_8_0 = "8.0"


#: Curated agent-actionable group selectors for the vmware-rest
#: composite surface (T4b #732). Surfaced verbatim by
#: ``list_operation_groups`` so the LLM client picks the right group
#: before drilling into ``search_operations``. Each text names the
#: group's main reads and its main changes. The same groups also hold
#: the operations ingested from the vSphere API files, and those have
#: their own safety levels. So a text names a safety level only next to a
#: named operation, never for the whole group (#4120;
#: ``tests/test_operation_group_texts.py`` checks it).
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "cluster": (
        "Use for clusters, DRS and resource pools. Read: a cluster's DRS "
        "settings and the moves DRS recommends now "
        "(cluster.drs_recommendations, safe). Changes: "
        "update every host of a cluster in turn (cluster.patch, dangerous); add a "
        "DRS rule that keeps VMs together or apart (cluster.drs_rule.create, "
        "dangerous) or runs a VM group on a host group "
        "(cluster.drs_vm_host_rule.create, caution); delete a DRS rule "
        "(cluster.drs_rule.delete, destructive); create a resource pool "
        "(resource_pool.create, caution) or delete one (resource_pool.delete, "
        "dangerous). Pair with 'host' for one host's maintenance and with 'vm' "
        "to move VMs."
    ),
    "events": (
        "Use for vCenter events: what changed, and when. Reads: the latest "
        "events, filtered by time, event type or one object (event.tail, safe); "
        "for a long history, open an event collector "
        "(POST:/EventManager/{moId}/CreateCollectorForEvents), page through it "
        "(POST:/EventHistoryCollector/{moId}/ReadNextEvents) and destroy it "
        "when done. Changes: write a note of your own into the event log, for "
        "example the start of a maintenance window "
        "(POST:/EventManager/{moId}/LogUserEvent). The right group for "
        "incident triage when you do not know yet which object to look at. "
        "Pair with 'vm' or 'host' once an event names an object."
    ),
    "performance": (
        "Use for the performance counters of one VM, host or cluster. Reads: "
        "the available counters and their recent samples in one call "
        "(performance.summary, safe); the samples of chosen counters "
        "(POST:/PerformanceManager/{moId}/QueryPerf) and the counter "
        "definitions (POST:/PerformanceManager/{moId}/QueryPerfCounter). "
        "Changes: set how long vCenter keeps statistics and how often it "
        "samples (POST:/PerformanceManager/{moId}/UpdatePerfInterval), or how "
        "much detail it collects per counter "
        "(POST:/PerformanceManager/{moId}/UpdateCounterLevelMapping). These two "
        "change the whole vCenter, not one object, and more detail costs "
        "database space. The right group for 'is this VM busy?' or 'how did "
        "CPU look in the last hour?'. Pair with 'vm' or 'host' to get the "
        "object id first."
    ),
    "storage": (
        "Use for datastores, the files on them, and VM storage policies. Reads: "
        "list datastores and read one (GET:/api/vcenter/datastore); capacity, "
        "free space and the VMs on each datastore (datastore.usage, safe); the "
        "files in one datastore folder (datastore.file.list, safe); the folders "
        "of a vSAN datastore with their size and the VMs that use them, or the "
        "files of one such folder (datastore.vsan.folder.list, safe); the "
        "top-level folders of every datastore that no registered VM uses "
        "(datastore.orphan_dirs, safe); make ESXi "
        "read a datastore's capacity and free space again (datastore.refresh, "
        "safe); list storage policies (storage_policy.list, safe). Changes: "
        "create a datastore folder (datastore.dir.create, caution); delete a file "
        "or folder, refused for files of registered VMs (datastore.file.delete, "
        "destructive); create a storage policy (storage_policy.create, caution) "
        "or delete one (storage_policy.delete, destructive). Pair with 'vm' when "
        "the next step acts on one VM."
    ),
    "networking": (
        "Use for networks, distributed switches and port groups. Reads: list "
        "networks (GET:/api/vcenter/network), and distributed port groups with "
        "their switch, VLAN and connected VMs (network.portgroup.audit, safe). "
        "Changes: create a distributed port group with a VLAN trunk or one "
        "access VLAN (network.portgroup.create, dangerous); change its VLAN "
        "(network.portgroup.vlan.set, dangerous) or its security policy "
        "(network.portgroup.security.set, dangerous); delete a distributed port "
        "group (network.portgroup.delete, destructive) or a port group on one "
        "host's standard switch (host.standard_portgroup.delete, destructive). "
        "Both deletes are refused while something uses the port group. Pair "
        "with 'vm' for one VM's NICs, and with 'host' before "
        "host.detach_from_vds."
    ),
    "vm": (
        "Use for virtual machines. Reads: list VMs (GET:/api/vcenter/vm), read "
        "one VM (GET:/api/vcenter/vm/{vm}), and a VM's CPU and memory limits "
        "(vm.resource_allocation.show, safe). To list a VM's snapshots, use "
        "vmware.snapshot.list; for a list of all VMs, vmware.vm.list. Changes: "
        "power one VM or many (vm.power, "
        "vm.power.bulk, dangerous); create, clone or deploy a VM (vm.create, "
        "vm.clone, vm.clone_from_template, vm.deploy_from_library, "
        "vm.import_from_library, dangerous); change CPU, memory, disks, NICs or "
        "CD drives (vm.resize, vm.disk.grow, vm.disk.attach, vm.nic.repoint, "
        "vm.cdrom.add, vm.device.cdrom, dangerous); set CPU and memory limits "
        "(vm.resource_allocation.set, caution); change boot settings and boot "
        "order (vm.boot.set, dangerous); upgrade the hardware version or VMware "
        "Tools (vm.hardware.upgrade, vm.tools.upgrade, dangerous); set or remove "
        "advanced settings (vm.advanced_settings.set, dangerous); unmount the "
        "VMware Tools installer (vm.tools.installer.unmount, caution); add or remove a PCI "
        "passthrough device (vm.pci_device.add, vm.pci_device.remove, dangerous); "
        "take a console screenshot (vm.screenshot, caution) or press special keys "
        "on the console (vm.keys.send, dangerous); move a VM to another "
        "host (vm.migrate, dangerous); take a snapshot (vm.snapshot.create, "
        "caution), revert to one (vm.snapshot.revert, dangerous) or delete one "
        "(vm.snapshot.delete, destructive) or all of them (vm.snapshot.delete_all, "
        "destructive); consolidate a VM's disks (vm.disks.consolidate, caution); "
        "delete a powered-off VM and its "
        "disks (vm.destroy, destructive); create a VM folder (folder.create, "
        "dangerous) or delete an empty one (folder.delete, destructive). Pair "
        "with 'storage', 'networking' or 'cluster' for the reads a create or a "
        "move needs."
    ),
    "host": (
        "Use for ESXi hosts. Reads: list hosts (GET:/api/vcenter/host). Changes: "
        "move every running VM off a host, then put it into maintenance mode "
        "(host.evacuate, dangerous); take a host out of maintenance mode "
        "(host.maintenance.exit, dangerous); move a host's VM NICs to a "
        "standard network, then take the host off a distributed switch "
        "(host.detach_from_vds, dangerous); mount an NFS export as a datastore "
        "(host.datastore_mount_nfs, dangerous); mark disks as flash or HDD "
        "(host.disk_mark_flash, dangerous); start, stop or restart an allowed "
        "host service (host.service_control, dangerous); set the NTP servers "
        "(host.ntp.set, dangerous), the DNS servers, search domains and host "
        "name (host.dns.set, dangerous) or the remote syslog targets "
        "(host.syslog.set, dangerous). To read a host's NTP, DNS and syslog "
        "settings, use vmware.host.config.show. Choose the host by name or "
        "moref. Pair with 'networking' before host.detach_from_vds, and with "
        "'cluster' or 'vm' for the reads before a change."
    ),
    "guest": (
        "Use for guest OS customization: how a cloned VM gets its host name and "
        "IP address on first boot. Changes: save a named customization spec "
        "with host name, IP per NIC, gateway and DNS, or a Windows sysprep spec "
        "(guest.customization_spec.create, dangerous); apply a saved spec to a "
        "powered-off VM, and power it on if asked (vm.customize, dangerous). "
        "Use it after a 'vm' clone or create. A spec can hold admin passwords; "
        "they never reach a reviewer."
    ),
    "guest_ops": (
        "Use to work inside a running VM's guest OS through VMware Tools, "
        "instead of 'govc guest.run'. Reads: list the guest's processes "
        "(vm.guest.process.list, safe); read its environment variables "
        "(vm.guest.env.read, safe); show its network settings, without a guest "
        "login (vm.guest.net.show, safe); read a file's size and attributes, or "
        "the file itself if asked (vm.guest.file.read, caution). Changes: write "
        "a file into the guest (vm.guest.file.write, dangerous); run a program "
        "in the guest (vm.guest.program.run, dangerous). The guest login comes "
        "from the target's Vault secret (guest_username, guest_password), never "
        "from a parameter. Needs VMware Tools in the guest."
    ),
    "namespace_management": (
        "Use for the vSphere Supervisor (Kubernetes on a cluster) and its "
        "vSphere Namespaces. Reads: the Supervisor state of a cluster "
        "(supervisor.status, safe) and the state of a namespace "
        "(namespace.status, safe), both with a ready flag to poll. Changes: turn "
        "on the Supervisor for one cluster (supervisor.enable, dangerous) or "
        "remove it (supervisor.disable, dangerous); both run in the background, "
        "so poll supervisor.status. Create a namespace with a storage policy, a "
        "Kubernetes release library and VM classes (namespace.create, caution); "
        "delete a namespace and everything in it (namespace.delete, "
        "destructive). Pair with 'storage' for the storage policy id, "
        "'content_library' for the library id, and 'networking' for the network "
        "settings."
    ),
    # Shared with the typed content-library reads and finds: the same key and text.
    CONTENT_LIBRARY_GROUP_KEY: CONTENT_LIBRARY_WHEN_TO_USE,
    # Shared with the two typed licence reads: the same key and text.
    LICENSE_GROUP_KEY: LICENSE_WHEN_TO_USE,
    # #4383: shared with the typed read vmware.alarm.triggered.list, which
    # registers under the same group key with the same text.
    ALARMS_GROUP_KEY: ALARMS_WHEN_TO_USE,
    "migration": MIGRATION_GROUP_WHEN_TO_USE,
    # Shared with the typed reads of host advanced settings and firewall.
    HOST_SECURITY_GROUP_KEY: HOST_SECURITY_WHEN_TO_USE,
    # #4394: shared with the typed read vmware.vcenter.deployment.status.
    INSTALL_GROUP_KEY: INSTALL_WHEN_TO_USE,
    # Shared with the two typed host-access reads: the same key and text.
    HOST_ACCESS_GROUP_KEY: HOST_ACCESS_WHEN_TO_USE,
}


def collect_composites(package: ModuleType) -> tuple[CompositeSpec, ...]:
    """Collect ``COMPOSITES`` from every module of *package*, sorted by op id.

    Imports each module of the package by name (``pkgutil.iter_modules``,
    the way the connector registry finds the connector packages). A module
    without ``COMPOSITES`` adds nothing. A row that is not a
    :class:`CompositeSpec`, or an op id that two rows share, raises here,
    so a wrong module fails at start-up and not at dispatch.
    """
    this_module = __name__.rpartition(".")[2]
    rows: list[tuple[str, CompositeSpec]] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if info.name == this_module:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for row in getattr(module, "COMPOSITES", ()):
            if not isinstance(row, CompositeSpec):
                raise TypeError(
                    f"{module.__name__}.COMPOSITES holds a {type(row).__name__}, "
                    "not a CompositeSpec"
                )
            rows.append((module.__name__, row))
    first_module: dict[str, str] = {}
    for module_name, row in rows:
        other = first_module.setdefault(row.op_id, module_name)
        if other != module_name:
            raise ValueError(f"composite {row.op_id!r} is in {other} and in {module_name}")
    return tuple(sorted((row for _, row in rows), key=lambda row: row.op_id))


def _register_preview_builders(specs: tuple[CompositeSpec, ...]) -> None:
    """Wire each row's park-time preview builder onto the per-op hook."""
    for spec in specs:
        if spec.preview is not None:
            register_preview_builder(spec.op_id, spec.preview)


#: Every vmware-rest composite, collected from the modules of this package.
_COMPOSITES: tuple[CompositeSpec, ...] = collect_composites(
    sys.modules[__name__.rpartition(".")[0]]
)
_register_preview_builders(_COMPOSITES)

#: Composites NOT registered on the 8.0 catalog, each with the reason; read
#: from the rows. ``tests/test_connectors_vmware_rest_catalog_parity.py``
#: checks that every composite on the 9.0 catalog is either on the 8.0
#: catalog or listed here, so a new composite cannot silently skip the 8.0
#: catalog.
COMPOSITES_NOT_ON_8_0: dict[str, str] = {
    spec.op_id: spec.not_on_8_0 for spec in _COMPOSITES if spec.not_on_8_0 is not None
}


async def register_vmware_composite_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every vmware-rest composite into ``endpoint_descriptor``.

    Idempotent: a second invocation against unchanged descriptions is a
    no-op for the embedding pipeline (the body-hash skip path in
    :func:`_register_in_session`). The runner
    (:func:`run_typed_op_registrars`) calls every registered registrar
    on every lifespan startup; the skip-re-embed branch keeps that
    cheap.

    Each composite's ``safety_level`` + ``requires_approval`` come from
    its :class:`CompositeSpec` row: reads pass ``"safe"`` / ``False``;
    writes pass ``"dangerous"`` / ``True`` (T4's defaults).

    Test seam: ``embedding_service`` lets test fixtures inject a stub
    so unit tests don't load the ONNX model. Production callers leave
    it ``None`` and each registration resolves the process-wide
    singleton.
    """
    await _register_specs(_COMPOSITES, version=_VERSION, embedding_service=embedding_service)
    register_migration_inventory_dispatch_surface()

    # #3349: publish each write composite's governed child-op set into the
    # discovery registry the GET /api/v1/operations/governed-subops surface
    # reads, so an operator assembles a composite's grant set without reading
    # the ``_SUB_OPS_*`` / ``_VIM_SUB_OPS_*`` manifests in source.
    register_vmware_governed_subops()


async def register_vmware_composite_operations_8_0(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the composites into the ``vmware-rest-8.0`` catalog (#3789).

    A fingerprinted 8.0.x target resolves to the 8.0 catalog, so an agent
    that picks that catalog must find the composites there too. Same rows,
    handlers and policy as the 9.0 catalog, minus
    :data:`COMPOSITES_NOT_ON_8_0`.

    The write steps of a composite are checked under the connector the
    target resolves to (``_step_connector_id`` in ``_write.py``, #4991), so
    on an 8.0.x target a step grant names ``vmware-rest-8.0`` and the
    approval resume of a parked step re-runs the composite on this catalog.
    """
    specs = tuple(spec for spec in _COMPOSITES if spec.op_id not in COMPOSITES_NOT_ON_8_0)
    await _register_specs(specs, version=_VERSION_8_0, embedding_service=embedding_service)
    register_migration_inventory_dispatch_surface()


async def _register_specs(
    specs: tuple[CompositeSpec, ...],
    *,
    version: str,
    embedding_service: EmbeddingService | None,
) -> None:
    """Upsert *specs* under ``(vmware, version, vmware-rest)``."""
    for spec in specs:
        await register_composite_operation(
            product=_PRODUCT,
            version=version,
            impl_id=_IMPL_ID,
            op_id=spec.op_id,
            handler=spec.handler,
            summary=spec.summary,
            description=spec.description,
            parameter_schema=spec.parameter_schema,
            response_schema=spec.response_schema,
            group_key=spec.group_key,
            when_to_use=_WHEN_TO_USE_BY_GROUP[spec.group_key],
            tags=spec.tags,
            safety_level=spec.safety_level,
            requires_approval=spec.requires_approval,
            llm_instructions=spec.llm_instructions,
            embedding_service=embedding_service,
        )
