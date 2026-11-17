# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.tools — admin work on the tool logins of people.

A synthetic connector package, like ``targets`` and ``policy``: no vendor
system backs it, and the wire connector id is ``tools-admin-1.x``.
Importing it queues the registrar of ``tools.sign_out`` (a tenant admin
signs out one tool kind of another person, see
:mod:`meho_backplane.connectors.tools.ops`), sets the ``tenant_admin``
role floor, lets only a person ask, and registers the approval card that
names the person by id.
"""

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.tools.ops import (
    TOOLS_SIGN_OUT_OP_ID,
    build_tools_sign_out_preview,
    register_tools_sign_out_operation,
    tools_sign_out_requester_refusal,
)
from meho_backplane.operations._preview import register_preview_builder
from meho_backplane.operations.role_floors import register_requester_check, register_role_floor
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_typed_op_registrar(register_tools_sign_out_operation)
register_role_floor(TOOLS_SIGN_OUT_OP_ID, TenantRole.TENANT_ADMIN)
register_requester_check(TOOLS_SIGN_OUT_OP_ID, tools_sign_out_requester_refusal)
register_preview_builder(TOOLS_SIGN_OUT_OP_ID, build_tools_sign_out_preview)

__all__ = ["register_tools_sign_out_operation"]
