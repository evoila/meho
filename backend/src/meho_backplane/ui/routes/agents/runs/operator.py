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
* :func:`resolve_run_reader` -- the read-path dependency. Synthesises a
  tenant-scoped :class:`Operator` with :attr:`TenantRole.OPERATOR`
  directly from the BFF session (the same no-JWT-round-trip path the
  memory list surface's :func:`build_read_operator` takes). Sound because
  the chassis session middleware already authenticated the request and the
  invoker enforces tenant isolation on ``operator.tenant_id``; the role is
  not consulted on the read path, so synthesising ``OPERATOR`` is correct
  for ``list_runs`` / ``poll`` and avoids a JWT decode on every poll tick.
"""

from __future__ import annotations

from fastapi import Depends, Request

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.routes.memory.operator import build_read_operator

__all__ = [
    "OperatorRoleProbe",
    "resolve_role_probe",
    "resolve_run_reader",
]

_require_session_dep = Depends(require_ui_session)


async def resolve_run_reader(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
) -> Operator:
    """FastAPI dependency: synthesise the tenant-scoped read :class:`Operator`.

    The runs list / detail handlers hand this to the invoker's ``list_runs``
    / ``poll``, which tenant-scope on :attr:`Operator.tenant_id`. The
    session middleware already authenticated the request, so synthesising an
    ``OPERATOR``-role operator from the session context (rather than
    re-decoding the access token) is the read-path shape the memory surface
    established (#877). Tenant isolation is the invoker's job and is keyed on
    the synthesised ``tenant_id``; a cross-tenant run id is invisible.
    """
    del request  # the session context is resolved by the dependency chain.
    return build_read_operator(session_ctx)
