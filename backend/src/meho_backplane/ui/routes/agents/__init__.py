# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agents UI routes: agent-definition list + detail + CRUD.

Initiative #1824 (G10.8 Agents console). Task #1825 (T1) stands up the
``/ui/agents`` top-level surface as the anchor scaffold the rest of the
console's agent surfaces hang off: a scannable list of the tenant's
agent definitions, a per-agent detail view, and create / edit /
enable-disable / delete. Subsequent Tasks layer the run console (T2
#1829), run history (T3 #1830), principals (T4 #1831), and grants (T5
#1832) onto this surface.

Module layout:

* :mod:`~meho_backplane.ui.routes.agents.routes` -- the request
  handlers (path / method / dependency wiring).
* :mod:`~meho_backplane.ui.routes.agents.views` -- the read renders
  (list + detail) and the row projections.
* :mod:`~meho_backplane.ui.routes.agents.forms` -- the write renders
  (create / edit / delete modal) + submit handlers. Delegates to
  :class:`~meho_backplane.agents.service.AgentDefinitionService`
  in-process so the UI write and the REST write share one validation +
  identity-ref-check + persist code path.
* :mod:`~meho_backplane.ui.routes.agents.operator` -- the role-lift
  dependencies: ``resolve_role_probe`` (soft, read paths) and
  ``resolve_operator_or_403`` (hard tenant_admin gate, write paths).

The umbrella :func:`build_agents_router` is mounted **before**
:func:`~meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`~meho_backplane.ui.routes.build_router` so the real
``/ui/agents`` handlers win the first-match-wins path lookup.
"""

from __future__ import annotations

from meho_backplane.ui.routes.agents.routes import build_agents_router

__all__ = ["build_agents_router"]
