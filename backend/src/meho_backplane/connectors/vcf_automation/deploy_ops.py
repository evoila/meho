# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for a deployment from a cloud template (tenant plane).

* ``vcfa.tenant.blueprint_request.create`` -- caution, approval:
  ``POST /blueprint/api/blueprint-requests``
* ``vcfa.tenant.blueprint_request.get`` -- safe:
  ``GET /blueprint/api/blueprint-requests/{requestId}``
* ``vcfa.tenant.deployment.delete`` -- dangerous, approval:
  ``DELETE /deployment/api/deployments/{deploymentId}``

Handlers live in :mod:`._deploy`. Field names come from the pinned
``vra-blueprint.json`` (``BlueprintRequest``) and
``vra-catalog-deployment.json`` (``Request``). The delete is ``dangerous``
(it removes the deployment's VMs; the person who asks cannot approve it
alone), like ``argocd.app.delete``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._deploy import PLAN_NOTE
from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, obj
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_BLUEPRINT_REQUEST_PATH,
    TENANT_BLUEPRINT_REQUESTS_PATH,
    TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["VCFA_DEPLOY_OPS"]

_STATUSES = "CREATED, STARTED, FINISHED, FAILED or CANCELLED"

_REQUEST_FIELDS: dict[str, Any] = {
    "id": field(["string", "null"], "The request id."),
    "status": field(["string", "null"], f"The request state: {_STATUSES}."),
    "plan": field(["boolean", "null"], "True when the request is only a plan."),
    "deploymentId": field(["string", "null"], "The id of the deployment it builds."),
    "deploymentName": field(["string", "null"], "The name of the deployment it builds."),
    "projectId": field(["string", "null"], "The project id."),
    "failureMessage": field(["string", "null"], "Why the request failed, when it failed."),
    "validationMessages": {
        "type": "array",
        "description": "Problems the template check found.",
        "items": obj(
            "One problem.",
            {
                "message": field("string", "What is wrong."),
                "type": field("string", "ERROR, WARNING or INFO."),
                "path": field("string", "Where in the template."),
            },
        ),
    },
}

_BLUEPRINT_REQUEST_CREATE = VcfaTypedOp(
    op_id="vcfa.tenant.blueprint_request.create",
    handler_attr="tenant_blueprint_request_create",
    plane="tenant",
    path=TENANT_BLUEPRINT_REQUESTS_PATH,
    summary="Deploy from a cloud template, or only plan it (tenant plane).",
    description=(
        "Sends POST /blueprint/api/blueprint-requests for a project, from a stored "
        "template (blueprint_id) or template YAML (content). plan=true is a dry run. "
        "The request runs in the background."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "minLength": 1,
                "description": "The project id (from vcfa.tenant.project.list).",
            },
            "blueprint_id": {
                "type": "string",
                "minLength": 1,
                "description": "The id of a stored cloud template. Give this or content.",
            },
            "blueprint_version": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional released version of the template; omit for the latest draft."
                ),
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "Cloud template YAML. Give this or blueprint_id.",
            },
            "deployment_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    "The name of the new deployment. When the project already has a "
                    "deployment with this name, nothing is requested."
                ),
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional description of the deployment.",
            },
            "inputs": {
                "type": "object",
                "description": "The template inputs, by input name.",
                "additionalProperties": True,
            },
            "plan": {
                "type": "boolean",
                "description": "True for a dry run that builds nothing. Default false.",
            },
            "reason": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional reason, kept with the request.",
            },
        },
        "required": ["project_id"],
        "additionalProperties": False,
        "examples": [
            {
                "project_id": "<project-id>",
                "blueprint_id": "<template-id>",
                "deployment_name": "<deployment-name>",
                "inputs": {"<input-name>": "<value>"},
                "plan": True,
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did: submitted, unchanged or invalid_request.",
                enum=["submitted", "unchanged", "invalid_request"],
            ),
            "request": {
                **obj("The deploy request; null when nothing was requested.", _REQUEST_FIELDS),
                "type": ["object", "null"],
            },
            "deployment": {
                **obj(
                    "Only on 'unchanged': the deployment in this project that has this name.",
                    {
                        "id": field(["string", "null"], "The deployment id."),
                        "name": field("string", "The deployment name."),
                        "projectId": field(["string", "null"], "The project id."),
                    },
                ),
                "type": ["object", "null"],
            },
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-writes",
    tags=("write", "vcfa", "tenant", "deployment"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to deploy a project's VMs from a cloud template, or with plan=true to "
            "check the template and inputs first."
        ),
        preconditions=(
            f"The project exists and may use a cloud zone and a network. Note: {PLAN_NOTE}. "
            "A person must approve the call."
        ),
        output_shape=(
            "{status: submitted | unchanged | invalid_request, request: {id, status, plan, "
            "deploymentId, deploymentName, projectId, failureMessage, validationMessages}, "
            "deployment, guidance}."
        ),
        next_step=(
            "Read vcfa.tenant.blueprint_request.get with request.id until status is "
            "FINISHED, FAILED or CANCELLED."
        ),
    ),
)

