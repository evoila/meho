# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the gcloud operations that set up a backup bucket and its service account.

Covers the five operations in
:mod:`meho_backplane.connectors.gcloud.ops_backup_bucket`:

* bucket create: URL and body (uniform access, public access blocked), a
  name that is already used, bad names and values refused before any call;
* settings update: sends only what differs, sends the metageneration it
  read, sends nothing for the same settings, refuses a locked retention
  period, refuses bad lifecycle rules and an empty change;
* bucket read: settings, access and lock; a failed policy read fails the
  call instead of showing partial data;
* access add: adds only the named member, sends the etag it read, keeps a
  member that is already there, refuses a stale etag, public members and
  a policy without an etag;
* service account create: no key, an ID already used, bad values;
* the parameter schemas and the safety levels.

Google is never called: every answer comes from ``respx``. The connector,
target and helpers come from ``test_connectors_gcloud_workload_identity``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx

from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud.ops import GCLOUD_OPS
from meho_backplane.connectors.gcloud.workload_identity import GcloudPolicyChangedError
from meho_backplane.operations._validate import validate_params
from tests.test_connectors_gcloud_workload_identity import (
    _IAM,
    _OPERATOR,
    _PROJECT,
    _TARGET,
    connector,
    no_wait,
)

__all__ = ["connector", "no_wait"]

_STORAGE = "https://storage.googleapis.com/storage/v1"
_BUCKET = "example-backups"
_BUCKET_URL = f"{_STORAGE}/b/{_BUCKET}"
_IAM_URL = f"{_BUCKET_URL}/iam"
_ROLE = "roles/storage.objectAdmin"
_WRITER = "serviceAccount:backup-writer@my-project-123.iam.gserviceaccount.com"
_OTHER = "group:ops@example.com"
_SA_ID = "backup-writer"
_SA_PARENT = f"{_IAM}/projects/{_PROJECT}/serviceAccounts"
_SA_EMAIL = f"{_SA_ID}@{_PROJECT}.iam.gserviceaccount.com"


def _op(op_id: str) -> Any:
    return next(o for o in GCLOUD_OPS if o.op_id == op_id)


def _bucket(**extra: Any) -> dict[str, Any]:
    return {
        "name": _BUCKET,
        "location": "EUROPE-WEST3",
        "storageClass": "STANDARD",
        "metageneration": "3",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        **extra,
    }


def _policy(etag: str = "CAE=") -> dict[str, Any]:
    return {
        "version": 3,
        "etag": etag,
        "bindings": [
            {"role": _ROLE, "members": [_OTHER]},
            {
                "role": "roles/storage.objectViewer",
                "members": [_OTHER],
                "condition": {"title": "office hours", "expression": "true"},
            },
        ],
    }


def _forbidden() -> Any:
    import httpx

    return httpx.Response(403, json={"error": {"message": "denied"}})


# ---------------------------------------------------------------------------
# Bucket create
# ---------------------------------------------------------------------------


async def test_bucket_create_sends_a_private_bucket(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_STORAGE}/b").respond(200, json=_bucket())
        result = await connector.gcloud_bucket_create(
            _OPERATOR,
            _TARGET,
            params={"bucket": _BUCKET, "location": "europe-west3", "storage_class": "NEARLINE"},
        )

    request = route.calls[0].request
    assert request.url.params["project"] == _PROJECT
    assert json.loads(request.content) == {
        "name": _BUCKET,
        "location": "europe-west3",
        "storageClass": "NEARLINE",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
    }
    assert result["bucket"] == _BUCKET
    assert result["uniform_access"] is True
    assert result["public_access_prevention"] == "enforced"
    assert result["retention_locked"] is False


async def test_bucket_create_defaults_to_standard_class(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_STORAGE}/b").respond(200, json=_bucket())
        await connector.gcloud_bucket_create(
            _OPERATOR, _TARGET, params={"bucket": _BUCKET, "location": "EU"}
        )
    assert json.loads(route.calls[0].request.content)["storageClass"] == "STANDARD"


async def test_bucket_create_fails_clearly_when_the_name_is_used(
    connector: GcloudConnector,
) -> None:
    import httpx

    with respx.mock() as mock:
        mock.post(f"{_STORAGE}/b").mock(return_value=httpx.Response(409, json={}))
        with pytest.raises(ValueError, match="already used"):
            await connector.gcloud_bucket_create(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "location": "EU"}
            )


