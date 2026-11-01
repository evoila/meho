# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows of the five host membership changes (#4349).

``_register.py`` splats :data:`HOST_MEMBERSHIP_COMPOSITES` into its
``_COMPOSITES`` table, and ``_governed_subops.py`` reads
:data:`HOST_MEMBERSHIP_GOVERNED_SUBOPS`. Every change has
``requires_approval=True``. ``host.add`` sends the host password from Vault to
vCenter, so ``broadcast/events.py`` pins it as ``credential_write``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites import (
    _host_add,
    _host_membership,
    _host_remove,
)
from meho_backplane.connectors.vmware_rest.composites._host_membership_schemas import (
    HOST_ADD_LLM_INSTRUCTIONS,
    HOST_ADD_PARAMETER_SCHEMA,
    HOST_ADD_RESPONSE_SCHEMA,
    HOST_CONNECT_LLM_INSTRUCTIONS,
    HOST_CONNECT_RESPONSE_SCHEMA,
    HOST_DISCONNECT_LLM_INSTRUCTIONS,
    HOST_DISCONNECT_RESPONSE_SCHEMA,
    HOST_MOVE_LLM_INSTRUCTIONS,
    HOST_MOVE_PARAMETER_SCHEMA,
    HOST_MOVE_RESPONSE_SCHEMA,
    HOST_ONLY_PARAMETER_SCHEMA,
    HOST_REMOVE_LLM_INSTRUCTIONS,
    HOST_REMOVE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_LIST_HOSTS,
    _OP_RETRIEVE_PROPERTIES,
)

__all__ = ["HOST_MEMBERSHIP_COMPOSITES", "HOST_MEMBERSHIP_GOVERNED_SUBOPS"]

_TAGS: Final = ("composite", "write", "host", "vi-json")

HOST_MEMBERSHIP_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": _host_add.OP_ID_HOST_ADD,
        "handler": _host_add.host_add_composite,
        "summary": "Add an ESXi host to a cluster or alone to a host folder; starts a vCenter job.",
        "description": (
            "Starts adding an ESXi host to a cluster or, alone, to a host folder. vCenter "
            "checks the host certificate against ssl_thumbprint. The password comes from "
            "Vault. Answers accepted with a task id; follow it with vmware.task.info."
        ),
        "parameter_schema": HOST_ADD_PARAMETER_SCHEMA,
        "response_schema": HOST_ADD_RESPONSE_SCHEMA,
        "group_key": "host",
        "tags": [*_TAGS, "vendor-job"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": HOST_ADD_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _host_membership.OP_ID_HOST_MOVE,
        "handler": _host_membership.host_move_composite,
        "summary": "Move a host in maintenance mode into a cluster, with its VMs.",
        "description": (
            "Moves a host that vCenter manages into a cluster of the same datacenter. The host "
            "must be in maintenance mode; every VM registered on it moves with it. Reads the "
            "host back after the task."
        ),
        "parameter_schema": HOST_MOVE_PARAMETER_SCHEMA,
        "response_schema": HOST_MOVE_RESPONSE_SCHEMA,
        "group_key": "host",
        "tags": list(_TAGS),
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": HOST_MOVE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _host_membership.OP_ID_HOST_CONNECT,
        "handler": _host_membership.host_connect_composite,
        "summary": "Reconnect a disconnected or not responding host to vCenter.",
        "description": (
            "Reconnects a host with the login vCenter already has. A connected host answers "
            "unchanged and nothing is sent. Reads the host back after the task."
        ),
        "parameter_schema": HOST_ONLY_PARAMETER_SCHEMA,
        "response_schema": HOST_CONNECT_RESPONSE_SCHEMA,
        "group_key": "host",
        "tags": list(_TAGS),
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": HOST_CONNECT_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _host_membership.OP_ID_HOST_DISCONNECT,
        "handler": _host_membership.host_disconnect_composite,
        "summary": "Disconnect a host from vCenter; its VMs keep running.",
        "description": (
            "Disconnects a host from vCenter. Its VMs keep running, but vCenter cannot manage "
            "them until the host is connected again. A disconnected host answers unchanged. "
            "Reads the host back after the task."
        ),
        "parameter_schema": HOST_ONLY_PARAMETER_SCHEMA,
        "response_schema": HOST_DISCONNECT_RESPONSE_SCHEMA,
        "group_key": "host",
        "tags": list(_TAGS),
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": HOST_DISCONNECT_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _host_remove.OP_ID_HOST_REMOVE,
        "handler": _host_remove.host_remove_composite,
        "summary": "Remove a host from vCenter, with the VMs registered on it.",
        "description": (
            "Removes a host from the vCenter inventory; vCenter forgets the VMs registered on "
            "it (their files stay). The host must be in maintenance mode or not connected. "
            "Needs a preview first; the preview lists what goes."
        ),
        "parameter_schema": HOST_ONLY_PARAMETER_SCHEMA,
        "response_schema": HOST_REMOVE_RESPONSE_SCHEMA,
        "group_key": "host",
        "tags": list(_TAGS),
        "safety_level": "destructive",
        "requires_approval": True,
        "llm_instructions": HOST_REMOVE_LLM_INSTRUCTIONS,
    },
)

_READS: Final = (_OP_RETRIEVE_PROPERTIES, _OP_CONTINUE_RETRIEVE_PROPERTIES)

#: VI-JSON vendor calls of each change (``_VIM_SUB_OPS_*`` naming, so the
#: vcenter.yaml sweep skips them; the vi-json reconcile lanes check them).
_VIM_SUB_OPS_HOST_ADD: Final = (
    *_READS,
    _host_add._OP_ADD_HOST,
    _host_add._OP_ADD_STANDALONE_HOST,
)
_VIM_SUB_OPS_HOST_MOVE: Final = (_OP_RETRIEVE_PROPERTIES, _host_membership._OP_MOVE_HOST_INTO)
_VIM_SUB_OPS_HOST_CONNECT: Final = (_OP_RETRIEVE_PROPERTIES, _host_membership._OP_RECONNECT_HOST)
_VIM_SUB_OPS_HOST_DISCONNECT: Final = (
    _OP_RETRIEVE_PROPERTIES,
    _host_membership._OP_DISCONNECT_HOST,
)
_VIM_SUB_OPS_HOST_REMOVE: Final = (
    *_READS,
    _host_remove._OP_DESTROY_HOST,
    _host_remove._OP_DESTROY_COMPUTE_RESOURCE,
)

#: composite op id -> the vendor calls it may make (REST and VI-JSON).
HOST_MEMBERSHIP_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _host_add.OP_ID_HOST_ADD: (_OP_LIST_HOSTS, *_VIM_SUB_OPS_HOST_ADD),
    _host_membership.OP_ID_HOST_MOVE: _VIM_SUB_OPS_HOST_MOVE,
    _host_membership.OP_ID_HOST_CONNECT: _VIM_SUB_OPS_HOST_CONNECT,
    _host_membership.OP_ID_HOST_DISCONNECT: _VIM_SUB_OPS_HOST_DISCONNECT,
    _host_remove.OP_ID_HOST_REMOVE: _VIM_SUB_OPS_HOST_REMOVE,
}
