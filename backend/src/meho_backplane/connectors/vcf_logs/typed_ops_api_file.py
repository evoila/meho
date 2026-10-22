# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of ``vrli.api_file.get``, the typed read of the API reference pages.

The body of the read lives in :mod:`meho_backplane.connectors.vcf_logs.api_file`;
this module holds what the operation tells the agent (summary, schemas,
guidance) and the text of its group. The ``typed_ops`` module of this package
imports it late and adds the op to ``VRLI_TYPED_OPS``, so both Logs
implementations (9.0 and 8.x) register it.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_logs.typed_ops import VrliTypedOp

__all__ = [
    "API_FILE_GROUP_KEY",
    "API_FILE_WHEN_TO_USE",
    "VRLI_API_FILE_GET_OP",
]

API_FILE_GROUP_KEY = "vrli-api-docs"

#: What the group holds. It makes no claim about the safety of the whole group.
API_FILE_WHEN_TO_USE = (
    "Use to read the API reference pages that a running Logs system serves: "
    "the page at /rest-api, the pages under /rest-api/ (for example "
    "/rest-api/supported, the Tech Preview API pages) and the Swagger or "
    "OpenAPI files they load (vrli.api_file.get). The read returns the file's "
    "size, SHA-256 and the system version, with the text in parts behind a "
    "result handle, so a vendor API file can be pinned and compared. It refuses "
    "every other path."
)

_PART_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "description": "Position of the part, from 0."},
        "text": {"type": "string", "description": "The part's text, at most 64 KiB."},
    },
}

VRLI_API_FILE_GET_OP = VrliTypedOp(
    op_id="vrli.api_file.get",
    handler_attr="api_file_get",
    summary="Read one API reference page or API description file the Logs system serves.",
    description=(
        "Reads one API reference page or Swagger/OpenAPI file from the Logs system "
        "and returns its path, HTTP status, content type, size, SHA-256 and the "
        "system version. The text comes back in parts behind a result handle. "
        "Only the reference paths and the usual API file paths are allowed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path on the Logs system, starting with '/'. Allowed: /rest-api and "
                    "paths under /rest-api/, or a Swagger/OpenAPI file path (a part named "
                    "api-docs, swagger-resources or swagger-config, or a name ending in "
                    "swagger.json, swagger.yaml, openapi.json or openapi.yaml). A query "
                    "string may hold only group and apiVersion. No '..', '//', ';', "
                    "scheme or host."
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
            "status": {"type": "integer", "description": "HTTP status the system returned."},
            "content_type": {
                "type": ["string", "null"],
                "description": "Content-Type header of the answer.",
            },
            "size": {"type": "integer", "description": "Size of the file in bytes."},
            "sha256": {"type": "string", "description": "SHA-256 of the file, hex."},
            "version": {
                "type": ["string", "null"],
                "description": (
                    "Logs system version, as the connector's version detection reports it."
                ),
            },
            "build": {
                "type": ["string", "null"],
                "description": "Logs system build number, when the system reports one.",
            },
            "version_error": {
                "type": ["string", "null"],
                "description": "Why the version read failed; null when it worked.",
            },
            "redirect_to": {
                "type": ["string", "null"],
                "description": (
                    "Path of a redirect answer, resolved against the path that was read; "
                    "null when the answer was not a redirect. Read that path next."
                ),
            },
            "parts": {"type": "integer", "description": "How many text parts there are."},
            "items": {
                "type": "array",
                "description": "The text parts in order; joining them gives the exact file.",
                "items": _PART_ITEM_SCHEMA,
            },
        },
    },
    group_key=API_FILE_GROUP_KEY,
    tags=("read-only", "vrli", "vcf-logs", "api-docs", "swagger", "openapi"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read an API reference page or API description file from a "
            "running Logs system, so the file can be pinned (size and SHA-256) or "
            "compared with a vendor file. Start with /rest-api; read /rest-api/supported "
            "for the Tech Preview API pages; then read the Swagger or OpenAPI files a "
            "page loads. A path outside the rule is refused before any request."
        ),
        "parameter_hints": {
            "path": (
                "One path, starting with '/'. /rest-api, a path under /rest-api/, or "
                "a Swagger/OpenAPI file path. No scheme, host, '..' or '//'."
            ),
        },
        "output_shape": (
            "{path, status, content_type, size, sha256, version, build, version_error, "
            "redirect_to, parts, items: [{index, text}]}. A short page comes back inline; "
            "a longer file returns a result handle with the scalars inline and the parts "
            "as rows. Read the rows with result_query in index order and join their text "
            "to rebuild the exact file; its SHA-256 equals sha256."
        ),
        "next_step": (
            "If status is 3xx, read redirect_to next. If status is 404, the page is not "
            "served at this address (the reference is on the web address, not the API "
            "port); say so. If the file is too large for one result handle, the read "
            "stops with an error that names the size. If version_error is set, the file "
            "is still complete; report the reason."
        ),
    },
)
