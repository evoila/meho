# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX service mesh diagnostics reads (the bodies live in ``typed_reads``).

``hcx.service_mesh.diagnose`` (in ``read_ops``) only starts a run. These two
reads complete the flow: ``hcx.service_mesh.diagnostics.list`` shows the
runs of one mesh with their status, and ``hcx.service_mesh.diagnostics.get``
returns the findings of one run. Both are ``safe`` and need no approval.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import (
    GROUP_SERVICE_MESH,
    HcxTypedOp,
    list_response_schema,
    string_schema,
)

__all__ = ["HCX_DIAGNOSTICS_OPS"]

_SERVICE_MESH_ID_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": "Id of the service mesh (serviceMeshId in hcx.service_mesh.list).",
}

#: The fields of one diagnostics run, as the Manager returns them.
_RUN_FIELDS: Final[dict[str, Any]] = {
    "diagId": string_schema("Id of the diagnostics run."),
    "serviceMeshId": string_schema("Id of the service mesh the run tested."),
    "status": string_schema("Vendor status of the run; says whether it is still running."),
    "message": string_schema("Vendor message about the run."),
    "reason": string_schema("Why the run has its status, when the Manager says so."),
    "triggeredBy": string_schema("Who or what started the run."),
    "applianceIds": {
        "type": "array",
        "description": "Ids of the appliances the run tested.",
        "items": {"type": "string"},
    },
    "startTime": {"type": ["integer", "null"], "description": "Start time, epoch milliseconds."},
    "endTime": {"type": ["integer", "null"], "description": "End time, epoch milliseconds."},
}

_DIAGNOSTICS_LIST = HcxTypedOp(
    op_id="hcx.service_mesh.diagnostics.list",
    handler_attr="service_mesh_diagnostics_list",
    summary="List the diagnostics runs of one HCX service mesh with their status.",
    description=(
        "Returns one row per diagnostics run of one service mesh: run id, status, message, "
        "start and end time, who started it and which appliances it tested. An optional "
        "status filter keeps only some runs."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": _SERVICE_MESH_ID_PARAM,
            "status": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Keep only runs whose vendor status is one of these values. "
                    "Leave out for every run."
                ),
            },
        },
        "required": ["service_mesh_id"],
        "additionalProperties": False,
        "examples": [{"service_mesh_id": "<service-mesh-id>"}],
    },
    response_schema=list_response_schema("One row per diagnostics run.", _RUN_FIELDS),
    group_key=GROUP_SERVICE_MESH,
    tags=("read-only", "hcx", "service-mesh", "diagnostics"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after hcx.service_mesh.diagnose to see whether the run is done, or to "
            "find earlier runs of a service mesh."
        ),
        "output_shape": (
            "{items: [{diagId, serviceMeshId, status, message, reason, triggeredBy, "
            "applianceIds, startTime, endTime}]}: one flat row per run, newest by "
            "startTime. A long list is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Pick the run (the newest one after a diagnose call) and call "
            "hcx.service_mesh.diagnostics.get with its diagId for the findings. If the "
            "status says the run is still going, call again later."
        ),
    },
)

_DIAGNOSTICS_GET = HcxTypedOp(
    op_id="hcx.service_mesh.diagnostics.get",
    handler_attr="service_mesh_diagnostics_get",
    summary="Read the findings of one diagnostics run of an HCX service mesh.",
    description=(
        "Returns one diagnostics run of a service mesh with its status, message, times, "
        "the appliances it tested and the full findings (details), plus the warnings "
        "and errors the Manager returned."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_mesh_id": _SERVICE_MESH_ID_PARAM,
            "diag_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Id of the diagnostics run (diagId in hcx.service_mesh.diagnostics.list)."
                ),
            },
        },
        "required": ["service_mesh_id", "diag_id"],
        "additionalProperties": False,
        "examples": [{"service_mesh_id": "<service-mesh-id>", "diag_id": "<diagnostics-run-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_RUN_FIELDS,
            "details": {
                "type": ["object", "null"],
                "description": "The findings of the run, as the Manager returns them.",
            },
            "warnings": {
                "type": "array",
                "description": "Warnings the Manager returned.",
                "items": {
                    "type": "object",
                    "properties": {"message": string_schema("Warning text.")},
                },
            },
            "errors": {
                "type": "array",
                "description": "Errors the Manager returned.",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": string_schema("Vendor error code."),
                        "message": string_schema("Error text."),
                    },
                },
            },
        },
    },
    group_key=GROUP_SERVICE_MESH,
    tags=("read-only", "hcx", "service-mesh", "diagnostics"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when a diagnostics run of a service mesh is done, to read what HCX found."
        ),
        "output_shape": (
            "{diagId, serviceMeshId, status, message, reason, triggeredBy, applianceIds, "
            "startTime, endTime, details, warnings, errors}. details holds the findings."
        ),
        "next_step": (
            "Report the findings in details and the message. If the status says the run "
            "is still going, call again later. If an appliance is named, call "
            "hcx.appliance.list for its state and tunnels. A 404 means the id is unknown."
        ),
    },
)

HCX_DIAGNOSTICS_OPS: Final[tuple[HcxTypedOp, ...]] = (_DIAGNOSTICS_LIST, _DIAGNOSTICS_GET)
