# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas, AI guidance and registration rows of the inventory-tree changes.

The four operations of the ``inventory`` group:

* ``vmware.composite.datacenter.create`` (caution) and
  ``vmware.composite.datacenter.delete`` (destructive) --
  :mod:`._inventory_tree`;
* ``vmware.composite.object.rename`` (caution) and
  ``vmware.composite.object.move`` (dangerous) -- :mod:`._object_rename_move`.

Each row is a dict of ``_CompositeSpec`` keyword arguments, so
:mod:`._register` adds them with one line.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._inventory_tree import (
    DATACENTER_FOLDER_MOID_PATTERN,
    OP_ID_DATACENTER_CREATE,
    OP_ID_DATACENTER_DELETE,
    datacenter_create_composite,
    datacenter_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._object_rename_move import (
    MOVE_FOLDER_MOID_PATTERN,
    MOVE_KINDS,
    MOVE_MAX_OBJECTS,
    OP_ID_OBJECT_MOVE,
    OP_ID_OBJECT_RENAME,
    RENAME_KINDS,
    object_move_composite,
    object_rename_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    _DELETE_STATUS_COMMON,
    _ISSUE_ITEM_SCHEMA,
    DATACENTER_MOID_PATTERN,
    _teardown_response_schema,
)

__all__ = ["INVENTORY_COMPOSITES", "INVENTORY_GROUP_KEY", "INVENTORY_WHEN_TO_USE"]

INVENTORY_GROUP_KEY: Final = "inventory"

INVENTORY_WHEN_TO_USE: Final = (
    "Use to change the vCenter inventory tree. Changes: create a datacenter "
    "(datacenter.create, caution) or delete an empty one (datacenter.delete, "
    "destructive); rename a VM, folder, datacenter, cluster, datastore, network, "
    "switch or resource pool (object.rename, caution); move objects into a folder, "
    "where the folder's permissions then apply to them (object.move, dangerous). "
    "To list datacenters, use vmware.datacenter.list; to find folders, use "
    "vmware.folder.list. Pair with 'vm' to create or delete a folder."
)

#: vCenter refuses an entity name of 80 characters or more.
_NAME_MAX: Final = 79

_ISSUES: Final[dict[str, Any]] = {
    "type": "array",
    "items": _ISSUE_ITEM_SCHEMA,
    "description": "Problems. A refusal before any change carries one error entry.",
}
_GUIDANCE: Final[dict[str, Any]] = {
    "type": ["string", "null"],
    "description": "What to do next; null on success.",
}
_TASK: Final[dict[str, Any]] = {
    "type": ["string", "null"],
    "description": "vCenter task id, when a task ran.",
}

# ---------------------------------------------------------------------------
# datacenter.create
# ---------------------------------------------------------------------------

_DATACENTER_CREATE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": _NAME_MAX,
            "description": "Name of the new datacenter. It must be new in its folder.",
        },
        "parent_folder_id": {
            "type": "string",
            "pattern": DATACENTER_FOLDER_MOID_PATTERN,
            "description": (
                "Optional datacenter folder id ('group-d...'). Left out, the datacenter "
                "goes into the root folder of the vCenter."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
    "examples": [{"name": "<datacenter-name>"}],
}

_DATACENTER_INFO_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": "The new datacenter as read back; null when it did not read back.",
    "properties": {
        "datacenter": {"type": "string", "description": "Datacenter id."},
        "name": {"type": ["string", "null"], "description": "Datacenter name."},
        "parent_folder_id": {"type": ["string", "null"], "description": "Parent folder id."},
        "vm_folder_id": {"type": ["string", "null"], "description": "Its root VM folder."},
        "host_folder_id": {"type": ["string", "null"], "description": "Its root host folder."},
        "datastore_folder_id": {
            "type": ["string", "null"],
            "description": "Its root datastore folder.",
        },
        "network_folder_id": {
            "type": ["string", "null"],
            "description": "Its root network folder.",
        },
    },
}

_DATACENTER_CREATE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["created"],
            "description": (
                "created: vCenter made the datacenter. A vCenter fault (for example "
                "DuplicateName) is not a status: the call fails with the fault text."
            ),
        },
        "name": {"type": "string", "description": "The asked name."},
        "parent_folder_id": {"type": "string", "description": "The folder it went into."},
        "datacenter": {"type": ["string", "null"], "description": "New datacenter id."},
        "datacenter_info": _DATACENTER_INFO_SCHEMA,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "name", "parent_folder_id", "datacenter"],
}

