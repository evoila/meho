# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameters, answers and AI guidance of the content-library capture and export.

The four operations live in :mod:`._library_capture` (capture a VM, publish a
library) and :mod:`._library_export` (copy the files of an item, export a VM
as OVF files). This module holds only their schemas and ``llm_instructions``,
so the handler modules stay short.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_MOID_PATTERN,
    CONTENT_LIBRARY_ID_PATTERN,
    DATASTORE_MOID_PATTERN,
    HOST_MOID_PATTERN,
    VM_FOLDER_MOID_PATTERN,
    VM_MOID_PATTERN,
)

__all__ = [
    "CAPTURE_LLM_INSTRUCTIONS",
    "CAPTURE_PARAMETER_SCHEMA",
    "CAPTURE_RESPONSE_SCHEMA",
    "EXPORT_OVF_LLM_INSTRUCTIONS",
    "EXPORT_OVF_PARAMETER_SCHEMA",
    "EXPORT_RESPONSE_SCHEMA",
    "ITEM_EXPORT_LLM_INSTRUCTIONS",
    "ITEM_EXPORT_PARAMETER_SCHEMA",
    "PUBLISH_SET_LLM_INSTRUCTIONS",
    "PUBLISH_SET_PARAMETER_SCHEMA",
    "PUBLISH_SET_RESPONSE_SCHEMA",
    "TEMPLATE_OVF",
    "TEMPLATE_VM",
]

TEMPLATE_OVF: Final = "OVF"
TEMPLATE_VM: Final = "VM_TEMPLATE"

#: A neutral example id. An example must pass the UUID pattern of its schema.
_EXAMPLE_ITEM_ID: Final = "00000000-0000-4000-8000-000000000001"
_RESOURCE_POOL_MOID_PATTERN: Final = r"^resgroup-[0-9]+$"


def _text(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


def _integer(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _flag(description: str) -> dict[str, Any]:
    return {"type": ["boolean", "null"], "description": description}


_LIBRARY_ID: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": CONTENT_LIBRARY_ID_PATTERN,
    "description": "The id of the library (a UUID).",
}
_LIBRARY_NAME: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "description": (
        "The library name. A name that matches more than one library is refused; then pass "
        "library_id."
    ),
}
_LIBRARY_ITEM_ID: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": CONTENT_LIBRARY_ID_PATTERN,
    "description": "The id of the library item (a UUID).",
}
_VM: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": VM_MOID_PATTERN,
    "description": "The VM moid, for example vm-42.",
}
_SECRET_REF_NOTE: Final = (
    "A Vault reference (a path), never the value itself. MEHO reads the field {key!r} of "
    "that secret."
)

_DESTINATION_PROPERTIES: Final[dict[str, Any]] = {
    "destination_url": {
        "type": "string",
        "pattern": "^https://",
        "maxLength": 2048,
        "description": (
            "The https base URL that takes the files with HTTP PUT, for example a WebDAV "
            "folder or an object store bucket path. No user name, password or query part."
        ),
    },
    "destination_token_ref": {
        "type": "string",
        "minLength": 1,
        "maxLength": 512,
        "description": (
            "The token MEHO sends to the destination as 'Authorization: Bearer'. "
            + _SECRET_REF_NOTE.format(key="token")
        ),
    },
    "overwrite": {
        "type": "boolean",
        "default": False,
        "description": "Replace files that are already at the destination. Default false.",
    },
}

# ---------------------------------------------------------------------------
# vmware.composite.vm.capture_to_library
# ---------------------------------------------------------------------------

