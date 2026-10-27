# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure Kubernetes Service (AKS) reads (group ``aks``).

The dispatch tests run the real dispatcher (parameter check, policy, audit,
result reduction), the real paging helper and the real token cache, with
only the Azure registrar run. Microsoft's login service and Azure Resource
Manager are mocked with respx. The Azure answers are shaped like Microsoft's
documented examples for api-version 2026-08-01. Every id, name, address and
secret here is made up.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    _paths,
    aks,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.aks_rows import (
    _addons,
    _identity_type,
    cluster_detail,
    cluster_row,
    node_pool_row,
    version_key,
    version_rows,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    OBJECT_B,
    SUBSCRIPTION_A,
    TENANT_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)
from tests._result_store_helpers import MemoryResultStore

API_VERSION = "2026-08-01"
RESOURCE_GROUP = "rg-example"
CLUSTER = "aks-example-01"
LOCATION = "westeurope"

#: Values that look like secrets. None of them may reach a result.
SP_SECRET = "fake-service-principal-secret"
WINDOWS_PASSWORD = "fake-windows-admin-password"
AAD_SECRET = "fake-aad-server-app-secret"
SSH_KEY = "ssh-rsa FAKE-PUBLIC-KEY-FOR-TESTS"
ADDON_SETTING = "fake-addon-setting-value"
MOTD = "ZmFrZS1tZXNzYWdlLW9mLXRoZS1kYXk="


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _azure_only_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """The real reducer over an in-memory store, also used by ``result_query``."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


async def _registered_connector() -> AzureConnector:
    """Run the Azure registrar and give the dispatcher's instance a stub loader."""
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


async def _call(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params,
    )


# ----------------------------------------------------------------------
# Azure answers, shaped like Microsoft's examples
# ----------------------------------------------------------------------


def _clusters_url() -> str:
    return (
        f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}"
        "/providers/Microsoft.ContainerService/managedClusters"
    )


def _cluster_url(name: str = CLUSTER, resource_group: str = RESOURCE_GROUP) -> str:
    return (
        f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ContainerService/managedClusters/{name}"
    )


def _versions_url(location: str = LOCATION) -> str:
    return (
        f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.ContainerService"
        f"/locations/{location}/kubernetesVersions"
    )


def _pool_profile(name: str, count: int, **extra: Any) -> dict[str, Any]:
    """One entry of a cluster's ``agentPoolProfiles``."""
    return {
        "name": name,
        "count": count,
        "vmSize": "Standard_D4s_v5",
        "osType": "Linux",
        "mode": "User",
        "messageOfTheDay": MOTD,
        "nodeLabels": {"team": "example"},
        **extra,
    }