_DATACENTER_CREATE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": "Call to create a new, empty datacenter in a vCenter.",
    "preconditions": (
        "The name must be new in the folder. Without parent_folder_id the datacenter "
        "goes into the root folder. A second person must approve the change."
    ),
    "output_shape": (
        "{status: created, name, parent_folder_id, datacenter, datacenter_info: "
        "{datacenter, name, parent_folder_id, vm_folder_id, host_folder_id, "
        "datastore_folder_id, network_folder_id}, guidance}."
    ),
    "next_step": (
        "Use host_folder_id to add hosts or clusters, and vm_folder_id as the parent of "
        "folder.create. A DuplicateName fault means the name exists: pick another name."
    ),
}

# ---------------------------------------------------------------------------
# datacenter.delete
# ---------------------------------------------------------------------------

_DATACENTER_DELETE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "datacenter": {
            "type": "string",
            "pattern": DATACENTER_MOID_PATTERN,
            "description": "Datacenter id, such as 'datacenter-3' (from vmware.datacenter.list).",
        },
    },
    "required": ["datacenter"],
    "additionalProperties": False,
    "examples": [{"datacenter": "datacenter-3"}],
}

_DATACENTER_DELETE_RESPONSE_SCHEMA: Final[dict[str, Any]] = _teardown_response_schema(
    ["deleted", "unchanged", "invalid_request", "precondition_failed", "still_present", "timeout"],
    _DELETE_STATUS_COMMON
    + " Here 'precondition_failed' means the datacenter is not empty, or vCenter did "
    "not fully answer what it holds. 'invalid_request' -- not a datacenter id. "
    "'timeout' -- Destroy_Task outran the poll bound.",
    object_properties={
        "kind": {"type": "string", "description": "Always 'datacenter'."},
        "moid": {"type": "string", "description": "Datacenter id."},
        "name": {"type": ["string", "null"], "description": "Datacenter name."},
        "parent": {"type": ["string", "null"], "description": "Parent folder id."},
        "child_count": {
            "type": "integer",
            "description": "Objects in its four root folders (0 for an empty datacenter).",
        },
    },
    blocker_properties={
        "kind": {"type": "string", "description": "vim type of the object, like Folder."},
        "moid": {"type": "string", "description": "Object id."},
        "name": {"type": ["string", "null"], "description": "Object name."},
    },
)

_DATACENTER_DELETE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": "Call to remove an empty datacenter that is no longer needed.",
    "preconditions": (
        "The datacenter must be empty: no host, cluster, VM, datastore, network or "
        "folder. Call preview_operation first and pass its preview_hash. A person must "
        "run it, and a second person must always approve it."
    ),
    "output_shape": (
        "{status: deleted|unchanged|invalid_request|precondition_failed|still_present|"
        "timeout, object: {kind, moid, name, parent, child_count}, blockers: [{kind, "
        "moid, name}], task, task_state, guidance}."
    ),
    "next_step": (
        "On precondition_failed, move or delete each object in blockers, then call "
        "again. Running it again after a delete gives unchanged."
    ),
}

# ---------------------------------------------------------------------------
# object.rename
# ---------------------------------------------------------------------------

_OBJECT_TYPE_DESCRIPTION: Final = (
    "Kind of the object: vm, folder, datacenter, cluster, standalone_host (the compute "
    "resource of a host outside a cluster), datastore, datastore_cluster, network (a "
    "standard port group), distributed_switch, distributed_portgroup or resource_pool."
)
_MOID_DESCRIPTION: Final = (
    "Object id that fits the kind: vm-..., group-v/h/s/n/d... (folder), datacenter-..., "
    "domain-c... (cluster), domain-s... (standalone host), datastore-..., group-p... "
    "(datastore cluster), network-..., dvs-..., dvportgroup-... or resgroup-..."
)

