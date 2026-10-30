# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Group text, the find call and response schemas of the typed content-library reads.

The operations are in :mod:`.typed_ops_content_library`. This module imports
nothing from the connector, so the composites, the finds and the reads can
share the group text and the find call id without an import cycle.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "CONTENT_LIBRARY_GROUP_KEY",
    "CONTENT_LIBRARY_WHEN_TO_USE",
    "FILE_ROW_SCHEMA",
    "FIND_LIBRARY_CALL",
    "ITEM_RESPONSE_SCHEMA",
    "LIBRARY_RESPONSE_SCHEMA",
    "OVF_OPTIONS_RESPONSE_SCHEMA",
    "strings",
    "text",
]

CONTENT_LIBRARY_GROUP_KEY: Final = "content_library"

#: The vendor call that finds libraries by name or type
#: (``Content.Library_find``), in the ``METHOD:/path`` form the ingest parser
#: emits. The composites, the typed find and the typed reads all send it.
FIND_LIBRARY_CALL: Final = "POST:/content/library?action=find"

#: Group text, shared by the typed content-library reads, the two typed finds
#: (``typed_ops_content_library_find.py``) and the content-library composites
#: (``composites/_register.py``), so the group has one text.
CONTENT_LIBRARY_WHEN_TO_USE: Final = (
    "Use for vCenter content libraries: the libraries that hold VM templates, OVF "
    "packages and ISO files. Reads: vmware.content_library.get (one library in full), "
    "vmware.content_library.item.get (one item), vmware.content_library.item.files.list "
    "(the files of an item and where they are stored), "
    "vmware.content_library.item.ovf_options (what an OVF item needs before a deploy: "
    "sizes, networks, properties, licence text), vmware.content_library.subscribed.probe "
    "(check a subscription URL before you create a subscribed library) and "
    "vmware.supervisor.content_library.list (the libraries the Supervisors use, vCenter 9 "
    "only). Local libraries: content_library.create makes one on a datastore and "
    "content_library.item.upload lets vCenter download an ISO, OVA or OVF file from an "
    "http(s) URL into a new item (both wait for approval). Subscribed libraries, for "
    "example the Kubernetes releases a Supervisor needs: "
    "content_library.subscribed.create and .sync (both wait for approval), "
    "subscribed.status and subscribed.items.list. Deletes (always wait for approval): "
    "content_library.delete (a whole library) and content_library.item.delete (one item "
    "of a local library). To get the id of a library or an item from its name, use the "
    "safe reads vmware.content_library.find and vmware.content_library.item.find. To "
    "deploy a VM from an item, use vm.deploy_from_library in the vm group."
)

_NULL_TEXT: Final[dict[str, Any]] = {"type": ["string", "null"]}


def text(description: str) -> dict[str, Any]:
    return {**_NULL_TEXT, "description": description}


def _flag(description: str) -> dict[str, Any]:
    return {"type": ["boolean", "null"], "description": description}


def _number(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


LIBRARY_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_id": {"type": "string", "description": "The library id (a UUID)."},
        "name": text("The library name."),
        "type": text("LOCAL or SUBSCRIBED."),
        "description": text("The library description."),
        "creation_time": text("When the library was created (ISO 8601)."),
        "last_modified_time": text("When the library last changed (ISO 8601)."),
        "last_sync_time": text("When a subscribed library last synced (ISO 8601)."),
        "version": text("Changes on every metadata change of the library."),
        "server_guid": text("The id of the vCenter that holds the library."),
        "state": text("The library state, for example ACTIVE; null when vCenter sends none."),
        "storage_backings": {
            "type": "array",
            "description": "Where the library stores its files.",
            "items": {
                "type": "object",
                "properties": {
                    "type": text("DATASTORE or OTHER."),
                    "datastore_id": text("The datastore moid, for type DATASTORE."),
                    "storage_uri": text("The storage URI, for type OTHER."),
                },
            },
        },
        "publish": {
            "type": ["object", "null"],
            "description": "Publish settings of a local library; null when vCenter sends none.",
            "properties": {
                "published": _flag("True when other vCenters can subscribe to this library."),
                "publish_url": text("The URL subscribers use."),
                "authentication_method": text("NONE or BASIC."),
                "user_name": text("The user name subscribers log in with (BASIC)."),
            },
        },
        "subscription": {
            "type": ["object", "null"],
            "description": "Subscription settings of a subscribed library; null otherwise.",
            "properties": {
                "subscription_url": text("The URL of the published library."),
                "authentication_method": text("NONE or BASIC."),
                "automatic_sync_enabled": _flag("True when vCenter syncs on its own."),
                "on_demand": _flag("True when file content downloads only when used."),
                "ssl_thumbprint": text("The certificate thumbprint vCenter trusts."),
                "user_name": text("The user name for BASIC login."),
                "source_library": text("The id of the published library, when known."),
            },
        },
        "item_count": _number("How many items the library holds."),
    },
    "required": ["library_id", "name", "type", "item_count"],
}

