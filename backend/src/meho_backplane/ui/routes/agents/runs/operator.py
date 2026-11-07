# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Role lift for the agent-runs UI surface (Task #1830, G10.8-T3).

The runs list + detail are operator-readable (Initiative #1824: reads =
operator, writes = tenant_admin). The two reads call the in-process
:class:`~meho_backplane.agent.invocation.AgentInvoker`, whose ``list_runs``
/ ``poll`` take a full :class:`~meho_backplane.auth.operator.Operator` and
tenant-scope every query on :attr:`Operator.tenant_id` (a cross-tenant run
surfaces as :class:`AgentRunNotFoundError`). The invoker does **not**
re-check the role on the read path -- the caller is expected to have gated
the surface on ``operator`` -- so the read handler needs a tenant-scoped
:class:`Operator`, not just the boolean role probe.

This module wires both halves:

* :func:`resolve_role_probe` -- re-exported from the connectors surface
  (one JWT-lift implementation across the console). Soft-fails to a
  no-privileges probe so the read surface keeps rendering through a
  transient JWKS hiccup; it only drives the UX affordance (the
  ``awaiting_approval`` deep-link is operator-visible regardless).
* :func:`resolve_run_reader` -- the read-path dependency. Checks the
  ``operator`` role in the session's access token (the same floor as the
  REST ``GET /api/v1/agents/runs``) through
  :func:`~meho_backplane.ui.routes.operator_read.require_ui_read_operator`,
  so a ``read_only`` session gets ``403 operator_required``. It returns the
  tenant-scoped :class:`Operator` the invoker reads take; the invoker
  enforces tenant isolation on ``operator.tenant_id``.
"""

from __future__ import annotations

from fastapi import Depends

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.routes.operator_read import require_ui_read_operator

__all__ = [
    "OperatorRoleProbe",
    "resolve_role_probe",
    "resolve_run_reader",
]

_read_operator_dep = Depends(require_ui_read_operator)


async def resolve_run_reader(
    operator: Operator = _read_operator_dep,
) -> Operator:
    """FastAPI dependency: the tenant-scoped read :class:`Operator`, at the operator floor.

    The runs list / detail handlers hand this to the invoker's ``list_runs``
    / ``poll``, which tenant-scope on :attr:`Operator.tenant_id`; a
    cross-tenant run id is invisible. A ``read_only`` session gets ``403
    operator_required`` before the handler runs.
    """
    return operator