_OBJECT_RENAME_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "object_type": {
            "type": "string",
            "enum": list(RENAME_KINDS),
            "description": _OBJECT_TYPE_DESCRIPTION,
        },
        "moid": {"type": "string", "minLength": 1, "description": _MOID_DESCRIPTION},
        "new_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": _NAME_MAX,
            "description": "The new name. It must be new among the object's siblings.",
        },
    },
    "required": ["object_type", "moid", "new_name"],
    "additionalProperties": False,
    "examples": [{"object_type": "vm", "moid": "vm-42", "new_name": "<new-vm-name>"}],
}

_OBJECT_RENAME_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["renamed", "unchanged", "invalid_request", "not_found", "timeout"],
            "description": (
                "renamed: the task ended and the name was read back; unchanged: it had "
                "that name, nothing sent; invalid_request: the kind and id do not fit; "
                "not_found: no such object; timeout: the task did not end in time. A "
                "vCenter fault (for example DuplicateName) fails the call with its text."
            ),
        },
        "object_type": {"type": "string", "description": "The asked kind."},
        "moid": {"type": "string", "description": "The object id."},
        "old_name": {"type": ["string", "null"], "description": "Name before the change."},
        "new_name": {"type": "string", "description": "The asked name."},
        "name_after": {
            "type": ["string", "null"],
            "description": "Name as read back after the task.",
        },
        "task": _TASK,
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "object_type", "moid", "new_name"],
}

_OBJECT_RENAME_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to rename one inventory object: a VM, folder, datacenter, cluster, "
        "datastore, datastore cluster, network, distributed switch or port group, or "
        "resource pool."
    ),
    "preconditions": (
        "Name the object by its kind and id, never by name (find the id with the list "
        "reads first). The new name must be new among its siblings. The change waits "
        "for approval."
    ),
    "output_shape": (
        "{status: renamed|unchanged|invalid_request|not_found|timeout, object_type, "
        "moid, old_name, new_name, name_after, task, issues, guidance}."
    ),
    "next_step": (
        "Safe to run twice: the same name gives unchanged. On timeout, read the object "
        "again before a retry. A host is not renamed here: its name comes from the host."
    ),
}

# ---------------------------------------------------------------------------
# object.move
# ---------------------------------------------------------------------------

_OBJECT_MOVE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "folder": {
            "type": "string",
            "pattern": MOVE_FOLDER_MOID_PATTERN,
            "description": (
                "Id of the folder to move into: a VM (group-v), host (group-h), datastore "
                "(group-s), network (group-n) or datacenter (group-d) folder."
            ),
        },
        "objects": {
            "type": "array",
            "minItems": 1,
            "maxItems": MOVE_MAX_OBJECTS,
            "uniqueItems": True,
            "description": "The objects to move, each by kind and id. The folder must hold "
            "their kind.",
            "items": {
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "enum": list(MOVE_KINDS),
                        "description": _OBJECT_TYPE_DESCRIPTION,
                    },
                    "moid": {"type": "string", "minLength": 1, "description": _MOID_DESCRIPTION},
                },
                "required": ["object_type", "moid"],
                "additionalProperties": False,
                "description": "One object to move.",
            },
        },
    },
    "required": ["folder", "objects"],
    "additionalProperties": False,
    "examples": [{"folder": "group-v7", "objects": [{"object_type": "vm", "moid": "vm-42"}]}],
}

_MOVE_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One object, as read before (and after) the move.",
    "properties": {
        "object_type": {"type": "string", "description": "The asked kind."},
        "moid": {"type": "string", "description": "Object id."},
        "name": {"type": ["string", "null"], "description": "Object name."},
        "present": {"type": "boolean", "description": "Whether the object exists."},
        "parent": {"type": ["string", "null"], "description": "Parent folder id."},
        "parent_before": {
            "type": ["string", "null"],
            "description": "Parent before the move (after a move only).",
        },
        "moved": {
            "type": "boolean",
            "description": "Whether it reads back in the folder (after a move only).",
        },
    },
}

_OBJECT_MOVE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "moved",
                "partial",
                "unchanged",
                "invalid_request",
                "not_found",
                "precondition_failed",
                "timeout",
            ],
            "description": (
                "moved: every object reads back in the folder; partial: some do not; "
                "unchanged: all were in it, nothing sent; invalid_request: bad ids; "
                "not_found: no such folder; precondition_failed: an object is missing or "
                "the folder cannot hold it, nothing moved; timeout: the task did not end "
                "in time. A vCenter fault fails the call with its text."
            ),
        },
        "folder": {"type": "string", "description": "The folder id."},
        "folder_name": {"type": ["string", "null"], "description": "The folder name."},
        "objects": {"type": "array", "items": _MOVE_ROW_SCHEMA, "description": "The objects."},
        "task": _TASK,
        "issues": _ISSUES,
        "guidance": _GUIDANCE,
    },
    "required": ["status", "folder", "objects"],
}