async def test_bucket_create_does_not_hide_a_forbidden_answer(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock() as mock:
        mock.post(f"{_STORAGE}/b").mock(return_value=_forbidden())
        with pytest.raises(httpx.HTTPStatusError):
            await connector.gcloud_bucket_create(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "location": "EU"}
            )


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": "Bad_Name", "location": "EU"},
        {"bucket": "a", "location": "EU"},
        {"bucket": "my.bucket", "location": "EU"},
        {"bucket": "../x/b", "location": "EU"},
        {"bucket": _BUCKET, "location": "../EU"},
        {"bucket": _BUCKET, "location": "EU", "storage_class": "FAST"},
        {"location": "EU"},
    ],
)
async def test_bucket_create_refuses_bad_values_before_any_call(
    connector: GcloudConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{_STORAGE}/b")
        with pytest.raises(ValueError):
            await connector.gcloud_bucket_create(_OPERATOR, _TARGET, params=params)
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Settings update
# ---------------------------------------------------------------------------


async def test_update_sends_only_the_changed_settings_with_the_metageneration(
    connector: GcloudConnector,
) -> None:
    rules = [{"action": {"type": "Delete"}, "condition": {"age": 90}}]
    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(200, json=_bucket(versioning={"enabled": True}))
        patch_route = mock.patch(_BUCKET_URL).respond(
            200,
            json=_bucket(
                versioning={"enabled": True},
                retentionPolicy={"retentionPeriod": "2592000"},
                lifecycle={"rule": rules},
                metageneration="4",
            ),
        )
        result = await connector.gcloud_bucket_update_settings(
            _OPERATOR,
            _TARGET,
            params={
                "bucket": _BUCKET,
                "versioning": True,
                "retention_period_seconds": 2592000,
                "lifecycle_rules": rules,
            },
        )

    request = patch_route.calls[0].request
    assert request.url.params["ifMetagenerationMatch"] == "3"
    assert json.loads(request.content) == {
        "retentionPolicy": {"retentionPeriod": "2592000"},
        "lifecycle": {"rule": rules},
    }
    assert result["changed"] is True
    assert result["retention_period_seconds"] == 2592000
    assert result["retention_locked"] is False
    assert result["lifecycle_rules"] == rules


async def test_update_with_the_same_settings_sends_nothing(connector: GcloudConnector) -> None:
    rules = [{"action": {"type": "Delete"}, "condition": {"age": 90}}]
    current = _bucket(
        versioning={"enabled": True},
        retentionPolicy={"retentionPeriod": "60"},
        lifecycle={"rule": rules},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BUCKET_URL).respond(200, json=current)
        patch_route = mock.patch(_BUCKET_URL)
        result = await connector.gcloud_bucket_update_settings(
            _OPERATOR,
            _TARGET,
            params={
                "bucket": _BUCKET,
                "versioning": True,
                "retention_period_seconds": 60,
                "lifecycle_rules": rules,
            },
        )
    assert patch_route.call_count == 0
    assert result["changed"] is False


async def test_update_zero_removes_the_retention_period(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(200, json=_bucket(retentionPolicy={"retentionPeriod": "60"}))
        patch_route = mock.patch(_BUCKET_URL).respond(200, json=_bucket())
        result = await connector.gcloud_bucket_update_settings(
            _OPERATOR, _TARGET, params={"bucket": _BUCKET, "retention_period_seconds": 0}
        )
    assert json.loads(patch_route.calls[0].request.content) == {"retentionPolicy": None}
    assert result["retention_period_seconds"] is None


async def test_update_refuses_to_change_a_locked_retention_period(
    connector: GcloudConnector,
) -> None:
    locked = _bucket(retentionPolicy={"retentionPeriod": "60", "isLocked": True})
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BUCKET_URL).respond(200, json=locked)
        patch_route = mock.patch(_BUCKET_URL)
        with pytest.raises(ValueError, match="locked"):
            await connector.gcloud_bucket_update_settings(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "retention_period_seconds": 120}
            )
    assert patch_route.call_count == 0


async def test_update_keeps_a_locked_period_when_it_is_the_same(
    connector: GcloudConnector,
) -> None:
    locked = _bucket(retentionPolicy={"retentionPeriod": "60", "isLocked": True})
    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(200, json=locked)
        patch_route = mock.patch(_BUCKET_URL).respond(200, json=locked)
        result = await connector.gcloud_bucket_update_settings(
            _OPERATOR,
            _TARGET,
            params={"bucket": _BUCKET, "retention_period_seconds": 60, "versioning": True},
        )
    assert json.loads(patch_route.calls[0].request.content) == {"versioning": {"enabled": True}}
    assert result["retention_locked"] is True