def _cluster_answer(name: str = CLUSTER, pools: int = 2) -> dict[str, Any]:
    """One ``ManagedCluster``. The secret-shaped fields must never be copied."""
    profiles = [_pool_profile("system", 3, mode="System")]
    profiles += [_pool_profile(f"user{n}", 2) for n in range(1, pools)]
    return {
        "id": (
            f"/subscriptions/{SUBSCRIPTION_A}/resourcegroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.ContainerService/managedClusters/{name}"
        ),
        "name": name,
        "type": "Microsoft.ContainerService/ManagedClusters",
        "location": LOCATION,
        "tags": {"tier": "example"},
        "sku": {"name": "Base", "tier": "Standard"},
        "identity": {"type": "SystemAssigned", "principalId": OBJECT_B, "tenantId": TENANT_A},
        "properties": {
            "provisioningState": "Succeeded",
            "powerState": {"code": "Running"},
            "kubernetesVersion": "1.30",
            "currentKubernetesVersion": "1.30.9",
            "dnsPrefix": f"{name}-dns",
            "fqdn": f"{name}-dns.hcp.example.net",
            "azurePortalFQDN": f"{name}-dns.portal.hcp.example.net",
            "supportPlan": "KubernetesOfficial",
            "nodeResourceGroup": f"MC_{RESOURCE_GROUP}_{name}_{LOCATION}",
            "agentPoolProfiles": profiles,
            "servicePrincipalProfile": {"clientId": "msi", "secret": SP_SECRET},
            "windowsProfile": {"adminUsername": "exampleadmin", "adminPassword": WINDOWS_PASSWORD},
            "aadProfile": {"managed": True, "serverAppSecret": AAD_SECRET},
            "linuxProfile": {
                "adminUsername": "azureuser",
                "ssh": {"publicKeys": [{"keyData": SSH_KEY}]},
            },
            "addonProfiles": {
                "omsagent": {
                    "enabled": True,
                    "config": {"logAnalyticsWorkspaceResourceID": ADDON_SETTING},
                },
                "azurepolicy": {"enabled": True},
                "httpApplicationRouting": {"enabled": False},
            },
            "networkProfile": {
                "networkPlugin": "azure",
                "networkPluginMode": "overlay",
                "networkPolicy": "cilium",
                "networkDataplane": "cilium",
                "outboundType": "loadBalancer",
                "loadBalancerSku": "standard",
                "podCidr": "192.0.2.0/24",
                "podCidrs": ["192.0.2.0/24"],
                "serviceCidr": "198.51.100.0/24",
                "serviceCidrs": ["198.51.100.0/24"],
            },
            "apiServerAccessProfile": {"enablePrivateCluster": False},
            "autoUpgradeProfile": {"upgradeChannel": "patch", "nodeOSUpgradeChannel": "NodeImage"},
            "workloadAutoScalerProfile": {"keda": {"enabled": True}},
            "securityProfile": {"workloadIdentity": {"enabled": True}},
            "identityProfile": {"kubeletidentity": {"clientId": OBJECT_B, "objectId": OBJECT_B}},
        },
    }


def _upgrade_answer(pools: int = 2) -> dict[str, Any]:
    """The upgrade profile of a cluster (Managed Clusters - Get Upgrade Profile)."""
    profiles: list[Any] = [
        {
            "name": "system",
            "kubernetesVersion": "1.30.9",
            "osType": "Linux",
            "upgrades": [{"kubernetesVersion": "1.30.10"}, {"kubernetesVersion": "1.31.2"}],
        }
    ]
    profiles += [
        {
            "name": f"user{n}",
            "kubernetesVersion": "1.29.11",
            "osType": "Linux",
            "upgrades": [{"kubernetesVersion": "1.30.9"}, {"kubernetesVersion": "1.30.10"}],
        }
        for n in range(1, pools)
    ]
    profiles.append("not-a-profile")
    return {
        "id": f"{_cluster_url()}/upgradeprofiles/default",
        "name": "default",
        "type": "Microsoft.ContainerService/managedClusters/upgradeprofiles",
        "properties": {
            "controlPlaneProfile": {
                "name": "master",
                "kubernetesVersion": "1.30.9",
                "osType": "Linux",
                "upgrades": [
                    {"kubernetesVersion": "1.31.2"},
                    {"kubernetesVersion": "1.30.10", "isPreview": False},
                    {"isPreview": True, "kubernetesVersion": "1.32.0"},
                    {"kubernetesVersion": "1.31.1"},
                ],
            },
            "agentPoolProfiles": profiles,
        },
    }


def _pool_answer(name: str, *, autoscaling: bool = False) -> dict[str, Any]:
    """One ``AgentPool`` (Agent Pools - List)."""
    properties: dict[str, Any] = {
        "count": 3,
        "vmSize": "Standard_D4s_v5",
        "osDiskSizeGB": 128,
        "osDiskType": "Managed",
        "maxPods": 110,
        "osType": "Linux",
        "osSKU": "AzureLinux",
        "mode": "System",
        "orchestratorVersion": "1.30",
        "currentOrchestratorVersion": "1.30.9",
        "nodeImageVersion": "AKSAzureLinux-V3gen2-202610.01.0",
        "provisioningState": "Succeeded",
        "powerState": {"code": "Running"},
        "availabilityZones": ["1", "2", "3"],
        "type": "VirtualMachineScaleSets",
        "scaleSetPriority": "Regular",
        "enableAutoScaling": autoscaling,
        "nodeLabels": {"team": "example"},
        "nodeTaints": ["dedicated=example:NoSchedule"],
        "messageOfTheDay": MOTD,
        "eTag": "example-etag",
    }
    if autoscaling:
        properties.update(minCount=1, maxCount=5)
    return {
        "id": f"{_cluster_url()}/agentPools/{name}",
        "name": name,
        "type": "Microsoft.ContainerService/managedClusters/agentPools",
        "properties": properties,
    }


