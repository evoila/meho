# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operation table, group texts and registrar of the Azure connector.

The Azure connector has typed operations only. Azure publishes its API
descriptions as Swagger 2.0, which MEHO's spec ingestion does not read, and
hand-written operations give clear groups, risk levels and guidance.

The operations are registered at start-up with ``source_kind="typed"``, so
they work on a fresh install with nothing to ingest. The bodies live in
:mod:`meho_backplane.connectors.azure.typed_reads`, the guidance for the AI in
:mod:`meho_backplane.connectors.azure._llm_instructions`. Thin methods on
:class:`~meho_backplane.connectors.azure.connector.AzureConnector` expose them
under the ``handler_attr`` names below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.azure import network
from meho_backplane.connectors.azure._llm_instructions import AZURE_READ_LLM_INSTRUCTIONS
from meho_backplane.connectors.azure._model import AzureTypedOp

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "AZURE_TYPED_OPS",
    "AZURE_TYPED_WHEN_TO_USE_BY_GROUP",
    "AzureTypedOp",
    "register_azure_typed_operations",
]

_log = structlog.get_logger(__name__)

_GROUP_ACCOUNT: Final[str] = "account"


#: The text of each group. The AI reads it through ``list_operation_groups``
#: to choose where to search.
AZURE_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_ACCOUNT: (
        "Start here for an Azure target. azure.about reads the subscription (ID, name "
        "and state), its tenant and cloud, and the app ID and object ID of the login "
        "MEHO uses. Use it first for a new target, after a secret change, or when "
        "another Azure read fails with an access or login error. "
        "azure.operation.status reads the state of a long-running Azure operation "
        "once, from the operation reference an Azure write returned; use it after a "
        "lost answer or a restart instead of sending the write again. Read-only."
    ),
    **network.WHEN_TO_USE,
}

_NO_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_TEXT_OR_NULL: Final[list[str]] = ["string", "null"]

_ABOUT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "subscription_id": {
            "type": _TEXT_OR_NULL,
            "description": "The subscription ID (a GUID).",
        },
        "display_name": {
            "type": _TEXT_OR_NULL,
            "description": "The subscription name, as set in Azure.",
        },
        "state": {
            "type": _TEXT_OR_NULL,
            "description": "Enabled, Warned, PastDue, Disabled or Deleted.",
        },
        "tenant_id": {
            "type": _TEXT_OR_NULL,
            "description": "The tenant (directory) ID the subscription belongs to.",
        },
        "cloud": {
            "type": "string",
            "description": "The Azure cloud: AzureCloud, AzureUSGovernment or AzureChinaCloud.",
        },
        "app_id": {
            "type": _TEXT_OR_NULL,
            "description": "Application (client) ID of the login, read from the access token.",
        },
        "object_id": {
            "type": _TEXT_OR_NULL,
            "description": "Object ID of the login's service principal, read from the token.",
        },
    },
    "required": ["subscription_id", "cloud"],
}

_READ_TAGS: Final[tuple[str, ...]] = ("read-only", "azure")

_EXAMPLE_POLL_URL: Final[str] = (
    "https://management.azure.com/subscriptions/00000000-0000-0000-0000-000000000000"
    "/providers/Microsoft.Compute/locations/westeurope/operations/"
    "11111111-0000-0000-0000-000000000000?api-version=2024-07-01"
)

_OPERATION_REFERENCE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "The operation reference an Azure write returned (its 'operation' field).",
    "properties": {
        "poll_url": {
            "type": "string",
            "maxLength": 4096,
            "description": "The URL Azure named for polling, as MEHO returned it.",
        },
        "poll_kind": {
            "type": "string",
            "enum": ["azure-asyncoperation", "location"],
            "description": "Which Azure header the URL came from.",
        },
        "resource_id": {
            "type": _TEXT_OR_NULL,
            "description": "The Azure resource ID the operation changes, when known.",
        },
        "started_at": {
            "type": _TEXT_OR_NULL,
            "description": "When MEHO started the operation (ISO 8601).",
        },
        "retry_after_seconds": {
            "type": ["number", "null"],
            "description": "The Retry-After of the answer that started the operation.",
        },
    },
    "required": ["poll_url", "poll_kind"],
}

