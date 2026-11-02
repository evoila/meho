# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the six typed content-library reads.

The handlers are in :mod:`.content_library_reads`. All six are ``safe`` reads
with ``requires_approval=False``. Two of them send a ``POST`` (the OVF
``filter`` and the subscription ``probe``), but those calls change nothing;
each description says so.

The group ``content_library`` is shared with the content-library composites
(create, sync, delete). Both registrars use :data:`CONTENT_LIBRARY_WHEN_TO_USE`,
so the group has one text.

Imported at the bottom of :mod:`.typed_ops` (after :class:`VmwareTypedOp` is
defined), like the other typed-op modules.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest._content_library_schemas import (
    CONTENT_LIBRARY_GROUP_KEY,
    CONTENT_LIBRARY_WHEN_TO_USE,
    FILE_ROW_SCHEMA,
    ITEM_RESPONSE_SCHEMA,
    LIBRARY_RESPONSE_SCHEMA,
    OVF_OPTIONS_RESPONSE_SCHEMA,
    strings,
    text,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CONTENT_LIBRARY_ID_PATTERN
from meho_backplane.connectors.vmware_rest.content_library_reads import (
    GET_OP_ID,
    ITEM_FILES_LIST_OP_ID,
    ITEM_GET_OP_ID,
    OVF_OPTIONS_OP_ID,
    PROBE_OP_ID,
    SUPERVISOR_LIST_OP_ID,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

__all__ = [
    "CONTENT_LIBRARY_GROUP_KEY",
    "CONTENT_LIBRARY_OPS",
    "CONTENT_LIBRARY_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_CONTENT_LIBRARY_GET_OP",
    "VMWARE_CONTENT_LIBRARY_ITEM_FILES_LIST_OP",
    "VMWARE_CONTENT_LIBRARY_ITEM_GET_OP",
    "VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP",
    "VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP",
    "VMWARE_SUPERVISOR_CONTENT_LIBRARY_LIST_OP",
    "WHEN_TO_USE_BY_GROUP",
]

#: A neutral example id. An example must pass the UUID pattern of its schema.
_EXAMPLE_ITEM_ID: Final = "00000000-0000-4000-8000-000000000001"
_UUID_NOTE: Final = f"a UUID, for example {_EXAMPLE_ITEM_ID}"

_LIBRARY_ITEM_ID: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": CONTENT_LIBRARY_ID_PATTERN,
    "description": f"The id of the library item ({_UUID_NOTE}).",
}

_TAGS: Final[tuple[str, ...]] = ("read-only", "vmware", "vcenter", "content-library")

# ---------------------------------------------------------------------------
# vmware.content_library.get
# ---------------------------------------------------------------------------

VMWARE_CONTENT_LIBRARY_GET_OP = VmwareTypedOp(
    op_id=GET_OP_ID,
    handler_attr="content_library_get",
    summary="Read one content library: type, storage, publish or subscription settings, items.",
    description=(
        "Reads one content library by id or name: its type (local or subscribed), where it "
        "stores files, the publish or subscription settings, the last sync and the number of "
        "items. No password is returned. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "pattern": CONTENT_LIBRARY_ID_PATTERN,
                "description": f"The id of the library ({_UUID_NOTE}).",
            },
            "library_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": (
                    "The library name. A name that matches more than one library is "
                    "refused; then pass library_id."
                ),
            },
        },
        "anyOf": [{"required": ["library_id"]}, {"required": ["library_name"]}],
        "additionalProperties": False,
        "examples": [{"library_name": "<library-name>"}],
    },
    response_schema=LIBRARY_RESPONSE_SCHEMA,
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see one content library in full: local or subscribed, which datastore "
            "holds it, whether it is published, where a subscribed library syncs from and "
            "when it last synced, and how many items it holds."
        ),
        "parameter_hints": {
            "library_id": "Pass the id when you have it.",
            "library_name": "Or pass the exact library name.",
        },
        "output_shape": (
            "{library_id, name, type, description, creation_time, last_modified_time, "
            "last_sync_time, version, server_guid, state, storage_backings: [{type, "
            "datastore_id, storage_uri}], publish: {published, publish_url, "
            "authentication_method, user_name} | null, subscription: {subscription_url, "
            "authentication_method, automatic_sync_enabled, on_demand, ssl_thumbprint, "
            "user_name, source_library} | null, item_count}."
        ),
        "next_step": (
            "To read one item, call vmware.content_library.item.get with its id. To sync a "
            "subscribed library, call vmware.composite.content_library.subscribed.sync."
        ),
    },
)

