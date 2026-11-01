# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the upgrade, binary and environment ops.

Seven ops for Aria Suite Lifecycle 8.x and VCF 9.0 fleet management on the
``fleet-rest-9.0`` connector. The handlers live in
:mod:`~meho_backplane.connectors.vcf_fleet.changes`; :mod:`.typed_ops` adds
these ops to ``FLEET_TYPED_OPS``.

Every change waits for approval. The starts are vendor jobs: they return
``accepted`` with the vendor's request id, and the caller follows the
request with ``fleet.request.get``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_fleet._typed_op import FleetTypedOp

__all__ = ["CHANGE_OPS", "CHANGE_WHEN_TO_USE_BY_GROUP"]

CHANGE_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "fleet-upgrade": (
        "Two changes for the products of a fleet environment: pre-check the upgrade "
        "of a product (fleet.product.upgrade.precheck) and start the upgrade "
        "(fleet.product.upgrade). Both start a vendor request; follow it with "
        "fleet.request.get. Map the upgrade file first with the binary operations."
    ),
    "fleet-binaries": (
        "One read and one change for product binaries (install and upgrade files): "
        "list the binaries the appliance has (fleet.binary.list), and find and map "
        "the binaries at a depot path (fleet.binary.map)."
    ),
    "fleet-environments": (
        "Three changes for fleet environments: pre-check a new environment "
        "(fleet.environment.precheck), create it from the checked spec "
        "(fleet.environment.create), and delete one (fleet.environment.delete). "
        "Each starts a vendor request; follow it with fleet.request.get. To list "
        "environments, use the fleet-inventory group."
    ),
}

_ACCEPTED_PROPERTIES: dict[str, Any] = {
    "status": {
        "type": "string",
        "enum": ["accepted", "already_running", "blocked"],
        "description": (
            "accepted: the appliance took the job. already_running: a request of the "
            "same kind runs on the same object, so nothing new was started. blocked: "
            "a running upgrade (or upgrade pre-check) of the product stops the start; "
            "see reason."
        ),
    },
    "request_id": {"type": "string", "description": "The vendor's request id of the job."},
    "request_state": {
        "type": "string",
        "description": (
            "With already_running or blocked: the vendor state of the running request."
        ),
    },
    "reason": {
        "type": "string",
        "description": "With blocked: which running request stops the start.",
    },
    "status_read": {
        "type": "object",
        "description": "The call that reads the job: fleet.request.get with the request id.",
        "properties": {
            "op_id": {"type": "string", "description": "Always fleet.request.get."},
            "params": {"type": "object", "description": "{request_id: <the request id>}."},
        },
    },
}

_ENV_ID = {
    "type": "string",
    "description": "The environment id, from fleet.environment.list (environmentId).",
}
_PRODUCT_ID = {
    "type": "string",
    "description": (
        "The product id in the environment, for example vra, vrops, vrli, vidm or vrni "
        "(from the products of fleet.environment.list)."
    ),
}

_UPGRADE_PROPERTIES: dict[str, Any] = {
    "environment_id": _ENV_ID,
    "product_id": _PRODUCT_ID,
    "product_version": {"type": "string", "description": "The version to upgrade to."},
    "repository_url": {
        "type": "string",
        "description": (
            "Where the upgrade file is. For the default repository type, the filePath "
            "of a mapped upgrade binary from fleet.binary.list."
        ),
    },
    "repository_type": {
        "type": "string",
        "description": "The vendor repository type. Default lcmRepository (a mapped binary).",
        "default": "lcmRepository",
    },
    "license_ref": {
        "type": "string",
        "pattern": "^locker:",
        "description": (
            "Optional licence for the new version, as a locker reference "
            "(locker:license:...). Never a licence key."
        ),
    },
    "take_product_snapshot": {
        "type": "boolean",
        "default": False,
        "description": "Take a snapshot of the product VMs before the upgrade.",
    },
    "retain_product_snapshot": {
        "type": "boolean",
        "default": False,
        "description": "Keep that snapshot after a good upgrade.",
    },
}

_UPGRADE_EXAMPLE = {
    "environment_id": "<environment-id>",
    "product_id": "<product-id>",
    "product_version": "<version>",
    "repository_url": "<binary-file-path>",
}

_UPGRADE_IDS_OUT: dict[str, Any] = {
    "environment_id": {"type": "string", "description": "The environment id that was sent."},
    "product_id": {"type": "string", "description": "The product id that was sent."},
}

_LOST_REPLY = (
    "After a lost reply, do not start again blindly. With the request id, read "
    "fleet.request.get. Without one, read the saved async result (run async: true so "
    "the request id is kept under a run handle), or fleet.request.list: a request of "
    "this kind on this product, running or ended, is yours. Start again only when "
    "there is none. A second call finds a running request, but not one that ended."
)

