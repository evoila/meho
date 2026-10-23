# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.audit — targetless read over MEHO's own audit log (#4670).

The fourth **synthetic** connector subpackage (after ``secret``,
``topology`` and ``targets``): no vendor connector backs it. Importing the
package (the lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors` pass
walks ``connectors/<product>/`` and imports each subpackage) queues
:func:`~meho_backplane.connectors.audit.ops.register_audit_integrity_operations`
onto the lifespan-driven registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
so the ``audit.loose_rows`` ``endpoint_descriptor`` row lands before the
first dispatch.

Like the other synthetic packages, this one calls neither
``register_connector`` nor ``register_connector_v2``: the synthetic
``audit-integrity-1.x`` identity has no connector class. The handler is a
module-level function the dispatcher routes to with
``connector_instance=None`` / ``target=None`` — the audit log is
tenant-scoped state, not a probed target.

Why the read is a dispatchable op at all: a Sensor pins
``(connector_id, op_id, params, assertion, cadence, severity)`` and the
check runner dispatches it on a schedule. Registering the loose-rows query
as a ``safe`` typed op is what lets the checks subsystem watch it with no
new code path. The default sensor template lives in
:mod:`meho_backplane.connectors.audit.sensor_template`.
"""

from meho_backplane.connectors.audit.ops import register_audit_integrity_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the audit.loose_rows typed-op upsert onto the lifespan-driven
# registrar list (run after the connector eager-import pass).
register_typed_op_registrar(register_audit_integrity_operations)

__all__ = [
    "register_audit_integrity_operations",
]
