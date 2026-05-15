# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vmware_rest.composites -- vmware-rest read composites.

Side-effect import: this package's ``__init__`` queues
:func:`register_vmware_composite_operations` onto the lifespan-driven
registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`.

The chassis lifespan's
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
invokes every registered registrar in registration order after
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
has walked every ``connectors/<product>/`` subpackage, so the
``endpoint_descriptor`` upserts for the 5 read composites land before
any dispatch can fire.

Layout mirrors the :mod:`meho_backplane.connectors.vault` pattern: the
``__init__`` wires the registrar; ``_register.py`` carries the
per-composite registration metadata; ``_read.py`` carries the handler
implementations; ``schemas.py`` carries the JSON Schema 2020-12
parameter contracts.

Scope: 5 read composites (G3.1-T5 / #508). 8 write composites land
under G3.1-T6 (#509).
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
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the composite-op upsert onto the lifespan-driven registrar list.
# The lifespan calls ``run_typed_op_registrars`` after
# ``_eager_import_connectors`` so every connector subpackage has self-
# registered by the time the runner iterates.
register_typed_op_registrar(register_vmware_composite_operations)

__all__ = [
    "cluster_drs_recommendations_composite",
    "datastore_usage_composite",
    "event_tail_composite",
    "network_portgroup_audit_composite",
    "performance_summary_composite",
    "register_vmware_composite_operations",
]
