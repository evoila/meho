# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What an MCP session may do before it has a current tenant (#5239).

A login with several tenants that sends no ``Meho-Tenant`` header gets an
:class:`~meho_backplane.auth.operator.Operator` with
``tenant_chosen=False``. Such a session may only start the connection and
call ``meho_status``, which lists the person's tenants and roles. Every
other request is refused with ``tenant_not_chosen``. One MCP session works
in one tenant, fixed by the header at the start; to switch, the client
changes the setting and starts a new session.

The check is an allow list, so a new method or tool is refused until it
is added here on purpose.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator

__all__ = ["STATUS_TOOL", "allowed_without_tenant"]

#: The one tool that answers without a current tenant.
STATUS_TOOL = "meho_status"

#: JSON-RPC methods that touch no tenant data. ``tools/list`` is filtered
#: to :data:`STATUS_TOOL` by the handler.
_METHODS = frozenset({"initialize", "ping", "tools/list"})


def allowed_without_tenant(operator: Operator, method: str, params: dict[str, Any] | None) -> bool:
    """Return whether the request may run for *operator*.

    Always ``True`` when the operator has a current tenant.
    """
    if operator.tenant_chosen:
        return True
    if method in _METHODS or method.startswith("notifications/"):
        return True
    return method == "tools/call" and isinstance(params, dict) and params.get("name") == STATUS_TOOL
