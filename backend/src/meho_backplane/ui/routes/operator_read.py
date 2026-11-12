# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``require_ui_read_operator`` -- the reader for console pages at the operator floor.

Several console read pages hand an :class:`Operator` to a tenant-scoped
read service: event sources, conventions and agent runs. Their REST twins
need the ``operator`` role (``require_role(TenantRole.OPERATOR)``), so the
console pages need it too.

The dependency chains :func:`~meho_backplane.ui.auth.middleware.require_ui_operator`:
it checks the role in the session's access token and answers
``403 operator_required`` for a ``read_only`` session before the handler
runs. After that check it builds the reader from the session row. The read
services key on ``tenant_id`` and ``sub`` only; the role on the reader is
the floor the check just proved.

Memory reads do not use this dependency: their REST floor is ``read_only``
(see :func:`~meho_backplane.ui.routes.memory.operator.build_read_operator`).
"""

from __future__ import annotations

from fastapi import Depends

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator

__all__ = ["require_ui_read_operator"]

_require_operator_dep = Depends(require_ui_operator)


async def require_ui_read_operator(
    session_ctx: UISessionContext = _require_operator_dep,
) -> Operator:
    """FastAPI dependency: check the operator role, then return the tenant-scoped reader.

    ``operator`` and ``tenant_admin`` pass. A ``read_only`` session gets
    ``403 operator_required`` from :func:`require_ui_operator`.
    """
    return Operator(
        sub=session_ctx.operator_sub,
        raw_jwt="ui-session",
        tenant_id=session_ctx.tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