def _versions_answer() -> dict[str, Any]:
    """Microsoft's List Kubernetes Versions example, plus one patch that sorts as a number."""
    return {
        "values": [
            {
                "capabilities": {"supportPlan": ["KubernetesOfficial"]},
                "isPreview": True,
                "patchVersions": {"1.26.0": {"upgrades": []}},
                "version": "1.26",
            },
            {
                "capabilities": {"supportPlan": ["KubernetesOfficial", "AKSLongTermSupport"]},
                "patchVersions": {
                    "1.23.12": {"upgrades": ["1.23.15", "1.24.6", "1.24.9"]},
                    "1.23.15": {"upgrades": ["1.24.6", "1.24.9"]},
                },
                "version": "1.23",
            },
            {
                "capabilities": {"supportPlan": ["KubernetesOfficial"]},
                "isDefault": True,
                "patchVersions": {
                    "1.24.10": {"upgrades": ["1.25.4"]},
                    "1.24.6": {"upgrades": ["1.24.9", "1.25.4", "1.25.5"]},
                    "1.24.9": {"upgrades": ["1.25.4", "1.25.5"]},
                },
                "version": "1.24",
            },
            "not-a-version",
        ]
    }


# ----------------------------------------------------------------------
# azure.aks.cluster.list
# ----------------------------------------------------------------------

_EXPECTED_ROW: dict[str, Any] = {
    "name": CLUSTER,
    "id": (
        f"/subscriptions/{SUBSCRIPTION_A}/resourcegroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.ContainerService/managedClusters/{CLUSTER}"
    ),
    "resource_group": RESOURCE_GROUP,
    "location": LOCATION,
    "kubernetes_version": "1.30",
    "current_kubernetes_version": "1.30.9",
    "provisioning_state": "Succeeded",
    "power_state": "Running",
    "sku_tier": "Standard",
    "support_plan": "KubernetesOfficial",
    "node_resource_group": f"MC_{RESOURCE_GROUP}_{CLUSTER}_{LOCATION}",
    "network_plugin": "azure",
    "network_plugin_mode": "overlay",
    "network_policy": "cilium",
    "network_dataplane": "cilium",
    "outbound_type": "loadBalancer",
    "private_cluster": False,
    "identity_type": "SystemAssigned",
    "auto_upgrade_channel": "patch",
    "node_os_upgrade_channel": "NodeImage",
    "node_pools": ["system", "user1"],
    "node_count": 5,
    "addons": ["azurepolicy", "keda", "omsagent", "workload_identity"],
}


