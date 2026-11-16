# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The operations of ``gcloud-rest-1.0`` that set up a backup bucket and its service account.

Five operations. The three bucket changes and the service account create
wait for a person to approve them:

* ``gcloud.storage.buckets.create`` -- make a bucket with uniform access and
  public access blocked (caution).
* ``gcloud.storage.buckets.update_settings`` -- versioning, retention period
  (not the lock) and lifecycle rules (caution).
* ``gcloud.storage.buckets.read`` -- the settings, who has access and whether
  the retention policy is locked (safe).
* ``gcloud.storage.buckets.access.add`` -- one member on one role of one
  bucket (dangerous).
* ``gcloud.iam.service_accounts.create`` -- a service account with no key
  (caution).

The handlers live in :mod:`meho_backplane.connectors.gcloud.backup_bucket`.

Google REST references:

* https://cloud.google.com/storage/docs/json_api/v1/buckets/insert
* https://cloud.google.com/storage/docs/json_api/v1/buckets/patch
* https://cloud.google.com/storage/docs/json_api/v1/buckets/setIamPolicy
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/create
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.gcloud.backup_bucket import (
    ACCOUNT_ID_PATTERN,
    BUCKET_NAME_PATTERN,
    LIFECYCLE_ACTIONS,
    LIFECYCLE_CONDITION_FIELDS,
    LOCATION_PATTERN,
    STORAGE_CLASSES,
)
from meho_backplane.connectors.gcloud.ops import GcloudOp
from meho_backplane.connectors.gcloud.workload_identity import MEMBER_PATTERN, ROLE_PATTERN

__all__ = ["BACKUP_BUCKET_OPS", "GROUP_STORAGE"]

GROUP_STORAGE: Final[str] = "storage"

_TAGS: Final[tuple[str, ...]] = ("gcloud", "storage", "backup")

_EXAMPLE_BUCKET: Final[str] = "example-backups"
_EXAMPLE_MEMBER: Final[str] = "serviceAccount:backup-writer@example-project.iam.gserviceaccount.com"

_BUCKET_PROP: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": BUCKET_NAME_PATTERN,
    "description": "The bucket name: 3 to 63 characters, a-z, 0-9, '-' and '_'. No dots.",
}

_CONDITION_TEXT: Final[dict[str, str]] = {
    "age": "Days since the object was made.",
    "numNewerVersions": "How many newer versions the object has.",
    "daysSinceNoncurrentTime": "Days since the object stopped being the live version.",
    "daysSinceCustomTime": "Days since the custom time of the object.",
    "isLive": "True for live versions, false for old versions.",
    "matchesStorageClass": "Storage classes the object must have.",
    "matchesPrefix": "Name prefixes the object must have.",
    "matchesSuffix": "Name suffixes the object must have.",
}

_CONDITION_TYPES: Final[dict[str, dict[str, Any]]] = {
    "integer": {"type": "integer", "minimum": 0},
    "boolean": {"type": "boolean"},
    "array": {"type": "array", "items": {"type": "string"}},
}

_LIFECYCLE_RULES_PROP: Final[dict[str, Any]] = {
    "type": "array",
    "description": (
        "The full list of lifecycle rules. It replaces the rules the bucket has now; an empty "
        "list removes them all. Each rule has an action and a condition, for example delete "
        "objects older than 90 days."
    ),
    "items": {
        "type": "object",
        "properties": {
            "action": {
                "type": "object",
                "description": "What to do with the object.",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": list(LIFECYCLE_ACTIONS),
                        "description": "Delete, SetStorageClass or AbortIncompleteMultipartUpload.",
                    },
                    "storageClass": {
                        "type": "string",
                        "enum": list(STORAGE_CLASSES),
                        "description": "Needed for SetStorageClass: the new class.",
                    },
                },
                "required": ["type"],
                "additionalProperties": False,
            },
            "condition": {
                "type": "object",
                "description": "When the action runs. At least one field.",
                "minProperties": 1,
                "properties": {
                    name: {**_CONDITION_TYPES[kind], "description": _CONDITION_TEXT[name]}
                    for name, kind in LIFECYCLE_CONDITION_FIELDS.items()
                },
                "additionalProperties": False,
            },
        },
        "required": ["action", "condition"],
        "additionalProperties": False,
    },
}

