# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the gcloud workload identity and service-account policy operations.

Covers the five operations in
:mod:`meho_backplane.connectors.gcloud.ops_workload_identity`:

* pool list and provider list: URL, paging, the shown fields, no key material;
* pool delete: reads first, marks deleted, says it can be restored for 30
  days, safe to run twice, clear answer for a missing pool;
* service-account policy read: asks for policy version 3;
* binding removal: drops only the named member from the named role, sends
  the etag it read, refuses a stale etag without a retry;
* strict value checks: a bad pool ID, service account, role or member never
  reaches Google;
* the checks before approval (``testIamPermissions``).

Google is never called: every answer comes from ``respx``. Auth is mocked as in
``test_connectors_gcloud_ops.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud.session import GcloudTargetLike
from meho_backplane.connectors.gcloud.workload_identity import GcloudPolicyChangedError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS, PreviewContext

_OPERATOR: Operator = synthesise_system_operator()

_PROJECT = "my-project-123"
_IAM = "https://iam.googleapis.com/v1"
_POOLS_URL = f"{_IAM}/projects/{_PROJECT}/locations/global/workloadIdentityPools"
_POOL_URL = f"{_POOLS_URL}/old-vault-pool"
_SA = "vault-auth@my-project-123.iam.gserviceaccount.com"
_SA_URL = f"{_IAM}/projects/{_PROJECT}/serviceAccounts/{_SA}"
_WIF_ROLE = "roles/iam.workloadIdentityUser"
_OLD_MEMBER = (
    "principalSet://iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/old-vault-pool/*"
)
_NEW_MEMBER = (
    "principalSet://iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/new-vault-pool/*"
)


@dataclass
class _StubTarget:
    name: str
    gcp_project: str
    gcp_impersonate_sa: str
    secret_ref: str
    auth_model: str | None = AuthModel.IMPERSONATION.value
    host: str = "gcp.invalid"
    port: int | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="gcloud-wif-test",
    gcp_project=_PROJECT,
    gcp_impersonate_sa="svc@my-project-123.iam.gserviceaccount.com",
    secret_ref="gcloud/test",
)


async def _empty_loader(_target: GcloudTargetLike, _operator: Operator) -> dict[str, Any]:
    return {}


def _patch_impersonated(**_kwargs: Any) -> MagicMock:
    creds = MagicMock()
    creds.token = None
    creds.valid = False

    def _refresh(_request: Any) -> None:
        creds.token = "test-bearer-token"
        creds.valid = True

    creds.refresh = _refresh
    return creds


def _adc_loader(scopes: list[str] | None = None) -> tuple[Any, str | None]:
    return MagicMock(), _PROJECT


@pytest.fixture
async def connector() -> Any:
    conn = GcloudConnector(credentials_loader=_empty_loader, adc_loader=_adc_loader)
    with patch("google.auth.impersonated_credentials.Credentials", side_effect=_patch_impersonated):
        yield conn
    await conn.aclose()


