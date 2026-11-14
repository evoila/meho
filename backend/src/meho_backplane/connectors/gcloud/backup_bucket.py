# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers that set up a Cloud Storage bucket for backups, and its service account.

The operation metadata is in
:mod:`meho_backplane.connectors.gcloud.ops_backup_bucket`. The handlers use
the value checks, the policy-conflict error and the HTTP helpers of
:mod:`meho_backplane.connectors.gcloud.workload_identity`.

* **Bucket create** sends one ``POST``. The bucket always has uniform
  access and public access blocked (``enforced``). A bucket name is global,
  so a name that is already used changes nothing and the call fails with a
  clear message.
* **Bucket settings update** reads the bucket, then sends ``PATCH`` with
  the ``metageneration`` it read as a precondition. It changes only what
  the caller named, sends nothing when the bucket already has these
  settings, and refuses to change a retention period that is locked.
* **Bucket read** reads the bucket and its IAM policy. When either read
  fails, the call fails: it never shows a partial answer.
* **Bucket access add** reads the policy (version 3), adds one member to
  one role in the binding without a condition, and writes the policy back
  with the ``etag`` it read. Public members are refused.
* **Service account create** sends one ``POST``. It makes no key.

Locking a retention policy and making a service account key are not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from meho_backplane.connectors.gcloud.session import GcloudTargetLike, gcp_project
from meho_backplane.connectors.gcloud.workload_identity import (
    _ETAG_CONFLICT_STATUSES,
    _IAM_API,
    MEMBER_PATTERN,
    ROLE_PATTERN,
    GcloudPolicyChangedError,
    _checked,
    _send_json_abs,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.gcloud.connector import GcloudConnector

__all__ = [
    "ACCOUNT_ID_PATTERN",
    "BUCKET_NAME_PATTERN",
    "LIFECYCLE_ACTIONS",
    "LIFECYCLE_CONDITION_FIELDS",
    "LOCATION_PATTERN",
    "STORAGE_CLASSES",
    "BackupBucketOps",
]

_STORAGE_API: Final[str] = "https://storage.googleapis.com/storage/v1"

#: 3 to 63 characters of a-z, 0-9, '-' and '_'. Names with dots are not accepted.
BUCKET_NAME_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$"

#: A location such as ``EU``, ``europe-west3`` or ``eur4``.
LOCATION_PATTERN: Final[str] = r"^[A-Za-z][A-Za-z0-9-]{1,40}$"

#: A service account ID: 6 to 30 characters, starts with a letter.
ACCOUNT_ID_PATTERN: Final[str] = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"

STORAGE_CLASSES: Final[tuple[str, ...]] = ("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE")

LIFECYCLE_ACTIONS: Final[tuple[str, ...]] = (
    "Delete",
    "SetStorageClass",
    "AbortIncompleteMultipartUpload",
)

#: The lifecycle condition fields MEHO accepts, with the type of each.
LIFECYCLE_CONDITION_FIELDS: Final[dict[str, str]] = {
    "age": "integer",
    "numNewerVersions": "integer",
    "daysSinceNoncurrentTime": "integer",
    "daysSinceCustomTime": "integer",
    "isLive": "boolean",
    "matchesStorageClass": "array",
    "matchesPrefix": "array",
    "matchesSuffix": "array",
}

_NOT_CHANGED: Final[str] = "Nothing was changed."

#: Members that may never be added to a bucket: they would make it public.
_PUBLIC_MEMBERS: Final[tuple[str, ...]] = ("allUsers", "allAuthenticatedUsers")


def _conn(ops: BackupBucketOps) -> GcloudConnector:
    """The connector these methods run on. ``BackupBucketOps`` is only ever its base."""
    return cast("GcloudConnector", ops)


def _bucket(params: dict[str, Any]) -> str:
    return _checked(
        params, "bucket", BUCKET_NAME_PATTERN, "a bucket name (3-63 characters a-z, 0-9, '-', '_')"
    )


def _bucket_url(bucket: str) -> str:
    return f"{_STORAGE_API}/b/{bucket}"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_CONDITION_CHECKS: Final[dict[str, Any]] = {
    "integer": _is_int,
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v),
}