_SETTINGS_PROPS: Final[dict[str, Any]] = {
    "bucket": {"type": ["string", "null"], "description": "The bucket name."},
    "location": {"type": ["string", "null"], "description": "Where the bucket is."},
    "storage_class": {"type": ["string", "null"], "description": "The default storage class."},
    "uniform_access": {
        "type": "boolean",
        "description": "True when access is set only by bucket IAM, not by object ACLs.",
    },
    "public_access_prevention": {
        "type": ["string", "null"],
        "description": "'enforced' when the bucket can never be made public; null when unknown.",
    },
    "versioning": {"type": "boolean", "description": "True when old object versions are kept."},
    "retention_period_seconds": {
        "type": ["integer", "null"],
        "description": "The retention period in seconds; null when the bucket has none.",
    },
    "retention_locked": {
        "type": "boolean",
        "description": "True when the retention policy is locked. A lock cannot be undone.",
    },
    "lifecycle_rules": {
        "type": "array",
        "items": {"type": "object"},
        "description": "The lifecycle rules, as Google shows them.",
    },
    "metageneration": {
        "type": ["string", "null"],
        "description": "Google's version number of the bucket settings.",
    },
}

_BUCKET_CREATE_OP = GcloudOp(
    op_id="gcloud.storage.buckets.create",
    handler_attr="gcloud_bucket_create",
    summary="Create one Cloud Storage bucket with uniform access and public access blocked.",
    description=(
        "Sends POST /storage/v1/b?project=<project> with the name, location and storage "
        "class. Uniform bucket-level access is on and public access prevention is "
        "'enforced'. A name that is already used (bucket names are global) changes nothing "
        "and the call fails. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "bucket": {
                **_BUCKET_PROP,
                "description": "The new bucket name. Same rules as above.",
            },
            "location": {
                "type": "string",
                "pattern": LOCATION_PATTERN,
                "description": "The location: a region such as europe-west3, or EU, US, eur4.",
            },
            "storage_class": {
                "type": "string",
                "enum": list(STORAGE_CLASSES),
                "default": "STANDARD",
                "description": "The default storage class. Default STANDARD.",
            },
        },
        "required": ["bucket", "location"],
        "additionalProperties": False,
        "examples": [
            {"bucket": _EXAMPLE_BUCKET, "location": "europe-west3", "storage_class": "NEARLINE"}
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_SETTINGS_PROPS,
            "note": {"type": "string", "description": "What the result means, in plain words."},
        },
        "required": ["bucket", "uniform_access", "versioning", "retention_locked", "note"],
        "additionalProperties": False,
    },
    group_key=GROUP_STORAGE,
    tags=(*_TAGS, "create"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call as the first step of setting up a backup bucket. The bucket is private by "
            "design: uniform access on, public access blocked."
        ),
        "preconditions": (
            "A person must approve the call. The login needs storage.buckets.create on the "
            "project. The name must be free in all of Google Cloud."
        ),
        "output_shape": (
            "{bucket, location, storage_class, uniform_access, public_access_prevention, "
            "versioning, retention_period_seconds, retention_locked, lifecycle_rules, "
            "metageneration, note}."
        ),
        "next_step": (
            "Set versioning, retention and lifecycle with gcloud.storage.buckets.update_settings. "
            "Make the service account with gcloud.iam.service_accounts.create and give it the "
            "bucket with gcloud.storage.buckets.access.add."
        ),
    },
)