_UPGRADE_PRECHECK = FleetTypedOp(
    op_id="fleet.product.upgrade.precheck",
    handler_attr="product_upgrade_precheck",
    summary="Start the pre-check of a product upgrade in a fleet environment.",
    description=(
        "Starts the vendor pre-check of the upgrade of one product to a version "
        "(POST .../products/{productId}/upgrade/pre-validate). Returns the vendor "
        "request id at once; the checks run on the appliance."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_UPGRADE_PROPERTIES),
        "required": ["environment_id", "product_id", "product_version", "repository_url"],
        "additionalProperties": False,
        "examples": [_UPGRADE_EXAMPLE],
    },
    response_schema={
        "type": "object",
        "properties": {**_ACCEPTED_PROPERTIES, **_UPGRADE_IDS_OUT},
    },
    group_key="fleet-upgrade",
    tags=("write", "fleet", "vcf", "upgrade", "precheck"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call before fleet.product.upgrade, to learn what would block the upgrade of "
            "a product without changing it."
        ),
        "preconditions": (
            "The upgrade file is mapped (fleet.binary.list shows it; use its filePath as "
            "repository_url). The call waits for a person's approval. This call path is "
            "not in the published 8.16 API reference; if the appliance answers 404, report "
            "it and do not retry."
        ),
        "output_shape": (
            "{status: accepted|already_running, request_id, status_read: {op_id, params}, "
            "environment_id, product_id}. accepted means started, not done."
        ),
        "next_step": (
            "Read fleet.request.get with request_id until the state is COMPLETED or FAILED. "
            "A FAILED pre-check carries the blocking checks in its error causes. " + _LOST_REPLY
        ),
    },
)

_UPGRADE = FleetTypedOp(
    op_id="fleet.product.upgrade",
    handler_attr="product_upgrade",
    summary="Start the upgrade of a product in a fleet environment.",
    description=(
        "Starts the vendor upgrade of one product to a version (POST "
        ".../products/{productId}/upgrade). The appliance checks first unless "
        "skip_precheck is true. Returns the vendor request id at once."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            **_UPGRADE_PROPERTIES,
            "skip_precheck": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Send preValidate false: start without the vendor's checks. Only "
                    "after a pre-check whose failures you judged harmless."
                ),
            },
        },
        "required": ["environment_id", "product_id", "product_version", "repository_url"],
        "additionalProperties": False,
        "examples": [_UPGRADE_EXAMPLE],
    },
    response_schema={
        "type": "object",
        "properties": {**_ACCEPTED_PROPERTIES, **_UPGRADE_IDS_OUT},
    },
    group_key="fleet-upgrade",
    tags=("write", "fleet", "vcf", "upgrade"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to upgrade one product (for example Aria Operations or Aria Operations "
            "for Logs) of a fleet environment to a newer version. Upgrading a product "
            "upgrades its whole cluster."
        ),
        "preconditions": (
            "fleet.product.upgrade.precheck passed, the upgrade file is mapped "
            "(fleet.binary.list), and a snapshot or backup exists or take_product_snapshot "
            "is true. The call waits for a person's approval; an agent needs a grant."
        ),
        "output_shape": (
            "{status: accepted|already_running, request_id, status_read: {op_id, params}, "
            "environment_id, product_id}. accepted means started, not done."
        ),
        "next_step": (
            "Read fleet.request.get with request_id until the state is COMPLETED or FAILED. "
            "The upgrade can take hours; while the product restarts the read may say not "
            "reachable, which is not a failure. " + _LOST_REPLY
        ),
    },
)

_BINARY_ROW = {
    "type": "object",
    "properties": {
        "filePath": {"type": "string", "description": "Where the file is on the appliance."},
        "name": {"type": "string", "description": "The file name."},
        "type": {"type": "string", "description": "install or upgrade."},
    },
}

_BINARY_LIST = FleetTypedOp(
    op_id="fleet.binary.list",
    handler_attr="binary_list",
    summary="List the product binaries (install and upgrade files) of the appliance.",
    description=(
        "Reads GET /lcm/lcops/api/v2/settings/product-binaries: the install and upgrade "
        "files the appliance has mapped, one row each. Read-only."
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
            "binaries": {
                "type": "array",
                "description": "The mapped product binaries, one row each.",
                "items": _BINARY_ROW,
            }
        },
    },
    group_key="fleet-binaries",
    tags=("read-only", "fleet", "vcf", "binaries"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which install and upgrade files are ready, before a new "
            "environment or a product upgrade."
        ),
        "output_shape": (
            "{binaries: [{filePath, name, type}]}. A long list is behind a result handle."
        ),
        "next_step": (
            "Use a filePath as repository_url of fleet.product.upgrade. If a file is "
            "missing, map it with fleet.binary.map."
        ),
    },
)