_BLUEPRINT_REQUEST_GET = VcfaTypedOp(
    op_id="vcfa.tenant.blueprint_request.get",
    handler_attr="tenant_blueprint_request_get",
    plane="tenant",
    path=TENANT_BLUEPRINT_REQUEST_PATH,
    summary="Read one deploy request, to see if it has ended (tenant plane).",
    description=(
        "Reads GET /blueprint/api/blueprint-requests/{requestId}. Call it again until "
        "status is FINISHED, FAILED or CANCELLED. A failed request has failureMessage."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The request id (from vcfa.tenant.blueprint_request.create).",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<request-id>"}],
    },
    response_schema=obj("One deploy request.", _REQUEST_FIELDS),
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant", "deployment"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call="Call after vcfa.tenant.blueprint_request.create, until the request ends.",
        output_shape=(
            "BlueprintRequest: id, status ("
            + _STATUSES
            + "), plan, deploymentId, failureMessage, validationMessages, plus more fields."
        ),
        next_step=(
            "FINISHED: read the deployment with vcfa.tenant.deployment.get and deploymentId. "
            "FAILED: read failureMessage. Otherwise wait about 30 seconds and read again."
        ),
        parameter_hints={"id": "The request id from vcfa.tenant.blueprint_request.create."},
    ),
)

_DEPLOYMENT_DELETE = VcfaTypedOp(
    op_id="vcfa.tenant.deployment.delete",
    handler_attr="tenant_deployment_delete",
    plane="tenant",
    path=TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH,
    summary="Delete one deployment and all its VMs (tenant plane).",
    description=(
        "Sends DELETE /deployment/api/deployments/{deploymentId}. This removes every "
        "resource of the deployment. Runs in the background. An unknown id answers "
        "'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The deployment id (from vcfa.tenant.deployment.list).",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [{"id": "<deployment-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did: submitted or unchanged.",
                enum=["submitted", "unchanged"],
            ),
            "request": {
                **obj(
                    "The delete request; null on 'unchanged'.",
                    {
                        "id": field(["string", "null"], "The request id."),
                        "name": field(["string", "null"], "The request label."),
                        "status": field(["string", "null"], "The request state."),
                        "deploymentId": field(["string", "null"], "The deployment id."),
                    },
                ),
                "type": ["object", "null"],
            },
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-writes",
    tags=("write", "vcfa", "tenant", "deployment", "delete"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to remove a deployment and its VMs, for example to undo a test build.",
        preconditions=(
            "Check the id and name with vcfa.tenant.deployment.get first. The VMs and "
            "their disks are deleted. A second person must approve the call."
        ),
        output_shape=(
            "{status: submitted | unchanged, request: {id, name, status, deploymentId}, guidance}."
        ),
        next_step="Read vcfa.tenant.deployment.get with the same id until it answers not found.",
    ),
)

#: The deploy ops, in the order of a build: request, wait, undo.
VCFA_DEPLOY_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _BLUEPRINT_REQUEST_CREATE,
    _BLUEPRINT_REQUEST_GET,
    _DEPLOYMENT_DELETE,
)


validate_typed_ops(VCFA_DEPLOY_OPS)
