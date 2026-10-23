# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`SrmConnector`.

Two reads ship here, on SRM 8.8, Live Site Recovery 9.0 and VCF 9.1
Protection and Recovery:

* ``srm.about`` -- ``GET /api/rest/srm/v2/server/info``: the server's
  name, status, version, build and paired vCenter;
* ``srm.api_file.get`` -- ``GET`` one of the two API files the appliance
  serves; the body lives in :mod:`meho_backplane.connectors.srm.api_file`.

The dataclass + tuple + registrar shape mirrors
:mod:`meho_backplane.connectors.hcx.typed_ops`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "SRM_TYPED_OPS",
    "SRM_TYPED_WHEN_TO_USE_BY_GROUP",
    "SrmTypedOp",
    "register_srm_typed_operations",
]

_GROUP_SERVER = "srm-server"
_GROUP_API_DOCS = "srm-api-docs"


@dataclass(frozen=True)
class SrmTypedOp:
    """Metadata for one SRM typed op registered at lifespan startup."""

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` text per group. Says what the group holds; it
#: makes no claim about the safety of the whole group.
SRM_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_SERVER: (
        "Use to see which Site Recovery Manager server a target is: its name, "
        "status, version, build and the vCenter it is paired with, plus a short "
        "view of its certificates (srm.about). One read."
    ),
    _GROUP_API_DOCS: (
        "Use to read an API description file that a running Site Recovery Manager "
        "appliance serves: the Site Recovery API file or the appliance setup API "
        "file (srm.api_file.get). The read returns a checked copy of the file with "
        "its size, SHA-256 and the SRM version, with the text in parts behind a "
        "result handle, so a vendor API file can be pinned and compared. One read."
    ),
}

_CERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Short view of one certificate.",
    "properties": {
        "subject_dn": {"type": ["string", "null"], "description": "Subject name."},
        "issuer_dn": {"type": ["string", "null"], "description": "Issuer name."},
        "serial_number": {"type": ["string", "null"], "description": "Serial number."},
        "not_before": {
            "type": ["integer", "null"],
            "description": "Start of validity, in milliseconds since 1970.",
        },
        "not_after": {
            "type": ["integer", "null"],
            "description": "End of validity, in milliseconds since 1970.",
        },
        "thumbprint": {"type": ["string", "null"], "description": "Certificate thumbprint."},
    },
}

_ABOUT = SrmTypedOp(
    op_id="srm.about",
    handler_attr="about",
    summary="Read the Site Recovery Manager server's name, status, version and build.",
    description=(
        "Reads GET /api/rest/srm/v2/server/info: server id, name, status, version, "
        "build, the paired vCenter and a short view of the certificates. MEHO uses "
        "the same call to detect the SRM version."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "id": {"type": ["string", "null"], "description": "Server id."},
            "name": {"type": ["string", "null"], "description": "Server name."},
            "url": {"type": ["string", "null"], "description": "Server address."},
            "server_status": {
                "type": ["string", "null"],
                "enum": ["OK", "WARNING", "ERROR", None],
                "description": "Server status.",
            },
            "version": {"type": ["string", "null"], "description": "SRM version."},
            "build_number": {"type": ["string", "null"], "description": "SRM build number."},
            "vc_guid": {
                "type": ["string", "null"],
                "description": "Id of the vCenter the server is paired with.",
            },
            "vc_name": {
                "type": ["string", "null"],
                "description": "Name of the vCenter the server is paired with.",
            },
            "extension_key": {
                "type": ["string", "null"],
                "description": "Key the server is registered with in vCenter.",
            },
            "certificates": {
                "type": "array",
                "description": "The server's certificates, short view.",
                "items": _CERT_SCHEMA,
            },
            "certificate_warnings": {
                "type": "array",
                "description": "Certificate warnings the server reports.",
                "items": {
                    "type": "object",
                    "description": "One certificate warning.",
                    "properties": {
                        "server_guid": {
                            "type": ["string", "null"],
                            "description": "Id of the server the warning is about.",
                        },
                        "server_url": {
                            "type": ["string", "null"],
                            "description": "Address of the server the warning is about.",
                        },
                        "certificate": _CERT_SCHEMA,
                    },
                },
            },
        },
    },
    group_key=_GROUP_SERVER,
    tags=("read-only", "srm", "live-recovery", "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a Site Recovery Manager target, to confirm which server it "
            "is and which version and build it runs (8.8, 9.0 or 9.1), and whether its "
            "status is OK."
        ),
        "output_shape": (
            "{id, name, url, server_status, version, build_number, vc_guid, vc_name, "
            "extension_key, certificates: [{subject_dn, issuer_dn, serial_number, "
            "not_before, not_after, thumbprint}], certificate_warnings: [{server_guid, "
            "server_url, certificate}]}. Dates are milliseconds since 1970."
        ),
        "next_step": (
            "If server_status is WARNING or ERROR, or certificate_warnings is not empty, "
            "report it. To read the vendor API file of this server, call srm.api_file.get."
        ),
    },
)

_PART_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One text part of the file.",
    "properties": {
        "index": {"type": "integer", "description": "Position of the part, from 0."},
        "text": {"type": "string", "description": "The part's text, at most 64 KiB."},
    },
}

_API_FILE_GET = SrmTypedOp(
    op_id="srm.api_file.get",
    handler_attr="api_file_get",
    summary="Read one API description file the Site Recovery Manager appliance serves.",
    description=(
        "Reads the Site Recovery API file or the appliance setup API file from the "
        "appliance and returns a checked copy: secret values are replaced. The answer "
        "has the path, HTTP status, content type, size, SHA-256 and the SRM version. "
        "The text comes back in parts behind a result handle."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "enum": ["srm", "configure"],
                "description": (
                    "Which file to read. srm: the Site Recovery API "
                    "(/api/rest/srm/v2/open-api.yaml). configure: the appliance setup "
                    "API (/api/rest/configure/v2/open-api.yaml)."
                ),
            },
        },
        "required": ["file"],
        "additionalProperties": False,
        "examples": [{"file": "srm"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "The file name that was asked for."},
            "path": {"type": "string", "description": "The path that was read."},
            "status": {"type": "integer", "description": "HTTP status the appliance returned."},
            "content_type": {
                "type": ["string", "null"],
                "description": "Content-Type header of the answer.",
            },
            "size": {"type": "integer", "description": "Size of the checked copy in bytes."},
            "sha256": {"type": "string", "description": "SHA-256 of the checked copy, hex."},
            "secrets_replaced": {
                "type": "integer",
                "description": (
                    "How many values were replaced in the copy: values under a secret name "
                    "(password, token, ...) by [REDACTED:param_name], and text that MEHO's "
                    "secret-redaction rules match (for example an Authorization header "
                    "example) by a [REDACTED:<kind>] placeholder."
                ),
            },
            "version": {
                "type": ["string", "null"],
                "description": "SRM version as the server reports it.",
            },
            "build": {
                "type": ["string", "null"],
                "description": "SRM build number as the server reports it.",
            },
            "version_error": {
                "type": ["string", "null"],
                "description": "Why the version read failed; null when it worked.",
            },
            "redirect_to": {
                "type": ["string", "null"],
                "description": "Path of a redirect answer. Not followed.",
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
    tags=("read-only", "srm", "live-recovery", "api-docs", "documentation"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read the API file of a Site Recovery Manager appliance, for "
            "example to pin the vendor API file of an SRM version or to check which "
            "API calls a version serves."
        ),
        "output_shape": (
            "{file, path, status, content_type, size, sha256, secrets_replaced, version, "
            "build, version_error, redirect_to, parts, items: [{index, text}]}. items hold "
            "the checked copy of the file in parts of at most 64 KiB, in order; a long file "
            "is behind a result handle. The copy is the parsed file written back as YAML or "
            "JSON, with every value under a secret name replaced by [REDACTED:param_name] "
            "and text that MEHO's secret-redaction rules match (such as an Authorization "
            "header example) replaced by a [REDACTED:<kind>] placeholder; secrets_replaced "
            "counts them. Join the parts in order to rebuild the copy; its SHA-256 equals "
            "sha256."
        ),
        "next_step": (
            "If status is not 200, read the text for the appliance's message. To "
            "rebuild the file, page the result handle with result_query in index order "
            "and join the text fields. A file that does not parse is refused; report it "
            "instead of retrying. A read that stops because the secret-redaction rules "
            "would still change the copy cannot return it; report it instead of retrying."
        ),
        "result_scalars": {
            "keys": [
                "file",
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

SRM_TYPED_OPS: tuple[SrmTypedOp, ...] = (_ABOUT, _API_FILE_GET)


async def register_srm_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`SRM_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list in this package's
    ``__init__``; the runner passes the process-wide embedding service
    (or a test stub), which is forwarded to ``register_typed_operation``.
    """
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in SRM_TYPED_OPS:
        handler = getattr(SrmConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"SrmConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = (
            None if op.group_key is None else SRM_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        )
        if op.group_key is not None and when_to_use is None:
            raise ValueError(
                f"SrmConnector typed op {op.op_id!r} declares group_key={op.group_key!r} "
                "but no curated when_to_use exists for that key."
            )
        await register_typed_operation(
            product=SrmConnector.product,
            version=SrmConnector.version,
            impl_id=SrmConnector.impl_id,
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
