# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Harbor typed write ``harbor.project.create`` -- create a project, with approval.

An agent that needs a private image project creates it through MEHO, so the
change waits for approval and lands in the audit log, instead of calling
Harbor's API outside MEHO.

The operation is safe to run twice. Harbor answers ``409`` when the project
name is taken. The handler reports that as ``created: false`` and changes
nothing, the same way ``k8s.namespace.create`` does. Any other error goes to
the dispatcher's ``connector_error`` envelope.

The handler body lives here and
:meth:`~meho_backplane.connectors.harbor.connector.HarborConnector.project_create`
is a thin shim, so the dispatcher can find the callable from its stored
``module.ClassName.method`` path. The registrar is queued in the package
``__init__`` next to the other Harbor registrars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.harbor._paths import _PROJECTS_PATH
from meho_backplane.connectors.harbor.ops import _HARBOR_PROJECTS_WHEN_TO_USE

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.connectors.harbor.session import HarborTargetLike
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["harbor_project_create_impl", "register_harbor_project_create_operation"]

_PROJECT_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Name of the new Harbor project. Lower-case letters, digits and "
                "separators only; Harbor refuses other names."
            ),
        },
        "public": {
            "type": "boolean",
            "default": False,
            "description": (
                "True makes the project public (anyone can pull). False, the default, "
                "makes it private."
            ),
        },
        "storage_limit": {
            "type": "integer",
            "minimum": -1,
            "description": (
                "Optional storage limit in bytes. -1 means no limit. Leave it out to use "
                "the Harbor default."
            ),
        },
    },
    "required": ["project_name"],
    "additionalProperties": False,
    "examples": [
        {"project_name": "demo-images"},
        {"project_name": "demo-images", "public": False, "storage_limit": 10737418240},
    ],
}

_PROJECT_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_name": {"type": "string", "description": "The requested project name."},
        "created": {
            "type": "boolean",
            "description": "True when this call made the project. False when the name was taken.",
        },
        "already_existed": {
            "type": "boolean",
            "description": "True when Harbor answered that the project name is taken.",
        },
        "read_back": {
            "type": "string",
            "description": "The operation that reads the project back.",
        },
    },
    "required": ["project_name", "created", "already_existed", "read_back"],
}

_PROJECT_CREATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Create a Harbor project, for example a private image project for a new app. "
        "Use this instead of calling Harbor's API outside MEHO."
    ),
    "preconditions": (
        "Check with harbor.project.list that the name is free, and tell the user whether the "
        "project should be private or public. The call waits for a second person to approve."
    ),
    "parameter_hints": {
        "project_name": "A new name in lower case, for example demo-images.",
        "public": "Leave it false for a private project. Set it true only when asked.",
        "storage_limit": "Bytes. Leave it out unless the user names a limit.",
    },
    "output_shape": (
        "{project_name, created, already_existed, read_back}. created is true when this "
        "call made the project. When the name is taken, created is false and nothing "
        "changed. The existing project may be set up differently from what you asked for."
    ),
    "next_step": (
        "Call harbor.project.info with the same project_name to read the project back and "
        "confirm that it is private."
    ),
}


async def harbor_project_create_impl(
    connector: HarborConnector,
    operator: Operator,
    target: HarborTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create the project with ``POST /api/v2.0/projects``; report a taken name as no-op."""
    project_name = str(params["project_name"])
    public = bool(params.get("public", False))
    body: dict[str, Any] = {
        "project_name": project_name,
        "metadata": {"public": "true" if public else "false"},
    }
    if params.get("storage_limit") is not None:
        body["storage_limit"] = int(params["storage_limit"])
    try:
        await connector._post_json(target, _PROJECTS_PATH, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != httpx.codes.CONFLICT:
            raise
        created = False
    else:
        created = True
    return {
        "project_name": project_name,
        "created": created,
        "already_existed": not created,
        "read_back": "harbor.project.info",
    }


async def register_harbor_project_create_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``harbor.project.create`` into ``endpoint_descriptor``.

    Called once per process from the FastAPI lifespan through
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    ``embedding_service`` is the test seam the other Harbor registrars have.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.project.create",
        handler=HarborConnector.project_create,
        summary="Create a Harbor project (private by default), after approval.",
        description=(
            "Creates a project with POST /api/v2.0/projects. Private unless public is true. "
            "Safe to run twice: a taken name gives created false and changes nothing. "
            "Waits for approval. Read it back with harbor.project.info."
        ),
        parameter_schema=_PROJECT_CREATE_PARAMETER_SCHEMA,
        response_schema=_PROJECT_CREATE_RESPONSE_SCHEMA,
        group_key="harbor-projects",
        when_to_use=_HARBOR_PROJECTS_WHEN_TO_USE,
        tags=["write", "harbor", "project"],
        safety_level="caution",
        requires_approval=True,
        changes_rights=True,
        llm_instructions=_PROJECT_CREATE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
