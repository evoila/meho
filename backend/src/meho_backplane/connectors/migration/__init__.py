# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.migration — migration assessment shapes and operations.

A package with no connector class, like ``connectors/audit/``. It holds the
common inventory shape, the assessment result shape
(:mod:`.contracts`), the Azure Migrate CSV mapping
(:mod:`.azure_migrate_csv`), the loader for stored inventories
(:mod:`.inventory_load`) and the operations (:mod:`.ops`).

Importing the package (the lifespan's connector import pass walks
``connectors/<product>/``) queues
:func:`~meho_backplane.connectors.migration.ops.register_migration_operations`
onto the lifespan registrar list, so the operations are in
``endpoint_descriptor`` before the first call. The connector id is
``migration-assess-1.x``.

The source adapters (:mod:`.source_register`) are queued the same way. They
are registered on the connectors they read (``hyperv.migration.inventory``
on ``hyperv-ssh-2022.x``, and so on), not on ``migration-assess-1.x``.
"""

from meho_backplane.connectors.migration.ops import register_migration_operations
from meho_backplane.connectors.migration.source_register import register_source_adapters
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_typed_op_registrar(register_migration_operations)
register_typed_op_registrar(register_source_adapters)

__all__ = [
    "register_migration_operations",
    "register_source_adapters",
]
