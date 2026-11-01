# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the gcloud operations that set up keyless Google access.

Covers the four operations in
:mod:`meho_backplane.connectors.gcloud.ops_workload_identity_write`:

* pool create: URL and body, waits for Google's operation and reads the
  pool, so the result says ACTIVE; a pending result; an ID already in use;
  an ID held by a deleted pool;
* provider create: OIDC body with uploaded public keys, audiences, mapping
  and condition; the result shows issuer, key count and condition, never
  key values; a provider that already exists;
* the key check: private parts, a wrong key type, unknown fields and
  missing public parts are refused before any call;
* key update: PATCH with ``updateMask=oidc.jwksJson``; the same keys send
  nothing; a missing or non-OIDC provider is refused;
* binding add: adds only the named member to the named role, sends the etag
  it read, keeps a member that is already there, refuses a stale etag,
  everyone-members and hidden conditions;
* the checks before approval and the parameter schemas.

Google is never called: every answer comes from ``respx``. The connector,
target and helpers come from ``test_connectors_gcloud_workload_identity``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx

from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud.ops import GCLOUD_OPS
from meho_backplane.connectors.gcloud.workload_identity import GcloudPolicyChangedError
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS, build_permission_preflight
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.dispatcher import _preflight_denied_refusal
from tests.test_connectors_gcloud_workload_identity import (
    _IAM,
    _NEW_MEMBER,
    _OLD_MEMBER,
    _OPERATOR,
    _POOLS_URL,
    _PROJECT,
    _PROJECT_TEST_URL,
    _SA,
    _SA_URL,
    _TARGET,
    _WIF_ROLE,
    _ctx,
    _policy,
    connector,
    no_wait,
)

__all__ = ["connector", "no_wait"]

_POOL = "cluster-pool"
_POOL_NAME = f"projects/{_PROJECT}/locations/global/workloadIdentityPools/{_POOL}"
_NEW_POOL_URL = f"{_IAM}/{_POOL_NAME}"
_PROVIDERS_URL = f"{_NEW_POOL_URL}/providers"
_PROVIDER_NAME = f"{_POOL_NAME}/providers/cluster-oidc"
_PROVIDER_URL = f"{_IAM}/{_PROVIDER_NAME}"
_POOL_OP = f"{_POOL_NAME}/operations/op-create"
_PROVIDER_OP = f"{_PROVIDER_NAME}/operations/op-1"
_ISSUER = "https://kubernetes.default.svc.cluster.local"