_BUCKET_UPDATE_OP = GcloudOp(
    op_id="gcloud.storage.buckets.update_settings",
    handler_attr="gcloud_bucket_update_settings",
    summary="Change versioning, the retention period or the lifecycle rules of one bucket.",
    description=(
        "Reads the bucket, then sends PATCH with only the settings that differ, and only if "
        "the bucket did not change in between. Settings it already has change nothing. A locked "
        "retention period cannot be changed; locking is not done here. Name at least one "
        "setting. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "bucket": _BUCKET_PROP,
            "versioning": {
                "type": "boolean",
                "description": "True keeps old object versions; false stops keeping them.",
            },
            "retention_period_seconds": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "How long objects must be kept, in seconds (86400 is one day). 0 removes "
                    "the retention period. This does not lock it."
                ),
            },
            "lifecycle_rules": _LIFECYCLE_RULES_PROP,
        },
        "required": ["bucket"],
        "minProperties": 2,
        "additionalProperties": False,
        "examples": [
            {
                "bucket": _EXAMPLE_BUCKET,
                "versioning": True,
                "retention_period_seconds": 2592000,
                "lifecycle_rules": [{"action": {"type": "Delete"}, "condition": {"age": 90}}],
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_SETTINGS_PROPS,
            "changed": {
                "type": "boolean",
                "description": "False when the bucket already had these settings.",
            },
            "note": {"type": "string", "description": "What the result means, in plain words."},
        },
        "required": [
            "bucket",
            "uniform_access",
            "versioning",
            "retention_locked",
            "changed",
            "note",
        ],
        "additionalProperties": False,
    },
    group_key=GROUP_STORAGE,
    tags=(*_TAGS, "update"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after the bucket exists, to keep old versions, protect objects for a set time "
            "or delete old objects automatically."
        ),
        "preconditions": (
            "A person must approve the call. The bucket must exist and the login needs "
            "storage.buckets.update on it. A retention period that is locked cannot be changed."
        ),
        "output_shape": (
            "The same fields as gcloud.storage.buckets.read without access, plus changed and "
            "note. lifecycle_rules in the call replaces all rules."
        ),
        "next_step": "Check the result with gcloud.storage.buckets.read.",
    },
)

_BUCKET_READ_OP = GcloudOp(
    op_id="gcloud.storage.buckets.read",
    handler_attr="gcloud_bucket_read",
    summary="Show the settings of one bucket, who has access, and whether retention is locked.",
    description=(
        "Reads the bucket and its IAM policy (version 3, so conditions show). Returns the "
        "location, storage class, uniform access, public access prevention, versioning, "
        "retention period and lock, lifecycle rules, and each role with its members. If either "
        "read fails, the call fails."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"bucket": _BUCKET_PROP},
        "required": ["bucket"],
        "additionalProperties": False,
        "examples": [{"bucket": _EXAMPLE_BUCKET}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_SETTINGS_PROPS,
            "access": {
                "type": "array",
                "description": "One entry per role binding of the bucket policy.",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": ["string", "null"], "description": "The role."},
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Who holds the role.",
                        },
                        "condition": {
                            "type": ["string", "null"],
                            "description": "The title of the binding's condition; null if none.",
                        },
                    },
                    "required": ["role", "members", "condition"],
                    "additionalProperties": False,
                },
            },
            "policy_etag": {"type": ["string", "null"], "description": "The policy etag."},
        },
        "required": ["bucket", "uniform_access", "versioning", "retention_locked", "access"],
        "additionalProperties": False,
    },
    group_key=GROUP_STORAGE,
    tags=("read-only", *_TAGS, "read"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check a backup bucket: before a change, to see the settings and who has "
            "access, or after one, to confirm it."
        ),
        "output_shape": (
            "{bucket, location, storage_class, uniform_access, public_access_prevention, "
            "versioning, retention_period_seconds, retention_locked, lifecycle_rules, "
            "metageneration, access: [{role, members, condition}], policy_etag}."
        ),
        "next_step": "Change settings with gcloud.storage.buckets.update_settings.",
    },
)

