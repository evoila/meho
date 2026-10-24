# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas shared by the SRM and vSphere Replication typed reads.

Both APIs return the same server summary and both ``api_file.get`` reads
return the same answer (:mod:`meho_backplane.connectors._shared.dr_rest`,
:mod:`meho_backplane.connectors._shared.dr_api_file`). The builders take
the product name, so each connector's text names its own product.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "API_FILE_NEXT_STEP",
    "API_FILE_OUTPUT_SHAPE",
    "API_FILE_RESULT_SCALARS",
    "SERVER_INFO_OUTPUT_SHAPE",
    "api_file_response_schema",
    "server_info_response_schema",
]

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

_PART_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One text part of the file.",
    "properties": {
        "index": {"type": "integer", "description": "Position of the part, from 0."},
        "text": {"type": "string", "description": "The part's text, at most 64 KiB."},
    },
}

#: The scalar fields of an ``api_file.get`` answer (``llm_instructions.result_scalars``).
API_FILE_RESULT_SCALARS: tuple[str, ...] = (
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
)

#: ``llm_instructions.output_shape`` of ``<product>.about``.
SERVER_INFO_OUTPUT_SHAPE = (
    "{id, name, url, server_status, version, build_number, vc_guid, vc_name, "
    "extension_key, certificates: [{subject_dn, issuer_dn, serial_number, "
    "not_before, not_after, thumbprint}], certificate_warnings: [{server_guid, "
    "server_url, certificate}]}. Dates are milliseconds since 1970."
)

#: ``llm_instructions.output_shape`` of ``<product>.api_file.get``.
API_FILE_OUTPUT_SHAPE = (
    "{file, path, status, content_type, size, sha256, secrets_replaced, version, "
    "build, version_error, redirect_to, parts, items: [{index, text}]}. items hold "
    "the checked copy of the file in parts of at most 64 KiB, in order; a long file "
    "is behind a result handle. The copy is the parsed file written back as YAML or "
    "JSON, with every value under a secret name replaced by [REDACTED:param_name] "
    "and text that MEHO's secret-redaction rules match (such as an Authorization "
    "header example) replaced by a [REDACTED:<kind>] placeholder; secrets_replaced "
    "counts them. Join the parts in order to rebuild the copy; its SHA-256 equals "
    "sha256."
)

#: ``llm_instructions.next_step`` of ``<product>.api_file.get``.
API_FILE_NEXT_STEP = (
    "If status is not 200, read the text for the appliance's message. To "
    "rebuild the file, page the result handle with result_query in index order "
    "and join the text fields. A file that does not parse is refused; report it "
    "instead of retrying. A read that stops because the secret-redaction rules "
    "would still change the copy cannot return it; report it instead of retrying."
)


def server_info_response_schema(product: str, vcenter: str) -> dict[str, Any]:
    """Response schema of ``<product>.about``.

    *product* names the product (``"SRM"``); *vcenter* says which vCenter
    ``vc_guid`` and ``vc_name`` name (``"the vCenter the server is paired
    with"``).
    """
    return {
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
            "version": {"type": ["string", "null"], "description": f"{product} version."},
            "build_number": {
                "type": ["string", "null"],
                "description": f"{product} build number.",
            },
            "vc_guid": {"type": ["string", "null"], "description": f"Id of {vcenter}."},
            "vc_name": {"type": ["string", "null"], "description": f"Name of {vcenter}."},
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
    }


def api_file_response_schema(product: str) -> dict[str, Any]:
    """Response schema of ``<product>.api_file.get``; *product* names the product (``"SRM"``)."""
    return {
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
                "description": f"{product} version as the server reports it.",
            },
            "build": {
                "type": ["string", "null"],
                "description": f"{product} build number as the server reports it.",
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
    }