_RSA_KEY = {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": "k1", "n": "bW9k", "e": "AQAB"}
_EC_KEY = {"kty": "EC", "kid": "k2", "crv": "P-256", "x": "eHg", "y": "eXk"}
_JWKS = {"keys": [_RSA_KEY, _EC_KEY]}

_PROVIDER_PARAMS: dict[str, Any] = {
    "pool_id": _POOL,
    "provider_id": "cluster-oidc",
    "issuer_uri": _ISSUER,
    "jwks": _JWKS,
    "allowed_audiences": ["search-reader"],
    "attribute_mapping": {"google.subject": "assertion.sub"},
    "attribute_condition": "assertion.sub == 'system:serviceaccount:search:reader'",
}


def _op(op_id: str) -> Any:
    return next(o for o in GCLOUD_OPS if o.op_id == op_id)


def _provider(jwks: dict[str, Any] | None = None, state: str = "ACTIVE") -> dict[str, Any]:
    oidc: dict[str, Any] = {"issuerUri": _ISSUER, "allowedAudiences": ["search-reader"]}
    if jwks is not None:
        oidc["jwksJson"] = json.dumps(jwks)
    return {
        "name": _PROVIDER_NAME,
        "state": state,
        "oidc": oidc,
        "attributeMapping": {"google.subject": "assertion.sub"},
        "attributeCondition": _PROVIDER_PARAMS["attribute_condition"],
    }


# ---------------------------------------------------------------------------
# Pool create
# ---------------------------------------------------------------------------


async def test_pool_create_waits_and_reports_active(
    connector: GcloudConnector, no_wait: list[float]
) -> None:
    with respx.mock() as mock:
        create = mock.post(_POOLS_URL).respond(200, json={"name": _POOL_OP, "done": False})
        mock.get(f"{_IAM}/{_POOL_OP}").respond(200, json={"name": _POOL_OP, "done": True})
        read = mock.get(_NEW_POOL_URL).respond(
            200, json={"name": _POOL_NAME, "state": "ACTIVE", "displayName": "Search"}
        )
        result = await connector.gcloud_wif_pool_create(
            _OPERATOR, _TARGET, params={"pool_id": _POOL, "display_name": "Search"}
        )

    request = create.calls[0].request
    assert request.url.params["workloadIdentityPoolId"] == _POOL
    assert json.loads(request.content) == {"displayName": "Search"}
    assert read.call_count == 1
    assert no_wait == [1.0]
    assert result["state"] == "ACTIVE"
    assert result["already_existed"] is False
    assert result["operation_done"] is True
    assert result["display_name"] == "Search"


async def test_pool_create_says_pending_when_google_is_slow(
    connector: GcloudConnector, no_wait: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    from meho_backplane.connectors.gcloud import workload_identity as wif

    monkeypatch.setattr(wif, "_OPERATION_POLL_TRIES", 2)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_POOLS_URL).respond(200, json={"name": _POOL_OP, "done": False})
        mock.get(f"{_IAM}/{_POOL_OP}").respond(200, json={"name": _POOL_OP, "done": False})
        read = mock.get(_NEW_POOL_URL).respond(200, json={})
        result = await connector.gcloud_wif_pool_create(
            _OPERATOR, _TARGET, params={"pool_id": _POOL}
        )

    assert read.call_count == 0
    assert result["state"] == "CREATE_PENDING"
    assert result["operation_done"] is False
    assert "workload_identity_pools.list" in result["note"]


async def test_pool_create_keeps_a_pool_that_already_exists(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.post(_POOLS_URL).respond(409, json={"error": {"code": 409}})
        mock.get(_NEW_POOL_URL).respond(200, json={"name": _POOL_NAME, "state": "ACTIVE"})
        result = await connector.gcloud_wif_pool_create(
            _OPERATOR, _TARGET, params={"pool_id": _POOL}
        )

    assert result["already_existed"] is True
    assert result["state"] == "ACTIVE"
    assert result["operation_name"] is None
    assert "changed nothing" in result["note"]


async def test_pool_create_refuses_an_id_held_by_a_deleted_pool(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.post(_POOLS_URL).respond(409, json={"error": {"code": 409}})
        mock.get(_NEW_POOL_URL).respond(200, json={"name": _POOL_NAME, "state": "DELETED"})
        with pytest.raises(ValueError, match="30 days"):
            await connector.gcloud_wif_pool_create(_OPERATOR, _TARGET, params={"pool_id": _POOL})


async def test_pool_create_fails_when_the_operation_fails(
    connector: GcloudConnector, no_wait: list[float]
) -> None:
    failed = {"name": _POOL_OP, "done": True, "error": {"message": "quota"}}
    with respx.mock() as mock:
        mock.post(_POOLS_URL).respond(200, json=failed)
        with pytest.raises(ConnectorUpstreamError, match="could not create") as info:
            await connector.gcloud_wif_pool_create(_OPERATOR, _TARGET, params={"pool_id": _POOL})
    assert info.value.upstream_message == "quota"


async def test_pool_create_refuses_a_bad_id(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        with pytest.raises(ValueError, match="pool ID"):
            await connector.gcloud_wif_pool_create(_OPERATOR, _TARGET, params={"pool_id": "gcp-x"})
        assert not mock.calls


# ---------------------------------------------------------------------------
# Provider create
# ---------------------------------------------------------------------------


async def test_provider_create_sends_oidc_with_keys_and_condition(
    connector: GcloudConnector, no_wait: list[float]
) -> None:
    with respx.mock() as mock:
        create = mock.post(_PROVIDERS_URL).respond(200, json={"name": _PROVIDER_OP, "done": True})
        mock.get(_PROVIDER_URL).respond(200, json=_provider(_JWKS))
        result = await connector.gcloud_wif_provider_create(
            _OPERATOR, _TARGET, params=_PROVIDER_PARAMS
        )

    request = create.calls[0].request
    assert request.url.params["workloadIdentityPoolProviderId"] == "cluster-oidc"
    body = json.loads(request.content)
    assert body["oidc"]["issuerUri"] == _ISSUER
    assert json.loads(body["oidc"]["jwksJson"]) == _JWKS
    assert body["oidc"]["allowedAudiences"] == ["search-reader"]
    assert body["attributeMapping"] == {"google.subject": "assertion.sub"}
    assert body["attributeCondition"] == _PROVIDER_PARAMS["attribute_condition"]
    assert result == {
        "pool_id": _POOL,
        "provider_id": "cluster-oidc",
        "name": _PROVIDER_NAME,
        "already_existed": False,
        "operation_name": _PROVIDER_OP,
        "operation_done": True,
        "state": "ACTIVE",
        "issuer_uri": _ISSUER,
        "key_count": 2,
        "attribute_condition": _PROVIDER_PARAMS["attribute_condition"],
        "note": "Google created the provider; its state is ACTIVE.",
    }
    assert "bW9k" not in json.dumps(result)


async def test_provider_create_maps_google_subject_by_default(
    connector: GcloudConnector, no_wait: list[float]
) -> None:
    params = {"pool_id": _POOL, "provider_id": "cluster-oidc", "issuer_uri": _ISSUER}
    with respx.mock() as mock:
        create = mock.post(_PROVIDERS_URL).respond(200, json={"name": _PROVIDER_OP, "done": True})
        mock.get(_PROVIDER_URL).respond(200, json=_provider())
        result = await connector.gcloud_wif_provider_create(_OPERATOR, _TARGET, params=params)

    body = json.loads(create.calls[0].request.content)
    assert body == {
        "oidc": {"issuerUri": _ISSUER},
        "attributeMapping": {"google.subject": "assertion.sub"},
    }
    assert result["key_count"] == 0


async def test_provider_create_shows_the_provider_that_already_exists(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.post(_PROVIDERS_URL).respond(409, json={"error": {"code": 409}})
        mock.get(_PROVIDER_URL).respond(200, json=_provider({"keys": [_RSA_KEY]}))
        result = await connector.gcloud_wif_provider_create(
            _OPERATOR, _TARGET, params=_PROVIDER_PARAMS
        )

    assert result["already_existed"] is True
    assert result["key_count"] == 1
    assert "update_keys" in result["note"]


@pytest.mark.parametrize(
    ("jwks", "message"),
    [
        ({"keys": [{**_RSA_KEY, "d": "c2VjcmV0"}]}, "private key parts"),
        ({"keys": [{**_RSA_KEY, "p": "x", "q": "y"}]}, "private key parts"),
        ({"keys": [{"kty": "oct", "k": "c2VjcmV0"}]}, "private key parts"),
        ({"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "eA"}]}, "only RSA and EC"),
        ({"keys": [{**_RSA_KEY, "x5c": ["cert"]}]}, "does not accept"),
        ({"keys": [{"kty": "RSA", "n": "bW9k"}]}, "needs the text fields n, e"),
        ({"keys": [{"kty": "EC", "crv": "P-256", "x": "eA"}]}, "needs the text fields"),
        ({"keys": []}, "key set"),
        ({"keys": [_RSA_KEY], "extra": 1}, "key set"),
    ],
)
async def test_provider_create_refuses_bad_keys_before_any_call(
    connector: GcloudConnector, jwks: dict[str, Any], message: str
) -> None:
    with respx.mock() as mock:
        with pytest.raises(ValueError, match=message) as info:
            await connector.gcloud_wif_provider_create(
                _OPERATOR, _TARGET, params={**_PROVIDER_PARAMS, "jwks": jwks}
            )
        assert not mock.calls
    assert "c2VjcmV0" not in str(info.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("issuer_uri", "http://insecure.example"),
        ("provider_id", "../x"),
        ("attribute_mapping", {"attribute.ns": "assertion.ns"}),
    ],
)
async def test_provider_create_refuses_bad_values(
    connector: GcloudConnector, key: str, value: Any
) -> None:
    with respx.mock() as mock:
        with pytest.raises(ValueError):
            await connector.gcloud_wif_provider_create(
                _OPERATOR, _TARGET, params={**_PROVIDER_PARAMS, key: value}
            )
        assert not mock.calls


def test_provider_schemas_refuse_private_keys_before_approval() -> None:
    """The parameter schema (checked before a call waits for approval) refuses
    a private key, a wrong key type and a mapping without google.subject."""
    for op_id in (
        "gcloud.iam.workload_identity_pools.providers.create",
        "gcloud.iam.workload_identity_pools.providers.update_keys",
    ):
        schema = _op(op_id).parameter_schema
        good = {"pool_id": _POOL, "provider_id": "cluster-oidc", "jwks": _JWKS}
        if "issuer_uri" in schema["properties"]:
            good["issuer_uri"] = _ISSUER
        assert validate_params(schema, good) == []
        assert validate_params(schema, {**good, "jwks": {"keys": [{**_RSA_KEY, "d": "x"}]}})
        assert validate_params(schema, {**good, "jwks": {"keys": [{"kty": "oct", "k": "x"}]}})
    create = _op("gcloud.iam.workload_identity_pools.providers.create").parameter_schema
    bad_mapping = {**_PROVIDER_PARAMS, "attribute_mapping": {"attribute.a": "assertion.a"}}
    assert validate_params(create, bad_mapping)


# ---------------------------------------------------------------------------
# Key update
# ---------------------------------------------------------------------------

_KEYS_PARAMS: dict[str, Any] = {"pool_id": _POOL, "provider_id": "cluster-oidc", "jwks": _JWKS}


async def test_update_keys_patches_only_the_keys(
    connector: GcloudConnector, no_wait: list[float]
) -> None:
    with respx.mock() as mock:
        mock.get(_PROVIDER_URL).respond(200, json=_provider({"keys": [_RSA_KEY]}))
        patch = mock.patch(_PROVIDER_URL).respond(200, json={"name": _PROVIDER_OP, "done": True})
        result = await connector.gcloud_wif_provider_update_keys(
            _OPERATOR, _TARGET, params=_KEYS_PARAMS
        )

    request = patch.calls[0].request
    assert request.url.params["updateMask"] == "oidc.jwksJson"
    body = json.loads(request.content)
    assert list(body) == ["oidc"]
    assert list(body["oidc"]) == ["jwksJson"]
    assert json.loads(body["oidc"]["jwksJson"]) == _JWKS
    assert result["changed"] is True
    assert result["keys_before"] == 1
    assert result["key_count"] == 2
    assert result["operation_done"] is True
    assert result["issuer_uri"] == _ISSUER


async def test_update_keys_with_the_same_keys_sends_nothing(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_PROVIDER_URL).respond(200, json=_provider(_JWKS))
        patch = mock.patch(_PROVIDER_URL).respond(200, json={})
        result = await connector.gcloud_wif_provider_update_keys(
            _OPERATOR, _TARGET, params=_KEYS_PARAMS
        )

    assert patch.call_count == 0
    assert result["changed"] is False
    assert result["operation_name"] is None


async def test_update_keys_refuses_a_missing_provider(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.get(_PROVIDER_URL).respond(404, json={"error": {"code": 404}})
        with pytest.raises(ConnectorResourceNotFoundError):
            await connector.gcloud_wif_provider_update_keys(_OPERATOR, _TARGET, params=_KEYS_PARAMS)


async def test_update_keys_refuses_a_provider_that_is_not_oidc(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.get(_PROVIDER_URL).respond(200, json={"name": _PROVIDER_NAME, "aws": {}})
        with pytest.raises(ValueError, match="not an OIDC provider"):
            await connector.gcloud_wif_provider_update_keys(_OPERATOR, _TARGET, params=_KEYS_PARAMS)


async def test_update_keys_refuses_private_keys_before_any_call(
    connector: GcloudConnector,
) -> None:
    params = {**_KEYS_PARAMS, "jwks": {"keys": [{**_EC_KEY, "d": "c2VjcmV0"}]}}
    with respx.mock() as mock:
        with pytest.raises(ValueError, match="private key parts"):
            await connector.gcloud_wif_provider_update_keys(_OPERATOR, _TARGET, params=params)
        assert not mock.calls


# ---------------------------------------------------------------------------
# Binding add
# ---------------------------------------------------------------------------

_MEMBER = (
    "principalSet://iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/cluster-pool/attribute.namespace/search"
)


async def test_binding_add_adds_only_the_member_and_sends_the_etag(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={"etag": "BwYnew="})
        result = await connector.gcloud_sa_binding_add(
            _OPERATOR,
            _TARGET,
            params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _MEMBER},
        )

    sent = json.loads(set_route.calls[0].request.content)["policy"]
    original = _policy()
    assert sent["etag"] == "BwYread="
    assert sent["version"] == 3
    assert sent["bindings"][0] == {
        "role": _WIF_ROLE,
        "members": [_OLD_MEMBER, _NEW_MEMBER, _MEMBER],
    }
    assert sent["bindings"][1:] == original["bindings"][1:]
    assert result == {
        "service_account_email": _SA,
        "role": _WIF_ROLE,
        "member": _MEMBER,
        "added": True,
        "members_with_role": 3,
        "etag": "BwYnew=",
    }


async def test_binding_add_makes_a_new_binding_when_the_role_has_none(
    connector: GcloudConnector,
) -> None:
    policy = _policy()
    policy["bindings"] = [policy["bindings"][1]]
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=policy)
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={"etag": "e2"})
        result = await connector.gcloud_sa_binding_add(
            _OPERATOR,
            _TARGET,
            params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _MEMBER},
        )

    sent = json.loads(set_route.calls[0].request.content)["policy"]
    assert sent["bindings"][-1] == {"role": _WIF_ROLE, "members": [_MEMBER]}
    assert result["members_with_role"] == 1


async def test_binding_add_keeps_a_member_that_is_already_there(
    connector: GcloudConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={})
        result = await connector.gcloud_sa_binding_add(
            _OPERATOR,
            _TARGET,
            params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _OLD_MEMBER},
        )

    assert set_route.call_count == 0
    assert result["added"] is False
    assert result["members_with_role"] == 2


async def test_binding_add_refuses_a_stale_etag_without_retry(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=_policy())
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(409, json={})
        with pytest.raises(GcloudPolicyChangedError, match="changed after MEHO read it"):
            await connector.gcloud_sa_binding_add(
                _OPERATOR,
                _TARGET,
                params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _MEMBER},
            )
    assert set_route.call_count == 1


async def test_binding_add_refuses_a_policy_with_hidden_conditions(
    connector: GcloudConnector,
) -> None:
    policy = _policy()
    policy["bindings"][2] = {"role": f"{_WIF_ROLE}_withcond_abc", "members": [_OLD_MEMBER]}
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_SA_URL}:getIamPolicy").respond(200, json=policy)
        set_route = mock.post(f"{_SA_URL}:setIamPolicy").respond(200, json={})
        with pytest.raises(ValueError, match="_withcond_"):
            await connector.gcloud_sa_binding_add(
                _OPERATOR,
                _TARGET,
                params={"service_account_email": _SA, "role": _WIF_ROLE, "member": _MEMBER},
            )
    assert set_route.call_count == 0


@pytest.mark.parametrize("member", ["allUsers", "allAuthenticatedUsers", "deleted:user:a@b.com"])
async def test_binding_add_refuses_everyone_and_deleted_members(
    connector: GcloudConnector, member: str
) -> None:
    with respx.mock() as mock:
        with pytest.raises(ValueError, match="does not add"):
            await connector.gcloud_sa_binding_add(
                _OPERATOR,
                _TARGET,
                params={"service_account_email": _SA, "role": _WIF_ROLE, "member": member},
            )
        assert not mock.calls


# ---------------------------------------------------------------------------
# Checks before approval
# ---------------------------------------------------------------------------

_POOL_CREATE = "gcloud.iam.workload_identity_pools.create"
_PROVIDER_CREATE = "gcloud.iam.workload_identity_pools.providers.create"
_UPDATE_KEYS = "gcloud.iam.workload_identity_pools.providers.update_keys"
_BINDING_ADD = "gcloud.iam.service_accounts.binding.add"


def test_every_new_change_has_a_check_before_approval() -> None:
    for op_id in (_POOL_CREATE, _PROVIDER_CREATE, _UPDATE_KEYS, _BINDING_ADD):
        assert op_id in _PERMISSION_PREFLIGHTS
        assert _op(op_id).requires_approval is True


async def test_pool_create_check_asks_the_project(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(_PROJECT_TEST_URL).respond(200, json={"permissions": []})
        check = await _PERMISSION_PREFLIGHTS[_POOL_CREATE](
            _ctx(connector, _POOL_CREATE, {"pool_id": _POOL})
        )

    assert json.loads(route.calls[0].request.content) == {
        "permissions": ["iam.workloadIdentityPools.create"]
    }
    assert check is not None
    assert check["will_be_denied"] is True


@pytest.mark.parametrize("pool_exists", [True, False])
async def test_provider_create_approval_path_needs_the_pool(
    connector: GcloudConnector, pool_exists: bool
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        if pool_exists:
            mock.get(_NEW_POOL_URL).respond(200, json={"name": _POOL_NAME, "state": "ACTIVE"})
        else:
            mock.get(_NEW_POOL_URL).respond(404, json={"error": {"code": 404}})
        project = mock.post(_PROJECT_TEST_URL).respond(
            200, json={"permissions": ["iam.workloadIdentityPoolProviders.create"]}
        )
        preflight = await build_permission_preflight(
            _ctx(connector, _PROVIDER_CREATE, _PROVIDER_PARAMS)
        )

    refusal = _preflight_denied_refusal(
        op_id=_PROVIDER_CREATE,
        proposed_effect={"permission_preflight": preflight},
        duration_ms=1.0,
    )
    assert preflight is not None
    if pool_exists:
        assert project.call_count == 1
        assert preflight["pool"] == _POOL_NAME
        assert refusal is None
    else:
        assert project.call_count == 0
        assert preflight["reason"] == "pool_not_found"
        assert refusal is not None
        assert refusal.status == "denied"


async def test_update_keys_check_denies_a_missing_provider(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_PROVIDER_URL).respond(404, json={"error": {"code": 404}})
        project = mock.post(_PROJECT_TEST_URL).respond(200, json={"permissions": []})
        check = await _PERMISSION_PREFLIGHTS[_UPDATE_KEYS](
            _ctx(connector, _UPDATE_KEYS, _KEYS_PARAMS)
        )

    assert project.call_count == 0
    assert check is not None
    assert check["reason"] == "provider_not_found"
    assert check["will_be_denied"] is True


async def test_update_keys_check_asks_for_the_update_permission(
    connector: GcloudConnector,
) -> None:
    with respx.mock() as mock:
        mock.get(_PROVIDER_URL).respond(200, json=_provider())
        project = mock.post(_PROJECT_TEST_URL).respond(
            200, json={"permissions": ["iam.workloadIdentityPoolProviders.update"]}
        )
        check = await _PERMISSION_PREFLIGHTS[_UPDATE_KEYS](
            _ctx(connector, _UPDATE_KEYS, _KEYS_PARAMS)
        )

    assert json.loads(project.calls[0].request.content) == {
        "permissions": ["iam.workloadIdentityPoolProviders.update"]
    }
    assert check is not None
    assert check["will_be_denied"] is False
    assert check["provider"] == _PROVIDER_NAME


async def test_binding_add_check_asks_the_service_account(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(f"{_SA_URL}:testIamPermissions").respond(
            200,
            json={
                "permissions": [
                    "iam.serviceAccounts.getIamPolicy",
                    "iam.serviceAccounts.setIamPolicy",
                ]
            },
        )
        check = await _PERMISSION_PREFLIGHTS[_BINDING_ADD](
            _ctx(
                connector,
                _BINDING_ADD,
                {"service_account_email": _SA, "role": _WIF_ROLE, "member": _MEMBER},
            )
        )

    assert route.call_count == 1
    assert check is not None
    assert check["will_be_denied"] is False


async def test_checks_are_skipped_for_a_bad_value(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        for op_id, params in (
            (_POOL_CREATE, {"pool_id": "../x"}),
            (_PROVIDER_CREATE, {**_PROVIDER_PARAMS, "pool_id": "../x"}),
            (_UPDATE_KEYS, {**_KEYS_PARAMS, "provider_id": "../x"}),
        ):
            assert await _PERMISSION_PREFLIGHTS[op_id](_ctx(connector, op_id, params)) is None
        assert not mock.calls


def test_examples_validate_and_carry_no_private_parts() -> None:
    for op_id in (_POOL_CREATE, _PROVIDER_CREATE, _UPDATE_KEYS, _BINDING_ADD):
        schema = _op(op_id).parameter_schema
        for example in schema["examples"]:
            assert validate_params(schema, example) == []
            assert '"d"' not in json.dumps(example)
