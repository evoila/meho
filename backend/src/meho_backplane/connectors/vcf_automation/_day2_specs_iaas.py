# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Day-2 op specs for machines and disks through the IaaS API.

Shared by Aria Automation 8 and VCF Automation 9 (see :mod:`.day2_ops`).
Field names come from the pinned ``vra-iaas.json`` (``Machine``,
``BlockDevice``, ``MachineSpecification``, ``BlockDeviceSpecification``,
``RequestTracker``).
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_automation._day2_paths import (
    BLOCK_DEVICES_PATH,
    MACHINES_PATH,
)
from meho_backplane.connectors.vcf_automation._day2_specs import (
    CONSTRAINTS,
    READ_TAGS,
    TAGS,
    WRITE_TAGS,
    Day2OpSpec,
    string_field,
    tracker_schema,
)
from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, list_of, obj

__all__ = ["iaas_specs"]


def _machine_list(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="machine.list",
        handler="machine_list",
        path=MACHINES_PATH,
        summary="List the machines: name, project, power state, address and zone.",
        description=(
            "Reads every page of GET /iaas/api/machines and returns one flat row per machine: "
            "id, name, project, deployment, power state, address, zone. Optional OData $filter."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "$filter": string_field(
                    "Optional OData filter, for example \"deploymentId eq '<deployment-id>'\"."
                ),
            },
            "additionalProperties": False,
            "examples": [{"$filter": "projectId eq '<project-id>'"}],
        },
        response_schema=obj(
            "The machines, as a flat list.",
            {
                "items": list_of(
                    "One row per machine.",
                    {
                        "id": field("string", "The machine id; also its resource id."),
                        "name": field("string", "The machine name."),
                        "projectId": field(["string", "null"], "The project id."),
                        "deploymentId": field(["string", "null"], "The deployment id."),
                        "powerState": field(
                            ["string", "null"], "ON, OFF, GUEST_OFF, UNKNOWN or SUSPEND."
                        ),
                        "address": field(["string", "null"], "The main IP address."),
                        "hostname": field(["string", "null"], "The host name."),
                        "externalZoneId": field(
                            ["string", "null"], "The zone on the cloud side (vSphere: cluster)."
                        ),
                        "externalRegionId": field(
                            ["string", "null"], "The region on the cloud side."
                        ),
                        "provisioningStatus": field(
                            ["string", "null"], "PROVISIONING, READY or SUSPEND."
                        ),
                        "createdAt": field(["string", "null"], "When it was created."),
                        "zoneIds": {
                            "type": "array",
                            "description": "The cloud zone ids the machine links to.",
                            "items": {"type": "string", "description": "One cloud zone id."},
                        },
                        "diskIds": {
                            "type": "array",
                            "description": "The disk (block device) ids the machine links to.",
                            "items": {"type": "string", "description": "One disk id."},
                        },
                    },
                ),
            },
        ),
        tags=(*READ_TAGS, "machine"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=guidance(
            when_to_call=(
                "Call to see which machines exist, their power state and address, or to find "
                "the machine id for a machine action."
            ),
            output_shape=(
                "{items: [{id, name, projectId, deploymentId, powerState, address, hostname, "
                "externalZoneId, externalRegionId, provisioningStatus, createdAt, zoneIds, "
                "diskIds}]}. A long list comes back as a link to the stored result."
            ),
            next_step=(
                f"Use id as resource_id in {p}.deployment.actions.list and "
                f"{p}.resource.action.run, or as machine_id in {p}.block_device.create."
            ),
        ),
    )