_BINARY_MAP = FleetTypedOp(
    op_id="fleet.binary.map",
    handler_attr="binary_map",
    summary="Find the product binaries at a depot path and map them into the appliance.",
    description=(
        "Two steps: find the binaries at a local or NFS path (POST product-binaries), "
        "then map them (POST product-binaries/download). Returns the vendor request id "
        "of the mapping, or the steps when one failed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "source_location": {
                "type": "string",
                "description": (
                    "The folder with the files: a path on the appliance (LOCAL) or "
                    "<nfs-server>:/<export-path> (NFS)."
                ),
            },
            "source_type": {
                "type": "string",
                "enum": ["LOCAL", "NFS"],
                "description": "LOCAL: a folder on the appliance. NFS: an NFS export.",
            },
            "names": {
                "type": "array",
                "items": {"type": "string", "description": "One file name to map."},
                "description": "Map only these file names. Empty or left out: map all found.",
            },
        },
        "required": ["source_location", "source_type"],
        "additionalProperties": False,
        "examples": [{"source_location": "<depot-path>", "source_type": "NFS"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "failed", "unknown"],
                "description": (
                    "accepted: mapping started. failed: a step failed; see steps. unknown: "
                    "the appliance did not answer the map step; it may be mapping."
                ),
            },
            "request_id": {"type": "string", "description": "The vendor request id of the map."},
            "status_read": _ACCEPTED_PROPERTIES["status_read"],
            "reason": {
                "type": "string",
                "description": "With unknown: what happened and what to read before a new call.",
            },
            "binaries": {
                "type": "array",
                "description": "The binaries sent to the map step.",
                "items": _BINARY_ROW,
            },
            "steps": {
                "type": "array",
                "description": "Each step: find, then map.",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "description": "find or map."},
                        "result": {
                            "type": "string",
                            "description": "ok, accepted, failed, unknown or not_run.",
                        },
                        "found": {"type": "integer", "description": "find: files found."},
                        "error": {"type": "string", "description": "The vendor's error text."},
                    },
                },
            },
        },
    },
    group_key="fleet-binaries",
    tags=("write", "fleet", "vcf", "binaries"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to make install or upgrade files on a depot usable by the appliance, "
            "before a new environment or a product upgrade."
        ),
        "preconditions": (
            "The files are at the path and the appliance can read it (an NFS export "
            "must be its own export, not a subfolder). The call waits for approval."
        ),
        "output_shape": (
            "{status, request_id, status_read, binaries: [{filePath, name, type}], "
            "steps: [{step, result, found?, error?}]}. A failed step stops the run; "
            "steps that ran stay done. unknown: the map step got no answer."
        ),
        "next_step": (
            "Read fleet.request.get with request_id until COMPLETED, then fleet.binary.list. "
            "After a lost reply or an unknown map step, read fleet.request.list and "
            "fleet.binary.list first and map only what is missing."
        ),
        "result_scalars": {"keys": ["status", "reason", "request_id", "status_read"]},
    },
)

_SPEC: dict[str, Any] = {
    "type": "object",
    "description": (
        "The vendor EnvironmentRequestDTO. Every password, certificate and licence is a "
        "locker reference (locker:password:<id>:<alias>), never a value."
    ),
    "properties": {
        "environmentName": {"type": "string", "description": "The new environment's name."},
        "environmentDescription": {"type": "string", "description": "A short description."},
        "infrastructure": {
            "type": "object",
            "description": "Where to deploy: vCenter, cluster, network, DNS, NTP.",
            "properties": {
                "properties": {
                    "type": "object",
                    "description": (
                        "The vendor infrastructure properties, for example dataCenterVmid, "
                        "vCenterName, cluster, storage, network, dns, ntp, defaultPassword."
                    ),
                }
            },
        },
        "products": {
            "type": "array",
            "description": "The products to deploy or import, each with id, version and nodes.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The product id, for example vrops."},
                    "version": {"type": "string", "description": "The product version."},
                    "properties": {"type": "object", "description": "The product properties."},
                    "nodes": {
                        "type": "array",
                        "description": "The product nodes (VMs), each with its type.",
                        "items": {"type": "object", "description": "One node."},
                    },
                },
            },
        },
    },
    "required": ["environmentName", "infrastructure", "products"],
}

_SPEC_EXAMPLE = {
    "spec": {
        "environmentName": "<environment-name>",
        "infrastructure": {"properties": {"dataCenterVmid": "<datacenter-id>"}},
        "products": [{"id": "<product-id>", "version": "<version>", "nodes": []}],
    }
}