_OPERATION_STATUS_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"operation": _OPERATION_REFERENCE_SCHEMA},
    "required": ["operation"],
    "additionalProperties": False,
    "examples": [
        {
            "operation": {
                "poll_url": _EXAMPLE_POLL_URL,
                "poll_kind": "azure-asyncoperation",
                "resource_id": None,
                "started_at": "2026-10-27T09:00:00+00:00",
                "retry_after_seconds": 30,
            }
        }
    ],
}

_OPERATION_STATUS_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "Succeeded, Failed, Canceled, or a running state such as InProgress.",
        },
        "done": {
            "type": "boolean",
            "description": "True when the status is Succeeded, Failed or Canceled.",
        },
        "error": {
            "type": ["object", "null"],
            "description": "Azure's error code and message when the operation failed.",
            "properties": {
                "code": {"type": _TEXT_OR_NULL, "description": "Azure's error code."},
                "message": {"type": _TEXT_OR_NULL, "description": "Azure's error text."},
            },
        },
        "result": {
            "type": ["object", "null"],
            "description": "What Azure returned when the operation succeeded, if anything.",
        },
        "retry_after_seconds": {
            "type": ["number", "null"],
            "description": "How long Azure asks to wait before the next poll.",
        },
        "operation": {
            "type": "object",
            "description": "The operation reference, to poll again later.",
            "properties": {
                "poll_url": {"type": "string", "description": "The poll URL."},
                "poll_kind": {"type": "string", "description": "The kind of poll URL."},
                "resource_id": {"type": _TEXT_OR_NULL, "description": "The resource ID."},
                "started_at": {"type": _TEXT_OR_NULL, "description": "The start time."},
                "retry_after_seconds": {
                    "type": ["number", "null"],
                    "description": "The Retry-After of the answer that started the operation.",
                },
            },
        },
    },
    "required": ["status", "done", "operation"],
}


#: Every Azure typed operation. Reads are ``safe`` and need no approval.
AZURE_TYPED_OPS: tuple[AzureTypedOp, ...] = (
    AzureTypedOp(
        op_id="azure.about",
        handler_attr="about",
        summary="Read the Azure subscription of this target and the login MEHO uses for it.",
        description=(
            "Reads GET /subscriptions/{subscriptionId} on Azure Resource Manager. "
            "Returns the subscription ID, name and state, the tenant ID, the cloud, "
            "and the app ID and object ID of the login. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_ABOUT_RESPONSE,
        group_key=_GROUP_ACCOUNT,
        tags=(*_READ_TAGS, "subscription", "identity", "about"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=AZURE_READ_LLM_INSTRUCTIONS["azure.about"],
    ),
    AzureTypedOp(
        op_id="azure.operation.status",
        handler_attr="operation_status",
        summary="Read the state of a long-running Azure operation once, without sending it again.",
        description=(
            "Polls the URL of an operation reference once (GET) and returns the state: "
            "Succeeded, Failed, Canceled or still running. The URL must be on the "
            "target's Resource Manager host and in its subscription. Read-only."
        ),
        parameter_schema=_OPERATION_STATUS_PARAMS,
        response_schema=_OPERATION_STATUS_RESPONSE,
        group_key=_GROUP_ACCOUNT,
        tags=(*_READ_TAGS, "operation", "long-running", "status"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=AZURE_READ_LLM_INSTRUCTIONS["azure.operation.status"],
    ),
    *network.OPS,
)


async def register_azure_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Store every operation of :data:`AZURE_TYPED_OPS` in ``endpoint_descriptor``.

    Runs at start-up after every connector package was imported. Safe to run
    twice: an unchanged operation is not embedded again.
    """
    # Imported here: the operations package loads the embedding model, which
    # connector unit tests should not pay for.
    from meho_backplane.connectors.azure.connector import AzureConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in AZURE_TYPED_OPS:
        handler = getattr(AzureConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"Azure typed op {op.op_id!r} names handler_attr={op.handler_attr!r}, "
                "but AzureConnector has no such method"
            )
        when_to_use = AZURE_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"Azure typed op {op.op_id!r} names group_key={op.group_key!r}, which has "
                "no text in AZURE_TYPED_WHEN_TO_USE_BY_GROUP"
            )
        await register_typed_operation(
            product=AzureConnector.product,
            version=AzureConnector.version,
            impl_id=AzureConnector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "azure_typed_operations_registered",
        count=len(AZURE_TYPED_OPS),
        product=AzureConnector.product,
        version=AzureConnector.version,
        impl_id=AzureConnector.impl_id,
    )
