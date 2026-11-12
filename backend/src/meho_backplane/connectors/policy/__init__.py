# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.policy — MEHO's own policy operations.

A synthetic connector package, like ``targets``: no vendor system backs
it, and the wire connector id is ``policy-registry-1.x``. Importing it
queues the registrar of ``policy.classify`` (which registers the
operation only while the ``POLICY_CLASSIFICATION`` switch is on), records
that a person needs the ``tenant_admin`` role for it, marks every request
of it as a change of rights, and marks it for approval on each call (no
standing grant and no run approval covers it).
"""

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.policy.classify import (
    POLICY_CLASSIFY_OP_ID,
    register_policy_classify_operation,
)
from meho_backplane.connectors.policy.preview import (
    POLICY_CLASSIFY_PREVIEW_OP_ID,
    register_policy_classify_preview_operation,
)
from meho_backplane.op_classes import declare_per_call_approval
from meho_backplane.operations.grant_approval import register_rights_change_op
from meho_backplane.operations.role_floors import register_role_floor
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_typed_op_registrar(register_policy_classify_operation)
register_typed_op_registrar(register_policy_classify_preview_operation)
register_role_floor(POLICY_CLASSIFY_OP_ID, TenantRole.TENANT_ADMIN)
register_role_floor(POLICY_CLASSIFY_PREVIEW_OP_ID, TenantRole.TENANT_ADMIN)
register_rights_change_op(POLICY_CLASSIFY_OP_ID)
declare_per_call_approval([POLICY_CLASSIFY_OP_ID])

__all__ = ["register_policy_classify_operation", "register_policy_classify_preview_operation"]
