# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.targets — targetless target-registry write ops.

``targets.register`` (#2861) and its opposite ``targets.remove`` (#4623),
plus ``targets.probe``, which detects a target's product and version
(``probe.py``), and ``targets.update``, which changes fields of one target
(``update.py``).

The third **synthetic** connector subpackage (after ``secret`` and
``topology``): no vendor connector backs it. Importing the package (the
lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors` pass
walks ``connectors/<product>/`` and imports each subpackage) queues
:func:`~meho_backplane.connectors.targets.ops.register_targets_registry_operations`
onto the lifespan-driven registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
so the ``targets.register`` and ``targets.remove`` ``endpoint_descriptor``
rows land before the first dispatch. Importing the package also registers
the ``targets.remove`` preview builder and the ``targets.probe`` park-time
permission check.

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
every ``targets.register`` and ``targets.update`` call waits as a durable
:class:`~meho_backplane.db.models.ApprovalRequest` until a person approves
it. REST calls made with a token become calls of these ops; the web console
calls :func:`~meho_backplane.api.v1.targets.create_target` directly.
"""

from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REMOVE_OP_ID,
    build_targets_register_preview,
    build_targets_remove_preview,
    register_targets_registry_operations,
    targets_register_card_warnings,
)
from meho_backplane.connectors.targets.probe import (
    TARGETS_PROBE_OP_ID,
    register_targets_probe_operation,
    targets_probe_preflight,
)
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    build_targets_update_preview,
    register_targets_update_operation,
    targets_update_card_warnings,
)
from meho_backplane.op_classes import declare_wait_when_set
from meho_backplane.operations._preview import (
    register_card_warnings,
    register_permission_preflight,
    register_preview_builder,
    register_preview_builder_switch,
)
from meho_backplane.operations.preview_needs_switch import register_preview_needs_switch
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.settings import get_settings

# Queue the targets.* typed-op upserts onto the lifespan-driven registrar
# list (run after the connector eager-import pass).
register_typed_op_registrar(register_targets_registry_operations)
# targets.probe lives in its own module, with its own registrar.
register_typed_op_registrar(register_targets_probe_operation)
# targets.update lives in its own module, with its own registrar.
register_typed_op_registrar(register_targets_update_operation)
# The preview of targets.remove names the target and its topology count.
register_preview_builder(TARGETS_REMOVE_OP_ID, build_targets_remove_preview)
register_preview_builder(TARGETS_UPDATE_OP_ID, build_targets_update_preview)
# The approval card of targets.register shows the new target's values. The
# agent's own preview gets the card only with PREVIEW_TYPED_WRITES on.
register_preview_builder(TARGETS_REGISTER_OP_ID, build_targets_register_preview)
register_preview_builder_switch(TARGETS_REGISTER_OP_ID, lambda: get_settings().preview_typed_writes)
# The agent's preview of targets.register opens only with PREVIEW_TYPED_WRITES
# on, although every call waits for approval.
register_preview_needs_switch(TARGETS_REGISTER_OP_ID)
register_permission_preflight(TARGETS_PROBE_OP_ID, targets_probe_preflight)
# The card shows the realm warnings of the target as it will be after the write.
register_card_warnings(TARGETS_REGISTER_OP_ID, targets_register_card_warnings)
register_card_warnings(TARGETS_UPDATE_OP_ID, targets_update_card_warnings)
# Every register call waits for a person (the address marker on host and fqdn).
# This rule also makes a register that sets one of these two fields wait.
declare_wait_when_set(TARGETS_REGISTER_OP_ID, ("secret_field_map", "flight_recorder_capture"))

__all__ = [
    "register_targets_probe_operation",
    "register_targets_registry_operations",
    "register_targets_update_operation",
]
