# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Workload identity and service-account policy operations of ``gcloud-rest-1.0``.

A workload identity pool lets an outside system (for example Vault or a CI
runner) get Google access without a key file. A pool holds providers; each
provider says which outside issuer is trusted and on which condition. The
outside identity then needs a binding on a service account (usually
``roles/iam.workloadIdentityUser``) to act as that account.

Five operations, all in the same :class:`GcloudOp` shape as the reads in
:mod:`meho_backplane.connectors.gcloud.ops`:

* ``gcloud.iam.workload_identity_pools.list`` -- the pools of the project (safe).
* ``gcloud.iam.workload_identity_pools.providers.list`` -- the providers of
  one pool, with ``issuerUri`` and ``attributeCondition`` (safe).
* ``gcloud.iam.workload_identity_pools.delete`` -- mark one pool deleted
  (dangerous; a person must approve). Google keeps a deleted pool for 30
  days and it can be restored in that time.
* ``gcloud.iam.service_accounts.policy.read`` -- the IAM policy on one
  service account (safe).
* ``gcloud.iam.service_accounts.binding.remove`` -- remove one member from
  one role on one service account (dangerous; a person must approve).

The handlers live in :mod:`meho_backplane.connectors.gcloud.workload_identity`.

Google REST references:

* https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/getIamPolicy
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/setIamPolicy
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.gcloud.ops import GcloudOp
from meho_backplane.connectors.gcloud.workload_identity import (
    MEMBER_PATTERN,
    POOL_ID_PATTERN,
    ROLE_PATTERN,
    SERVICE_ACCOUNT_PATTERN,
)

__all__ = ["WORKLOAD_IDENTITY_OPS"]

GROUP_WORKLOAD_IDENTITY: Final[str] = "workload_identity"

_TAGS: Final[tuple[str, ...]] = ("gcloud", "iam")

_EXAMPLE_SA: Final[str] = "vault-auth@example-project.iam.gserviceaccount.com"
_EXAMPLE_MEMBER: Final[str] = (
    "principalSet://iam.googleapis.com/projects/123456789012/locations/global/"
    "workloadIdentityPools/old-vault-pool/*"
)

_POOL_ID_PROP: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": POOL_ID_PATTERN,
    "description": (
        "The pool ID, the last part of the pool name: 4 to 32 characters, "
        "a-z, 0-9 and '-'. Get it from gcloud.iam.workload_identity_pools.list."
    ),
}

_SHOW_DELETED_PROP: Final[dict[str, Any]] = {
    "type": "boolean",
    "default": False,
    "description": "When true, also list items that are marked deleted. Default false.",
}

_SA_PROP: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": SERVICE_ACCOUNT_PATTERN,
    "description": (
        "The service account e-mail (name@project.iam.gserviceaccount.com) or its "
        "numeric unique ID. Get it from gcloud.iam.service_accounts.list."
    ),
}

_BINDINGS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "The role bindings of the policy.",
    "items": {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": "The role, e.g. roles/iam.workloadIdentityUser.",
            },
            "members": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The members that hold the role.",
            },
            "condition": {
                "type": ["object", "null"],
                "description": "The IAM condition of the binding; null when it has none.",
            },
        },
        "required": ["role", "members"],
        "additionalProperties": False,
    },
}