def _policy(etag: str = "BwYread=") -> dict[str, Any]:
    return {
        "version": 3,
        "etag": etag,
        "bindings": [
            {"role": _WIF_ROLE, "members": [_OLD_MEMBER, _NEW_MEMBER]},
            {"role": "roles/iam.serviceAccountTokenCreator", "members": [_OLD_MEMBER]},
            {
                "role": _WIF_ROLE,
                "members": [_OLD_MEMBER],
                "condition": {"title": "only-main", "expression": "true"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Pools and providers
# ---------------------------------------------------------------------------


async def test_pools_list_follows_pages_and_shows_rows(connector: GcloudConnector) -> None:
    seen: list[dict[str, str]] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if "pageToken" not in request.url.params:
            pool = {
                "name": "projects/123/locations/global/workloadIdentityPools/old-vault-pool",
                "displayName": "Old Vault",
                "state": "ACTIVE",
            }
            return httpx.Response(
                200, json={"workloadIdentityPools": [pool], "nextPageToken": "p2"}
            )
        deleted = {
            "name": "projects/123/locations/global/workloadIdentityPools/gone-pool",
            "state": "DELETED",
            "disabled": True,
            "expireTime": "2026-11-28T00:00:00Z",
        }
        return httpx.Response(200, json={"workloadIdentityPools": [deleted]})

    with respx.mock() as mock:
        mock.get(_POOLS_URL).mock(side_effect=_answer)
        result = await connector.gcloud_wif_pools_list(
            _OPERATOR, _TARGET, params={"show_deleted": True}
        )

    assert result["total"] == 2
    assert result["rows"][0]["pool_id"] == "old-vault-pool"
    assert result["rows"][0]["display_name"] == "Old Vault"
    assert result["rows"][1] == {
        "pool_id": "gone-pool",
        "name": "projects/123/locations/global/workloadIdentityPools/gone-pool",
        "display_name": None,
        "description": None,
        "state": "DELETED",
        "disabled": True,
        "expire_time": "2026-11-28T00:00:00Z",
    }
    assert seen[0] == {"showDeleted": "true"}
    assert seen[1] == {"showDeleted": "true", "pageToken": "p2"}


async def test_providers_list_shows_issuer_and_condition_without_key_material(
    connector: GcloudConnector,
) -> None:
    provider = {
        "name": (
            "projects/123/locations/global/workloadIdentityPools/old-vault-pool/providers/vault"
        ),
        "state": "ACTIVE",
        "oidc": {
            "issuerUri": "https://vault.example.com/v1/identity/oidc",
            "allowedAudiences": ["gcp"],
            "jwksJson": '{"keys": []}',
        },
        "attributeMapping": {"google.subject": "assertion.sub"},
        "attributeCondition": "assertion.role == 'gcp'",
    }
    with respx.mock() as mock:
        mock.get(f"{_POOL_URL}/providers").respond(
            200, json={"workloadIdentityPoolProviders": [provider]}
        )
        result = await connector.gcloud_wif_providers_list(
            _OPERATOR, _TARGET, params={"pool_id": "old-vault-pool"}
        )

    row = result["rows"][0]
    assert result["total"] == 1
    assert row["provider_id"] == "vault"
    assert row["kind"] == "oidc"
    assert row["issuer_uri"] == "https://vault.example.com/v1/identity/oidc"
    assert row["allowed_audiences"] == ["gcp"]
    assert row["attribute_condition"] == "assertion.role == 'gcp'"
    assert row["attribute_mapping"] == {"google.subject": "assertion.sub"}
    assert "jwksJson" not in json.dumps(result)


# ---------------------------------------------------------------------------
# Pool delete
# ---------------------------------------------------------------------------


async def test_pool_delete_marks_deleted_and_says_it_can_be_restored(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        get = mock.get(_POOL_URL).respond(200, json={"name": "x", "state": "ACTIVE"})
        delete = mock.delete(_POOL_URL).respond(
            200, json={"name": "projects/123/locations/global/operations/op-1", "done": True}
        )
        result = await connector.gcloud_wif_pool_delete(
            _OPERATOR, _TARGET, params={"pool_id": "old-vault-pool"}
        )

    assert get.call_count == 1
    assert delete.call_count == 1
    assert result["state"] == "DELETED"
    assert result["already_deleted"] is False
    assert result["restorable_for_days"] == 30
    assert result["operation_done"] is True
    assert "30 days" in result["note"]
    assert "restored" in result["note"]


async def test_pool_delete_on_a_deleted_pool_sends_nothing(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_POOL_URL).respond(200, json={"name": "x", "state": "DELETED"})
        delete = mock.delete(_POOL_URL).respond(200, json={})
        result = await connector.gcloud_wif_pool_delete(
            _OPERATOR, _TARGET, params={"pool_id": "old-vault-pool"}
        )

    assert delete.call_count == 0
    assert result["already_deleted"] is True
    assert result["restorable_for_days"] == 30


async def test_pool_delete_of_a_missing_pool_is_not_found(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.get(_POOL_URL).respond(404, json={"error": {"code": 404}})
        with pytest.raises(ConnectorResourceNotFoundError):
            await connector.gcloud_wif_pool_delete(
                _OPERATOR, _TARGET, params={"pool_id": "old-vault-pool"}
            )


# ---------------------------------------------------------------------------
# Strict value checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pool_id", ["../other", "old-vault-pool/providers", "gcp-reserved", "abc", "A-POOL", "x\n"]
)
async def test_bad_pool_id_never_reaches_google(connector: GcloudConnector, pool_id: str) -> None:
    with respx.mock() as mock:  # no route: any call would fail the test
        with pytest.raises(ValueError, match="pool_id"):
            await connector.gcloud_wif_pool_delete(_OPERATOR, _TARGET, params={"pool_id": pool_id})
        with pytest.raises(ValueError, match="pool_id"):
            await connector.gcloud_wif_providers_list(
                _OPERATOR, _TARGET, params={"pool_id": pool_id}
            )
        assert not mock.calls


@pytest.mark.parametrize(
    ("override", "key"),
    [
        ({"service_account_email": f"{_SA}/keys"}, "service_account_email"),
        ({"service_account_email": "a@b.com"}, "service_account_email"),
        ({"service_account_email": "../x@y.iam.gserviceaccount.com"}, "service_account_email"),
        ({"role": "roles/x y"}, "role"),
        ({"role": "editor"}, "role"),
        ({"member": "user:a b@example.com"}, "member"),
        ({"member": "someone"}, "member"),
        ({"member": "principal://evil.example.com/x"}, "member"),
    ],
)
async def test_bad_binding_values_never_reach_google(
    connector: GcloudConnector, override: dict[str, str], key: str
) -> None:
    params = {"service_account_email": _SA, "role": _WIF_ROLE, "member": _OLD_MEMBER, **override}
    with respx.mock() as mock:
        with pytest.raises(ValueError, match=key):
            await connector.gcloud_sa_binding_remove(_OPERATOR, _TARGET, params=params)
        assert not mock.calls


# ---------------------------------------------------------------------------
# Service-account policy
# ---------------------------------------------------------------------------


async def test_sa_policy_read_asks_for_version_3(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        result = await connector.gcloud_sa_policy_read(
            _OPERATOR, _TARGET, params={"service_account_email": _SA}
        )

    assert json.loads(route.calls[0].request.content) == {"options": {"requestedPolicyVersion": 3}}
    assert result["etag"] == "BwYread="
    assert result["version"] == 3
    assert result["bindings"][0] == {
        "role": _WIF_ROLE,
        "members": [_OLD_MEMBER, _NEW_MEMBER],
        "condition": None,
    }
    assert result["bindings"][2]["condition"]["title"] == "only-main"


async def test_binding_remove_drops_only_the_member_and_sends_the_read_etag(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={"etag": "BwYnew="})
        result = await connector.gcloud_sa_binding_remove(
            _OPERATOR,
            _TARGET,
            params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _OLD_MEMBER},
        )

    sent = json.loads(set_route.calls[0].request.content)["policy"]
    assert sent["etag"] == "BwYread="
    assert sent["version"] == 3
    assert sent["bindings"] == [
        {"role": _WIF_ROLE, "members": [_NEW_MEMBER]},
        {"role": "roles/iam.serviceAccountTokenCreator", "members": [_OLD_MEMBER]},
        {
            "role": _WIF_ROLE,
            "members": [_OLD_MEMBER],
            "condition": {"title": "only-main", "expression": "true"},
        },
    ]
    assert result == {
        "service_account_email": _SA,
        "role": _WIF_ROLE,
        "member": _OLD_MEMBER,
        "removed": True,
        "members_left": 1,
        "etag": "BwYnew=",
    }


async def test_binding_remove_drops_a_binding_left_empty(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={"etag": "BwYnew="})
        result = await connector.gcloud_sa_binding_remove(
            _OPERATOR,
            _TARGET,
            params={
                "service_account_email": _SA,
                "role": "roles/iam.serviceAccountTokenCreator",
                "member": _OLD_MEMBER,
            },
        )

    roles = [
        b["role"] for b in json.loads(set_route.calls[0].request.content)["policy"]["bindings"]
    ]
    assert roles == [_WIF_ROLE, _WIF_ROLE]
    assert result["members_left"] == 0


async def test_binding_remove_of_an_absent_member_sends_nothing(
    connector: GcloudConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={})
        result = await connector.gcloud_sa_binding_remove(
            _OPERATOR,
            _TARGET,
            params={
                "service_account_email": _SA,
                "role": "roles/iam.serviceAccountTokenCreator",
                "member": _NEW_MEMBER,
            },
        )

    assert set_route.call_count == 0
    assert result["removed"] is False
    assert result["members_left"] == 1


async def test_binding_remove_refuses_a_member_held_only_through_a_condition(
    connector: GcloudConnector,
) -> None:
    policy = _policy()
    policy["bindings"] = [policy["bindings"][2]]
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=policy)
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={})
        with pytest.raises(ValueError, match="condition"):
            await connector.gcloud_sa_binding_remove(
                _OPERATOR,
                _TARGET,
                params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _OLD_MEMBER},
            )
    assert set_route.call_count == 0


async def test_binding_remove_refuses_a_stale_etag_without_retry(
    connector: GcloudConnector,
) -> None:
    conflict = {"error": {"code": 409, "status": "ABORTED", "message": "concurrent changes"}}
    with respx.mock() as mock:
        get_route = mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(409, json=conflict)
        with pytest.raises(GcloudPolicyChangedError, match="changed after MEHO read it") as info:
            await connector.gcloud_sa_binding_remove(
                _OPERATOR,
                _TARGET,
                params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _OLD_MEMBER},
            )

    assert info.value.http_status == 409
    assert get_route.call_count == 1
    assert set_route.call_count == 1


# ---------------------------------------------------------------------------
# Checks before approval
# ---------------------------------------------------------------------------


def _ctx(connector: GcloudConnector, op_id: str, params: dict[str, Any]) -> PreviewContext:
    descriptor = MagicMock()
    descriptor.op_id = op_id
    return PreviewContext(
        descriptor=descriptor,
        connector_instance=connector,
        operator=_OPERATOR,
        target=_TARGET,
        params=params,
    )


_DELETE_OP = "gcloud.iam.workload_identity_pools.delete"
_REMOVE_OP = "gcloud.iam.service_accounts.binding.remove"
_REMOVE_PARAMS = {"service_account_email": _SA, "role": _WIF_ROLE, "member": _OLD_MEMBER}


def test_both_writes_have_a_check_before_approval() -> None:
    assert _DELETE_OP in _PERMISSION_PREFLIGHTS
    assert _REMOVE_OP in _PERMISSION_PREFLIGHTS


async def test_pool_delete_check_passes_with_the_permission(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_POOL_URL}:testIamPermissions").respond(
            200, json={"permissions": ["iam.workloadIdentityPools.delete"]}
        )
        check = await _PERMISSION_PREFLIGHTS[_DELETE_OP](
            _ctx(connector, _DELETE_OP, {"pool_id": "old-vault-pool"})
        )

    assert json.loads(route.calls[0].request.content) == {
        "permissions": ["iam.workloadIdentityPools.delete"]
    }
    assert check is not None
    assert check["will_be_denied"] is False
    assert (
        check["path"]
        == f"projects/{_PROJECT}/locations/global/workloadIdentityPools/old-vault-pool"
    )