def _lifecycle_rules(raw: Any) -> list[dict[str, Any]]:
    """Check the lifecycle rules and return them in Google's shape."""
    if not isinstance(raw, list):
        raise ValueError(f"'lifecycle_rules' must be a list. {_NOT_CHANGED}")
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(raw):
        action = rule.get("action") if isinstance(rule, dict) else None
        condition = rule.get("condition") if isinstance(rule, dict) else None
        if not isinstance(action, dict) or action.get("type") not in LIFECYCLE_ACTIONS:
            raise ValueError(
                f"Lifecycle rule {index} needs an action type of "
                f"{', '.join(LIFECYCLE_ACTIONS)}. {_NOT_CHANGED}"
            )
        if (
            action["type"] == "SetStorageClass"
            and action.get("storageClass") not in STORAGE_CLASSES
        ):
            raise ValueError(
                f"Lifecycle rule {index} (SetStorageClass) needs a storageClass of "
                f"{', '.join(STORAGE_CLASSES)}. {_NOT_CHANGED}"
            )
        if not isinstance(condition, dict) or not condition:
            raise ValueError(f"Lifecycle rule {index} needs a condition. {_NOT_CHANGED}")
        for name, value in condition.items():
            kind = LIFECYCLE_CONDITION_FIELDS.get(name)
            if kind is None or not _CONDITION_CHECKS[kind](value):
                raise ValueError(
                    f"Lifecycle rule {index} has a condition MEHO does not accept ({name!r}). "
                    f"Allowed: {', '.join(LIFECYCLE_CONDITION_FIELDS)}. {_NOT_CHANGED}"
                )
        rules.append(
            {
                "action": {k: action[k] for k in ("type", "storageClass") if k in action},
                "condition": dict(condition),
            }
        )
    return rules


def _bucket_settings(bucket: dict[str, Any]) -> dict[str, Any]:
    """The settings of a bucket, in plain fields."""
    iam = bucket.get("iamConfiguration") or {}
    retention = bucket.get("retentionPolicy") or {}
    period = retention.get("retentionPeriod")
    return {
        "bucket": bucket.get("name"),
        "location": bucket.get("location"),
        "storage_class": bucket.get("storageClass"),
        "uniform_access": (iam.get("uniformBucketLevelAccess") or {}).get("enabled") is True,
        "public_access_prevention": iam.get("publicAccessPrevention"),
        "versioning": (bucket.get("versioning") or {}).get("enabled") is True,
        "retention_period_seconds": int(period) if period is not None else None,
        "retention_locked": retention.get("isLocked") is True,
        "lifecycle_rules": (bucket.get("lifecycle") or {}).get("rule") or [],
        "metageneration": bucket.get("metageneration"),
    }


def _binding_rows(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": b.get("role"),
            "members": list(b.get("members") or []),
            "condition": (b.get("condition") or {}).get("title") if b.get("condition") else None,
        }
        for b in policy.get("bindings") or []
    ]


async def _get_policy(
    connector: GcloudConnector, target: GcloudTargetLike, bucket: str, *, operator: Operator
) -> dict[str, Any]:
    """Read the IAM policy of a bucket, asking for version 3 so conditions are shown."""
    return await connector._get_json_abs(
        target,
        f"{_bucket_url(bucket)}/iam",
        operator=operator,
        params={"optionsRequestedPolicyVersion": 3},
    )


# ---------------------------------------------------------------------------
# Operation bodies
# ---------------------------------------------------------------------------


