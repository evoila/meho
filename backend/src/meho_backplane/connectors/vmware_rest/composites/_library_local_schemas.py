# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the local content-library changes.

For ``vmware.composite.content_library.create`` (:mod:`._library_local`) and
``vmware.composite.content_library.item.upload`` (:mod:`._library_upload`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import CONTENT_LIBRARY_ID_PATTERN

__all__ = [
    "CONTENT_LIBRARY_CREATE_LLM_INSTRUCTIONS",
    "CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA",
    "CONTENT_LIBRARY_CREATE_RESPONSE_SCHEMA",
    "CONTENT_LIBRARY_ITEM_UPLOAD_LLM_INSTRUCTIONS",
    "CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA",
    "CONTENT_LIBRARY_ITEM_UPLOAD_RESPONSE_SCHEMA",
    "DEFAULT_WAIT_SECONDS",
    "MAX_WAIT_SECONDS",
]

#: How long the upload reads the download state before it answers in_progress.
DEFAULT_WAIT_SECONDS: Final = 300
MAX_WAIT_SECONDS: Final = 1800

_NULLABLE_STR: Final = {"type": ["string", "null"]}
_GUIDANCE: Final = {**_NULLABLE_STR, "description": "What happened and what to do next."}
_ERROR_TYPE: Final = {**_NULLABLE_STR, "description": "vCenter error type, when vCenter said no."}

_LIBRARY_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Library id (a UUID)."},
        "name": {**_NULLABLE_STR, "description": "Library name."},
        "type": {**_NULLABLE_STR, "description": "LOCAL or SUBSCRIBED."},
    },
}

# ---------------------------------------------------------------------------
# content_library.create
# ---------------------------------------------------------------------------

CONTENT_LIBRARY_CREATE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "Name of the new library. If a library has this name, nothing is made.",
        },
        "datastore": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the datastore that stores the library files (exact match). A name "
                "that matches no datastore or more than one gives datastore_not_found or "
                "ambiguous_datastore, and nothing is made."
            ),
        },
        "description": {
            "type": "string",
            "description": "Optional text that says what the library is for.",
        },
    },
    "required": ["name", "datastore"],
    "additionalProperties": False,
    "examples": [{"name": "<library-name>", "datastore": "<datastore-name>"}],
}

CONTENT_LIBRARY_CREATE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "created",
                "unchanged",
                "precondition_failed",
                "datastore_not_found",
                "ambiguous_datastore",
                "refused",
                "not_verified",
            ],
            "description": (
                "created: the library exists and reads back. unchanged: a local library "
                "with this name already exists, nothing sent. precondition_failed: the "
                "name is used by other libraries, nothing sent. datastore_not_found / "
                "ambiguous_datastore: nothing sent. refused: vCenter said no, nothing "
                "changed. not_verified: sent, but the library does not read back."
            ),
        },
        "name": {"type": "string", "description": "The name from the call."},
        "library_id": {**_NULLABLE_STR, "description": "Id of the new or existing library."},
        "datastore_id": {**_NULLABLE_STR, "description": "Datastore id the library uses."},
        "type": {**_NULLABLE_STR, "description": "Library type as read back (LOCAL)."},
        "existing": {
            "type": "array",
            "items": _LIBRARY_ROW,
            "description": "Libraries that already had the name.",
        },
        "error_type": _ERROR_TYPE,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "name"],
}

CONTENT_LIBRARY_CREATE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to make a local content library, for example before you add ISO or OVA "
        "files with content_library.item.upload or deploy from it with "
        "vm.deploy_from_library. For a library that copies another (subscribed), use "
        "content_library.subscribed.create."
    ),
    "preconditions": (
        "Know the datastore name (vmware.composite.datastore.usage lists them). If a "
        "library already has the name, nothing is made. A second person must approve."
    ),
    "parameter_hints": {
        "name": "Library name; pick one no other library uses.",
        "datastore": "Exact datastore name, not the id.",
    },
    "output_shape": (
        "{status: created|unchanged|precondition_failed|datastore_not_found|"
        "ambiguous_datastore|refused|not_verified, name, library_id, datastore_id, type, "
        "existing: [{id, name, type}], error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a second call answers unchanged with the same library_id. Use "
        "library_id with content_library.item.upload. On precondition_failed, pick another "
        "name. Delete the library with content_library.delete."
    ),
}

# ---------------------------------------------------------------------------
# content_library.item.upload
# ---------------------------------------------------------------------------