# ---------------------------------------------------------------------------
# vmware.content_library.item.get
# ---------------------------------------------------------------------------

VMWARE_CONTENT_LIBRARY_ITEM_GET_OP = VmwareTypedOp(
    op_id=ITEM_GET_OP_ID,
    handler_attr="content_library_item_get",
    summary="Read one content library item: type, size, version, cached or not, source item.",
    description=(
        "Reads one content library item by id: its library, name, type (OVF, VM template, "
        "ISO or other), size in bytes, content version, whether its files are cached, when it "
        "last changed, and its source item in a subscribed library. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"library_item_id": _LIBRARY_ITEM_ID},
        "required": ["library_item_id"],
        "additionalProperties": False,
        "examples": [{"library_item_id": _EXAMPLE_ITEM_ID}],
    },
    response_schema=ITEM_RESPONSE_SCHEMA,
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see one library item: what type it is, how big it is, whether its "
            "files are on this vCenter (cached) and which library holds it."
        ),
        "parameter_hints": {"library_item_id": "The item id (a UUID)."},
        "output_shape": (
            "{library_item_id, library_id, name, type, description, size, cached, "
            "content_version, metadata_version, version, creation_time, last_modified_time, "
            "last_sync_time, source_id, security_compliance}. size is in bytes."
        ),
        "next_step": (
            "For the files of the item, call vmware.content_library.item.files.list. For an "
            "OVF item, call vmware.content_library.item.ovf_options before a deploy."
        ),
    },
)

# ---------------------------------------------------------------------------
# vmware.content_library.item.files.list
# ---------------------------------------------------------------------------

VMWARE_CONTENT_LIBRARY_ITEM_FILES_LIST_OP = VmwareTypedOp(
    op_id=ITEM_FILES_LIST_OP_ID,
    handler_attr="content_library_item_files_list",
    summary="List the files of one content library item and where each file is stored.",
    description=(
        "Lists the files of one content library item: name, size, checksum, cached or not, "
        "and the datastore and storage path of each file. A long list comes back as a link "
        "to the stored result. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"library_item_id": _LIBRARY_ITEM_ID},
        "required": ["library_item_id"],
        "additionalProperties": False,
        "examples": [{"library_item_id": _EXAMPLE_ITEM_ID}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "library_item_id": {"type": "string", "description": "The item id (a UUID)."},
            "file_count": {"type": "integer", "description": "How many files the item has."},
            "files": {
                "type": "array",
                "description": "One row per file of the item.",
                "items": FILE_ROW_SCHEMA,
            },
        },
        "required": ["library_item_id", "file_count", "files"],
    },
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the files of one library item: their sizes and checksums, whether "
            "they are cached, and on which datastore they are stored."
        ),
        "parameter_hints": {"library_item_id": "The item id (a UUID)."},
        "output_shape": (
            "{library_item_id, file_count, files: [{name, size, cached, version, "
            "checksum_algorithm, checksum, datastore_ids, storage_uris}]}. size is in bytes. "
            "A long list comes back as a link to the stored result; read it with "
            "result_query."
        ),
        "next_step": (
            "To check a file on the datastore, use the datastore and path from storage_uris. "
            "For the item itself, call vmware.content_library.item.get."
        ),
    },
)

