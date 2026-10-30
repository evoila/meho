# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure target shape and secret shape.

Every id, name and secret here is made up.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.connectors.azure import session as azure_session
from meho_backplane.connectors.azure.session import (
    AZURE_CLOUDS,
    AzureCredentials,
    AzureSecretShapeError,
    AzureTargetConfigError,
    check_tenant_match,
    parse_credentials,
    resolve_target_config,
)
from tests._azure_fixtures import (
    CLIENT_A,
    SECRET_A,
    SUBSCRIPTION_A,
    TENANT_A,
    TENANT_B,
    AzureTargetStub,
    operator,
)

_REF = "tenants/example/azure-login-a"


def test_a_target_with_only_a_subscription_uses_the_public_cloud() -> None:
    config = resolve_target_config(AzureTargetStub())

    assert config.subscription_id == SUBSCRIPTION_A
    assert config.tenant_id is None
    assert config.cloud is AZURE_CLOUDS["AzureCloud"]


def test_ids_are_read_as_lower_case_guids() -> None:
    extras = {"subscription_id": SUBSCRIPTION_A.upper(), "tenant_id": f" {TENANT_A.upper()} "}
    config = resolve_target_config(AzureTargetStub(extras=extras))

    assert config.subscription_id == SUBSCRIPTION_A
    assert config.tenant_id == TENANT_A


def test_a_government_target_uses_the_government_hosts() -> None:
    target = AzureTargetStub(
        host="management.usgovcloudapi.net",
        extras={"subscription_id": SUBSCRIPTION_A, "cloud": "AzureUSGovernment"},
    )
    cloud = resolve_target_config(target).cloud

    assert cloud.login_host == "login.microsoftonline.us"
    assert cloud.arm_scope == "https://management.core.usgovcloudapi.net/.default"


def test_the_cloud_table_names_hosts_and_scopes() -> None:
    table = {
        name: (cloud.management_host, cloud.login_host, cloud.arm_scope)
        for name, cloud in AZURE_CLOUDS.items()
    }
    assert table == {
        "AzureCloud": (
            "management.azure.com",
            "login.microsoftonline.com",
            "https://management.azure.com/.default",
        ),
        "AzureUSGovernment": (
            "management.usgovcloudapi.net",
            "login.microsoftonline.us",
            "https://management.core.usgovcloudapi.net/.default",
        ),
        "AzureChinaCloud": (
            "management.chinacloudapi.cn",
            "login.chinacloudapi.cn",
            "https://management.chinacloudapi.cn/.default",
        ),
    }


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"extras": {}}, "extras.subscription_id"),
        ({"extras": {"subscription_id": "sub-value-not-a-guid"}}, "extras.subscription_id"),
        (
            {"extras": {"subscription_id": SUBSCRIPTION_A, "tenant_id": "tenant-value-not-a-guid"}},
            "extras.tenant_id",
        ),
        ({"extras": {"subscription_id": SUBSCRIPTION_A, "cloud": "MoonCloud"}}, "extras.cloud"),
        ({"host": "attacker.example.net"}, "host must be"),
        (
            {"extras": {"subscription_id": SUBSCRIPTION_A, "cloud": "AzureChinaCloud"}},
            "host must be",
        ),
        ({"extras": {"subscription_id": SUBSCRIPTION_A, "scheme": "http"}}, "host must be"),
        ({"port": 8443}, "host must be"),
        ({"verify_tls": False}, "verify_tls must stay on"),
    ],
)
def test_an_unusable_target_fails_naming_the_field(changes: dict[str, Any], field: str) -> None:
    target = AzureTargetStub(**changes)

    with pytest.raises(AzureTargetConfigError) as caught:
        resolve_target_config(target)

    message = str(caught.value)
    assert field in message
    assert "azure-sub-a" in message
    assert "value-not-a-guid" not in message


def test_parse_credentials_reads_the_three_keys_and_ignores_others() -> None:
    data = {
        "tenant_id": f"{TENANT_A.upper()}\n",
        "client_id": f" {CLIENT_A} ",
        "client_secret": f"{SECRET_A}\n",
        "subscription_id": SUBSCRIPTION_A,
        "note": "extra keys are ignored",
    }
    credentials = parse_credentials(data, target_name="azure-sub-a", secret_ref=_REF)

    assert credentials == AzureCredentials(TENANT_A, CLIENT_A, SECRET_A)
    assert SECRET_A not in repr(credentials)


@pytest.mark.parametrize("missing", ["tenant_id", "client_id", "client_secret"])
def test_a_missing_key_is_named_and_no_value_is_shown(missing: str) -> None:
    data: dict[str, object] = {
        "tenant_id": TENANT_A,
        "client_id": CLIENT_A,
        "client_secret": SECRET_A,
    }
    data[missing] = "  "

    with pytest.raises(AzureSecretShapeError) as caught:
        parse_credentials(data, target_name="azure-sub-a", secret_ref=_REF)

    message = str(caught.value)
    assert repr(missing) in message
    assert _REF in message
    for value in (TENANT_A, CLIENT_A, SECRET_A):
        assert value not in message


@pytest.mark.parametrize("key", ["tenant_id", "client_id"])
def test_an_id_that_is_not_a_guid_is_named(key: str) -> None:
    data: dict[str, object] = {
        "tenant_id": TENANT_A,
        "client_id": CLIENT_A,
        "client_secret": SECRET_A,
    }
    data[key] = "id-value-not-a-guid"

    with pytest.raises(AzureSecretShapeError) as caught:
        parse_credentials(data, target_name="azure-sub-a", secret_ref=_REF)

    assert repr(key) in str(caught.value)
    assert "id-value-not-a-guid" not in str(caught.value)


def test_a_tenant_that_differs_from_the_secret_fails_clearly() -> None:
    target = AzureTargetStub(extras={"subscription_id": SUBSCRIPTION_A, "tenant_id": TENANT_B})
    config = resolve_target_config(target)

    with pytest.raises(AzureTargetConfigError) as caught:
        check_tenant_match(target, config, AzureCredentials(TENANT_A, CLIENT_A, SECRET_A))

    message = str(caught.value)
    assert "extras.tenant_id is not the tenant_id in the secret" in message
    assert TENANT_A not in message and TENANT_B not in message


def test_a_target_without_tenant_accepts_the_secrets_tenant() -> None:
    target = AzureTargetStub()
    config = resolve_target_config(target)

    check_tenant_match(target, config, AzureCredentials(TENANT_A, CLIENT_A, SECRET_A))


@pytest.mark.asyncio
async def test_the_loader_reads_the_secret_with_the_shared_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = AsyncMock(
        return_value={"tenant_id": TENANT_A, "client_id": CLIENT_A, "client_secret": SECRET_A}
    )
    monkeypatch.setattr(azure_session, "load_vault_secret_data", reader)
    target = AzureTargetStub()
    person = operator()

    credentials = await azure_session.load_credentials_from_vault(target, person)

    assert credentials == AzureCredentials(TENANT_A, CLIENT_A, SECRET_A)
    reader.assert_awaited_once_with(target, person)