async def test_update_fails_when_the_bucket_changed_in_between(
    connector: GcloudConnector,
) -> None:
    import httpx

    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(200, json=_bucket())
        mock.patch(_BUCKET_URL).mock(return_value=httpx.Response(412, json={}))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.gcloud_bucket_update_settings(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "versioning": True}
            )


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": _BUCKET},
        {"bucket": _BUCKET, "versioning": "yes"},
        {"bucket": _BUCKET, "retention_period_seconds": -1},
        {"bucket": _BUCKET, "retention_period_seconds": True},
        {"bucket": _BUCKET, "retention_period_seconds": "60"},
        {"bucket": _BUCKET, "lifecycle_rules": "delete"},
        {"bucket": _BUCKET, "lifecycle_rules": [{"action": {"type": "Wipe"}, "condition": {}}]},
        {"bucket": _BUCKET, "lifecycle_rules": [{"action": {"type": "Delete"}, "condition": {}}]},
        {
            "bucket": _BUCKET,
            "lifecycle_rules": [{"action": {"type": "Delete"}, "condition": {"age": "old"}}],
        },
        {
            "bucket": _BUCKET,
            "lifecycle_rules": [{"action": {"type": "Delete"}, "condition": {"colour": 1}}],
        },
        {
            "bucket": _BUCKET,
            "lifecycle_rules": [{"action": {"type": "SetStorageClass"}, "condition": {"age": 1}}],
        },
        {"bucket": "../b", "versioning": True},
    ],
)
async def test_update_refuses_bad_values_before_any_call(
    connector: GcloudConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        read = mock.get(_BUCKET_URL)
        with pytest.raises(ValueError):
            await connector.gcloud_bucket_update_settings(_OPERATOR, _TARGET, params=params)
    assert read.call_count == 0


# ---------------------------------------------------------------------------
# Bucket read
# ---------------------------------------------------------------------------


async def test_read_shows_settings_access_and_lock(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(
            200,
            json=_bucket(
                versioning={"enabled": True},
                retentionPolicy={"retentionPeriod": "60", "isLocked": True},
            ),
        )
        iam = mock.get(_IAM_URL).respond(200, json=_policy())
        result = await connector.gcloud_bucket_read(_OPERATOR, _TARGET, params={"bucket": _BUCKET})

    assert iam.calls[0].request.url.params["optionsRequestedPolicyVersion"] == "3"
    assert result["retention_locked"] is True
    assert result["retention_period_seconds"] == 60
    assert result["versioning"] is True
    assert result["policy_etag"] == "CAE="
    assert result["access"] == [
        {"role": _ROLE, "members": [_OTHER], "condition": None},
        {"role": "roles/storage.objectViewer", "members": [_OTHER], "condition": "office hours"},
    ]


async def test_read_shows_a_bucket_with_no_settings_as_off_not_unknown(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(200, json={"name": _BUCKET})
        mock.get(_IAM_URL).respond(200, json={"etag": "CAE="})
        result = await connector.gcloud_bucket_read(_OPERATOR, _TARGET, params={"bucket": _BUCKET})
    assert result["access"] == []
    assert result["versioning"] is False
    assert result["retention_period_seconds"] is None
    assert result["public_access_prevention"] is None
    assert result["lifecycle_rules"] == []


async def test_read_fails_when_the_policy_cannot_be_read(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock() as mock:
        mock.get(_BUCKET_URL).respond(200, json=_bucket())
        mock.get(_IAM_URL).mock(return_value=_forbidden())
        with pytest.raises(httpx.HTTPStatusError):
            await connector.gcloud_bucket_read(_OPERATOR, _TARGET, params={"bucket": _BUCKET})


async def test_read_fails_when_the_bucket_does_not_exist(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BUCKET_URL).mock(return_value=httpx.Response(404, json={}))
        iam = mock.get(_IAM_URL)
        with pytest.raises(httpx.HTTPStatusError):
            await connector.gcloud_bucket_read(_OPERATOR, _TARGET, params={"bucket": _BUCKET})
    assert iam.call_count == 0


# ---------------------------------------------------------------------------
# Access add
# ---------------------------------------------------------------------------


async def test_access_add_adds_only_the_member_and_sends_the_etag(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.get(_IAM_URL).respond(200, json=_policy())
        put = mock.put(_IAM_URL).respond(200, json={"etag": "CAI="})
        result = await connector.gcloud_bucket_access_add(
            _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _WRITER}
        )

    sent = json.loads(put.calls[0].request.content)
    original = _policy()
    assert sent["etag"] == "CAE="
    assert sent["version"] == 3
    assert sent["bindings"][0] == {"role": _ROLE, "members": [_OTHER, _WRITER]}
    assert sent["bindings"][1:] == original["bindings"][1:]
    assert result == {
        "bucket": _BUCKET,
        "role": _ROLE,
        "member": _WRITER,
        "added": True,
        "members_with_role": 2,
        "etag": "CAI=",
    }


async def test_access_add_makes_a_new_binding_when_the_role_has_none(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.get(_IAM_URL).respond(200, json={"etag": "CAE="})
        put = mock.put(_IAM_URL).respond(200, json={"etag": "CAI="})
        result = await connector.gcloud_bucket_access_add(
            _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _WRITER}
        )
    assert json.loads(put.calls[0].request.content)["bindings"] == [
        {"role": _ROLE, "members": [_WRITER]}
    ]
    assert result["members_with_role"] == 1


async def test_access_add_ignores_a_member_held_only_through_a_condition(
    connector: GcloudConnector,
) -> None:
    policy = {
        "etag": "CAE=",
        "bindings": [
            {"role": _ROLE, "members": [_WRITER], "condition": {"title": "t", "expression": "x"}}
        ],
    }
    with respx.mock() as mock:
        mock.get(_IAM_URL).respond(200, json=policy)
        put = mock.put(_IAM_URL).respond(200, json={"etag": "CAI="})
        result = await connector.gcloud_bucket_access_add(
            _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _WRITER}
        )
    sent = json.loads(put.calls[0].request.content)["bindings"]
    assert sent[0] == policy["bindings"][0]
    assert sent[1] == {"role": _ROLE, "members": [_WRITER]}
    assert result["added"] is True


async def test_access_add_keeps_a_member_that_is_already_there(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_IAM_URL).respond(200, json=_policy())
        put = mock.put(_IAM_URL)
        result = await connector.gcloud_bucket_access_add(
            _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _OTHER}
        )
    assert put.call_count == 0
    assert result["added"] is False
    assert result["members_with_role"] == 1


async def test_access_add_refuses_a_stale_etag_without_retry(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock() as mock:
        mock.get(_IAM_URL).respond(200, json=_policy())
        put = mock.put(_IAM_URL).mock(return_value=httpx.Response(412, json={}))
        with pytest.raises(GcloudPolicyChangedError, match="changed after MEHO read it"):
            await connector.gcloud_bucket_access_add(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _WRITER}
            )
    assert put.call_count == 1


async def test_access_add_does_not_hide_other_write_errors(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock() as mock:
        mock.get(_IAM_URL).respond(200, json=_policy())
        mock.put(_IAM_URL).mock(return_value=_forbidden())
        with pytest.raises(httpx.HTTPStatusError):
            await connector.gcloud_bucket_access_add(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _WRITER}
            )


async def test_access_add_refuses_a_policy_without_an_etag(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_IAM_URL).respond(200, json={"bindings": []})
        put = mock.put(_IAM_URL)
        with pytest.raises(ValueError, match="without an etag"):
            await connector.gcloud_bucket_access_add(
                _OPERATOR, _TARGET, params={"bucket": _BUCKET, "role": _ROLE, "member": _WRITER}
            )
    assert put.call_count == 0


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": _BUCKET, "role": _ROLE, "member": "allUsers"},
        {"bucket": _BUCKET, "role": _ROLE, "member": "allAuthenticatedUsers"},
        {"bucket": _BUCKET, "role": _ROLE, "member": "deleted:user:a@example.com?uid=1"},
        {"bucket": _BUCKET, "role": "admin", "member": _WRITER},
        {"bucket": _BUCKET, "role": _ROLE, "member": "someone"},
        {"bucket": "../b", "role": _ROLE, "member": _WRITER},
    ],
)
async def test_access_add_refuses_bad_values_before_any_call(
    connector: GcloudConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        read = mock.get(_IAM_URL)
        with pytest.raises(ValueError):
            await connector.gcloud_bucket_access_add(_OPERATOR, _TARGET, params=params)
    assert read.call_count == 0


# ---------------------------------------------------------------------------
# Service account create
# ---------------------------------------------------------------------------


async def test_sa_create_makes_an_account_with_no_key(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_SA_PARENT).respond(
            200,
            json={
                "email": _SA_EMAIL,
                "uniqueId": "123",
                "displayName": "Backup writer",
                "name": f"projects/{_PROJECT}/serviceAccounts/{_SA_EMAIL}",
            },
        )
        keys = mock.post(f"{_SA_PARENT}/{_SA_EMAIL}/keys")
        result = await connector.gcloud_sa_create(
            _OPERATOR,
            _TARGET,
            params={"account_id": _SA_ID, "display_name": "Backup writer"},
        )

    assert json.loads(route.calls[0].request.content) == {
        "accountId": _SA_ID,
        "serviceAccount": {"displayName": "Backup writer"},
    }
    assert keys.call_count == 0
    assert result["email"] == _SA_EMAIL
    assert result["unique_id"] == "123"
    assert result["already_existed"] is False
    assert "key" in result["note"]


async def test_sa_create_shows_the_account_that_already_exists(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock() as mock:
        mock.post(_SA_PARENT).mock(return_value=httpx.Response(409, json={}))
        mock.get(f"{_SA_PARENT}/{_SA_EMAIL}").respond(
            200, json={"email": _SA_EMAIL, "uniqueId": "123", "disabled": True}
        )
        result = await connector.gcloud_sa_create(_OPERATOR, _TARGET, params={"account_id": _SA_ID})
    assert result["already_existed"] is True
    assert result["disabled"] is True
    assert result["email"] == _SA_EMAIL


async def test_sa_create_does_not_hide_a_forbidden_answer(connector: GcloudConnector) -> None:
    import httpx

    with respx.mock() as mock:
        mock.post(_SA_PARENT).mock(return_value=_forbidden())
        with pytest.raises(httpx.HTTPStatusError):
            await connector.gcloud_sa_create(_OPERATOR, _TARGET, params={"account_id": _SA_ID})


@pytest.mark.parametrize(
    "params",
    [
        {"account_id": "short"},
        {"account_id": "1backup-writer"},
        {"account_id": "Backup-Writer"},
        {"account_id": "backup-writer-", "display_name": "x"},
        {"account_id": "../backup-writer"},
        {"account_id": _SA_ID, "display_name": "x" * 101},
        {"account_id": _SA_ID, "display_name": 5},
        {},
    ],
)
async def test_sa_create_refuses_bad_values_before_any_call(
    connector: GcloudConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_SA_PARENT)
        with pytest.raises(ValueError):
            await connector.gcloud_sa_create(_OPERATOR, _TARGET, params=params)
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Schemas and risk levels
# ---------------------------------------------------------------------------

_LEVELS = {
    "gcloud.storage.buckets.create": ("caution", True),
    "gcloud.storage.buckets.update_settings": ("caution", True),
    "gcloud.storage.buckets.read": ("safe", False),
    "gcloud.storage.buckets.access.add": ("dangerous", True),
    "gcloud.iam.service_accounts.create": ("caution", True),
}


@pytest.mark.parametrize("op_id", sorted(_LEVELS))
def test_risk_level_and_approval_follow_the_issue(op_id: str) -> None:
    op = _op(op_id)
    assert (op.safety_level, op.requires_approval) == _LEVELS[op_id]


@pytest.mark.parametrize("op_id", sorted(_LEVELS))
def test_examples_validate(op_id: str) -> None:
    schema = _op(op_id).parameter_schema
    for example in schema["examples"]:
        assert validate_params(schema, example) == []


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("gcloud.storage.buckets.create", {"bucket": "UPPER", "location": "EU"}),
        ("gcloud.storage.buckets.create", {"bucket": _BUCKET, "location": "EU", "x": 1}),
        ("gcloud.storage.buckets.update_settings", {"bucket": _BUCKET}),
        (
            "gcloud.storage.buckets.update_settings",
            {"bucket": _BUCKET, "lock_retention": True},
        ),
        (
            "gcloud.storage.buckets.access.add",
            {"bucket": _BUCKET, "role": _ROLE, "member": "allUsers2"},
        ),
        ("gcloud.iam.service_accounts.create", {"account_id": _SA_ID, "create_key": True}),
    ],
)
def test_schemas_refuse_bad_calls_before_approval(op_id: str, params: dict[str, Any]) -> None:
    assert validate_params(_op(op_id).parameter_schema, params) != []