CAPTURE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": _VM,
        "library_id": _LIBRARY_ID,
        "library_name": _LIBRARY_NAME,
        "item_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "The name of the new library item.",
        },
        "description": {
            "type": "string",
            "maxLength": 1024,
            "description": "A description of the new item.",
        },
        "template_type": {
            "type": "string",
            "enum": [TEMPLATE_OVF, TEMPLATE_VM],
            "default": TEMPLATE_OVF,
            "description": (
                "OVF: an OVF template (works in any local library, can be exported). "
                "VM_TEMPLATE: a vSphere VM template (needs a placement). Default OVF."
            ),
        },
        "datastore": {
            "type": "string",
            "pattern": DATASTORE_MOID_PATTERN,
            "description": (
                "VM_TEMPLATE only: the datastore moid for the template files, for example "
                "datastore-17. Default: the datastore of the library."
            ),
        },
        "placement": {
            "type": "object",
            "description": (
                "VM_TEMPLATE only, and then needed: where vCenter puts the template. Give at "
                "least one field."
            ),
            "properties": {
                "folder": {
                    "type": "string",
                    "pattern": VM_FOLDER_MOID_PATTERN,
                    "description": "A VM folder moid, for example group-v3.",
                },
                "resource_pool": {
                    "type": "string",
                    "pattern": _RESOURCE_POOL_MOID_PATTERN,
                    "description": "A resource pool moid, for example resgroup-8.",
                },
                "host": {
                    "type": "string",
                    "pattern": HOST_MOID_PATTERN,
                    "description": "A host moid, for example host-12.",
                },
                "cluster": {
                    "type": "string",
                    "pattern": CLUSTER_MOID_PATTERN,
                    "description": "A cluster moid, for example domain-c7.",
                },
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
    },
    "required": ["vm", "item_name"],
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "library_name": "<library-name>", "item_name": "<item-name>"},
        {
            "vm": "vm-42",
            "library_id": _EXAMPLE_ITEM_ID,
            "item_name": "<item-name>",
            "template_type": TEMPLATE_VM,
            "placement": {"cluster": "domain-c7"},
        },
    ],
}

_ITEM_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The item as vCenter shows it after the change.",
    "properties": {
        "library_item_id": _text("The item id (a UUID)."),
        "name": _text("The item name."),
        "type": _text("The item type: ovf or vm-template."),
        "size": _integer("The size of the item files in bytes."),
        "content_version": _text("The content version of the item."),
    },
}

_COMMON_ANSWER: Final[dict[str, Any]] = {
    "error_type": _text("The vCenter error type when vCenter refused the call."),
    "messages": {
        "type": "array",
        "items": {"type": "string"},
        "description": "The messages vCenter sent with a failure.",
    },
    "guidance": _text("What happened and what to do next, in plain words."),
}

CAPTURE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "captured",
                "already_exists",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "failed",
            ],
            "description": "captured on success; already_exists when the item name is taken.",
        },
        "vm": _text("The VM moid."),
        "library_id": _text("The library id."),
        "template_type": _text("OVF or VM_TEMPLATE."),
        "library_item_id": _text("The id of the new (or existing) item."),
        "client_token": _text(
            "The Client-Token MEHO sent with an OVF capture. vCenter makes nothing new when "
            "the same request comes again with this token."
        ),
        "item": _ITEM_SCHEMA,
        **_COMMON_ANSWER,
    },
    "required": ["status", "guidance"],
}

CAPTURE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to save a VM into a local content library: as an OVF template (to deploy it "
        "again or to export it as files) or as a VM template."
    ),
    "preconditions": (
        "The library must be a local library. For OVF the VM should be powered off; vCenter "
        "refuses a running VM. A VM_TEMPLATE capture needs a placement. Waits until a second "
        "person approves. A large VM takes a long time: call it with async: true."
    ),
    "parameter_hints": {
        "vm": "The VM moid, for example vm-42.",
        "item_name": "A name no other item of the library has.",
        "template_type": "OVF (default) or VM_TEMPLATE.",
    },
    "output_shape": (
        "{status, vm, library_id, template_type, library_item_id, client_token, item: "
        "{library_item_id, name, type, size, content_version}, error_type, messages, "
        "guidance}. An item name that is taken gives already_exists and changes nothing."
    ),
    "next_step": (
        "To copy the OVF files out of vCenter, call vmware.composite.content_library.item."
        "export with the library_item_id. To deploy it, call vmware.composite.vm."
        "deploy_from_library."
    ),
}

# ---------------------------------------------------------------------------
# vmware.composite.content_library.publish.set
# ---------------------------------------------------------------------------

PUBLISH_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_id": _LIBRARY_ID,
        "library_name": _LIBRARY_NAME,
        "published": {
            "type": "boolean",
            "description": "True to publish the local library, false to stop publishing.",
        },
        "authentication": {
            "type": "string",
            "enum": ["NONE", "BASIC"],
            "description": (
                "How subscribers log in. BASIC needs a password (user name vcsp); NONE needs "
                "none. Leave out to keep the current setting."
            ),
        },
        "password_ref": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": "The new subscription password. "
            + _SECRET_REF_NOTE.format(key="password"),
        },
        "current_password_ref": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "The current subscription password, needed when the library uses BASIC and "
                "you change the password, turn off the login or stop publishing. "
                + _SECRET_REF_NOTE.format(key="password")
            ),
        },
    },
    "required": ["published"],
    "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
    "additionalProperties": False,
    "examples": [
        {"library_name": "<library-name>", "published": True, "authentication": "NONE"},
        {
            "library_id": _EXAMPLE_ITEM_ID,
            "published": True,
            "authentication": "BASIC",
            "password_ref": "<vault-path-of-password>",
        },
    ],
}