ITEM_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_item_id": {"type": "string", "description": "The item id (a UUID)."},
        "library_id": text("The id of the library that holds the item."),
        "name": text("The item name."),
        "type": text("The item type, for example ovf, vm-template or iso."),
        "description": text("The item description."),
        "size": _number("The size of all files of the item, in bytes."),
        "cached": _flag("True when the file content is on this vCenter's storage."),
        "content_version": text("Changes when the files of the item change."),
        "metadata_version": text("Changes when the item's metadata changes."),
        "version": text("Changes on every change of the item."),
        "creation_time": text("When the item was created (ISO 8601)."),
        "last_modified_time": text("When the item last changed (ISO 8601)."),
        "last_sync_time": text("When the item last synced, in a subscribed library (ISO 8601)."),
        "source_id": text("The id of the published item, in a subscribed library."),
        "security_compliance": _flag("True when the item meets the library's security policy."),
    },
    "required": ["library_item_id", "name", "type"],
}

FILE_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": text("The file name, for example disk-0.vmdk."),
        "size": _number("The file size in bytes."),
        "cached": _flag("True when the file content is on this vCenter's storage."),
        "version": text("The file version."),
        "checksum_algorithm": text("The checksum type, for example SHA256."),
        "checksum": text("The checksum of the file."),
        "datastore_ids": strings("The datastores that hold the file."),
        "storage_uris": strings("Where the file is stored, for example a ds:/// URI."),
    },
}

OVF_OPTIONS_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "library_item_id": {"type": "string", "description": "The item id (a UUID)."},
        "name": text("The name in the OVF package."),
        "annotation": text("The note in the OVF package."),
        "eulas": strings("The licence texts (EULA) a deploy must accept."),
        "networks": strings("The network names of the OVF package, to map at deploy."),
        "storage_groups": strings("The storage group names of the OVF package."),
        "deployment_options": {
            "type": "array",
            "description": "The sizes the package offers. Empty when it offers none.",
            "items": {
                "type": "object",
                "properties": {
                    "key": text("The key to pass as deployment_option at deploy."),
                    "label": text("The display name, for example Small."),
                    "description": text("What the option sets up."),
                    "default_choice": _flag("True for the default option."),
                },
            },
        },
        "default_deployment_option": text("The key of the default option, when there is one."),
        "properties": {
            "type": "array",
            "description": "The OVF properties a deploy can set.",
            "items": {
                "type": "object",
                "properties": {
                    "id": text("The property id to set at deploy."),
                    "label": text("The display name."),
                    "description": text("What the property is for."),
                    "type": text("The value type, for example string or password."),
                    "category": text("The group the property starts, if any."),
                    "class_id": text("The OVF class id."),
                    "instance_id": text("The OVF instance id."),
                    "value": text("The default value; always null for a password property."),
                },
            },
        },
        "disk_size": {
            "type": ["object", "null"],
            "description": "The disk sizes of a deploy, in bytes; null when vCenter sends none.",
            "properties": {
                "approximate_download_size": _number("Download size."),
                "approximate_flat_deployment_size": _number("Size with thick disks."),
                "approximate_sparse_deployment_size": _number("Size with thin disks."),
                "variable_disk_size": _flag("True when a disk size depends on a property."),
            },
        },
        "ip_allocation": {
            "type": ["object", "null"],
            "description": "How the package gets IP addresses; null when vCenter sends none.",
            "properties": {
                "supported_ip_allocation_policy": strings("The policies the package supports."),
                "ip_allocation_policy": text("The policy a deploy uses by default."),
                "supported_ip_protocol": strings("IPV4, IPV6 or both."),
                "ip_protocol": text("The protocol a deploy uses by default."),
            },
        },
    },
    "required": ["library_item_id", "eulas", "networks", "deployment_options", "properties"],
}
