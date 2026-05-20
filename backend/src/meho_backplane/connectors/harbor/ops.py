# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Harbor robot lifecycle typed ops (G3.5-T9 #621).

Registers two scoped-write ops against ``connector_id="harbor-rest-2.x"``:

* ``harbor.robot.create`` — create a project-scoped robot account.
  Returns the minted secret in its response payload; classified
  ``credential_mint`` by :func:`~meho_backplane.broadcast.events.classify_op`
  so the broadcast collapses to aggregate-only and the secret never
  appears in the SSE stream.

* ``harbor.robot.delete`` — delete a project-scoped robot account by
  numeric ID. Classified ``write`` (suffix-based). No secret material
  in the response payload.

Both ops use non-retried HTTP calls (Harbor write endpoints are
non-idempotent). ``harbor.robot.create`` calls
:meth:`~meho_backplane.connectors.harbor.connector.HarborConnector._post_json`;
``harbor.robot.delete`` calls the pooled httpx client directly (DELETE
is not in ``_IDEMPOTENT_METHODS`` and ``HttpConnector`` provides no
``_delete_json`` helper — calling the client directly keeps the no-retry
contract explicit).

Handler methods live on
:class:`~meho_backplane.connectors.harbor.connector.HarborConnector`
(``robot_create`` / ``robot_delete``) so the dispatcher's
:func:`~meho_backplane.operations.dispatcher._maybe_bind_method` can
bind them to the per-process connector instance at dispatch time.
This mirrors the bind9 / Kubernetes pattern for connectors whose
handlers need the HTTP transport.