def _block_device_list(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="block_device.list",
        handler="block_device_list",
        path=BLOCK_DEVICES_PATH,
        summary="List the disks (block devices) and the machines they belong to.",
        description=(
            "Reads every page of GET /iaas/api/block-devices and returns one flat row per disk: "
            "id, name, size in GB, type, status, persistent, project, deployment and machine "
            "ids. Optional OData $filter."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "$filter": string_field(
                    "Optional OData filter, for example \"projectId eq '<project-id>'\"."
                ),
            },
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema=obj(
            "The disks, as a flat list.",
            {
                "items": list_of(
                    "One row per disk.",
                    {
                        "id": field("string", "The disk id."),
                        "name": field("string", "The disk name."),
                        "capacityInGB": field(["integer", "null"], "The size in GB."),
                        "type": field(["string", "null"], "SSD, HDD, CDROM or FLOPPY."),
                        "status": field(["string", "null"], "DETACHED, ATTACHED or AVAILABLE."),
                        "persistent": field(
                            ["boolean", "null"], "True when the disk stays after a delete."
                        ),
                        "projectId": field(["string", "null"], "The project id."),
                        "deploymentId": field(["string", "null"], "The deployment id."),
                        "externalZoneId": field(["string", "null"], "The zone on the cloud side."),
                        "provisioningStatus": field(
                            ["string", "null"], "PROVISIONING, READY or SUSPEND."
                        ),
                        "createdAt": field(["string", "null"], "When it was created."),
                        "machineIds": {
                            "type": "array",
                            "description": "The machines the disk is attached to.",
                            "items": {"type": "string", "description": "One machine id."},
                        },
                    },
                ),
            },
        ),
        tags=(*READ_TAGS, "disk", "block-device"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=guidance(
            when_to_call="Call to see the disks, their size and state, and their machines.",
            output_shape=(
                "{items: [{id, name, capacityInGB, type, status, persistent, projectId, "
                "deploymentId, externalZoneId, provisioningStatus, createdAt, machineIds}]}. "
                "A long list comes back as a link to the stored result."
            ),
            next_step=(
                f"Attach a free disk with {p}.block_device.create and block_device_id, or match "
                f"machineIds against {p}.machine.list."
            ),
        ),
    )


def _machine_create(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="machine.create",
        handler="machine_create",
        path=MACHINES_PATH,
        summary="Create a machine directly through the IaaS API, outside a cloud template.",
        description=(
            "Sends POST /iaas/api/machines with a name, a project, a flavor and an image (the "
            "names from the flavor and image profiles). A machine of that name in the project "
            "answers 'unchanged'. Built in the background."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": string_field("The machine name.", maxLength=128),
                "project_id": string_field("The project id (from the project list op)."),
                "flavor": string_field("The flavor name from the project's flavor profile."),
                "image": string_field(
                    "The image name from the image profile, for example 'RHEL 9'."
                ),
                "description": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional description of the machine.",
                },
                "deployment_id": string_field("Optional deployment to add the machine to."),
                "tags": TAGS,
                "constraints": CONSTRAINTS,
            },
            "required": ["name", "project_id", "flavor", "image"],
            "additionalProperties": False,
            "examples": [
                {
                    "name": "<vm-name>",
                    "project_id": "<project-id>",
                    "flavor": "<flavor-name>",
                    "image": "<image-name>",
                }
            ],
        },
        response_schema={
            "type": "object",
            "properties": {
                "status": field(
                    "string",
                    "What the op did: submitted or unchanged.",
                    enum=["submitted", "unchanged"],
                ),
                "request": tracker_schema("The create request; null on 'unchanged'."),
                "machine": {
                    **obj(
                        "Only on 'unchanged': the machine that has this name.",
                        {
                            "id": field(["string", "null"], "The machine id."),
                            "name": field("string", "The machine name."),
                            "projectId": field(["string", "null"], "The project id."),
                            "powerState": field(["string", "null"], "The power state."),
                        },
                    ),
                    "type": ["object", "null"],
                },
                "guidance": field("string", "A short note on what to do next."),
            },
            "required": ["status", "guidance"],
            "additionalProperties": True,
        },
        tags=(*WRITE_TAGS, "machine", "create"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call=(
                "Call to build one machine without a cloud template. For a full workload, a "
                "deploy from a cloud template is usually better."
            ),
            preconditions=(
                "The project may use a cloud zone that has the flavor and image. The machine uses "
                "quota and costs money. No password or key is taken here. A second person must "
                "approve the call."
            ),
            output_shape=(
                "{status: submitted | unchanged, request: {id, name, status, progress, message, "
                "resources}, machine, guidance}."
            ),
            next_step=f"Read {p}.machine.list with a name filter until the machine shows up.",
        ),
    )


def _block_device_create(p: str) -> Day2OpSpec:
    return Day2OpSpec(
        name="block_device.create",
        handler="block_device_create",
        path=BLOCK_DEVICES_PATH,
        summary="Create a disk and attach it to a machine, or attach an existing disk.",
        description=(
            "Sends POST /iaas/api/block-devices, waits up to 60 seconds for it, then POST "
            "/iaas/api/machines/{id}/disks. With block_device_id it only attaches that disk."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "machine_id": string_field(f"The machine to attach to (from {p}.machine.list)."),
                "block_device_id": string_field(
                    f"An existing disk to attach (from {p}.block_device.list). Give this, or "
                    "name, project_id and capacity_gb for a new disk."
                ),
                "name": string_field("The name of the new disk.", maxLength=128),
                "project_id": string_field(
                    "The project of the new disk; use the machine's project."
                ),
                "capacity_gb": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "The size of the new disk in GB.",
                },
                "persistent": {
                    "type": "boolean",
                    "description": "True when the new disk stays after a delete. Default false.",
                },
                "description": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional description of the new disk.",
                },
            },
            "required": ["machine_id"],
            "oneOf": [
                {"required": ["block_device_id"]},
                {"required": ["name", "project_id", "capacity_gb"]},
            ],
            "additionalProperties": False,
            "examples": [
                {
                    "machine_id": "<machine-id>",
                    "name": "<disk-name>",
                    "project_id": "<project-id>",
                    "capacity_gb": 10,
                }
            ],
        },
        response_schema={
            "type": "object",
            "properties": {
                "status": field(
                    "string",
                    "submitted (attach started), in_progress (disk still being made) or failed.",
                    enum=["submitted", "in_progress", "failed"],
                ),
                "create_request": tracker_schema(
                    "The disk create request; null for an existing disk."
                ),
                "attach_request": tracker_schema(
                    "The attach request; null when nothing was attached."
                ),
                "block_device_id": field(["string", "null"], "The id of the disk."),
                "guidance": field("string", "A short note on what to do next."),
            },
            "required": ["status", "guidance"],
            "additionalProperties": True,
        },
        tags=(*WRITE_TAGS, "disk", "block-device", "create"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=guidance(
            when_to_call="Call to give a machine one more disk.",
            preconditions=(
                "The machine exists. A new disk uses storage quota. The add-disk machine action "
                f"({p}.resource.action.run) is the other way. A second person must approve."
            ),
            output_shape=(
                "{status: submitted | in_progress | failed, create_request, attach_request "
                "(each {id, name, status, progress, message, resources}), block_device_id, "
                "guidance}."
            ),
            next_step=(
                f"submitted: read {p}.block_device.list until machineIds has the machine. "
                "in_progress: run again later with block_device_id. failed: read the message."
            ),
        ),
    )


def iaas_specs(p: str) -> tuple[Day2OpSpec, ...]:
    """The machine and disk day-2 specs under the op id prefix *p*."""
    return (_machine_list(p), _block_device_list(p), _machine_create(p), _block_device_create(p))