CONTENT_LIBRARY_ITEM_UPLOAD_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_id": {
            "type": "string",
            "pattern": CONTENT_LIBRARY_ID_PATTERN,
            "description": "Id (a UUID) of the local library. If given, library_name is not used.",
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the local library; it must match exactly one library.",
        },
        "item_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "Name of the item in the library.",
        },
        "source_url": {
            "type": "string",
            "pattern": r"^[hH][tT][tT][pP][sS]?://[^\s/@]+(/\S*)?$",
            "description": (
                "http or https URL that vCenter downloads the file from. vCenter must be "
                "able to reach it. No user name or password in the URL."
            ),
        },
        "file_name": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^[^/\\]+$",
            "description": (
                "Name of the file in the item, e.g. 'installer.iso'. Default: the last part "
                "of the URL path."
            ),
        },
        "item_type": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Item type that tells vCenter how to read the file: 'iso' for an ISO, 'ovf' "
                "for an OVF or OVA. Leave out for a plain file."
            ),
        },
        "description": {"type": "string", "description": "Optional text for the item."},
        "ssl_certificate_thumbprint": {
            "type": "string",
            "minLength": 1,
            "description": (
                "SHA-1 thumbprint of the https server's certificate, for a server that "
                "vCenter does not trust yet."
            ),
        },
        "wait_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_WAIT_SECONDS,
            "default": DEFAULT_WAIT_SECONDS,
            "description": (
                "How long MEHO waits for the download before it answers in_progress. The "
                "download goes on in vCenter either way."
            ),
        },
    },
    "required": ["item_name", "source_url"],
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [
        {
            "library_name": "<library-name>",
            "item_name": "<item-name>",
            "source_url": "https://<depot-host>/iso/<file-name>.iso",
            "item_type": "iso",
        }
    ],
}

_ITEM_INFO: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "id": {"type": "string", "description": "Item id (a UUID)."},
        "name": {**_NULLABLE_STR, "description": "Item name."},
        "type": {**_NULLABLE_STR, "description": "Item type, e.g. iso or ovf."},
        "size_bytes": {"type": ["integer", "null"], "description": "Size of its files in bytes."},
        "cached": {
            "type": ["boolean", "null"],
            "description": "Whether the content is stored in this library.",
        },
    },
    "description": "The item as read back, or null.",
}

CONTENT_LIBRARY_ITEM_UPLOAD_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "uploaded",
                "unchanged",
                "in_progress",
                "upload_failed",
                "invalid_request",
                "library_not_found",
                "precondition_failed",
                "refused",
                "not_verified",
            ],
            "description": (
                "uploaded: the download is done and the item reads back. unchanged: the "
                "item has content already, nothing sent. in_progress: vCenter is still "
                "downloading. upload_failed: the download failed; the item stays empty. "
                "invalid_request / library_not_found / precondition_failed (not a local "
                "library): nothing sent. refused: vCenter said no to the item, nothing "
                "changed. not_verified: done, but the item does not read back."
            ),
        },
        "library_id": {**_NULLABLE_STR, "description": "Library id."},
        "item_id": {**_NULLABLE_STR, "description": "Item id."},
        "item_name": {**_NULLABLE_STR, "description": "The item name from the call."},
        "session_id": {**_NULLABLE_STR, "description": "Id of the vCenter update session."},
        "session_state": {
            **_NULLABLE_STR,
            "description": "Last session state: ACTIVE, DONE, ERROR or CANCELED.",
        },
        "item": _ITEM_INFO,
        "error_type": _ERROR_TYPE,
        "guidance": _GUIDANCE,
    },
    "required": ["status"],
}

CONTENT_LIBRARY_ITEM_UPLOAD_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to put an ISO, OVA or OVF file from a web server into a local content "
        "library, for example an installer ISO before you mount it on a VM, or an "
        "appliance OVA before vm.deploy_from_library. vCenter downloads the file itself."
    ),
    "preconditions": (
        "The library must be local (content_library.create makes one). vCenter must reach "
        "the URL; give ssl_certificate_thumbprint for an https server it does not trust. "
        "One file per call. A second person must approve."
    ),
    "parameter_hints": {
        "library_id": "Library id; library_name is not used when this is given.",
        "library_name": "Exact library name; it must match exactly one library.",
        "item_type": "'iso' for an ISO, 'ovf' for an OVF or OVA.",
        "wait_seconds": "0 to answer at once with in_progress; at most 1800.",
    },
    "output_shape": (
        "{status: uploaded|unchanged|in_progress|upload_failed|invalid_request|"
        "library_not_found|precondition_failed|refused|not_verified, library_id, item_id, "
        "item_name, session_id, session_state, item: {id, name, type, size_bytes, cached}, "
        "error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: an item that has content answers unchanged, and an empty item "
        "from a failed try is filled again. On in_progress, call again later with the same "
        "parameters. On upload_failed, read guidance, fix the URL or the server, and call "
        "again. Delete an item with content_library.item.delete."
    ),
}
