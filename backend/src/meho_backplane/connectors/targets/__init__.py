# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.targets — targetless target-registry write op (#2861).

The third **synthetic** connector subpackage (after ``secret`` and
``topology``): no vendor connector backs it. Importing the package (the
lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors` pass
walks ``connectors/<product>/`` and imports each subpackage) queues
:func:`~meho_backplane.connectors.targets.ops.register_targets_registry_operations`
onto the lifespan-driven registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
so the ``targets.register`` ``endpoint_descriptor`` row lands before the
first dispatch.

Like the other synthetic packages, this one calls neither
``register_connector`` nor ``register_connector_v2``: the synthetic
``targets-registry-1.x`` identity has no connector class. The handler is a
module-level function the dispatcher routes to with
``connector_instance=None`` / ``target=None`` — the registry is
tenant-scoped state, not a probed target.

Why the op exists as a dispatchable descriptor at all: routing the MCP
write front (``meho_targets_register``) through
:func:`~meho_backplane.operations.dispatcher.dispatch` puts it behind
:func:`~meho_backplane.operations._validate.policy_gate` — the seam where
an AGENT principal's ``caution``-level write parks as a durable
:class:`~meho_backplane.db.models.ApprovalRequest` while a human
``tenant_admin`` keeps the immediate path. The REST / UI fronts are
human-only and keep calling
:func:`~meho_backplane.api.v1.targets.create_target` directly.
"""

from meho_backplane.connectors.targets.ops import register_targets_registry_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the targets.register typed-op upsert onto the lifespan-driven
# registrar list (run after the connector eager-import pass).
register_typed_op_registrar(register_targets_registry_operations)

__all__ = [
    "register_targets_registry_operations",
]
