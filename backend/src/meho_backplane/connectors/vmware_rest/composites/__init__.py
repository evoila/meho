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
``endpoint_descriptor`` upserts for the 14 composites land before
any dispatch can fire.

Layout mirrors the :mod:`meho_backplane.connectors.vault` pattern: the
``__init__`` wires the registrar; ``_register.py`` carries the
per-composite registration metadata; ``_read.py`` and ``_write.py``
carry the handler implementations; ``schemas.py`` carries the JSON
Schema 2020-12 parameter + response contracts.

Scope:

* 5 read composites (G3.1-T5 / #508) --
  ``safety_level="safe"`` + ``requires_approval=False`` overrides.
  (The former ``host.network_uplinks`` / ``host.vsan_health`` reads
  were re-shipped as ``source_kind="typed"`` ops in #2258; see
  :mod:`~meho_backplane.connectors.vmware_rest.typed_ops`.)
* 9 write composites (G3.1-T6 / #509, plus single-VM ``vm.power`` /
  #2301) -- inherit T4's ``safety_level="dangerous"`` +
  ``requires_approval=True`` defaults.
  They cover every state-mutating workflow Goal #214 names as
  required for govc-wrapper retirement: ``vm.create``, ``vm.clone``,
  ``vm.snapshot.revert``, ``vm.migrate``, ``vm.power`` (single VM,
  incl. Tools soft shutdown), ``vm.power.bulk``,
  ``host.evacuate`` (first recursive composite),
  ``host.detach_from_vds``, ``cluster.patch``.
"""

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
    cluster_patch_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_create_composite,
    vm_migrate_composite,
    vm_power_bulk_composite,
    vm_power_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the composite-op upsert onto the lifespan-driven registrar list.
# The lifespan calls ``run_typed_op_registrars`` after
# ``_eager_import_connectors`` so every connector subpackage has self-
# registered by the time the runner iterates.
register_typed_op_registrar(register_vmware_composite_operations)

# Side-effect import: registers the 9 write composites' park-time
# ``proposed_effect`` preview builders (#1608) onto the per-op hook in
# :mod:`meho_backplane.operations._preview` — mirrors how
# ``connectors/argocd/__init__`` wires ``ops_write_preview``.
from meho_backplane.connectors.vmware_rest.composites import _write_preview  # noqa: E402,F401

__all__ = [
    "cluster_drs_recommendations_composite",
    "cluster_patch_composite",
    "datastore_usage_composite",
    "event_tail_composite",
    "host_detach_from_vds_composite",
    "host_evacuate_composite",
    "network_portgroup_audit_composite",
    "performance_summary_composite",
    "register_vmware_composite_operations",
    "vm_clone_composite",
    "vm_create_composite",
    "vm_migrate_composite",
    "vm_power_bulk_composite",
    "vm_power_composite",
    "vm_snapshot_revert_composite",
]