# ---------------------------------------------------------------------------
# vmware.content_library.item.ovf_options
# ---------------------------------------------------------------------------

_MOID_NOTE: Final = "as vCenter shows it, for example"

VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP = VmwareTypedOp(
    op_id=OVF_OPTIONS_OP_ID,
    handler_attr="content_library_item_ovf_options",
    summary="Read what an OVF library item needs before a deploy: sizes, networks, properties.",
    description=(
        "Reads the deploy options of one OVF library item for a resource pool: sizes, "
        "networks, storage groups, disk sizes, licence text (EULA) and properties. vCenter "
        "uses a POST call for this, but it changes nothing. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "library_item_id": _LIBRARY_ITEM_ID,
            "resource_pool": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    f"The resource pool moid the deploy would use, {_MOID_NOTE} "
                    "resgroup-10. vCenter needs it to read the options."
                ),
            },
            "host": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    f"Optional. The host moid the deploy would use, {_MOID_NOTE} host-12."
                ),
            },
            "folder": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    f"Optional. The VM folder moid the deploy would use, {_MOID_NOTE} group-v3."
                ),
            },
        },
        "required": ["library_item_id", "resource_pool"],
        "additionalProperties": False,
        "examples": [
            {"library_item_id": _EXAMPLE_ITEM_ID, "resource_pool": "<resource-pool-moid>"}
        ],
    },
    response_schema=OVF_OPTIONS_RESPONSE_SCHEMA,
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=(*_TAGS, "ovf"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before vmware.composite.vm.deploy_from_library to see what the OVF item "
            "offers: the size options, the network names to map, the properties to set, "
            "the disk sizes and the licence text."
        ),
        "parameter_hints": {
            "library_item_id": "The id of an OVF item (type ovf).",
            "resource_pool": "The resource pool moid of the planned deploy.",
            "host": "Optional host moid of the planned deploy.",
            "folder": "Optional VM folder moid of the planned deploy.",
        },
        "output_shape": (
            "{library_item_id, name, annotation, eulas: [text], networks: [name], "
            "storage_groups: [name], deployment_options: [{key, label, description, "
            "default_choice}], default_deployment_option, properties: [{id, label, "
            "description, type, category, class_id, instance_id, value}], disk_size: "
            "{approximate_download_size, approximate_flat_deployment_size, "
            "approximate_sparse_deployment_size, variable_disk_size} | null, ip_allocation: "
            "{supported_ip_allocation_policy, ip_allocation_policy, supported_ip_protocol, "
            "ip_protocol} | null}. Sizes are in bytes. A password property's value is null."
        ),
        "next_step": (
            "Pass a key from deployment_options as deployment_option, map each name in "
            "networks in network_mappings, and set property ids in ovf_properties of "
            "vmware.composite.vm.deploy_from_library."
        ),
    },
)

# ---------------------------------------------------------------------------
# vmware.content_library.subscribed.probe
# ---------------------------------------------------------------------------

VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP = VmwareTypedOp(
    op_id=PROBE_OP_ID,
    handler_attr="content_library_subscribed_probe",
    summary="Check a subscription URL before you create a subscribed content library.",
    description=(
        "Asks vCenter to reach a published library's URL. Returns whether it is reachable, "
        "the certificate thumbprint it shows and any error. vCenter uses a POST call for "
        "this, but it changes nothing. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "subscription_url": {
                "type": "string",
                "minLength": 8,
                "maxLength": 2048,
                "pattern": "^https?://",
                "description": (
                    "The URL of the published library, usually ending in lib.json. It must "
                    "start with http:// or https://."
                ),
            },
        },
        "required": ["subscription_url"],
        "additionalProperties": False,
        "examples": [{"subscription_url": "https://<publisher-host>/<path>/lib.json"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "subscription_url": {"type": "string", "description": "The URL that was checked."},
            "status": text(
                "SUCCESS, INVALID_URL, TIMED_OUT, HOST_NOT_FOUND, RESOURCE_NOT_FOUND, "
                "INVALID_CREDENTIALS, CERTIFICATE_ERROR or UNKNOWN_ERROR."
            ),
            "reachable": {
                "type": "boolean",
                "description": (
                    "True when vCenter reached the source: status SUCCESS, or "
                    "INVALID_CREDENTIALS (the source answered but wants a login)."
                ),
            },
            "needs_login": {
                "type": "boolean",
                "description": "True when status is INVALID_CREDENTIALS: the source wants a login.",
            },
            "ssl_thumbprint": text("The certificate thumbprint the source shows (https)."),
            "error_messages": strings("The error texts vCenter sent."),
        },
        "required": ["subscription_url", "status", "reachable", "needs_login", "error_messages"],
    },
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=(*_TAGS, "subscribed"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before vmware.composite.content_library.subscribed.create to check that "
            "vCenter reaches the URL and to read the certificate thumbprint."
        ),
        "parameter_hints": {"subscription_url": "The full URL of the published library."},
        "output_shape": (
            "{subscription_url, status, reachable, needs_login, ssl_thumbprint, "
            "error_messages: [text]}. The check sends no login: a source that needs one "
            "answers INVALID_CREDENTIALS. Then reachable is true and needs_login is true."
        ),
        "next_step": (
            "When reachable is true, pass the URL and the ssl_thumbprint to "
            "vmware.composite.content_library.subscribed.create. When needs_login is true, "
            "also pass the user name and password of the source."
        ),
        "result_scalars": {"keys": ["reachable", "needs_login", "status"]},
    },
)

# ---------------------------------------------------------------------------
# vmware.supervisor.content_library.list
# ---------------------------------------------------------------------------

VMWARE_SUPERVISOR_CONTENT_LIBRARY_LIST_OP = VmwareTypedOp(
    op_id=SUPERVISOR_LIST_OP_ID,
    handler_attr="supervisor_content_library_list",
    summary="List the content libraries that vSphere Supervisors use (vCenter 9 and newer).",
    description=(
        "Lists the content libraries set up for vSphere Supervisors, with the status of each "
        "(CONFIGURING, READY, REMOVING or ERROR). Needs vCenter 9.0 or newer; on vCenter 8 "
        "it stops with a clear message. Read-only."
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
            "library_count": {"type": "integer", "description": "How many libraries are set."},
            "libraries": {
                "type": "array",
                "description": "One row per library the Supervisors use.",
                "items": {
                    "type": "object",
                    "properties": {
                        "library_id": text("The content library id."),
                        "status": text("CONFIGURING, READY, REMOVING or ERROR."),
                    },
                },
            },
        },
        "required": ["library_count", "libraries"],
    },
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=(*_TAGS, "supervisor"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which content libraries the vSphere Supervisors use and whether "
            "each one is READY. Works on vCenter 9.0 and newer only."
        ),
        "parameter_hints": {},
        "output_shape": "{library_count, libraries: [{library_id, status}]}.",
        "next_step": (
            "For the name and sync state of a library, call vmware.content_library.get with "
            "its library_id."
        ),
    },
)

#: The six reads, in the order :data:`.typed_ops.VMWARE_TYPED_OPS` lists them.
CONTENT_LIBRARY_OPS: Final[tuple[VmwareTypedOp, ...]] = (
    VMWARE_CONTENT_LIBRARY_GET_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_GET_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FILES_LIST_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP,
    VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP,
    VMWARE_SUPERVISOR_CONTENT_LIBRARY_LIST_OP,
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = CONTENT_LIBRARY_OPS
WHEN_TO_USE_BY_GROUP: dict[str, str] = {CONTENT_LIBRARY_GROUP_KEY: CONTENT_LIBRARY_WHEN_TO_USE}
