# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builders for the ArgoCD write ops.

G11.7 follow-up (#1452). Wires the three ArgoCD write ops that can compute a
side-effect-free preview onto the per-op builder hook shipped by #1437
(:mod:`meho_backplane.operations._preview`):

==============================  =====================================================
op_id                           preview stored in ``ApprovalRequest.proposed_effect``
==============================  =====================================================
``argocd.app.set``              ``{before_spec, after_spec}``
``argocd.appproject.update``    ``{before_spec, after_spec}``
``argocd.app.delete``           ``{cascade_resources: [...]}``
==============================  =====================================================

Each builder reuses the **read-only** snapshot helpers the handlers in
:mod:`meho_backplane.connectors.argocd.ops_write` already use
(:func:`_read_app_spec` / :func:`_read_cascade_resources` /
:func:`_read_project_spec`) — it issues only the read GETs
(``GET /api/v1/applications/{name}``, ``.../resource-tree``,
``GET /api/v1/projects``) and never the mutating PUT/DELETE. The mutation
stays parked until a human approves; the builder only computes what the
approved call *would* change so the reviewer reads the diff / cascade in the
approval queue first.

``after_spec`` at park time is the **proposed** spec the dispatch carried in
``params`` — what the approved ``PUT`` would apply. (The handler's in-result
``after_spec`` is the argocd-server-accepted spec echoed by the PUT response,
which only exists post-approval.)

The ops with no natural read-only preview (``app.sync`` / ``app.rollback`` /
``app.refresh``) register no builder here, so they fall through to the
dispatcher's identifier-only default — no preview, no regression.

The whole-builder ``classify_op`` redaction gate runs in
:func:`~meho_backplane.operations._preview.build_proposed_effect` before any
builder fires: ``argocd.app.delete`` / ``appproject.update`` classify as
``write`` and ``argocd.app.set`` as ``other`` — none is a credential class,
so none is suppressed, and none of the snapshots carries secret material
(an ApplicationSpec / AppProjectSpec / resource-tree node is GitOps
topology, not credentials).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.argocd.connector import ArgoCdConnector
from meho_backplane.connectors.argocd.ops_write import (
    _delete_cascade_flag,
    _project_name,
    _project_query,
    _read_app_spec,
    _read_cascade_resources,
    _read_project_spec,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.argocd.session import ArgoCdTargetLike


def _resolve_argocd(ctx: PreviewContext) -> tuple[ArgoCdConnector, ArgoCdTargetLike] | None:
    """Return the resolved (connector, target) pair, or ``None`` to decline.

    A preview needs both a live ArgoCD connector instance and a dispatch
    target to read against; either missing ⇒ no preview (the caller falls
    back to the identifier-only default).
    """
    connector = ctx.connector_instance
    if not isinstance(connector, ArgoCdConnector) or ctx.target is None:
        return None
    return connector, ctx.target


async def _app_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``argocd.app.set`` — read current spec, echo the proposed spec.

    ``before_spec`` is the live ApplicationSpec (read-only GET); ``after_spec``
    is the spec the parked dispatch would PUT. No mutation fires.
    """
    resolved = _resolve_argocd(ctx)
    if resolved is None:
        return None
    connector, target = resolved
    name = str(ctx.params["name"])
    query = _project_query(ctx.params)
    before_spec = await _read_app_spec(connector, ctx.operator, target, name=name, query=query)
    after_spec = ctx.params.get("spec")
    return {"before_spec": before_spec, "after_spec": after_spec}


async def _app_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``argocd.app.delete`` — read the managed resource tree (cascade).

    Reads the resource tree to capture the cascade list a cascading delete
    would remove. No DELETE fires. A non-cascading delete previews an empty
    list (it removes no managed resources).
    """
    resolved = _resolve_argocd(ctx)
    if resolved is None:
        return None
    connector, target = resolved
    name = str(ctx.params["name"])
    cascade = _delete_cascade_flag(ctx.params)
    query = _project_query(ctx.params)
    cascade_resources = await _read_cascade_resources(
        connector, ctx.operator, target, name=name, cascade=cascade, query=query
    )
    return {"cascade_resources": cascade_resources}


async def _appproject_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``argocd.appproject.update`` — read current spec, echo proposed.

    ``before_spec`` is the live AppProjectSpec (read-only GET of the project
    list); ``after_spec`` is the spec the parked dispatch would PUT. No
    mutation fires.
    """
    resolved = _resolve_argocd(ctx)
    if resolved is None:
        return None
    connector, target = resolved
    project = ctx.params.get("project")
    if not isinstance(project, dict):
        return None
    name = _project_name(project)
    before_spec = await _read_project_spec(connector, ctx.operator, target, name=name)
    after_spec = project.get("spec")
    return {"before_spec": before_spec, "after_spec": after_spec}


def _register_argocd_preview_builders() -> None:
    """Wire the ArgoCD park-time preview builders. Called at import time.

    Only the three ops with a natural read-only preview register; the rest
    (``app.sync`` / ``app.rollback`` / ``app.refresh`` / ``appproject.create``)
    fall through to the identifier-only default.
    """
    register_preview_builder("argocd.app.set", _app_set_preview)
    register_preview_builder("argocd.app.delete", _app_delete_preview)
    register_preview_builder("argocd.appproject.update", _appproject_update_preview)


_register_argocd_preview_builders()
