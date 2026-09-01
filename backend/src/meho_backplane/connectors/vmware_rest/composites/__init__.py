# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vmware_rest.composites -- vmware-rest composites.

Side-effect import: this package's ``__init__`` queues
:func:`register_vmware_composite_operations` onto the lifespan-driven
registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`.

The chassis lifespan's
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
invokes every registered registrar in registration order after
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
has walked every ``connectors/<product>/`` subpackage, so the
``endpoint_descriptor`` upserts for the 33 composites land before
any dispatch can fire.

Layout mirrors the :mod:`meho_backplane.connectors.vault` pattern: the
``__init__`` wires the registrar; ``_register.py`` carries the
per-composite registration metadata; ``_read.py`` and ``_write.py``
carry the handler implementations; ``schemas.py`` carries the JSON
Schema 2020-12 parameter + response contracts.

Scope:

* 9 read composites (G3.1-T5 / #508 + the 4 guest-ops reads
  ``vm.guest.process.list`` / ``env.read`` / ``net.show`` /
  ``file.read`` / #3100) --
  ``safety_level="safe"`` + ``requires_approval=False`` overrides.
  (The former ``host.network_uplinks`` / ``host.vsan_health`` reads
  were re-shipped as ``source_kind="typed"`` ops in #2258; see
  :mod:`~meho_backplane.connectors.vmware_rest.typed_ops`.)
* 24 write composites (G3.1-T6 / #509, the guest-ops write
  ``vm.guest.file.write`` / #3100, single-VM ``vm.power`` /
  #2301, the mutating VI-JSON ``vm.disk.grow`` / #2893, the
  folder-template ``vm.clone_from_template`` / #2894, the vim
  cluster / inventory writes ``cluster.drs_rule.create`` +
  ``folder.create`` / #2895, the #2891 post-clone hardware
  reconfigure trio ``vm.resize`` / ``vm.nic.repoint`` /
  ``vm.device.cdrom``, the two GOSC composites / #2892, the OVF/OVA
  content-library deploy ``vm.deploy_from_library`` / #2909, and the
  three host-domain writes ``host.datastore_mount_nfs`` /
  ``host.disk_mark_flash`` / ``host.service_control`` / #3182) -- inherit
  T4's ``safety_level="dangerous"`` +
  ``requires_approval=True`` defaults. The 24th, the destructive-tier
  ``vm.destroy`` / #3198, is the first ``safety_level="destructive"``
  composite (still ``requires_approval=True``) — the governed-delete tier.
  They cover every state-mutating workflow Goal #214 names as
  required for govc-wrapper retirement: ``vm.create``, ``vm.clone``,
  ``vm.clone_from_template`` (folder-template clone via CloneVM_Task,
  with optional inline guest customization), ``vm.snapshot.revert``,
  ``vm.migrate``, ``vm.power`` (single VM,
  incl. Tools soft shutdown), ``vm.power.bulk``, ``vm.disk.grow`` (the
  first mutating VI-JSON composite — disk capacity has no REST path),
  ``host.evacuate`` (first recursive composite),
  ``host.detach_from_vds``, ``cluster.patch``,
  ``cluster.drs_rule.create`` (DRS rule by explicit VM list, no REST
  path), ``folder.create`` (synchronous vim ``CreateFolder``), the
  post-clone hardware reconfigure trio ``vm.resize``,
  ``vm.nic.repoint``, ``vm.device.cdrom`` (#2891), plus guest OS
  customization: ``guest.customization_spec.create`` and
  ``vm.customize`` (GOSC create + apply, secret-hygienic) (#2892), plus
  ``vm.deploy_from_library`` (OVF/OVA content-library deploy, retiring
  ``govc library.deploy``) (#2909).
"""

from meho_backplane.connectors.vmware_rest.composites._guest import (
    guest_env_read_composite,
    guest_file_read_composite,
    guest_file_write_composite,
    guest_net_show_composite,
    guest_process_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._host import (
    datastore_mount_nfs_composite,
    disk_mark_flash_composite,
    service_control_composite,
)
from meho_backplane.connectors.vmware_rest.composites._read import (
    cluster_drs_recommendations_composite,
    datastore_usage_composite,
    event_tail_composite,
    network_portgroup_audit_composite,
    performance_summary_composite,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    register_vmware_composite_operations,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_drs_rule_create_composite,
    cluster_patch_composite,
    folder_create_composite,
    guest_customization_spec_create_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    network_portgroup_create_composite,
    network_portgroup_security_set_composite,
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_customize_composite,
    vm_deploy_from_library_composite,
    vm_destroy_composite,
    vm_device_cdrom_composite,
    vm_disk_grow_composite,
    vm_migrate_composite,
    vm_nic_repoint_composite,
    vm_power_bulk_composite,
    vm_power_composite,
    vm_resize_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the composite-op upsert onto the lifespan-driven registrar list.
# The lifespan calls ``run_typed_op_registrars`` after
# ``_eager_import_connectors`` so every connector subpackage has self-
# registered by the time the runner iterates.
register_typed_op_registrar(register_vmware_composite_operations)

# Side-effect import: registers the 24 write composites' park-time
# ``proposed_effect`` preview builders (#1608, destructive-tier
# blast-radius #3198) onto the per-op hook in
# :mod:`meho_backplane.operations._preview` — mirrors how
# ``connectors/argocd/__init__`` wires ``ops_write_preview``.
from meho_backplane.connectors.vmware_rest.composites import _write_preview  # noqa: E402,F401

__all__ = [
    "cluster_drs_recommendations_composite",
    "cluster_drs_rule_create_composite",
    "cluster_patch_composite",
    "datastore_mount_nfs_composite",
    "datastore_usage_composite",
    "disk_mark_flash_composite",
    "event_tail_composite",
    "folder_create_composite",
    "guest_customization_spec_create_composite",
    "guest_env_read_composite",
    "guest_file_read_composite",
    "guest_file_write_composite",
    "guest_net_show_composite",
    "guest_process_list_composite",
    "host_detach_from_vds_composite",
    "host_evacuate_composite",
    "network_portgroup_audit_composite",
    "network_portgroup_create_composite",
    "network_portgroup_security_set_composite",
    "performance_summary_composite",
    "register_vmware_composite_operations",
    "service_control_composite",
    "vm_clone_composite",
    "vm_clone_from_template_composite",
    "vm_create_composite",
    "vm_customize_composite",
    "vm_deploy_from_library_composite",
    "vm_destroy_composite",
    "vm_device_cdrom_composite",
    "vm_disk_grow_composite",
    "vm_migrate_composite",
    "vm_nic_repoint_composite",
    "vm_power_bulk_composite",
    "vm_power_composite",
    "vm_resize_composite",
    "vm_snapshot_revert_composite",
]