@pytest.mark.asyncio
async def test_cluster_list_reads_every_page_and_builds_one_row_per_cluster(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()
    first_page = {
        "value": [_cluster_answer(), "not-a-cluster"],
        "nextLink": f"{_clusters_url()}?api-version={API_VERSION}&$skiptoken=page2",
    }
    second_page = {"value": [{"name": "bare"}]}

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(_clusters_url()).mock(
            side_effect=[
                httpx.Response(200, json=first_page),
                httpx.Response(200, json=second_page),
            ]
        )
        result = await _call("azure.aks.cluster.list", {})

    assert result.status == "ok", result.error
    assert result.handle is None
    assert arm.call_count == 2
    assert arm.calls[0].request.url.params["api-version"] == API_VERSION
    assert arm.calls[1].request.url.params["$skiptoken"] == "page2"
    assert result.result["value"][0] == _EXPECTED_ROW
    bare = result.result["value"][1]
    assert bare["name"] == "bare"
    assert bare["node_pools"] == [] and bare["addons"] == []
    assert bare["node_count"] is None and bare["private_cluster"] is False
    assert (result.result["count"], result.result["pages_read"]) == (2, 2)
    assert result.result["truncated"] is False
    assert login_token() not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_cluster_list_over_50_rows_goes_behind_a_handle_and_keeps_its_counts(
    _stub_embedding: AsyncMock, session: AsyncSession, store: MemoryResultStore
) -> None:
    await _registered_connector()
    names = [f"aks-example-{n:02d}" for n in range(60)]
    page = {"value": [_cluster_answer(name) for name in names]}

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(_clusters_url()).respond(200, json=page)
        result = await _call("azure.aks.cluster.list", {})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 60
    assert result.result["count"] == 60
    assert result.result["truncated"] is False
    assert result.result["pages_read"] == 1

    query = ResultQuerySpec.model_validate(
        {"filter": [{"field": "name", "op": "=", "value": names[7]}]}
    )
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    assert [row["name"] for row in answer["rows"]] == [names[7]]
    assert answer["rows"][0]["node_pools"] == ["system", "user1"]
    assert answer["rows"][0]["addons"] == ["azurepolicy", "keda", "omsagent", "workload_identity"]


# ----------------------------------------------------------------------
# azure.aks.cluster.show
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_show_returns_the_cluster_with_its_upgrade_options(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        cluster = mock.get(_cluster_url()).respond(200, json=_cluster_answer())
        upgrades = mock.get(f"{_cluster_url()}/upgradeProfiles/default").respond(
            200, json=_upgrade_answer()
        )
        result = await _call(
            "azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP, "name": CLUSTER}
        )

    assert result.status == "ok", result.error
    assert result.handle is None
    assert cluster.calls[0].request.url.params["api-version"] == API_VERSION
    assert upgrades.calls[0].request.url.params["api-version"] == API_VERSION
    answer = result.result
    assert {key: answer[key] for key in _EXPECTED_ROW} == _EXPECTED_ROW
    assert answer["fqdn"] == f"{CLUSTER}-dns.hcp.example.net"
    assert answer["private_fqdn"] is None
    assert answer["pod_cidrs"] == ["192.0.2.0/24"]
    assert answer["service_cidrs"] == ["198.51.100.0/24"]
    assert answer["upgrades"] == {
        "control_plane": {
            "kubernetes_version": "1.30.9",
            "upgrades": ["1.30.10", "1.31.1", "1.31.2"],
            "preview_upgrades": ["1.32.0"],
        },
        "node_pools": [
            {
                "name": "system",
                "os_type": "Linux",
                "kubernetes_version": "1.30.9",
                "upgrades": ["1.30.10", "1.31.2"],
                "preview_upgrades": [],
            },
            {
                "name": "user1",
                "os_type": "Linux",
                "kubernetes_version": "1.29.11",
                "upgrades": ["1.30.9", "1.30.10"],
                "preview_upgrades": [],
            },
        ],
    }


@pytest.mark.asyncio
async def test_cluster_show_and_list_never_return_a_secret_field(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(_cluster_url()).respond(200, json=_cluster_answer())
        mock.get(f"{_cluster_url()}/upgradeProfiles/default").respond(200, json=_upgrade_answer())
        mock.get(_clusters_url()).respond(200, json={"value": [_cluster_answer()]})
        shown = await _call(
            "azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP, "name": CLUSTER}
        )
        listed = await _call("azure.aks.cluster.list", {})

    for result in (shown, listed):
        assert result.status == "ok", result.error
        text = json.dumps(result.model_dump(mode="json"))
        for secret in (SP_SECRET, WINDOWS_PASSWORD, AAD_SECRET, SSH_KEY, ADDON_SETTING, MOTD):
            assert secret not in text
        assert login_token() not in text
        assert "exampleadmin" not in text and "azureuser" not in text


@pytest.mark.asyncio
async def test_a_big_cluster_show_is_kept_whole_and_its_upgrades_read_by_path(
    _stub_embedding: AsyncMock, session: AsyncSession, store: MemoryResultStore
) -> None:
    """Many node pools make the answer bigger than 4 KB: it is stored whole."""
    await _registered_connector()

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(_cluster_url()).respond(200, json=_cluster_answer(pools=40))
        mock.get(f"{_cluster_url()}/upgradeProfiles/default").respond(
            200, json=_upgrade_answer(pools=40)
        )
        result = await _call(
            "azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP, "name": CLUSTER}
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["name"] == CLUSTER
    assert result.result["power_state"] == "Running"
    assert "upgrades" in result.result["top_level_fields"]

    query = ResultQuerySpec.model_validate({"path": "/upgrades/control_plane"})
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    assert answer["value"]["upgrades"] == ["1.30.10", "1.31.1", "1.31.2"]


@pytest.mark.asyncio
async def test_cluster_show_of_an_unknown_cluster_is_not_found(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()
    missing = {"error": {"code": "ResourceNotFound", "message": "The cluster was not found."}}

    with respx.mock(assert_all_called=False) as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(_cluster_url()).respond(404, json=missing)
        upgrades = mock.get(f"{_cluster_url()}/upgradeProfiles/default").respond(
            200, json=_upgrade_answer()
        )
        result = await _call(
            "azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP, "name": CLUSTER}
        )

    assert result.status == "not_found", result.error
    assert not upgrades.called


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("azure.aks.cluster.show", {"resource_group": "..", "name": CLUSTER}),
        ("azure.aks.cluster.show", {"resource_group": "rg/other", "name": CLUSTER}),
        ("azure.aks.cluster.show", {"resource_group": "rg?x=1", "name": CLUSTER}),
        ("azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP, "name": "../other"}),
        ("azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP, "name": "-bad-"}),
        ("azure.aks.cluster.show", {"resource_group": RESOURCE_GROUP}),
        ("azure.aks.nodepool.list", {"resource_group": "rg x", "cluster": CLUSTER}),
        ("azure.aks.nodepool.list", {"resource_group": RESOURCE_GROUP, "cluster": "a%2Fb"}),
        ("azure.aks.version.list", {"location": "West Europe"}),
        ("azure.aks.version.list", {"location": "../westeurope"}),
        ("azure.aks.version.list", {}),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_parameter_is_refused_before_the_login_and_any_request(
    op_id: str, params: dict[str, Any], _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()

    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        result = await _call(op_id, params)

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not login.called
    assert not [call for call in mock.calls if call.request.url.host == "management.azure.com"]


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("azure.aks.cluster.show", {"resource_group": "rg-example\n", "name": CLUSTER}),
        ("azure.aks.nodepool.list", {"resource_group": RESOURCE_GROUP, "cluster": "aks01\n"}),
        ("azure.aks.version.list", {"location": "westeurope\n"}),
    ],
)
@pytest.mark.asyncio
async def test_a_line_break_that_passes_the_schema_is_refused_by_the_connector(
    op_id: str, params: dict[str, Any], _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A ``$`` in a JSON Schema pattern lets a line break at the end through."""
    await _registered_connector()

    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        result = await _call(op_id, params)

    assert result.status == "error"
    assert result.error == "connector_error: AksParameterError"
    assert result.extras["remediation"]
    assert "\n" not in result.extras["exception_message"]
    assert not login.called
    assert not [call for call in mock.calls if call.request.url.host == "management.azure.com"]


# ----------------------------------------------------------------------
# azure.aks.nodepool.list
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nodepool_list_returns_size_count_autoscaling_os_and_version(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()
    page = {"value": [_pool_answer("system"), _pool_answer("user1", autoscaling=True), 7]}

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        pools = mock.get(f"{_cluster_url()}/agentPools").respond(200, json=page)
        result = await _call(
            "azure.aks.nodepool.list", {"resource_group": RESOURCE_GROUP, "cluster": CLUSTER}
        )

    assert result.status == "ok", result.error
    assert result.handle is None
    assert pools.calls[0].request.url.params["api-version"] == API_VERSION
    assert result.result["value"][1] == {
        "name": "user1",
        "mode": "System",
        "vm_size": "Standard_D4s_v5",
        "count": 3,
        "autoscaling": True,
        "min_count": 1,
        "max_count": 5,
        "os_type": "Linux",
        "os_sku": "AzureLinux",
        "orchestrator_version": "1.30",
        "current_orchestrator_version": "1.30.9",
        "node_image_version": "AKSAzureLinux-V3gen2-202610.01.0",
        "power_state": "Running",
        "provisioning_state": "Succeeded",
        "availability_zones": ["1", "2", "3"],
        "max_pods": 110,
        "priority": "Regular",
        "pool_type": "VirtualMachineScaleSets",
    }
    first = result.result["value"][0]
    assert first["autoscaling"] is False
    assert first["min_count"] is None and first["max_count"] is None
    assert result.result["count"] == 2
    assert result.result["cluster"] == CLUSTER
    assert result.result["resource_group"] == RESOURCE_GROUP
    text = json.dumps(result.model_dump(mode="json"))
    assert MOTD not in text and "dedicated=example" not in text and "example-etag" not in text


@pytest.mark.asyncio
async def test_nodepool_list_over_50_pools_keeps_cluster_and_resource_group_at_the_handle(
    _stub_embedding: AsyncMock, session: AsyncSession, store: MemoryResultStore
) -> None:
    """AKS allows up to 100 node pools, so a list over 50 rows can happen."""
    await _registered_connector()
    names = [f"pool{n:02d}" for n in range(60)]
    first_page = {
        "value": [_pool_answer(name) for name in names[:30]],
        "nextLink": f"{_cluster_url()}/agentPools?api-version={API_VERSION}&$skiptoken=next",
    }
    second_page = {"value": [_pool_answer(name) for name in names[30:]]}

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(f"{_cluster_url()}/agentPools").mock(
            side_effect=[
                httpx.Response(200, json=first_page),
                httpx.Response(200, json=second_page),
            ]
        )
        result = await _call(
            "azure.aks.nodepool.list", {"resource_group": RESOURCE_GROUP, "cluster": CLUSTER}
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert (summary["count"], summary["pages_read"], summary["truncated"]) == (60, 2, False)
    assert (summary["cluster"], summary["resource_group"]) == (CLUSTER, RESOURCE_GROUP)

    query = ResultQuerySpec.model_validate(
        {"filter": [{"field": "name", "op": "=", "value": "pool42"}]}
    )
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    assert [(row["name"], row["vm_size"]) for row in answer["rows"]] == [
        ("pool42", "Standard_D4s_v5")
    ]


# ----------------------------------------------------------------------
# azure.aks.version.list
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_list_sorts_the_versions_and_names_the_default(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector()

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        versions = mock.get(_versions_url()).respond(200, json=_versions_answer())
        result = await _call("azure.aks.version.list", {"location": LOCATION})

    assert result.status == "ok", result.error
    assert result.handle is None
    assert versions.call_count == 1
    assert versions.calls[0].request.url.params["api-version"] == API_VERSION
    assert result.result == {
        "value": [
            {
                "version": "1.23",
                "is_default": False,
                "is_preview": False,
                "support_plans": ["KubernetesOfficial", "AKSLongTermSupport"],
                "patch_versions": ["1.23.12", "1.23.15"],
            },
            {
                "version": "1.24",
                "is_default": True,
                "is_preview": False,
                "support_plans": ["KubernetesOfficial"],
                "patch_versions": ["1.24.6", "1.24.9", "1.24.10"],
            },
            {
                "version": "1.26",
                "is_default": False,
                "is_preview": True,
                "support_plans": ["KubernetesOfficial"],
                "patch_versions": ["1.26.0"],
            },
        ],
        "count": 3,
        "location": LOCATION,
        "default_version": "1.24",
    }


@pytest.mark.asyncio
async def test_version_list_over_50_rows_keeps_location_and_default_at_the_handle(
    _stub_embedding: AsyncMock, session: AsyncSession, store: MemoryResultStore
) -> None:
    await _registered_connector()
    answer = {
        "values": [
            {
                "version": f"1.{minor}",
                "isDefault": minor == 40,
                "capabilities": {"supportPlan": ["KubernetesOfficial"]},
                "patchVersions": {f"1.{minor}.0": {"upgrades": []}},
            }
            for minor in range(60)
        ]
    }

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(_versions_url()).respond(200, json=answer)
        result = await _call("azure.aks.version.list", {"location": LOCATION})

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert (summary["row_count"], summary["count"]) == (60, 60)
    assert (summary["location"], summary["default_version"]) == (LOCATION, "1.40")

    query = ResultQuerySpec.model_validate(
        {"filter": [{"field": "is_default", "op": "=", "value": True}]}
    )
    rows = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    assert [row["version"] for row in rows["rows"]] == ["1.40"]


# ----------------------------------------------------------------------
# No cluster credential, ever
# ----------------------------------------------------------------------

#: The Azure actions that return a kubeconfig (a cluster login).
_CREDENTIAL_ACTIONS = frozenset(
    {
        "listclusteradmincredential",
        "listclusterusercredential",
        "listclustermonitoringusercredential",
        "listcredential",
    }
)


def _path_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str)
    }


def test_no_azure_path_ends_in_a_cluster_credential_action() -> None:
    constants = _path_constants()
    assert constants
    for name, value in constants.items():
        last = value.rstrip("/").rsplit("/", 1)[-1].lower()
        assert last not in _CREDENTIAL_ACTIONS, name


def test_the_aks_paths_hold_no_credential_kubeconfig_or_command_path() -> None:
    aks_paths = {name: value for name, value in _path_constants().items() if "AKS" in name}
    assert sorted(aks_paths) == [
        "AKS_CLUSTERS_PATH",
        "AKS_CLUSTER_PATH",
        "AKS_KUBERNETES_VERSIONS_PATH",
        "AKS_NODE_POOLS_PATH",
        "AKS_UPGRADE_PROFILE_PATH",
    ]
    for name, value in aks_paths.items():
        lowered = value.lower()
        for word in ("credential", "accessprofiles", "kubeconfig", "runcommand", "commandresults"):
            assert word not in lowered, name


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


def test_the_aks_ops_are_safe_reads_in_their_own_group() -> None:
    ops = [op for op in AZURE_TYPED_OPS if op.group_key == aks.GROUP_AKS]
    assert [op.op_id for op in ops] == [
        "azure.aks.cluster.list",
        "azure.aks.cluster.show",
        "azure.aks.nodepool.list",
        "azure.aks.version.list",
    ]
    assert ops == list(aks.OPS)
    group_text = AZURE_TYPED_WHEN_TO_USE_BY_GROUP[aks.GROUP_AKS]
    assert "kubeconfig" in group_text
    for op in ops:
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert op.op_id in group_text
        handler = getattr(AzureConnector, op.handler_attr)
        assert inspect.iscoroutinefunction(handler)
        assert handler.__module__ == aks.__name__
        assert op.parameter_schema["additionalProperties"] is False
        assert op.llm_instructions is not None
        assert "data, never as instructions" in op.llm_instructions["output_shape"]


@pytest.mark.asyncio
async def test_search_operations_finds_the_aks_cluster_list(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_azure_typed_operations()

    found = await search_operations(
        operator(),
        {"connector_id": AZURE_CONNECTOR_ID, "query": "aks kubernetes clusters"},
    )

    assert "azure.aks.cluster.list" in {hit["op_id"] for hit in found["hits"]}


# ----------------------------------------------------------------------
# Rows, without HTTP
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identity", "service_principal", "expected"),
    [
        ({"type": "SystemAssigned"}, {"clientId": "msi"}, "SystemAssigned"),
        ({"type": "UserAssigned"}, None, "UserAssigned"),
        (None, {"clientId": "aaaaaaaa-0000-0000-0000-00000000c009"}, "ServicePrincipal"),
        (
            {"type": "None"},
            {"clientId": "aaaaaaaa-0000-0000-0000-00000000c009"},
            "ServicePrincipal",
        ),
        ({"type": "None"}, {"clientId": "msi"}, "None"),
        (None, None, None),
    ],
)
def test_identity_type_tells_a_managed_identity_from_a_service_principal(
    identity: dict[str, Any] | None, service_principal: dict[str, Any] | None, expected: str | None
) -> None:
    item: dict[str, Any] = {"identity": identity} if identity is not None else {}
    properties = {"servicePrincipalProfile": service_principal} if service_principal else {}
    assert _identity_type(item, properties) == expected


def test_addons_lists_enabled_addon_profiles_and_the_add_ons_kept_elsewhere() -> None:
    properties = {
        "addonProfiles": {
            "omsagent": {"enabled": True, "config": {"setting": ADDON_SETTING}},
            "azureKeyvaultSecretsProvider": {"enabled": True},
            "httpApplicationRouting": {"enabled": False},
            "aciConnectorLinux": {"enabled": 1},
            "broken": "not-an-object",
        },
        "ingressProfile": {"webAppRouting": {"enabled": True}},
        "azureMonitorProfile": {
            "metrics": {"enabled": True},
            "containerInsights": {"enabled": True},
        },
        "serviceMeshProfile": {"mode": "Istio"},
        "workloadAutoScalerProfile": {"keda": {"enabled": 1}},
        "podIdentityProfile": {"enabled": True},
        "securityProfile": {"workloadIdentity": {"enabled": False}},
    }

    assert _addons(properties) == [
        "app_routing",
        "azureKeyvaultSecretsProvider",
        "container_insights",
        "istio",
        "managed_prometheus",
        "omsagent",
        "pod_identity",
    ]
    assert _addons({"serviceMeshProfile": {"mode": "Disabled"}}) == []
    assert _addons({}) == []


def test_rows_survive_odd_answers() -> None:
    odd = {
        "id": 7,
        "name": "",
        "sku": "Standard",
        "properties": {
            "agentPoolProfiles": [{"name": "a", "count": True}, "x", {"name": 3, "count": 2}],
            "powerState": "Running",
            "networkProfile": None,
            "apiServerAccessProfile": {"enablePrivateCluster": "true"},
        },
    }

    row = cluster_row(odd)
    assert row["name"] is None and row["id"] is None and row["resource_group"] is None
    assert row["sku_tier"] is None and row["power_state"] is None
    assert row["network_plugin"] is None and row["private_cluster"] is False
    assert row["node_pools"] == ["a"] and row["node_count"] == 2

    detail = cluster_detail({"properties": {"networkProfile": {"podCidr": "192.0.2.0/24"}}}, {})
    assert detail["pod_cidrs"] == ["192.0.2.0/24"] and detail["service_cidrs"] == []
    assert detail["upgrades"] == {
        "control_plane": {"kubernetes_version": None, "upgrades": [], "preview_upgrades": []},
        "node_pools": [],
    }

    pool = node_pool_row({"name": "p1", "properties": {"count": "3", "availabilityZones": "1"}})
    assert pool["count"] is None and pool["availability_zones"] == []

    assert version_rows({"value": [{"version": "1.30"}]}) == []
    assert version_rows({"values": "not-a-list"}) == []


def test_version_key_compares_each_part_as_a_number() -> None:
    versions = ["1.30.10", "1.30.9", "1.4.2", "1.30.9-hotfix", "1.30"]
    assert sorted(versions, key=version_key) == [
        "1.4.2",
        "1.30",
        "1.30.9",
        "1.30.10",
        "1.30.9-hotfix",
    ]