_PUBLISH_FIELDS: Final[list[str]] = [
    "published",
    "publish_url",
    "authentication_method",
    "user_name",
]

_PUBLISH_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The publish settings. No password is shown.",
    "properties": {
        "published": _flag("True when the library is published."),
        "publish_url": _text("The URL subscribers use."),
        "authentication_method": _text("NONE or BASIC."),
        "user_name": _text("The user name for BASIC (always vcsp)."),
    },
}

PUBLISH_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "updated",
                "unchanged",
                "not_found",
                "invalid_request",
                "precondition_failed",
                "refused",
                "failed",
            ],
            "description": "updated on success; unchanged when the settings are already so.",
        },
        "library_id": _text("The library id."),
        "before": _PUBLISH_SCHEMA,
        "publish": _PUBLISH_SCHEMA,
        "password_changed": _flag("True when a new password was set."),
        **_COMMON_ANSWER,
    },
    "required": ["status", "guidance"],
}

PUBLISH_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to publish a local content library so other vCenters can subscribe to it, to "
        "stop publishing it, or to set its subscription password."
    ),
    "preconditions": (
        "The library must be a local library. Passwords come only as Vault references. When "
        "the library uses BASIC, changing the password, turning off the login or stopping "
        "the publishing needs current_password_ref. Waits until a second person approves."
    ),
    "parameter_hints": {
        "published": "true or false.",
        "authentication": "NONE or BASIC; leave out to keep it.",
        "password_ref": "Vault path of the new password (field 'password').",
    },
    "output_shape": (
        "{status, library_id, before: {published, publish_url, authentication_method, "
        "user_name}, publish: {...same...}, password_changed, error_type, messages, "
        "guidance}. No password is ever returned."
    ),
    "next_step": (
        "Give publish.publish_url to the vCenter that subscribes, and create the subscribed "
        "library there with vmware.composite.content_library.subscribed.create."
    ),
    # Keep both settings blocks in the answer when the messages list spills.
    "result_objects": {
        "objects": {
            "before": _PUBLISH_FIELDS,
            "publish": _PUBLISH_FIELDS,
        }
    },
}

# ---------------------------------------------------------------------------
# The two exports
# ---------------------------------------------------------------------------

_FILE_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The file name."},
        "size": {"type": "integer", "description": "Bytes copied."},
        "sha256": _text("The SHA-256 of the copied bytes; null when the copy failed."),
        "destination_path": {
            "type": "string",
            "description": "The path of the file at the destination (no host, no token).",
        },
        "status": {
            "type": "string",
            "enum": ["done", "partial", "failed"],
            "description": (
                "done: copied and renamed. partial: copied, but the destination cannot rename, "
                "so the file keeps the ending .meho-partial. failed: not copied."
            ),
        },
    },
    "required": ["name", "size", "sha256", "destination_path", "status"],
}

_CLEANUP_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "What MEHO removed at the end of the run; null when nothing was opened.",
    "properties": {
        "download_session_deleted": _flag("True when the vCenter download session was ended."),
        "temporary_item_deleted": _flag(
            "True when the temporary OVF item was deleted; null when none was made."
        ),
        "note": _text("Why a clean-up step failed; null when all went well."),
    },
}

EXPORT_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["exported", "not_found", "invalid_request", "precondition_failed", "failed"],
            "description": "exported when every file was copied and the clean-up ran.",
        },
        "library_item_id": _text("The item whose files were copied."),
        "vm": _text("The VM moid, for an export of a VM."),
        "temporary_item": {
            "type": "boolean",
            "description": "True when MEHO captured the VM into a temporary item first.",
        },
        "client_tokens": {
            "type": "object",
            "description": "The Client-Token of each create call MEHO sent.",
            "properties": {
                "capture": _text("The token of the OVF capture; null when none ran."),
                "download_session": _text("The token of the download session create."),
            },
        },
        "files": {
            "type": "array",
            "description": "One flat row per file, in copy order.",
            "items": _FILE_ROW,
        },
        "leftovers_removed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Leftovers of an earlier broken run that MEHO removed.",
        },
        "cleanup": _CLEANUP_SCHEMA,
        **_COMMON_ANSWER,
    },
    "required": ["status", "files", "guidance"],
}

