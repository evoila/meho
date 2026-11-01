# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSphere Namespace: create, delete and status.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _namespace
from meho_backplane.connectors.vmware_rest.composites._namespace import (
    namespace_create_composite,
    namespace_delete_composite,
    namespace_status_composite,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_ops import (
    SUPERVISOR_9X_REASON,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _namespace_create_preview,
    _namespace_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    NAMESPACE_CREATE_PARAMETER_SCHEMA,
    NAMESPACE_CREATE_RESPONSE_SCHEMA,
    NAMESPACE_DELETE_PARAMETER_SCHEMA,
    NAMESPACE_DELETE_RESPONSE_SCHEMA,
    NAMESPACE_STATUS_PARAMETER_SCHEMA,
    NAMESPACE_STATUS_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.namespace.create",
        handler=namespace_create_composite,
        summary="Create a vSphere Namespace on a ready Supervisor.",
        description=(
            "Creates a vSphere Namespace on a Supervisor, the place a Kubernetes guest "
            "cluster is created in. Optional: who may use it, storage policies, content "
            "libraries and VM classes. vCenter sets it up in the background; poll "
            "namespace.status."
        ),
        parameter_schema=NAMESPACE_CREATE_PARAMETER_SCHEMA,
        response_schema=NAMESPACE_CREATE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "write", "namespace-management", "namespace", "vks", "supervisor"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.namespace.create"],
        preview=_namespace_create_preview,
        not_on_8_0=SUPERVISOR_9X_REASON,
        governed_subops=_namespace._SUB_OPS_NAMESPACE_CREATE,
    ),
    CompositeSpec(
        op_id="vmware.composite.namespace.delete",
        handler=namespace_delete_composite,
        summary="Delete a vSphere Namespace and everything in it, then check that it is gone.",
        description=(
            "Deletes a vSphere Namespace by name, then reads it again: deleted, removing "
            "(still being torn down) or still_present. Everything in it is deleted too "
            "(Kubernetes guest clusters, pods, volumes). A second person must always approve."
        ),
        parameter_schema=NAMESPACE_DELETE_PARAMETER_SCHEMA,
        response_schema=NAMESPACE_DELETE_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=[
            "composite",
            "write",
            "namespace-management",
            "namespace",
            "destroy",
            "destructive",
        ],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.namespace.delete"],
        preview=_namespace_delete_preview,
        not_on_8_0=SUPERVISOR_9X_REASON,
        governed_subops=_namespace._SUB_OPS_NAMESPACE_DELETE,
    ),
    CompositeSpec(
        op_id="vmware.composite.namespace.status",
        handler=namespace_status_composite,
        summary="Read the state of a vSphere Namespace (exists, config status, ready).",
        description=(
            "Reads a vSphere Namespace: exists, config_status, a ready flag (RUNNING), usage "
            "stats and the latest messages. A namespace that does not exist gives "
            "exists=false, not an error. Use it to poll after namespace.create or "
            "namespace.delete. Read-only."
        ),
        parameter_schema=NAMESPACE_STATUS_PARAMETER_SCHEMA,
        response_schema=NAMESPACE_STATUS_RESPONSE_SCHEMA,
        group_key="namespace_management",
        tags=["composite", "read-only", "namespace-management", "namespace", "vks", "supervisor"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.namespace.status"],
        not_on_8_0=SUPERVISOR_9X_REASON,
    ),
)