async def bucket_create(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.storage.buckets.create``: one bucket, uniform access, public access blocked."""
    name = _bucket(params)
    location = _checked(params, "location", LOCATION_PATTERN, "a location such as europe-west3")
    storage_class = params.get("storage_class", "STANDARD")
    if storage_class not in STORAGE_CLASSES:
        raise ValueError(
            f"'storage_class' must be one of {', '.join(STORAGE_CLASSES)}. {_NOT_CHANGED}"
        )
    body = {
        "name": name,
        "location": location,
        "storageClass": storage_class,
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
    }
    try:
        created = await connector._post_json_abs(
            target,
            f"{_STORAGE_API}/b?project={gcp_project(target)}",
            operator=operator,
            json_body=body,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            raise ValueError(
                f"The bucket name {name!r} is already used. Bucket names are global, so it may "
                f"belong to another project. {_NOT_CHANGED}"
            ) from exc
        raise
    return {
        **_bucket_settings(created),
        "note": (
            f"Google created the bucket in {created.get('location')}. Uniform access is on and "
            "public access is blocked."
        ),
    }


def _wanted_settings(params: dict[str, Any]) -> dict[str, Any]:
    """The settings the caller named, checked. Raises when none is named or one is bad."""
    wanted: dict[str, Any] = {}
    if "versioning" in params:
        if not isinstance(params["versioning"], bool):
            raise ValueError(f"'versioning' must be true or false. {_NOT_CHANGED}")
        wanted["versioning"] = params["versioning"]
    if "retention_period_seconds" in params:
        seconds = params["retention_period_seconds"]
        if not _is_int(seconds) or seconds < 0:
            raise ValueError(
                "'retention_period_seconds' must be a whole number of seconds, or 0 to remove "
                f"the retention period. {_NOT_CHANGED}"
            )
        wanted["retention_period_seconds"] = seconds
    if "lifecycle_rules" in params:
        wanted["lifecycle_rules"] = _lifecycle_rules(params["lifecycle_rules"])
    if not wanted:
        raise ValueError(
            "Name at least one setting to change: versioning, retention_period_seconds or "
            f"lifecycle_rules. {_NOT_CHANGED}"
        )
    return wanted


def _patch_body(wanted: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """The ``PATCH`` body: only the settings that differ from what the bucket has now."""
    body: dict[str, Any] = {}
    if "versioning" in wanted and wanted["versioning"] != before["versioning"]:
        body["versioning"] = {"enabled": wanted["versioning"]}
    seconds = wanted.get("retention_period_seconds")
    if seconds is not None and seconds != (before["retention_period_seconds"] or 0):
        if before["retention_locked"]:
            raise ValueError(
                "The retention policy of this bucket is locked, so its period cannot be "
                f"changed or removed. {_NOT_CHANGED}"
            )
        body["retentionPolicy"] = {"retentionPeriod": str(seconds)} if seconds else None
    if "lifecycle_rules" in wanted and wanted["lifecycle_rules"] != before["lifecycle_rules"]:
        body["lifecycle"] = {"rule": wanted["lifecycle_rules"]}
    return body


async def bucket_update_settings(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.storage.buckets.update_settings``: versioning, retention period, lifecycle."""
    name = _bucket(params)
    wanted = _wanted_settings(params)
    current = await connector._get_json_abs(target, _bucket_url(name), operator=operator)
    before = _bucket_settings(current)
    body = _patch_body(wanted, before)
    if not body:
        return {
            **before,
            "changed": False,
            "note": "The bucket already has these settings. MEHO changed nothing.",
        }
    patched = await _send_json_abs(
        connector,
        target,
        "PATCH",
        f"{_bucket_url(name)}?ifMetagenerationMatch={before['metageneration']}",
        operator=operator,
        json_body=body,
    )
    return {
        **_bucket_settings(patched),
        "changed": True,
        "note": "Google changed the settings of the bucket.",
    }


async def bucket_read(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.storage.buckets.read``: settings, who has access, retention lock."""
    name = _bucket(params)
    bucket = await connector._get_json_abs(target, _bucket_url(name), operator=operator)
    policy = await _get_policy(connector, target, name, operator=operator)
    return {
        **_bucket_settings(bucket),
        "access": _binding_rows(policy),
        "policy_etag": policy.get("etag"),
    }


async def bucket_access_add(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.storage.buckets.access.add``: read, add one member, write with etag."""
    name = _bucket(params)
    role = _checked(params, "role", ROLE_PATTERN, "a role such as roles/storage.objectAdmin")
    member = _checked(params, "member", MEMBER_PATTERN, "an IAM member as written in the policy")
    if member in _PUBLIC_MEMBERS or member.startswith("deleted:"):
        raise ValueError(
            f"MEHO does not add the member {member!r}: it means everyone, or an account that "
            f"is deleted. {_NOT_CHANGED}"
        )

    policy = await _get_policy(connector, target, name, operator=operator)
    bindings = list(policy.get("bindings") or [])
    plain = [b for b in bindings if b.get("role") == role and not b.get("condition")]
    result: dict[str, Any] = {
        "bucket": name,
        "role": role,
        "member": member,
        "added": False,
        "members_with_role": sum(len(b.get("members") or []) for b in plain),
        "etag": policy.get("etag"),
    }
    if any(member in (b.get("members") or []) for b in plain):
        return result
    if not policy.get("etag"):
        raise ValueError(
            "Google returned the policy without an etag, so MEHO cannot make a safe change. "
            f"{_NOT_CHANGED}"
        )

    new_bindings: list[dict[str, Any]] = []
    done = False
    for binding in bindings:
        if not done and binding in plain:
            binding = {**binding, "members": [*(binding.get("members") or []), member]}
            done = True
        new_bindings.append(binding)
    if not done:
        new_bindings.append({"role": role, "members": [member]})
    try:
        written = await _send_json_abs(
            connector,
            target,
            "PUT",
            f"{_bucket_url(name)}/iam",
            operator=operator,
            json_body={**policy, "version": 3, "bindings": new_bindings},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in _ETAG_CONFLICT_STATUSES:
            raise GcloudPolicyChangedError(
                f"The policy of bucket {name!r} changed after MEHO read it, so Google refused "
                "the change (the etag no longer matches). MEHO changed nothing and does not "
                "retry. Read the policy again and call again.",
                http_status=exc.response.status_code,
                upstream_message=None,
            ) from exc
        raise
    result.update(
        added=True, members_with_role=result["members_with_role"] + 1, etag=written.get("etag")
    )
    return result


async def service_account_create(
    connector: GcloudConnector,
    operator: Operator,
    target: GcloudTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``gcloud.iam.service_accounts.create``: a service account with no key."""
    account_id = _checked(
        params,
        "account_id",
        ACCOUNT_ID_PATTERN,
        "an account ID (6-30 characters a-z, 0-9, '-', starts with a letter)",
    )
    display_name = params.get("display_name")
    if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 100):
        raise ValueError(f"'display_name' must be text of at most 100 characters. {_NOT_CHANGED}")
    project = gcp_project(target)
    parent = f"{_IAM_API}/projects/{project}/serviceAccounts"
    body: dict[str, Any] = {"accountId": account_id}
    if display_name:
        body["serviceAccount"] = {"displayName": display_name}
    existed = False
    try:
        account = await connector._post_json_abs(target, parent, operator=operator, json_body=body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        account = await connector._get_json_abs(
            target,
            f"{parent}/{account_id}@{project}.iam.gserviceaccount.com",
            operator=operator,
        )
        existed = True
    return {
        "email": account.get("email"),
        "unique_id": account.get("uniqueId"),
        "display_name": account.get("displayName"),
        "disabled": account.get("disabled") is True,
        "already_existed": existed,
        "note": (
            "A service account with this ID already exists. MEHO changed nothing."
            if existed
            else "Google created the service account. It has no key and no role yet."
        ),
    }


class BackupBucketOps:
    """The methods of ``GcloudConnector`` that set up a backup bucket and its service account."""

    async def gcloud_bucket_create(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.storage.buckets.create``: make one bucket."""
        return await bucket_create(_conn(self), operator, target, params)

    async def gcloud_bucket_update_settings(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.storage.buckets.update_settings``: change versioning, retention, lifecycle."""
        return await bucket_update_settings(_conn(self), operator, target, params)

    async def gcloud_bucket_read(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.storage.buckets.read``: settings, access and retention lock."""
        return await bucket_read(_conn(self), operator, target, params)

    async def gcloud_bucket_access_add(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.storage.buckets.access.add``: add one member to one role."""
        return await bucket_access_add(_conn(self), operator, target, params)

    async def gcloud_sa_create(
        self, operator: Operator, target: GcloudTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``gcloud.iam.service_accounts.create``: make one service account, no key."""
        return await service_account_create(_conn(self), operator, target, params)