_ACCESS_ADD_OP = GcloudOp(
    op_id="gcloud.storage.buckets.access.add",
    handler_attr="gcloud_bucket_access_add",
    summary="Add one member to one role on a bucket's IAM policy.",
    description=(
        "Reads the bucket policy, adds only the named member to the named role and sends "
        "setIamPolicy with the etag it read. A member already there changes nothing. A policy "
        "that changed in between changes nothing. Public and deleted: members are refused. "
        "Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "bucket": _BUCKET_PROP,
            "role": {
                "type": "string",
                "pattern": ROLE_PATTERN,
                "description": "The role to give, e.g. roles/storage.objectAdmin.",
            },
            "member": {
                "type": "string",
                "pattern": MEMBER_PATTERN,
                "maxLength": 1024,
                "description": (
                    "The member to add, e.g. serviceAccount:..., user:... or group:.... "
                    "allUsers, allAuthenticatedUsers and deleted: members are refused."
                ),
            },
        },
        "required": ["bucket", "role", "member"],
        "additionalProperties": False,
        "examples": [
            {
                "bucket": _EXAMPLE_BUCKET,
                "role": "roles/storage.objectAdmin",
                "member": _EXAMPLE_MEMBER,
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "bucket": {"type": "string", "description": "The bucket."},
            "role": {"type": "string", "description": "The role."},
            "member": {"type": "string", "description": "The member."},
            "added": {
                "type": "boolean",
                "description": "True when the member was added; false when it was already there.",
            },
            "members_with_role": {
                "type": "integer",
                "description": "How many members hold the role without a condition now.",
            },
            "etag": {"type": ["string", "null"], "description": "The policy etag after the call."},
        },
        "required": ["bucket", "role", "member", "added", "members_with_role"],
        "additionalProperties": False,
    },
    group_key=GROUP_STORAGE,
    tags=(*_TAGS, "policy"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to let one identity use a bucket, for example to give the backup service "
            "account roles/storage.objectAdmin on the backup bucket."
        ),
        "preconditions": (
            "A person must approve the call. The bucket must exist and the login needs "
            "storage.buckets.getIamPolicy and storage.buckets.setIamPolicy on it. Only the "
            "binding without a condition is changed. Give the smallest role that works."
        ),
        "output_shape": (
            "{bucket, role, member, added, members_with_role, etag}. added=false means the "
            "member already held the role; nothing was sent."
        ),
        "next_step": (
            "If Google refuses because the policy changed after MEHO read it, MEHO does not "
            "retry: read the bucket with gcloud.storage.buckets.read and call again."
        ),
    },
)

_SA_CREATE_OP = GcloudOp(
    op_id="gcloud.iam.service_accounts.create",
    handler_attr="gcloud_sa_create",
    summary="Create one service account. It gets no key and no role.",
    description=(
        "Sends POST /v1/projects/<project>/serviceAccounts with the account ID and an "
        "optional display name. An ID that is already used changes nothing and shows that "
        "account. No key is made. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "pattern": ACCOUNT_ID_PATTERN,
                "description": (
                    "The new account ID: 6 to 30 characters, a-z, 0-9 and '-', starts with a "
                    "letter. It becomes <id>@<project>.iam.gserviceaccount.com."
                ),
            },
            "display_name": {
                "type": "string",
                "maxLength": 100,
                "description": "Optional short name shown in the Google console.",
            },
        },
        "required": ["account_id"],
        "additionalProperties": False,
        "examples": [{"account_id": "backup-writer", "display_name": "Backup writer"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "email": {"type": ["string", "null"], "description": "The service account e-mail."},
            "unique_id": {"type": ["string", "null"], "description": "The numeric ID."},
            "display_name": {"type": ["string", "null"], "description": "The short name."},
            "disabled": {"type": "boolean", "description": "True when the account is disabled."},
            "already_existed": {
                "type": "boolean",
                "description": "True when the account was already there; nothing changed.",
            },
            "note": {"type": "string", "description": "What the result means, in plain words."},
        },
        "required": ["email", "already_existed", "note"],
        "additionalProperties": False,
    },
    group_key="iam",
    tags=("gcloud", "iam", "write", "create", "service-accounts"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to make the identity that will write backups to a bucket. The account has "
            "no key and no role; it cannot do anything until it is given access."
        ),
        "preconditions": (
            "A person must approve the call. The login needs iam.serviceAccounts.create on the "
            "project."
        ),
        "output_shape": "{email, unique_id, display_name, disabled, already_existed, note}.",
        "next_step": (
            "Give the account access to a bucket with gcloud.storage.buckets.access.add, using "
            "member serviceAccount:<email>."
        ),
    },
)

#: The five operations, in the order they are registered.
BACKUP_BUCKET_OPS: Final[tuple[GcloudOp, ...]] = (
    _BUCKET_CREATE_OP,
    _BUCKET_UPDATE_OP,
    _BUCKET_READ_OP,
    _ACCESS_ADD_OP,
    _SA_CREATE_OP,
)