_POOL_LIST_OP = GcloudOp(
    op_id="gcloud.iam.workload_identity_pools.list",
    handler_attr="gcloud_wif_pools_list",
    summary="List the workload identity pools of the project.",
    description=(
        "Reads GET iam.googleapis.com/v1/projects/<id>/locations/global/"
        "workloadIdentityPools and follows every page. A pool lets an outside system "
        "get Google access without a key file. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"show_deleted": _SHOW_DELETED_PROP},
        "additionalProperties": False,
        "examples": [{}, {"show_deleted": True}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": "One row per pool.",
                "items": {
                    "type": "object",
                    "properties": {
                        "pool_id": {"type": "string", "description": "The pool ID."},
                        "name": {"type": "string", "description": "The full resource name."},
                        "display_name": {"type": ["string", "null"], "description": "Name."},
                        "description": {"type": ["string", "null"], "description": "Text."},
                        "state": {
                            "type": ["string", "null"],
                            "description": "ACTIVE or DELETED.",
                        },
                        "disabled": {
                            "type": "boolean",
                            "description": "True when the pool may not be used.",
                        },
                        "expire_time": {
                            "type": ["string", "null"],
                            "description": "For a deleted pool: when Google removes it for good.",
                        },
                    },
                    "required": ["pool_id", "name", "disabled"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer", "description": "The number of rows."},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key=GROUP_WORKLOAD_IDENTITY,
    tags=(*_TAGS, "read-only", "workload-identity"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which workload identity pools exist, for example before "
            "removing an old access path or to check which outside systems may get "
            "Google access without a key file."
        ),
        "output_shape": (
            "{rows: [{pool_id, name, display_name, description, state, disabled, "
            "expire_time}], total}. state is ACTIVE or DELETED; deleted pools show "
            "only with show_deleted=true."
        ),
        "next_step": (
            "Call gcloud.iam.workload_identity_pools.providers.list with a pool_id to "
            "see which outside issuer the pool trusts."
        ),
    },
)

_PROVIDER_LIST_OP = GcloudOp(
    op_id="gcloud.iam.workload_identity_pools.providers.list",
    handler_attr="gcloud_wif_providers_list",
    summary="List the providers of one workload identity pool, with issuer and condition.",
    description=(
        "Reads GET .../workloadIdentityPools/<pool>/providers and follows every page. "
        "Each row shows the kind (oidc, aws, saml, x509), issuerUri, allowed audiences, "
        "attributeMapping and attributeCondition. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"pool_id": _POOL_ID_PROP, "show_deleted": _SHOW_DELETED_PROP},
        "required": ["pool_id"],
        "additionalProperties": False,
        "examples": [{"pool_id": "old-vault-pool"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": "One row per provider.",
                "items": {
                    "type": "object",
                    "properties": {
                        "provider_id": {"type": "string", "description": "The provider ID."},
                        "name": {"type": "string", "description": "The full resource name."},
                        "display_name": {"type": ["string", "null"], "description": "Name."},
                        "description": {"type": ["string", "null"], "description": "Text."},
                        "state": {
                            "type": ["string", "null"],
                            "description": "ACTIVE or DELETED.",
                        },
                        "disabled": {
                            "type": "boolean",
                            "description": "True when the provider may not be used.",
                        },
                        "kind": {
                            "type": ["string", "null"],
                            "description": "oidc, aws, saml or x509.",
                        },
                        "issuer_uri": {
                            "type": ["string", "null"],
                            "description": "The OIDC issuer URL (issuerUri); null for other kinds.",
                        },
                        "allowed_audiences": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The OIDC audiences the provider accepts.",
                        },
                        "aws_account_id": {
                            "type": ["string", "null"],
                            "description": "The AWS account ID; null for other kinds.",
                        },
                        "attribute_mapping": {
                            "type": "object",
                            "description": "How outside claims map to Google attributes.",
                        },
                        "attribute_condition": {
                            "type": ["string", "null"],
                            "description": (
                                "The condition (attributeCondition) an outside token must "
                                "meet; null means every valid token is accepted."
                            ),
                        },
                        "expire_time": {
                            "type": ["string", "null"],
                            "description": "For a deleted provider: when Google removes it.",
                        },
                    },
                    "required": ["provider_id", "name", "disabled"],
                    "additionalProperties": False,
                },
            },
            "total": {"type": "integer", "description": "The number of rows."},
        },
        "required": ["rows", "total"],
        "additionalProperties": False,
    },
    group_key=GROUP_WORKLOAD_IDENTITY,
    tags=(*_TAGS, "read-only", "workload-identity"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which outside issuer one pool trusts and on which condition, "
            "for example to find out which system an old pool belongs to."
        ),
        "output_shape": (
            "{rows: [{provider_id, name, display_name, description, state, disabled, "
            "kind, issuer_uri, allowed_audiences, aws_account_id, attribute_mapping, "
            "attribute_condition, expire_time}], total}. Key material (jwksJson, SAML "
            "metadata, trust stores) is never returned."
        ),
        "next_step": (
            "To find who may act as a service account through this pool, call "
            "gcloud.iam.service_accounts.policy.read and look for principal:// or "
            "principalSet:// members that name the pool."
        ),
    },
)

_POOL_DELETE_OP = GcloudOp(
    op_id="gcloud.iam.workload_identity_pools.delete",
    handler_attr="gcloud_wif_pool_delete",
    summary="Mark one workload identity pool deleted; it can be restored for 30 days.",
    description=(
        "Sends DELETE .../workloadIdentityPools/<pool>. Google only marks the pool "
        "deleted: it stops working at once, can be restored for 30 days, and its ID "
        "cannot be used again in that time. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"pool_id": _POOL_ID_PROP},
        "required": ["pool_id"],
        "additionalProperties": False,
        "examples": [{"pool_id": "old-vault-pool"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "pool_id": {"type": "string", "description": "The pool ID."},
            "name": {"type": "string", "description": "The full resource name."},
            "state": {"type": "string", "description": "DELETED: the pool is marked deleted."},
            "already_deleted": {
                "type": "boolean",
                "description": "True when the pool was already marked deleted; nothing was sent.",
            },
            "restorable_for_days": {
                "type": "integer",
                "description": "How many days Google keeps the pool so it can be restored.",
            },
            "operation_name": {
                "type": ["string", "null"],
                "description": "The Google operation that does the delete.",
            },
            "operation_done": {
                "type": ["boolean", "null"],
                "description": "True when Google reports the operation as finished.",
            },
            "note": {"type": "string", "description": "What the delete means, in plain words."},
        },
        "required": ["pool_id", "name", "state", "already_deleted", "restorable_for_days", "note"],
        "additionalProperties": False,
    },
    group_key=GROUP_WORKLOAD_IDENTITY,
    tags=(*_TAGS, "write", "delete", "workload-identity"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to remove an old workload identity pool, so that no outside system "
            "can get Google access through it any more."
        ),
        "preconditions": (
            "A person must approve the call. The login needs "
            "iam.workloadIdentityPools.delete; MEHO checks this, and that the pool "
            "exists, before it asks for approval. Read the providers first, so the "
            "approver knows which outside system loses access."
        ),
        "output_shape": (
            "{pool_id, name, state: 'DELETED', already_deleted, restorable_for_days: 30, "
            "operation_name, operation_done, note}. The pool is only marked deleted: "
            "Google keeps it for 30 days and it can be restored (undelete) in that time."
        ),
        "next_step": (
            "Remove the service-account bindings that name the pool with "
            "gcloud.iam.service_accounts.binding.remove. Calling the delete again on a "
            "pool that is already deleted changes nothing."
        ),
    },
)

_SA_POLICY_READ_OP = GcloudOp(
    op_id="gcloud.iam.service_accounts.policy.read",
    handler_attr="gcloud_sa_policy_read",
    summary="Read the IAM policy on one service account: who may use or act as it.",
    description=(
        "Sends POST iam.googleapis.com/v1/projects/<id>/serviceAccounts/<sa>:getIamPolicy "
        "with policy version 3, so bindings with conditions show too. Returns version, "
        "etag and the role bindings. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"service_account_email": _SA_PROP},
        "required": ["service_account_email"],
        "additionalProperties": False,
        "examples": [{"service_account_email": _EXAMPLE_SA}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "service_account_email": {"type": "string", "description": "The service account."},
            "version": {"type": ["integer", "null"], "description": "The policy version."},
            "etag": {"type": ["string", "null"], "description": "The policy etag."},
            "bindings": _BINDINGS_SCHEMA,
        },
        "required": ["service_account_email", "bindings"],
        "additionalProperties": False,
    },
    group_key="iam",
    tags=(*_TAGS, "read-only", "service-accounts", "policy"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see who may act as or use one service account, for example which "
            "workload identity pool members hold roles/iam.workloadIdentityUser on it. "
            "gcloud.iam.policy.read shows the project policy, not this one."
        ),
        "output_shape": (
            "{service_account_email, version, etag, bindings: [{role, members, "
            "condition}]}. condition is null for a binding without a condition."
        ),
        "next_step": (
            "To remove one member from one role, call "
            "gcloud.iam.service_accounts.binding.remove with the role and member "
            "exactly as shown here."
        ),
    },
)