Registration is triggered at lifespan startup via
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`;
the registrar is queued in
:mod:`~meho_backplane.connectors.harbor.__init__` alongside the
connector-registry entry.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["register_harbor_robot_operations"]


# ---------------------------------------------------------------------------
# harbor.robot.create
# ---------------------------------------------------------------------------

_HARBOR_ROBOT_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "pattern": "^(?=.*\\S)[a-zA-Z0-9_-]+$",
            "description": (
                "Robot account name (alphanumeric, hyphens, underscores). "
                "Harbor prefixes it as 'robot$<project>+<name>' in the response."
            ),
        },
        "project": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Harbor project name that scopes this robot account.",
        },
        "duration": {
            "type": "integer",
            "description": (
                "Validity period in days. -1 means the account never expires. "
                "Prefer a short duration for temporary access; use -1 for "
                "long-lived CI credentials only."
            ),
            "default": -1,
        },
    },
    "required": ["name", "project", "duration"],
    "additionalProperties": False,
}

_HARBOR_ROBOT_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "integer",
            "description": "Harbor-assigned numeric robot account ID.",
        },
        "name": {
            "type": "string",
            "description": (
                "Harbor-formatted robot name (e.g. 'robot$project+name'). "
                "Use this as the username when authenticating with Harbor."
            ),
        },
        "secret": {
            "type": "string",
            "description": (
                "Minted robot credential. Returned ONLY on creation — "
                "Harbor does not expose it again after this call. "
                "Store it immediately and treat it as a password."
            ),
        },
    },
    "required": ["id", "name", "secret"],
}

_HARBOR_ROBOT_CREATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a project-scoped robot account in Harbor. Use when the operator "
        "needs a machine credential (CI/CD push/pull token, automated deployment "
        "credential) for a specific Harbor project. "
        "IMPORTANT: The response payload contains a freshly-minted secret credential "
        "that is returned ONLY once and cannot be retrieved later. The caller receives "
        "the full response including the secret; the broadcast feed does NOT "
        "(credential_mint classification keeps it aggregate-only). "
        "Store the secret immediately after this call."
    ),
    "parameter_hints": {
        "name": (
            "Robot account name — alphanumeric, hyphens, underscores. "
            "Harbor prefixes it as 'robot$<project>+<name>' in the response; "
            "supply only the suffix (e.g. 'ci-push', not 'robot$myproject+ci-push')."
        ),
        "project": "Harbor project name. The robot is scoped to this project only.",
        "duration": (
            "Validity in days. -1 = never expires. "
            "Prefer a short duration (e.g. 90) for temporary access. "
            "Use -1 only for long-lived CI credentials."
        ),
    },
    "output_shape": (
        "On success: {id: <int>, name: 'robot$<project>+<name>', secret: '<minted-secret>'}. "
        "The secret is a one-time value — save it immediately. "
        "On failure: a connector_error OperationResult with "
        "extras.exception_class naming the failure class."
    ),
}


# ---------------------------------------------------------------------------
# harbor.robot.delete
# ---------------------------------------------------------------------------

_HARBOR_ROBOT_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Harbor project name that scopes the robot account.",
        },
        "id": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Numeric robot account ID (returned by harbor.robot.create). "
                "Harbor's delete endpoint is keyed on the numeric ID, not the name."
            ),
        },
    },
    "required": ["project", "id"],
    "additionalProperties": False,
}

_HARBOR_ROBOT_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "integer",
            "description": "Echo of the deleted robot account ID.",
        },
        "deleted": {
            "type": "boolean",
            "description": "Always true on success.",
        },
    },
    "required": ["id", "deleted"],
}

_HARBOR_ROBOT_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete a project-scoped robot account from Harbor by its numeric ID. "
        "Use when decommissioning a CI credential or revoking machine access "
        "to a Harbor project. Requires the numeric 'id' returned by "
        "harbor.robot.create — Harbor's delete endpoint is keyed on the ID, "
        "not the robot name. This operation is irreversible; the account and "
        "its associated secret are permanently removed."
    ),
    "parameter_hints": {
        "project": "Harbor project name that scopes the robot account.",
        "id": (
            "Numeric robot account ID from harbor.robot.create's response "
            "(the 'id' field). Not the Harbor-formatted name string."
        ),
    },
    "output_shape": (
        "On success: {id: <int>, deleted: true}. "
        "Harbor returns HTTP 200 with an empty body; the id echo is synthetic. "
        "On failure: a connector_error OperationResult with "
        "extras.exception_class naming the failure class."
    ),
}


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------


async def register_harbor_robot_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert harbor.robot.create and harbor.robot.delete into ``endpoint_descriptor``.

    Called once per process from the FastAPI lifespan via
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`.
    Idempotent: a second call with unchanged descriptions is a no-op for
    the embedding pipeline (body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

    Test seam: ``embedding_service`` lets fixtures inject a stub so
    chassis tests don't load the ONNX model.
    """
    from meho_backplane.connectors.harbor.connector import HarborConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.robot.create",
        handler=HarborConnector.robot_create,
        summary="Create a project-scoped robot account in Harbor.",
        description=(
            "Creates a project-scoped robot account via "
            "POST /api/v2.0/projects/{project}/robots. "
            "The response payload contains a freshly-minted secret credential "
            "returned ONLY on creation — Harbor does not expose it again. "
            "Classified credential_mint: the broadcast collapses to aggregate-only "
            "so the secret never appears in the SSE feed. "
            "Non-idempotent — never auto-retried by the HttpConnector "
            "tenacity decorator. safety_level=caution."
        ),
        parameter_schema=_HARBOR_ROBOT_CREATE_PARAMETER_SCHEMA,
        response_schema=_HARBOR_ROBOT_CREATE_RESPONSE_SCHEMA,
        group_key="robot",
        tags=["write", "credential-mint"],
        safety_level="caution",
        requires_approval=False,
        llm_instructions=_HARBOR_ROBOT_CREATE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )

    await register_typed_operation(
        product="harbor",
        version="2.x",
        impl_id="harbor-rest",
        op_id="harbor.robot.delete",
        handler=HarborConnector.robot_delete,
        summary="Delete a project-scoped robot account from Harbor.",
        description=(
            "Deletes a project-scoped robot account via "
            "DELETE /api/v2.0/projects/{project}/robots/{id}. "
            "Requires the numeric robot ID (returned by harbor.robot.create). "
            "Non-idempotent write — permanent removal. safety_level=caution."
        ),
        parameter_schema=_HARBOR_ROBOT_DELETE_PARAMETER_SCHEMA,
        response_schema=_HARBOR_ROBOT_DELETE_RESPONSE_SCHEMA,
        group_key="robot",
        tags=["write", "destructive"],
        safety_level="caution",
        requires_approval=False,
        llm_instructions=_HARBOR_ROBOT_DELETE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
