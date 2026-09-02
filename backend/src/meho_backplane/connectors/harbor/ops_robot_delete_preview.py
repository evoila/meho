# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time blast-radius preview builder for ``harbor.robot.delete`` (#3288).

The Harbor arm of the governed-delete tier. ``harbor.robot.delete`` was promoted
to ``safety_level=destructive`` + ``requires_approval=True`` by the #3288
operator ruling: it permanently removes a **credential-bearing principal**, and
re-creating the robot mints a BRAND-NEW secret, so every consumer still
authenticating with the old credential breaks silently (the lab signal
``bind9-harbor-dangerous-writes-bypass-approval-gate.yaml``). This module wires
the mandatory blast-radius statement onto the per-op builder hook shipped by
#1437 (:mod:`meho_backplane.operations._preview`) — the same shape the bind9
(:mod:`~meho_backplane.connectors.bind9.ops_record_delete_preview`) and ArgoCD
(:mod:`~meho_backplane.connectors.argocd.ops_write_preview`) previews use.

The builder derives the blast radius from the **dispatch params alone** — no
live Harbor call. ``harbor.robot.create`` only ever mints ``level=project``
robots scoped to the named project, so the delete's ``{project, id}`` params
fully identify what dies: a project-scoped robot and its single project
association. A params-only preview is deliberate over a live
``GET /robots/{id}`` enrichment — it keeps the builder robust (a destructive op
whose preview *raises* is refused ``blast_radius_required`` and can never be
approved, so a network-dependent read would turn a transient Harbor blip into a
permanent inability to govern-delete) and surgical (no new dispatched HTTP path
for the spec-reconcile lane to reconcile). The reviewer still sees the robot
identity + its project scope + the honest irreversibility class.

The returned ``{"blast_radius": {...}}`` sub-dict is promoted by the dispatcher
to the top of ``ApprovalRequest.proposed_effect`` (#3197), so a ``destructive``
op cannot park without the approver seeing what is destroyed:

* ``object`` — the robot identity ``{kind: "harbor_robot", id, project, level:
  "project"}``.
* ``children`` — the enumerable reference that goes with it: the project
  association ``{kind: "project_association", project}`` (the access footprint
  the robot loses). One project by construction (project-scoped robots).
* ``irreversibility`` — ``"recreatable_new_secret"``: unlike a bind9 record
  (``"recreatable"`` from the same rdata), a Harbor robot CAN be re-created but
  only with a fresh secret — the old credential is gone, so recovery is not
  transparent to consumers. The class is honest so the approver weighs the real
  cost (every consumer of the old secret breaks) against the recovery path.

Declines (returns ``None`` → identifier-only default → the park is refused
``blast_radius_required``, fail-closed) when the connector/target is unresolved
or the required params are missing/malformed.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

_DELETE_OP_ID = "harbor.robot.delete"


async def _harbor_robot_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the mandatory destructive-tier blast radius for ``robot.delete``.

    Params-only (no live call). See the module docstring for the shape and the
    decline contract. Never issues the DELETE — it stays parked until a human
    approves.
    """
    connector = ctx.connector_instance
    if not isinstance(connector, HarborConnector) or ctx.target is None:
        return None
    project = ctx.params.get("project")
    robot_id = ctx.params.get("id")
    if not isinstance(project, str) or not project:
        return None
    if not isinstance(robot_id, int) or isinstance(robot_id, bool):
        return None

    return {
        "blast_radius": {
            "object": {
                "kind": "harbor_robot",
                "id": robot_id,
                "project": project,
                "level": "project",
            },
            "children": [{"kind": "project_association", "project": project}],
            "irreversibility": "recreatable_new_secret",
            "match_count": 1,
        },
    }


def _register_harbor_robot_delete_preview_builder() -> None:
    """Wire the ``harbor.robot.delete`` park-time preview builder. Import-time.

    Idempotent (``register_preview_builder`` overwrites), so a test reload is a
    no-op-equivalent — same contract as the bind9 / ArgoCD wirings.
    """
    register_preview_builder(_DELETE_OP_ID, _harbor_robot_delete_preview)


_register_harbor_robot_delete_preview_builder()
