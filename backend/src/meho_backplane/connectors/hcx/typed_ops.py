# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`HcxConnector`.

``hcx.api_file.get`` is defined here (its body lives in
:mod:`meho_backplane.connectors.hcx.api_file`); the reads of the manager,
site pairings, service mesh, jobs, alerts and inventory are defined in
:mod:`meho_backplane.connectors.hcx.read_ops`,
:mod:`meho_backplane.connectors.hcx.diagnostics_ops` and
:mod:`meho_backplane.connectors.hcx.inventory_ops`; the profile, site pairing
and certificate changes in :mod:`meho_backplane.connectors.hcx.profile_ops` and
:mod:`meho_backplane.connectors.hcx.site_pairing_ops`; the service mesh changes
and the task read in :mod:`meho_backplane.connectors.hcx.service_mesh_ops`; the
network extension operations in
:mod:`meho_backplane.connectors.hcx.network_extension_ops`; the migration
operations in :mod:`meho_backplane.connectors.hcx.migration_ops`. The dataclass +
tuple + registrar shape mirrors :mod:`meho_backplane.connectors.nsx.typed_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.hcx.diagnostics_ops import HCX_DIAGNOSTICS_OPS
from meho_backplane.connectors.hcx.inventory_ops import HCX_INVENTORY_OPS
from meho_backplane.connectors.hcx.migration_ops import (
    HCX_MIGRATION_OPS,
    HCX_MIGRATION_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.hcx.network_extension_ops import (
    HCX_NETWORK_EXTENSION_OPS,
    HCX_NETWORK_EXTENSION_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.hcx.op_types import HcxTypedOp
from meho_backplane.connectors.hcx.profile_ops import (
    HCX_PROFILE_OPS,
    HCX_PROFILE_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.hcx.read_ops import HCX_READ_OPS, HCX_READ_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.hcx.service_mesh_ops import HCX_SERVICE_MESH_OPS
from meho_backplane.connectors.hcx.site_pairing_ops import HCX_SITE_PAIRING_OPS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "HCX_TYPED_OPS",
    "HCX_TYPED_WHEN_TO_USE_BY_GROUP",
    "HcxTypedOp",
    "register_hcx_typed_operations",
]

_GROUP_API_DOCS = "hcx-api-docs"


#: Curated ``when_to_use`` text per group. Says what the group holds; it
#: makes no claim about the safety of the whole group.
HCX_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_API_DOCS: (
        "Use to read the API documentation that a running HCX Manager serves: "
        "the documentation page under /hybridity/docs and the Swagger or "
        "OpenAPI files it loads (hcx.api_file.get). The read returns the "
        "file's size, SHA-256 and the Manager version, with the text in parts "
        "behind a result handle, so a vendor API file can be pinned and "
        "compared. It refuses every other path."
    ),
    **HCX_READ_WHEN_TO_USE_BY_GROUP,
    **HCX_PROFILE_WHEN_TO_USE_BY_GROUP,
    **HCX_NETWORK_EXTENSION_WHEN_TO_USE_BY_GROUP,
    **HCX_MIGRATION_WHEN_TO_USE_BY_GROUP,
}

_PART_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "description": "Position of the part, from 0."},
        "text": {"type": "string", "description": "The part's text, at most 64 KiB."},
    },
}

_API_FILE_GET = HcxTypedOp(
    op_id="hcx.api_file.get",
    handler_attr="api_file_get",
    summary="Read one API documentation page or API description file the HCX Manager serves.",
    description=(
        "Reads one documentation page or Swagger/OpenAPI file from the HCX Manager "
        "and returns its path, HTTP status, content type, size, SHA-256 and the "
        "Manager version. The text comes back in parts behind a result handle. "
        "Only the documentation paths and the usual API file paths are allowed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path on the HCX Manager, starting with '/'. Allowed: /hybridity/docs "
                    "and paths under /hybridity/docs/, or a Swagger/OpenAPI file path (a part "
                    "named api-docs, swagger-resources or swagger-config, or a name ending in "
                    "swagger.json, swagger.yaml, openapi.json or openapi.yaml). A query string "
                    "may hold only group and apiVersion. No '..', '//', ';', scheme or host."
                ),
            },
        },
        "required": ["path"],
        "additionalProperties": False,
        "examples": [{"path": "<path>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path that was read."},
            "status": {"type": "integer", "description": "HTTP status the Manager returned."},
            "content_type": {
                "type": ["string", "null"],
                "description": "Content-Type header of the answer.",
            },
            "size": {"type": "integer", "description": "Size of the checked copy in bytes."},
            "sha256": {"type": "string", "description": "SHA-256 of the checked copy, hex."},
            "secrets_replaced": {
                "type": "integer",
                "description": (
                    "How many values under a secret name (password, token, licence key, "
                    "...) were replaced by [REDACTED:param_name] in the copy."
                ),
            },
            "version": {
                "type": ["string", "null"],
                "description": "HCX Manager version as the Manager reports it.",
            },
            "build": {
                "type": ["string", "null"],
                "description": "HCX Manager build number, when the Manager reports one.",
            },
            "version_error": {
                "type": ["string", "null"],
                "description": "Why the version read failed; null when it worked.",
            },
            "redirect_to": {
                "type": ["string", "null"],
                "description": (
                    "Path of a redirect answer, resolved against the path that was read; "
                    "read that path next. Not followed."
                ),
            },
            "parts": {"type": "integer", "description": "How many text parts there are."},
            "items": {
                "type": "array",
                "description": (
                    "The text parts of the checked copy, in order. Joined, they are the copy."
                ),
                "items": _PART_ITEM_SCHEMA,
            },
        },
    },
    group_key=_GROUP_API_DOCS,
    tags=("read-only", "hcx", "api-docs", "documentation"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read the HCX API documentation page (/hybridity/docs) or one of "
            "the Swagger/OpenAPI files it loads, for example to pin a vendor API file "
            "or to check which API calls an HCX Manager version serves."
        ),
        "output_shape": (
            "{path, status, content_type, size, sha256, secrets_replaced, version, build, "
            "version_error, redirect_to, parts, items: [{index, text}]}. items hold the "
            "checked copy of the file in parts of at most 64 KiB, in order; a long file is "
            "behind a result handle. The copy is the parsed file written back (JSON, YAML, "
            "XML or HTML) with every value under a secret name replaced by "
            "[REDACTED:param_name]; secrets_replaced counts them. Join the parts in order "
            "to rebuild the copy; its SHA-256 equals sha256."
        ),
        "next_step": (
            "If redirect_to is set, call again with that path. If status is not 200, "
            "read the text for the Manager's message. To rebuild the file, page the "
            "result handle with result_query in index order and join the text fields. "
            "A refused path means the path is outside the rule; fix the path. A file that "
            "does not parse, or is JavaScript, CSS or plain text, is refused; report it "
            "instead of retrying. A read that stops because the secret-redaction rules "
            "would change the copy cannot return it; report it instead of retrying."
        ),
        "result_scalars": {
            "keys": [
                "path",
                "status",
                "content_type",
                "size",
                "sha256",
                "secrets_replaced",
                "version",
                "build",
                "version_error",
                "redirect_to",
                "parts",
            ]
        },
    },
)

HCX_TYPED_OPS: tuple[HcxTypedOp, ...] = (
    _API_FILE_GET,
    *HCX_READ_OPS,
    *HCX_DIAGNOSTICS_OPS,
    *HCX_INVENTORY_OPS,
    *HCX_PROFILE_OPS,
    *HCX_SITE_PAIRING_OPS,
    *HCX_SERVICE_MESH_OPS,
    *HCX_NETWORK_EXTENSION_OPS,
    *HCX_MIGRATION_OPS,
)


async def register_hcx_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`HCX_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list in this package's
    ``__init__``; the runner passes the process-wide embedding service
    (or a test stub), which is forwarded to ``register_typed_operation``.
    """
    from meho_backplane.connectors.hcx.connector import HcxConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in HCX_TYPED_OPS:
        handler = getattr(HcxConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"HcxConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else HCX_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"HcxConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                "but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=HcxConnector.product,
            version=HcxConnector.version,
            impl_id=HcxConnector.impl_id,
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