_SA_BINDING_REMOVE_OP = GcloudOp(
    op_id="gcloud.iam.service_accounts.binding.remove",
    handler_attr="gcloud_sa_binding_remove",
    summary="Remove one member from one role on a service account's IAM policy.",
    description=(
        "Reads the service account policy, drops only the named member from the named "
        "role and sends setIamPolicy with the etag it read. If the policy changed in "
        "between, Google refuses and MEHO changes nothing. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_account_email": _SA_PROP,
            "role": {
                "type": "string",
                "pattern": ROLE_PATTERN,
                "description": (
                    "The role, exactly as in the policy, e.g. roles/iam.workloadIdentityUser."
                ),
            },
            "member": {
                "type": "string",
                "pattern": MEMBER_PATTERN,
                "maxLength": 1024,
                "description": (
                    "The member to remove, exactly as in the policy, e.g. "
                    "serviceAccount:..., user:..., principal://... or principalSet://..."
                ),
            },
        },
        "required": ["service_account_email", "role", "member"],
        "additionalProperties": False,
        "examples": [
            {
                "service_account_email": _EXAMPLE_SA,
                "role": "roles/iam.workloadIdentityUser",
                "member": _EXAMPLE_MEMBER,
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "service_account_email": {"type": "string", "description": "The service account."},
            "role": {"type": "string", "description": "The role."},
            "member": {"type": "string", "description": "The member."},
            "removed": {
                "type": "boolean",
                "description": "True when the member was removed; false when it was not there.",
            },
            "members_left": {
                "type": "integer",
                "description": "How many members still hold the role without a condition.",
            },
            "etag": {
                "type": ["string", "null"],
                "description": "The policy etag after the call.",
            },
        },
        "required": ["service_account_email", "role", "member", "removed", "members_left"],
        "additionalProperties": False,
    },
    group_key="iam",
    tags=(*_TAGS, "write", "service-accounts", "policy"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to take away one member's role on one service account, for example "
            "the roles/iam.workloadIdentityUser binding that lets an old workload "
            "identity pool act as the account."
        ),
        "preconditions": (
            "A person must approve the call. The login needs "
            "iam.serviceAccounts.getIamPolicy and iam.serviceAccounts.setIamPolicy on "
            "the account; MEHO checks this, and that the account exists, before it asks "
            "for approval. Only a binding without a condition is changed; a member that "
            "holds the role only through a condition is refused."
        ),
        "output_shape": (
            "{service_account_email, role, member, removed, members_left, etag}. "
            "removed=false means the member did not hold the role; nothing was sent."
        ),
        "next_step": (
            "If Google refuses because the policy changed after MEHO read it, MEHO does "
            "not retry: read the policy again with gcloud.iam.service_accounts.policy.read "
            "and call again if the member is still there."
        ),
    },
)

#: The five operations, in the order they are registered.
WORKLOAD_IDENTITY_OPS: Final[tuple[GcloudOp, ...]] = (
    _POOL_LIST_OP,
    _PROVIDER_LIST_OP,
    _POOL_DELETE_OP,
    _SA_POLICY_READ_OP,
    _SA_BINDING_REMOVE_OP,
)