_OBJECT_MOVE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to move VMs, folders, clusters, hosts, datastores, networks, switches or "
        "datacenters into another folder of the same datacenter."
    ),
    "preconditions": (
        "The folder must hold the objects' kind: VMs into a VM folder, clusters and hosts "
        "into a host folder, and so on. The folder's permissions then apply to the "
        "objects, so check who can see the folder. Up to 50 objects per call. An agent "
        "needs a grant; the change waits for approval."
    ),
    "output_shape": (
        "{status: moved|partial|unchanged|invalid_request|not_found|precondition_failed|"
        "timeout, folder, folder_name, objects: [{object_type, moid, name, present, "
        "parent, parent_before, moved}], task, issues, guidance}."
    ),
    "next_step": (
        "Safe to run twice: objects already in the folder are skipped. On partial, check "
        "the rows with moved=false. To move a resource pool, use the cluster group."
    ),
}

#: The registration rows, as ``_CompositeSpec`` keyword arguments.
INVENTORY_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_DATACENTER_CREATE,
        "handler": datacenter_create_composite,
        "summary": "Create a new, empty datacenter in a vCenter.",
        "description": (
            "Creates a datacenter in the root folder or in a datacenter folder "
            "(Folder.CreateDatacenter), then reads it back with its four root folders. "
            "Waits for approval."
        ),
        "parameter_schema": _DATACENTER_CREATE_PARAMETER_SCHEMA,
        "response_schema": _DATACENTER_CREATE_RESPONSE_SCHEMA,
        "group_key": INVENTORY_GROUP_KEY,
        "tags": ["composite", "write", "inventory", "datacenter", "vi-json"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": _DATACENTER_CREATE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_DATACENTER_DELETE,
        "handler": datacenter_delete_composite,
        "summary": "Delete an empty datacenter.",
        "description": (
            "Deletes one datacenter (Datacenter.Destroy_Task). Refused before any change "
            "while it holds a host, cluster, VM, datastore, network or folder; the answer "
            "names each one. A second person must always approve."
        ),
        "parameter_schema": _DATACENTER_DELETE_PARAMETER_SCHEMA,
        "response_schema": _DATACENTER_DELETE_RESPONSE_SCHEMA,
        "group_key": INVENTORY_GROUP_KEY,
        "tags": ["composite", "write", "inventory", "datacenter", "destroy", "destructive"],
        "safety_level": "destructive",
        "requires_approval": True,
        "llm_instructions": _DATACENTER_DELETE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_OBJECT_RENAME,
        "handler": object_rename_composite,
        "summary": "Rename one inventory object (VM, folder, cluster, datastore, network, ...).",
        "description": (
            "Renames one object, named by kind and id (ManagedEntity.Rename_Task), then "
            "reads the name back. Sends nothing when it has the name already. Waits for "
            "approval."
        ),
        "parameter_schema": _OBJECT_RENAME_PARAMETER_SCHEMA,
        "response_schema": _OBJECT_RENAME_RESPONSE_SCHEMA,
        "group_key": INVENTORY_GROUP_KEY,
        "tags": ["composite", "write", "inventory", "rename", "vi-json"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": _OBJECT_RENAME_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_OBJECT_MOVE,
        "handler": object_move_composite,
        "summary": "Move inventory objects into a folder; the folder's permissions then apply.",
        "description": (
            "Moves up to 50 objects into one folder (Folder.MoveIntoFolder_Task). Refuses "
            "a missing object or one the folder cannot hold, before any change. Reads each "
            "object's parent back. Waits for approval."
        ),
        "parameter_schema": _OBJECT_MOVE_PARAMETER_SCHEMA,
        "response_schema": _OBJECT_MOVE_RESPONSE_SCHEMA,
        "group_key": INVENTORY_GROUP_KEY,
        "tags": ["composite", "write", "inventory", "move", "vi-json"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": _OBJECT_MOVE_LLM_INSTRUCTIONS,
    },
)
