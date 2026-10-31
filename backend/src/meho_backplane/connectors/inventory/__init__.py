# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.inventory — the estate inventory and its exporter.

A package with no connector class, like ``connectors/audit/``. It holds the
common inventory shape and the assessment result shape (:mod:`.contracts`),
the loader for stored inventories (:mod:`.inventory_load`), the exporter
read ``inventory.export`` (:mod:`.ops`) with its table of file formats
(:mod:`.formats`, first format :mod:`.azure_migrate_csv`), and the source
adapters.

Importing the package (the lifespan's connector import pass walks
``connectors/<product>/``) queues
:func:`~meho_backplane.connectors.inventory.ops.register_inventory_operations`
onto the lifespan registrar list, so the operations are in
``endpoint_descriptor`` before the first call. The connector id is
``inventory-1.x``.

The source adapters (:mod:`.source_register`) are queued the same way. They
are registered on the connectors they read (``hyperv.migration.inventory``
on ``hyperv-ssh-2022.x``, and so on), not on ``inventory-1.x``.
"""

from meho_backplane.connectors.inventory.ops import register_inventory_operations
from meho_backplane.connectors.inventory.source_register import register_source_adapters
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_typed_op_registrar(register_inventory_operations)
register_typed_op_registrar(register_source_adapters)

__all__ = [
    "register_inventory_operations",
    "register_source_adapters",
]