async def test_binding_remove_check_denies_without_set_permission(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:testIamPermissions").respond(
            200, json={"permissions": ["iam.serviceAccounts.getIamPolicy"]}
        )
        check = await _PERMISSION_PREFLIGHTS[_REMOVE_OP](
            _ctx(connector, _REMOVE_OP, _REMOVE_PARAMS)
        )

    assert check is not None
    assert check["will_be_denied"] is True
    assert check["granted"] == ["iam.serviceAccounts.getIamPolicy"]


async def test_check_denies_a_missing_pool(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.post(f"{_POOL_URL}:testIamPermissions").respond(404, json={"error": {"code": 404}})
        check = await _PERMISSION_PREFLIGHTS[_DELETE_OP](
            _ctx(connector, _DELETE_OP, {"pool_id": "old-vault-pool"})
        )

    assert check is not None
    assert check["will_be_denied"] is True
    assert check["reason"] == "pool_not_found"


async def test_check_is_skipped_when_google_fails(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:testIamPermissions").respond(500, json={})
        check = await _PERMISSION_PREFLIGHTS[_REMOVE_OP](
            _ctx(connector, _REMOVE_OP, _REMOVE_PARAMS)
        )

    assert check is None


async def test_check_is_skipped_for_a_bad_value(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        check = await _PERMISSION_PREFLIGHTS[_DELETE_OP](
            _ctx(connector, _DELETE_OP, {"pool_id": "../x"})
        )
        assert not mock.calls
    assert check is None
