# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`GcloudConnector`.

G3.7-T5 (#848) adds eight read-only typed ops registered against the
GCP REST surfaces (cloudresourcemanager, compute, iam, serviceusage).
All ops are registered via ``register_typed_operation()`` at lifespan
startup through :meth:`GcloudConnector.register_gcloud_typed_operations`.

Ops
---

* ``gcloud.about`` — project identity summary (wraps fingerprint; canonical
  identity op for the agent).
* ``gcloud.project.describe`` — full ``GET /v1/projects/<id>`` response.
* ``gcloud.services.list`` — enabled (or all) APIs via Service Usage.
* ``gcloud.iam.service_accounts.list`` — SA inventory via IAM API.
* ``gcloud.compute.instances.list`` — aggregated instance inventory;
  large responses return a JSONFlux-compatible ``rows`` + ``total`` envelope.
* ``gcloud.compute.networks.list`` — VPC networks.
* ``gcloud.compute.subnetworks.list`` — subnet inventory.
* ``gcloud.iam.policy.read`` — project IAM policy via CRM ``getIamPolicy``.

GCP REST URL references
-----------------------
* Cloud Resource Manager v1:
  https://cloud.google.com/resource-manager/reference/rest/v1/projects
* Service Usage v1:
  https://cloud.google.com/service-usage/docs/reference/rest/v1/services/list
* IAM v1 (service accounts):
  https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/list
* Compute Engine v1 (instances.aggregatedList, networks.list,
  subnetworks.aggregatedList):
  https://cloud.google.com/compute/docs/reference/rest/v1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["GCLOUD_OPS", "GcloudOp"]


@dataclass(frozen=True)
class GcloudOp:
    """Metadata for one gcloud op the connector registers at startup.

    Fields mirror the keyword arguments that
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :meth:`GcloudConnector.register_gcloud_typed_operations`
    can splat the dataclass into the helper without per-op boilerplate.
    ``handler_attr`` is the attribute name on :class:`GcloudConnector`
    that exposes the async handler; the connector resolves the bound method
    against itself at registration time so the dispatcher's import-handler
    walk can recover the callable from the persisted
    ``module.ClassName.method`` dotted path.

    Mirrors the :class:`~meho_backplane.connectors.bind9.ops.Bind9Op` and
    :class:`~meho_backplane.connectors.kubernetes.ops.KubernetesOp` shapes.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


# ---------------------------------------------------------------------------
# gcloud.about
# ---------------------------------------------------------------------------

_GCLOUD_ABOUT_OP = GcloudOp(
    op_id="gcloud.about",
    handler_attr="gcloud_about",
    summary=(
        "Return GCP project identity: project_id, project_number, lifecycle_state, organization."
    ),
    description=(
        "Calls ``GET https://cloudresourcemanager.googleapis.com/v1/projects/<id>`` "
        "and returns the project's identity fields. Use as the canonical first call "
        "when connecting to a gcloud target: confirms the impersonation chain works, "
        "identifies the project's numerical ID and lifecycle state, and resolves the "
        "parent organization when the project has one. No params required; safe on "
        "any reachable GCP project."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": ["string", "null"]},
            "project_number": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
            "lifecycle_state": {"type": ["string", "null"]},
            "organization": {"type": ["string", "null"]},
            "create_time": {"type": ["string", "null"]},
            "labels": {"type": "object"},
        },
        "required": ["project_id"],
        "additionalProperties": True,
    },
    group_key="identity",
    tags=("read-only", "identity", "gcloud", "cloudresourcemanager"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first when connecting to a new gcloud target or when the agent "
            "needs to confirm which GCP project is behind the target and whether "
            "it is active. Returns the project_number (used in some GCP API paths), "
            "lifecycle_state (ACTIVE / DELETE_REQUESTED / etc.), and the parent "
            "organization ID when the project belongs to one."
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict: project_id (string), project_number (string), name (display "
            "name), lifecycle_state (string), organization (org-ID string or null), "
            "create_time (RFC3339 string or null), labels (dict)."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.project.describe
# ---------------------------------------------------------------------------

_GCLOUD_PROJECT_DESCRIBE_OP = GcloudOp(
    op_id="gcloud.project.describe",
    handler_attr="gcloud_project_describe",
    summary="Return the full Cloud Resource Manager project resource.",
    description=(
        "Calls ``GET https://cloudresourcemanager.googleapis.com/v1/projects/<id>`` "
        "and returns the raw project resource dict from the CRM v1 API. Includes all "
        "fields: projectId, projectNumber, name, lifecycleState, createTime, labels, "
        "parent. Use when the full structured resource (not the identity summary) is "
        "needed — e.g. to read custom labels, creation timestamp, or the exact parent "
        "resource type (folder vs organization)."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "projectId": {"type": ["string", "null"]},
            "projectNumber": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
            "lifecycleState": {"type": ["string", "null"]},
            "createTime": {"type": ["string", "null"]},
            "labels": {"type": "object"},
            "parent": {"type": ["object", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="project",
    tags=("read-only", "project", "gcloud", "cloudresourcemanager"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator needs the full CRM project resource — e.g. to "
            "read all custom labels, the exact parent type (folder vs org), or the "
            "create_time. Prefer ``gcloud.about`` for a quick identity check; use "
            "this op when the full resource dict is needed for downstream logic."
        ),
        "parameter_hints": {},
        "output_shape": (
            "Raw CRM v1 project resource dict: projectId, projectNumber, name, "
            "lifecycleState, createTime (RFC3339), labels (dict), "
            "parent ({type, id} or null)."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.services.list
# ---------------------------------------------------------------------------

_GCLOUD_SERVICES_LIST_OP = GcloudOp(
    op_id="gcloud.services.list",
    handler_attr="gcloud_services_list",
    summary="List GCP services (APIs) enabled on the project.",
    description=(
        "Calls ``GET https://serviceusage.googleapis.com/v1/projects/<id>/services`` "
        "with an optional ``filter=state:ENABLED`` to restrict to enabled APIs. "
        "Follows ``nextPageToken`` pagination to return all matching services. "
        "Each row carries the service name (e.g. "
        "``compute.googleapis.com``), the display title, and the current state "
        "(ENABLED / DISABLED). Use to audit which GCP APIs are active on the project "
        "before calling other ops that require specific APIs to be enabled."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "enabled_only": {
                "type": "boolean",
                "description": (
                    "When true (default), restrict to state=ENABLED services. "
                    "When false, return all services regardless of state."
                ),
                "default": True,
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "state": {"type": "string"},
                    },
                    "required": ["name", "state"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key="services",
    tags=("read-only", "services", "gcloud", "serviceusage"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to discover which GCP APIs are enabled on the project. Use "
            "``enabled_only=true`` (default) for an audit of active services before "
            "invoking ops that require specific APIs (compute, iam, etc.). Use "
            "``enabled_only=false`` to see all available services including disabled ones."
        ),
        "parameter_hints": {
            "enabled_only": "Set false to include disabled services in the response."
        },
        "output_shape": (
            "{'rows': [{name, title, state}], 'total': <int>}. ``name`` is the full "
            "API name (e.g. 'compute.googleapis.com'). ``state`` is 'ENABLED' or "
            "'DISABLED'."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.iam.service_accounts.list
# ---------------------------------------------------------------------------

_GCLOUD_IAM_SERVICE_ACCOUNTS_LIST_OP = GcloudOp(
    op_id="gcloud.iam.service_accounts.list",
    handler_attr="gcloud_iam_service_accounts_list",
    summary="List IAM service accounts in the project.",
    description=(
        "Calls ``GET https://iam.googleapis.com/v1/projects/<id>/serviceAccounts`` "
        "and follows ``nextPageToken`` pagination to return all SAs. Each row "
        "carries the SA email, unique ID, display name, description, and whether "
        "it is disabled. Use to audit the SA inventory before assigning roles or "
        "before picking an impersonation target."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "unique_id": {"type": ["string", "null"]},
                        "display_name": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "disabled": {"type": "boolean"},
                    },
                    "required": ["email", "disabled"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key="iam",
    tags=("read-only", "iam", "gcloud", "service-accounts"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to list all service accounts in the project. Use before role "
            "assignment audits, before picking an impersonation target, or to verify "
            "a specific SA email exists. Pair with ``gcloud.iam.policy.read`` to "
            "cross-reference which SAs have project-level roles."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{'rows': [{email, unique_id, display_name, description, disabled}], "
            "'total': <int>}. ``disabled`` is a boolean; ``unique_id`` is the "
            "numeric SA ID string."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.compute.instances.list
# ---------------------------------------------------------------------------

_GCLOUD_COMPUTE_INSTANCES_LIST_OP = GcloudOp(
    op_id="gcloud.compute.instances.list",
    handler_attr="gcloud_compute_instances_list",
    summary="List Compute Engine instances (all zones) in the project.",
    description=(
        "Calls ``GET https://compute.googleapis.com/compute/v1/projects/<id>/aggregated/"
        "instances`` (aggregated across all zones) and follows ``nextPageToken`` pagination. "
        "Each row carries zone, name, machine_type, status, internal_ips, external_ips, "
        "and creation_timestamp. Large responses (many instances across many zones) "
        "return a ``rows`` + ``total`` envelope compatible with the JSONFlux reducer. "
        "Use to get a full project-wide VM inventory in one call rather than looping "
        "per-zone."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "zone": {
                "type": "string",
                "description": (
                    "Optional zone filter (e.g. 'europe-west3-a'). When set, only "
                    "instances in that zone are returned via the per-zone list API "
                    "instead of aggregatedList. Omit to return all zones."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "zone": {"type": ["string", "null"]},
                        "name": {"type": ["string", "null"]},
                        "machine_type": {"type": ["string", "null"]},
                        "status": {"type": ["string", "null"]},
                        "internal_ips": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "external_ips": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "creation_timestamp": {"type": ["string", "null"]},
                    },
                    "required": ["zone", "name", "status"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key="compute",
    tags=("read-only", "compute", "gcloud", "instances"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call for a project-wide VM inventory. Omit ``zone`` to get all instances "
            "across all zones (uses aggregatedList — one API call). Set ``zone`` when "
            "the operator already knows the target zone and wants a shorter response. "
            "The response envelope (``rows`` + ``total``) is compatible with the "
            "JSONFlux reducer — on large projects the reducer will store the full "
            "payload out-of-band and return a handle."
        ),
        "parameter_hints": {
            "zone": ("Zone name like 'europe-west3-a'. Omit to list all zones via aggregatedList.")
        },
        "output_shape": (
            "{'rows': [{zone, name, machine_type, status, internal_ips, "
            "external_ips, creation_timestamp}], 'total': <int>}. "
            "``internal_ips`` and ``external_ips`` are lists of IP strings."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.compute.networks.list
# ---------------------------------------------------------------------------

_GCLOUD_COMPUTE_NETWORKS_LIST_OP = GcloudOp(
    op_id="gcloud.compute.networks.list",
    handler_attr="gcloud_compute_networks_list",
    summary="List VPC networks in the project.",
    description=(
        "Calls ``GET https://compute.googleapis.com/compute/v1/projects/<id>/global/networks`` "
        "and follows ``nextPageToken`` pagination. Each row carries the network name, "
        "auto_create_subnetworks flag, routing_config (REGIONAL_MANAGED / GLOBAL_DYNAMIC), "
        "MTU, and creation_timestamp. Use to audit the VPC topology before inspecting "
        "subnets or firewall rules."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "auto_create_subnetworks": {"type": ["boolean", "null"]},
                        "routing_mode": {"type": ["string", "null"]},
                        "mtu": {"type": ["integer", "null"]},
                        "creation_timestamp": {"type": ["string", "null"]},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key="compute",
    tags=("read-only", "compute", "gcloud", "networking"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to enumerate VPC networks in the project. Use as the first step "
            "in a network-topology audit before drilling into subnets "
            "(``gcloud.compute.subnetworks.list``). The ``auto_create_subnetworks`` "
            "flag distinguishes auto-mode from custom-mode VPCs."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{'rows': [{name, auto_create_subnetworks, routing_mode, mtu, "
            "creation_timestamp}], 'total': <int>}. ``routing_mode`` is "
            "'REGIONAL_MANAGED' or 'GLOBAL_DYNAMIC'."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.compute.subnetworks.list
# ---------------------------------------------------------------------------

_GCLOUD_COMPUTE_SUBNETWORKS_LIST_OP = GcloudOp(
    op_id="gcloud.compute.subnetworks.list",
    handler_attr="gcloud_compute_subnetworks_list",
    summary="List VPC subnets (all regions) in the project.",
    description=(
        "Calls ``GET https://compute.googleapis.com/compute/v1/projects/<id>/aggregated/"
        "subnetworks`` (aggregated across all regions) and follows ``nextPageToken`` pagination. "
        "Each row carries region, name, cidr_range, network (parent VPC URL), purpose, "
        "and private_ip_google_access flag. Use to audit subnet allocation across regions "
        "before deploying resources or investigating connectivity."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "region": {
                "type": "string",
                "description": (
                    "Optional region filter (e.g. 'europe-west3'). When set, only "
                    "subnets in that region are returned via the per-region API "
                    "instead of aggregatedList."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "region": {"type": ["string", "null"]},
                        "name": {"type": "string"},
                        "cidr_range": {"type": ["string", "null"]},
                        "network": {"type": ["string", "null"]},
                        "purpose": {"type": ["string", "null"]},
                        "private_ip_google_access": {"type": ["boolean", "null"]},
                        "creation_timestamp": {"type": ["string", "null"]},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key="compute",
    tags=("read-only", "compute", "gcloud", "networking"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to enumerate all subnets across all regions. Omit ``region`` for "
            "a full project-wide audit (aggregatedList). Set ``region`` when the "
            "operator already knows which region to inspect. Pair with "
            "``gcloud.compute.networks.list`` to map subnets to their parent VPCs."
        ),
        "parameter_hints": {"region": "Region name like 'europe-west3'. Omit to list all regions."},
        "output_shape": (
            "{'rows': [{region, name, cidr_range, network, purpose, "
            "private_ip_google_access, creation_timestamp}], 'total': <int>}. "
            "``network`` is the full self-link URL of the parent VPC."
        ),
    },
)

# ---------------------------------------------------------------------------
# gcloud.iam.policy.read
# ---------------------------------------------------------------------------

_GCLOUD_IAM_POLICY_READ_OP = GcloudOp(
    op_id="gcloud.iam.policy.read",
    handler_attr="gcloud_iam_policy_read",
    summary="Read the project-level IAM policy (all bindings).",
    description=(
        "Calls ``POST https://cloudresourcemanager.googleapis.com/v1/projects/<id>:getIamPolicy`` "
        "and returns the full policy: version, etag, and all role bindings. "
        "Each binding carries the role (e.g. ``roles/editor``) and the list of "
        "members (``user:``, ``serviceAccount:``, ``group:`` principals). "
        "Use to audit who has what roles on the project before issuing IAM changes "
        "or investigating a permission-denied failure."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "version": {"type": ["integer", "null"]},
            "etag": {"type": ["string", "null"]},
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "condition": {"type": ["object", "null"]},
                    },
                    "required": ["role", "members"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["bindings"],
        "additionalProperties": True,
    },
    group_key="iam",
    tags=("read-only", "iam", "gcloud", "policy"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to audit the project-level IAM policy — i.e. who has which roles "
            "on the project. Use before investigating a permission-denied failure "
            "('does this SA have the required role?'), before assigning new roles, "
            "or to produce an access-review report. Pair with "
            "``gcloud.iam.service_accounts.list`` to cross-reference SA emails in "
            "the policy bindings."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{'version': <int>, 'etag': '<string>', 'bindings': [{role, members: "
            "['user:...', 'serviceAccount:...', ...], condition}]}. "
            "``condition`` is null for unconditional bindings."
        ),
    },
)


# ---------------------------------------------------------------------------
# Merged tuple
# ---------------------------------------------------------------------------


def _gcloud_ops() -> tuple[GcloudOp, ...]:
    """Return the full registration tuple for all G3.7-T5 gcloud ops."""
    return (
        _GCLOUD_ABOUT_OP,
        _GCLOUD_PROJECT_DESCRIBE_OP,
        _GCLOUD_SERVICES_LIST_OP,
        _GCLOUD_IAM_SERVICE_ACCOUNTS_LIST_OP,
        _GCLOUD_COMPUTE_INSTANCES_LIST_OP,
        _GCLOUD_COMPUTE_NETWORKS_LIST_OP,
        _GCLOUD_COMPUTE_SUBNETWORKS_LIST_OP,
        _GCLOUD_IAM_POLICY_READ_OP,
    )


#: The ops :class:`GcloudConnector` registers at lifespan startup.
#:
#: G3.7-T5 (#848) ships all eight read-only ops:
#: ``gcloud.about``, ``gcloud.project.describe``, ``gcloud.services.list``,
#: ``gcloud.iam.service_accounts.list``, ``gcloud.compute.instances.list``,
#: ``gcloud.compute.networks.list``, ``gcloud.compute.subnetworks.list``,
#: ``gcloud.iam.policy.read``.
GCLOUD_OPS: tuple[GcloudOp, ...] = _gcloud_ops()