_ASYNC_NOTE: Final = (
    "The copy can take hours: call it with async: true, so MEHO answers at once with a run "
    "handle, and read the stored result when the run ends. If MEHO restarts during the run, "
    "the run ends as failed and is not resumed; call it again."
)

ITEM_EXPORT_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"library_item_id": _LIBRARY_ITEM_ID, **_DESTINATION_PROPERTIES},
    "required": ["library_item_id", "destination_url", "destination_token_ref"],
    "additionalProperties": False,
    "examples": [
        {
            "library_item_id": _EXAMPLE_ITEM_ID,
            "destination_url": "https://<backup-store>/<folder>/",
            "destination_token_ref": "<vault-path-of-token>",
        }
    ],
}

ITEM_EXPORT_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to copy the files of one content library item (an OVF template, an ISO or other "
        "files) out of vCenter to a web server or object store that takes HTTP PUT."
    ),
    "preconditions": (
        "The destination must take HTTP PUT under destination_url and accept the token. "
        "Waits until a second person approves. " + _ASYNC_NOTE
    ),
    "parameter_hints": {
        "library_item_id": "From vmware.content_library.item.list.",
        "destination_url": "https base URL; each file goes to <destination_url>/<file name>.",
        "destination_token_ref": "Vault path of the token (field 'token').",
    },
    "output_shape": (
        "{status, library_item_id, files: [{name, size, sha256, destination_path, status}], "
        "leftovers_removed, cleanup: {download_session_deleted, temporary_item_deleted, "
        "note}, client_tokens, error_type, messages, guidance}. On a failure files lists "
        "what was copied before it."
    ),
    "next_step": (
        "Compare the sha256 of each file at the destination with the answer. A file with "
        "status partial still has the ending .meho-partial: rename it at the destination."
    ),
}

EXPORT_OVF_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "vm": {
            **_VM,
            "description": (
                "The VM moid, for example vm-42. MEHO captures it into a temporary OVF item "
                "of the library first, and deletes that item at the end."
            ),
        },
        "library_id": {
            **_LIBRARY_ID,
            "description": "The local library for the temporary item (a UUID).",
        },
        "library_name": {
            **_LIBRARY_NAME,
            "description": "Or the name of that local library.",
        },
        "library_item_id": {
            **_LIBRARY_ITEM_ID,
            "description": "Or an OVF item that already exists; then MEHO captures nothing.",
        },
        **_DESTINATION_PROPERTIES,
    },
    "required": ["destination_url", "destination_token_ref"],
    "oneOf": [
        {
            "required": ["vm"],
            "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
            "not": {"required": ["library_item_id"]},
        },
        {"required": ["library_item_id"], "not": {"required": ["vm"]}},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "vm": "vm-42",
            "library_name": "<library-name>",
            "destination_url": "https://<backup-store>/<folder>/",
            "destination_token_ref": "<vault-path-of-token>",
        }
    ],
}

EXPORT_OVF_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to export a VM as OVF files (the .ovf descriptor and its disks) to a web "
        "server or object store that takes HTTP PUT."
    ),
    "preconditions": (
        "Pass vm with a local library for a temporary item, or library_item_id of an OVF "
        "item. The VM should be powered off; vCenter refuses a running VM. Waits until a "
        "second person approves. " + _ASYNC_NOTE
    ),
    "parameter_hints": {
        "vm": "The VM moid, for example vm-42.",
        "library_name": "A local library with room for one copy of the VM.",
        "destination_token_ref": "Vault path of the token (field 'token').",
    },
    "output_shape": (
        "{status, vm, library_item_id, temporary_item, files: [{name, size, sha256, "
        "destination_path, status}], leftovers_removed, cleanup: {download_session_deleted, "
        "temporary_item_deleted, note}, client_tokens, error_type, messages, guidance}."
    ),
    "next_step": (
        "Compare the sha256 of each file at the destination with the answer. To keep a copy "
        "in the library too, use vmware.composite.vm.capture_to_library and export that item."
    ),
}
