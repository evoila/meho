# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``idb`` connectors: the six operations, the login and the fingerprint, on a fake vendor.

The handlers run against :class:`tests._idb_fake.FakeIdb` through respx, so
every vendor call is the real HTTP request the connector sends. Made-up
names and ids only.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.idb import (
    IdbConnectorBase,
    IdbCredentials,
    IdbRestConnector,
    IdbWsaConnector,
)
from meho_backplane.connectors.idb.reads import scim_filter
from meho_backplane.connectors.idb.session import (
    IdbConfigError,
    parse_credentials,
    resolve_tenant,
)
from meho_backplane.settings import get_settings

from ._idb_fake import BASE_URL, HOST, TENANT, FakeIdb, scim_group, scim_user

_TENANT = UUID("00000000-0000-0000-0000-000000004508")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Target:
    def __init__(self, version: str | None = None, **extras: Any) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "idb-test"
        self.host = HOST
        self.port = 443
        self.secret_ref = "tenants/test/idb-test"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = dict(extras)
        self.version = version
        self.fingerprint = type("_FP", (), {"version": version})()


def _operator() -> Operator:
    return Operator(
        sub="op-idb",
        name="IDB operator",
        email=None,
        raw_jwt="op.idb.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _connector(cls: type[IdbConnectorBase], fake: FakeIdb) -> IdbConnectorBase:
    async def _loader(target: Any, operator: Operator) -> IdbCredentials:
        return IdbCredentials(client_id=fake.client_id, client_secret=fake.client_secret)

    return cls(credentials_loader=_loader)


async def _run(
    cls: type[IdbConnectorBase],
    fake: FakeIdb,
    handler: str,
    params: dict[str, Any],
    target: _Target | None = None,
) -> Any:
    conn = _connector(cls, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        return await getattr(conn, handler)(_operator(), target or _Target(), params)


def _wsa() -> FakeIdb:
    fake = FakeIdb(product="wsa")
    fake.identity_providers = [
        {
            "id": "idp-1",
            "name": "Built-in",
            "identityProviderType": "EMBEDDED",
            "nIDPStatus": 1,
            "directoryConfigurations": [{"name": "example.test", "directoryId": "dir-1"}],
            "certExpiry": "2027-01-01",
            "certPrivateKey": "planted-cert-private-key",  # gitleaks:allow NOSONAR -- planted
        }
    ]
    fake.directories = [
        {
            "directoryId": "dir-1",
            "name": "example.test",
            "type": "ACTIVE_DIRECTORY_LDAP",
            "syncConfigurationEnabled": True,
            "deleteInProgress": False,
            "lastSyncTime": 1761900000000,
            "lastSyncStatus": "FAILED",
            "lastSyncError": "bind failed",
            "password": "bind-password-must-not-show",  # gitleaks:allow NOSONAR -- planted
            "syncClientToken": "planted-sync-client-token",  # gitleaks:allow NOSONAR -- planted
            "bindDN": "cn=meho,dc=example,dc=test",
        }
    ]
    fake.users = [scim_user("alice", groups=2), scim_user("bob")]
    fake.groups = [scim_group("vcf-admins", members=3)]
    return fake


def _idb(version: str = "9.1.0.0") -> FakeIdb:
    fake = FakeIdb(product="idb", version=version)
    fake.users = [scim_user("alice"), scim_user("carol", active=False)]
    fake.groups = [scim_group("vcf-admins", members=150)]
    return fake


# ---------------------------------------------------------------------------
# Target settings and secret
# ---------------------------------------------------------------------------


def test_tenant_defaults_to_customer_and_is_checked() -> None:
    assert resolve_tenant(_Target()) == "CUSTOMER"
    assert resolve_tenant(_Target(tenant="other_1")) == "other_1"
    with pytest.raises(IdbConfigError, match="tenant"):
        resolve_tenant(_Target(tenant="../x"))


def test_secret_needs_client_fields_and_error_names_fields_only() -> None:
    creds = parse_credentials({"client_id": " c ", "client_secret": "s-planted"}, target_name="t")
    assert creds.client_id == "c"
    assert "s-planted" not in repr(creds)
    with pytest.raises(IdbConfigError) as info:
        parse_credentials({"username": "x", "password": "pw-planted"}, target_name="t")
    assert "pw-planted" not in str(info.value)
    assert "['password', 'username']" in str(info.value)


def test_scim_filter_escapes_quotes_and_checks_match() -> None:
    assert scim_filter("userName", 'a"b\\c', "eq") == 'userName eq "a\\"b\\\\c"'
    with pytest.raises(ValueError, match="match"):
        scim_filter("userName", "a", "pr")


def test_both_implementations_have_disjoint_bands() -> None:
    assert IdbWsaConnector.supported_version_range == ">=3.3,<4.0"
    assert IdbRestConnector.supported_version_range == ">=9.0,<10.0"
    assert (IdbWsaConnector.product, IdbWsaConnector.impl_id) == ("idb", "idb-wsa")
    assert (IdbRestConnector.product, IdbRestConnector.impl_id) == ("idb", "idb-rest")


# ---------------------------------------------------------------------------
# Workspace ONE Access 3.3
# ---------------------------------------------------------------------------


async def test_wsa_about_reads_health_and_version() -> None:
    fake = _wsa()
    result = await _run(IdbWsaConnector, fake, "about", {})
    assert result["product"] == "Workspace ONE Access"
    assert result["version"] == "3.3.7.0"
    assert result["healthy"] is True
    assert result["failing_checks"] == ["MessagingConnectionOk"]
    login = fake.calls_of("POST", "/SAAS/auth/oauthtoken")
    assert len(login) == 1
    assert login[0].content == b"grant_type=client_credentials"


async def test_wsa_identity_providers_are_flat_rows() -> None:
    result = await _run(IdbWsaConnector, _wsa(), "identity_provider_list", {})
    assert result["total"] == 1
    row = result["identity_providers"][0]
    assert row == {
        "id": "idp-1",
        "name": "Built-in",
        "type": "EMBEDDED",
        "status": "1",
        "directories": ["example.test"],
        "certificate": {"certExpiry": "2027-01-01"},
    }
    assert "planted-cert-private-key" not in json.dumps(result)


async def test_wsa_identity_provider_call_sends_the_vendor_media_type() -> None:
    fake = _wsa()
    await _run(IdbWsaConnector, fake, "identity_provider_list", {})
    call = fake.calls_of("GET", "/SAAS/jersey/manager/api/identityProviders")[0]
    assert call.url.params["onlyEnabledAdapters"] == "true"
    assert "identityprovider.summary.list+json" in call.headers["Accept"]


async def test_wsa_directory_list_has_sync_fields_and_no_secret() -> None:
    result = await _run(IdbWsaConnector, _wsa(), "directory_list", {})
    row = result["directories"][0]
    assert row["directory_id"] == "dir-1"
    assert row["sync_enabled"] is True
    assert row["sync"]["lastSyncStatus"] == "FAILED"
    assert row["sync"]["lastSyncTime"] == 1761900000000
    assert row["errors"] == {"lastSyncError": "bind failed"}
    assert "syncClientToken" not in row["sync"]
    assert "bindDN" not in row["sync"]
    for planted in ("bind-password-must-not-show", "planted-sync-client-token"):
        assert planted not in json.dumps(result)


async def test_wsa_directory_sync_posts_once_with_safeguards_on() -> None:
    fake = _wsa()
    result = await _run(IdbWsaConnector, fake, "directory_sync", {"directory_id": "dir-1"})
    assert result["started"] is True
    assert result["op_class"] == "write"
    assert result["answer"] == {"syncStatus": "STARTED"}
    assert "planted-" not in json.dumps(result)
    posts = [c for c in fake.calls_of("POST") if c.url.path.endswith("/syncprofile/sync")]
    assert len(posts) == 1
    assert json.loads(posts[0].content) == {"ignoreSafeguards": False}
    assert "directory.sync.profile.sync+json" in posts[0].headers["Content-Type"]


async def test_wsa_directory_sync_unknown_directory_raises_vendor_error() -> None:
    with pytest.raises(httpx.HTTPStatusError) as info:
        await _run(IdbWsaConnector, _wsa(), "directory_sync", {"directory_id": "dir-x"})
    assert info.value.response.status_code == 404
    assert "directory.not.found" in info.value.response.text


async def test_wsa_user_find_returns_rows_without_password() -> None:
    fake = _wsa()
    result = await _run(IdbWsaConnector, fake, "user_find", {"name": "ali"})
    assert result["total"] == 1
    user = result["users"][0]
    assert user["user_name"] == "alice"
    assert user["email"] == "alice@example.test"
    assert user["domain"] == "example.test"
    assert user["user_status"] == "enabled"
    assert user["groups"] == ["group-0", "group-1"]
    assert "user-password-must-not-show" not in json.dumps(result)
    call = fake.calls_of("POST", "/SAAS/jersey/manager/api/scim/Users/.search")[0]
    assert json.loads(call.content) == {"filter": 'userName co "ali"', "count": 100}


async def test_wsa_group_find_lists_members() -> None:
    result = await _run(IdbWsaConnector, _wsa(), "group_find", {"name": "vcf", "match": "sw"})
    group = result["groups"][0]
    assert group["display_name"] == "vcf-admins"
    assert group["group_type"] == "EXTERNAL"
    assert group["member_count"] == 3
    assert group["members"] == ["user-0", "user-1", "user-2"]
    assert group["members_truncated"] is False


# ---------------------------------------------------------------------------
# VCF Identity Broker 9.x
# ---------------------------------------------------------------------------


async def test_idb_about_reads_properties_under_the_tenant() -> None:
    fake = _idb()
    result = await _run(IdbRestConnector, fake, "about", {})
    assert result["version"] == "9.1.0.0"
    assert result["deployment_type"] == "VCF"
    assert result["tenant"] == TENANT
    assert len(fake.calls_of("POST", f"/acs/t/{TENANT}/token")) == 1


@pytest.mark.parametrize("version", ["9.0", "9.0.0.0", "9.0.1"])
async def test_idb_about_on_a_known_9_0_stops_before_any_call(version: str) -> None:
    fake = _idb()
    loads = 0

    async def _loader(target: Any, operator: Operator) -> IdbCredentials:
        nonlocal loads
        loads += 1
        return IdbCredentials(client_id=fake.client_id, client_secret=fake.client_secret)

    conn = IdbRestConnector(credentials_loader=_loader)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        with pytest.raises(NotImplementedError, match=r"9\.1 and newer") as info:
            await conn.about(_operator(), _Target(version=version), {})
    assert f"Identity Broker {version} has no version call" in str(info.value)
    assert loads == 0
    assert fake.calls == []


async def test_idb_about_without_a_known_version_stops_on_404() -> None:
    fake = _idb()
    fake.properties_status = 404
    with pytest.raises(NotImplementedError, match=r"9\.1 and newer"):
        await _run(IdbRestConnector, fake, "about", {})
    assert len(fake.calls_of("GET", f"/acs/t/{TENANT}/properties")) == 1


async def test_idb_about_on_9_1_makes_the_call() -> None:
    fake = _idb(version="9.1.0.0")
    result = await _run(IdbRestConnector, fake, "about", {}, _Target(version="9.1"))
    assert result["version"] == "9.1.0.0"


@pytest.mark.parametrize("version", ["9.0", "9.1", "9.1.1"])
@pytest.mark.parametrize(
    ("handler", "op_id", "params"),
    [
        ("identity_provider_list", "idb.identity_provider.list", {}),
        ("directory_list", "idb.directory.list", {}),
        ("directory_sync", "idb.directory.sync", {"directory_id": "dir-1"}),
    ],
)
async def test_idb_directory_operations_stop_early_without_a_call(
    version: str, handler: str, op_id: str, params: dict[str, Any]
) -> None:
    fake = _idb()
    with pytest.raises(NotImplementedError) as info:
        await _run(IdbRestConnector, fake, handler, params, _Target(version=version))
    message = str(info.value)
    assert message.startswith(f"{op_id}: VCF Identity Broker {version} has no API call")
    assert "VCF Operations" in message
    assert fake.calls == []


@pytest.mark.parametrize("version", ["9.0", "9.1", "9.1.1"])
async def test_idb_user_find_uses_scim_v2_on_every_9x(version: str) -> None:
    fake = _idb()
    result = await _run(
        IdbRestConnector, fake, "user_find", {"name": "carol", "match": "eq"}, _Target(version)
    )
    assert result["users"][0]["active"] is False
    call = fake.calls_of("POST", f"/usergroup/t/{TENANT}/scim/v2/Users/.search")[0]
    assert call.headers["Content-Type"] == "application/scim+json"
    assert "user-password-must-not-show" not in json.dumps(result)


async def test_idb_group_find_caps_members() -> None:
    result = await _run(IdbRestConnector, _idb(), "group_find", {"name": "admins"})
    group = result["groups"][0]
    assert group["member_count"] == 150
    assert len(group["members"]) == 100
    assert group["members_truncated"] is True


async def test_idb_uses_the_tenant_from_extras() -> None:
    fake = _idb()
    conn = _connector(IdbRestConnector, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            await conn.about(_operator(), _Target(tenant="other"), {})
        assert mock.calls.last.request.url.path == "/acs/t/other/token"


# ---------------------------------------------------------------------------
# Login and token cache
# ---------------------------------------------------------------------------


async def test_token_is_cached_and_a_refused_token_is_dropped_only_when_sent() -> None:
    fake = _wsa()
    conn = _connector(IdbWsaConnector, fake)
    target = _Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        await conn.user_find(_operator(), target, {"name": "a"})
        await conn.group_find(_operator(), target, {"name": "a"})
        assert fake.logins == 1
        stale = httpx.Request("GET", BASE_URL, headers={"Authorization": "Bearer old"})
        await conn.invalidate_session(target, rejected=stale)
        await conn.user_find(_operator(), target, {"name": "a"})
        assert fake.logins == 1
        await conn.invalidate_session(target)
        await conn.user_find(_operator(), target, {"name": "a"})
        assert fake.logins == 2


async def test_refused_login_is_an_auth_error_and_forgets_the_secret() -> None:
    fake = _wsa()
    loads = 0

    async def _loader(target: Any, operator: Operator) -> IdbCredentials:
        nonlocal loads
        loads += 1
        secret = "wrong-secret" if loads == 1 else fake.client_secret
        return IdbCredentials(client_id=fake.client_id, client_secret=secret)

    conn = IdbWsaConnector(credentials_loader=_loader)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        with pytest.raises(ConnectorAuthError) as info:
            await conn.about(_operator(), _Target(), {})
        assert info.value.status_code == 401
        assert "wrong-secret" not in str(info.value)
        result = await conn.about(_operator(), _Target(), {})
    assert result["healthy"] is True
    assert loads == 2


def test_trace_masks_token_fields() -> None:
    conn = IdbWsaConnector()
    response = httpx.Response(
        200,
        json={
            "access_token": "t",
            "a": [{"password": "p", "b": 1}],
            "syncClientToken": "s",
            "licenseKey": "l",
            "private_key": "k",
            "token_ttl": 60,
        },
    )
    assert conn.trace_response_body(response) == {
        "access_token": "<redacted>",
        "a": [{"password": "<redacted>", "b": 1}],
        "syncClientToken": "<redacted>",
        "licenseKey": "<redacted>",
        "private_key": "<redacted>",
        "token_ttl": 60,
    }


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


async def test_fingerprint_detects_workspace_one_access_without_login() -> None:
    fake = _wsa()
    conn = _connector(IdbRestConnector, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        fp = await conn.fingerprint(_Target(), _operator())
    assert fp.reachable is True
    assert fp.version == "3.3.7.0"
    assert fp.extras["product_line"] == "workspace-one-access"
    assert fake.logins == 0


async def test_fingerprint_detects_identity_broker_and_reads_its_version() -> None:
    fake = _idb(version="9.1.1.0")
    conn = _connector(IdbRestConnector, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        fp = await conn.fingerprint(_Target(), _operator())
    assert fp.reachable is True
    assert fp.version == "9.1.1.0"
    assert fp.extras["product_line"] == "identity-broker"


async def test_fingerprint_on_identity_broker_9_0_has_no_version() -> None:
    fake = _idb()
    fake.properties_status = 404
    conn = _connector(IdbRestConnector, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        fp = await conn.fingerprint(_Target(), _operator())
    assert fp.reachable is True
    assert fp.version is None
    assert fp.extras["version_error"] == "HTTPStatusError"


async def test_fingerprint_unreachable_when_neither_product_answers() -> None:
    conn = IdbRestConnector()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(404))
        fp = await conn.fingerprint(_Target())
        probe = await conn.probe(_Target())
    assert fp.reachable is False
    assert probe.ok is False
