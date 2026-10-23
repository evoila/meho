# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for a deployment from a cloud template (VCF Automation tenant plane).

Three ops: start a deploy request (or only a plan of it), read a deploy
request until it ends, and delete one deployment. Metadata lives in
:mod:`.deploy_ops`. The blueprint and deployment APIs take the same tenant
bearer as ``/iaas/api`` (:func:`._routing.plane_for_path`).

An upstream 4xx/5xx raises :exc:`httpx.HTTPStatusError`; the dispatcher maps it
to ``connector_error`` with ``http_status`` and ``upstream_message``. The one
exception is a ``404`` on the delete: the deployment is already gone, so the
op answers ``unchanged``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._lookups import find_by_odata_name, quote_segment
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_BLUEPRINT_REQUEST_PATH,
    TENANT_BLUEPRINT_REQUESTS_PATH,
    TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
    TENANT_IAAS_API_VERSION,
)
from meho_backplane.connectors.vcf_automation.typed_ops import TENANT_DEPLOYMENTS_PATH

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "PLAN_NOTE",
    "blueprint_request_body",
    "tenant_blueprint_request_create",
    "tenant_blueprint_request_get",
    "tenant_deployment_delete",
]

#: Op param -> ``BlueprintRequest`` field.
_REQUEST_FIELDS: Final[dict[str, str]] = {
    "project_id": "projectId",
    "blueprint_id": "blueprintId",
    "blueprint_version": "blueprintVersion",
    "content": "content",
    "deployment_name": "deploymentName",
    "description": "description",
    "inputs": "inputs",
    "reason": "reason",
}

PLAN_NOTE: Final[str] = (
    "a plan only checks the template and its inputs; it does not find placement "
    "errors (no fitting zone, network or quota). Only a real request finds those"
)
_WAIT_NOTE: Final[str] = (
    "read vcfa.tenant.blueprint_request.get with request.id until status is "
    "FINISHED, FAILED or CANCELLED"
)


def blueprint_request_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``POST /blueprint/api/blueprint-requests`` body (``BlueprintRequest``)."""
    body: dict[str, Any] = {
        wire: params[param]
        for param, wire in _REQUEST_FIELDS.items()
        if params.get(param) not in (None, "")
    }
    body["plan"] = bool(params.get("plan", False))
    return body


def _request_view(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("id", "status", "plan", "deploymentId", "deploymentName", "projectId")
    return {
        **{key: row.get(key) for key in keys},
        "failureMessage": row.get("failureMessage"),
        "validationMessages": row.get("validationMessages") or [],
    }


async def tenant_blueprint_request_create(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.blueprint_request.create`` — ``POST /blueprint/api/blueprint-requests``.

    Exactly one of ``blueprint_id`` and ``content`` must be set, else
    ``invalid_request`` before any call. A real request (not a plan) whose
    ``deployment_name`` already names a deployment answers ``unchanged``, so
    a second run does not build a second deployment.
    """
    if bool(params.get("blueprint_id")) == bool(params.get("content")):
        return {
            "status": "invalid_request",
            "request": None,
            "deployment": None,
            "guidance": (
                "give exactly one of blueprint_id (a stored template) and content (template YAML)"
            ),
        }
    plan = bool(params.get("plan", False))
    name = params.get("deployment_name")
    if not plan and name:
        existing = await find_by_odata_name(
            connector,
            target,
            operator,
            TENANT_DEPLOYMENTS_PATH,
            name,
            params={"apiVersion": TENANT_IAAS_API_VERSION},
        )
        if existing is not None:
            return {
                "status": "unchanged",
                "request": None,
                "deployment": {key: existing.get(key) for key in ("id", "name", "projectId")},
                "guidance": f"a deployment named {name!r} already exists; nothing was requested",
            }
    created = await connector._post_json(
        target,
        TENANT_BLUEPRINT_REQUESTS_PATH,
        operator=operator,
        json=blueprint_request_body(params),
    )
    return {
        "status": "submitted",
        "request": _request_view(created),
        "deployment": None,
        "guidance": f"{PLAN_NOTE}; {_WAIT_NOTE}" if plan else _WAIT_NOTE,
    }


async def tenant_blueprint_request_get(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.blueprint_request.get`` — ``GET …/blueprint-requests/{requestId}``."""
    path = TENANT_BLUEPRINT_REQUEST_PATH.format(requestId=quote_segment(params["id"]))
    return await connector._request_json(target, "GET", path, operator=operator)


async def tenant_deployment_delete(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.tenant.deployment.delete`` — ``DELETE /deployment/api/deployments/{deploymentId}``.

    Starts the delete of the deployment and all its resources. A ``404``
    (no such deployment) answers ``unchanged``, so a second run is safe.
    """
    path = TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH.format(deploymentId=quote_segment(params["id"]))
    try:
        request = await connector._post_json(target, path, operator=operator, verb="DELETE")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return {
            "status": "unchanged",
            "request": None,
            "guidance": "no deployment has this id; nothing was deleted",
        }
    return {
        "status": "submitted",
        "request": {key: request.get(key) for key in ("id", "name", "status", "deploymentId")},
        "guidance": (
            "the delete runs in the background; read vcfa.tenant.deployment.get with the "
            "same id until it answers not found"
        ),
    }