_ENV_OUT: dict[str, Any] = {
    **_ACCEPTED_PROPERTIES,
    "environment_name": {"type": "string", "description": "The environment name of the spec."},
}

_ENV_PRECHECK = FleetTypedOp(
    op_id="fleet.environment.precheck",
    handler_attr="environment_precheck",
    summary="Start the pre-check of a new fleet environment spec.",
    description=(
        "Starts the vendor check of an environment spec (POST "
        "/lcm/lcops/api/v2/environments/pre-validate). Nothing is deployed. Returns the "
        "vendor request id at once."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"spec": _SPEC},
        "required": ["spec"],
        "additionalProperties": False,
        "examples": [_SPEC_EXAMPLE],
    },
    response_schema={"type": "object", "properties": _ENV_OUT},
    group_key="fleet-environments",
    tags=("write", "fleet", "vcf", "environment", "precheck", "credential"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call before fleet.environment.create, with the same spec.",
        "preconditions": (
            "The binaries are mapped and every secret of the spec is in the locker. A spec "
            "with a clear password, licence key or private key is refused. The call waits "
            "for approval."
        ),
        "output_shape": "{status: accepted, request_id, status_read, environment_name}.",
        "next_step": (
            "Read fleet.request.get with request_id until COMPLETED or FAILED; fix what "
            "failed, then call fleet.environment.create with the same spec."
        ),
    },
)

_ENV_CREATE = FleetTypedOp(
    op_id="fleet.environment.create",
    handler_attr="environment_create",
    summary="Start creating a fleet environment: deploy or import its products.",
    description=(
        "Starts the vendor create of an environment from a pre-checked spec (POST "
        "/lcm/lcops/api/v2/environments). It deploys or imports the products. Returns "
        "the vendor request id at once."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"spec": _SPEC},
        "required": ["spec"],
        "additionalProperties": False,
        "examples": [_SPEC_EXAMPLE],
    },
    response_schema={"type": "object", "properties": _ENV_OUT},
    group_key="fleet-environments",
    tags=("write", "fleet", "vcf", "environment", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call to deploy or import products as a new fleet environment.",
        "preconditions": (
            "fleet.environment.precheck passed for this spec. Every secret is a locker "
            "reference. The call waits for approval; an agent needs a grant."
        ),
        "output_shape": (
            "{status: accepted|already_running, request_id, status_read, environment_name}. "
            "accepted means started, not done."
        ),
        "next_step": (
            "Read fleet.request.get with request_id until COMPLETED or FAILED (a deploy "
            "takes long), then fleet.environment.list. After a lost reply, read the saved "
            "async result (run async: true so the request id is kept), fleet.request.list "
            "and fleet.environment.list first; call again only when none shows the "
            "environment or a create request for it, running or ended."
        ),
    },
)

_ENV_DELETE = FleetTypedOp(
    op_id="fleet.environment.delete",
    handler_attr="environment_delete",
    summary="Delete a fleet environment, and if asked also its product VMs.",
    description=(
        "Starts the vendor delete of an environment (DELETE "
        "/lcm/lcops/api/v2/environments/{environmentId}). With delete_vms true, the "
        "product VMs are deleted from vCenter too. Needs a preview first."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "environment_id": _ENV_ID,
            "delete_vms": {
                "type": "boolean",
                "default": False,
                "description": "Also delete the product VMs from vCenter. Cannot be undone.",
            },
        },
        "required": ["environment_id"],
        "additionalProperties": False,
        "examples": [{"environment_id": "<environment-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_ACCEPTED_PROPERTIES,
            "environment_id": {"type": "string", "description": "The deleted environment id."},
            "delete_vms": {"type": "boolean", "description": "Whether the VMs are deleted."},
        },
    },
    group_key="fleet-environments",
    tags=("write", "delete", "destructive", "fleet", "vcf", "environment"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call only when a person asked to remove a fleet environment.",
        "preconditions": (
            "A person runs it: call preview_operation with the same inputs, then "
            "call_operation with the preview_hash. The preview names the environment, its "
            "products and what cannot be undone. An agent is always refused."
        ),
        "output_shape": "{status: accepted, request_id, status_read, environment_id, delete_vms}.",
        "next_step": (
            "Read fleet.request.get with request_id until COMPLETED or FAILED, then "
            "fleet.environment.list. After a lost reply, read fleet.environment.list first."
        ),
    },
)

#: The ops this module adds, in the order an operator uses them.
CHANGE_OPS: Final[tuple[FleetTypedOp, ...]] = (
    _BINARY_LIST,
    _BINARY_MAP,
    _UPGRADE_PRECHECK,
    _UPGRADE,
    _ENV_PRECHECK,
    _ENV_CREATE,
    _ENV_DELETE,
)
