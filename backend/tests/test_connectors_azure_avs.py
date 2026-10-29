# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure VMware Solution family of the Azure connector (``azure.avs.*``).

Every dispatch test runs the real dispatcher (policy, audit, result
reduction) with only the Azure registrar run. Microsoft's login service and
Azure Resource Manager are mocked with respx.

The Azure answers below have the shape of the examples next to the pinned
API description (``Microsoft.AVS`` 2026-03-01: ``PrivateClouds_ListInSubscription``
and its stretched variant, ``PrivateClouds_Get``, ``Clusters_List``,
``Hosts_List``, ``Locations_CheckQuotaAvailability``). A few fields the
examples leave out (for example ``extendedNetworkBlocks`` and a host's
``provisioningState``) are added from the definitions in the same file, so
the fixtures fill every field of every row. Every id, name, host name,
address and secret here is made up; the addresses come from the ranges
reserved for documentation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    _http,
    _paging,
    _paths,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.avs import OPS
from meho_backplane.connectors.azure.avs_reads import (
    avs_cluster_list_impl,
    avs_host_list_impl,
    avs_quota_show_impl,
    cluster_row,
    host_row,
    private_cloud_row,
    quota_answer,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    OBJECT_A,
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

RG = "rg-avs-example"
CLOUD = "avs-cloud-01"
CLUSTER = "Cluster-2"
AVS_VERSION = "2026-03-01"

#: Made-up secret values. Azure's API description marks these fields
#: write-only, so a real answer never holds them; none may reach a result.
VCENTER_PASSWORD = "fake-vcenter-password-for-tests"
NSXT_PASSWORD = "fake-nsxt-password-for-tests"
LDAP_PASSWORD = "fake-ldap-password-for-tests"
LICENSE_KEY = "FAKE0-LICEN-SEKEY-FORTE-STS00"
SECRETS = (VCENTER_PASSWORD, NSXT_PASSWORD, LDAP_PASSWORD, LICENSE_KEY)


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
async def registered(_stub_embedding: AsyncMock, session: AsyncSession) -> AzureConnector:
    """Run the Azure registrar and give the dispatcher's instance a stub loader."""
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """The real result reduction, over an in-memory store that result_query reads too."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


# ----------------------------------------------------------------------
# Azure answers, shaped like the examples of the API description
# ----------------------------------------------------------------------


def _url(suffix: str) -> str:
    return f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}{suffix}"


def _cloud_id(name: str = CLOUD) -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{RG}"
        f"/providers/Microsoft.AVS/privateClouds/{name}"
    )


def _circuit(name: str) -> dict[str, Any]:
    circuit_id = (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-avs-network"
        f"/providers/Microsoft.Network/expressRouteCircuits/{name}"
    )
    return {
        "expressRouteID": circuit_id,
        "primarySubnet": "198.51.100.32/30",
        "secondarySubnet": "198.51.100.36/30",
        "expressRoutePrivatePeeringID": f"{circuit_id}/peerings/AzurePrivatePeering",
    }


def _private_cloud(name: str, *, stretched: bool = False, **props: Any) -> dict[str, Any]:
    """One ``PrivateCloud`` like the ``PrivateClouds_ListInSubscription`` examples."""
    hosts = [f"esx{n:02d}.{name}.avs.example.test" for n in range(1, 4)]
    properties: dict[str, Any] = {
        "availability": (
            {"strategy": "DualZone", "zone": 1, "secondaryZone": 2}
            if stretched
            else {"strategy": "SingleZone", "zone": 1}
        ),
        "networkBlock": "198.51.100.0/22",
        "extendedNetworkBlocks": ["203.0.113.0/24"],
        "circuit": _circuit("er-avs-primary"),
        "managementCluster": {
            "clusterId": 1,
            "clusterSize": 3,
            "vsanDatastoreName": "vsanDatastore",
            "hosts": hosts,
        },
        "endpoints": {
            "nsxtManager": f"https://nsx.{name}.avs.example.test/",
            "vcsa": f"https://vc.{name}.avs.example.test/",
            "hcxCloudManager": f"https://hcx.{name}.avs.example.test/",
            "nsxtManagerIp": "198.51.100.3",
            "vcenterIp": "198.51.100.2",
            "hcxCloudManagerIp": "198.51.100.4",
        },
        "identitySources": [
            {
                "alias": "EXAMPLE",
                "baseGroupDN": "ou=groups,dc=example,dc=test",
                "baseUserDN": "ou=users,dc=example,dc=test",
                "domain": "example.test",
                "name": "example-ldap",
                "primaryServer": "ldaps://ldap1.example.test:636/",
                "secondaryServer": "ldaps://ldap2.example.test:636/",
                "ssl": "Enabled",
            }
        ],
        "internet": "Disabled",
        "vcfLicense": {
            "kind": "vcf5",
            "endDate": "2027-12-31T23:59:59Z",
            "cores": 16,
            "broadcomSiteId": "000000",
            "broadcomContractNumber": "000000",
            "provisioningState": "Succeeded",
        },
        "provisioningState": "Succeeded",
        **props,
    }
    if stretched:
        properties["secondaryCircuit"] = _circuit("er-avs-secondary")
    return {
        "id": _cloud_id(name),
        "location": "westeurope",
        "name": name,
        "sku": {"name": "AV36P"},
        "identity": {"principalId": OBJECT_A, "tenantId": TENANT_A, "type": "SystemAssigned"},
        "properties": properties,
        "tags": {},
        "type": "Microsoft.AVS/privateClouds",
    }


def _cloud_with_secrets(name: str) -> dict[str, Any]:
    """A private cloud whose answer holds the write-only secrets, against Azure's own rules."""
    cloud = _private_cloud(name, vcenterPassword=VCENTER_PASSWORD, nsxtPassword=NSXT_PASSWORD)
    cloud["properties"]["identitySources"][0]["password"] = LDAP_PASSWORD
    cloud["properties"]["vcfLicense"]["licenseKey"] = LICENSE_KEY
    return cloud


def _cluster(name: str, *, size: int = 3) -> dict[str, Any]:
    """One ``Cluster`` like the ``Clusters_List`` example."""
    return {
        "id": f"{_cloud_id()}/clusters/{name}",
        "name": name,
        "sku": {"name": "AV64"},
        "properties": {
            "clusterSize": size,
            "hosts": [f"esx{n:02d}.{name.lower()}.avs.example.test" for n in range(1, size + 1)],
            "provisioningState": "Succeeded",
        },
        "type": "Microsoft.AVS/privateClouds/clusters",
    }


def _host(name: str, **props: Any) -> dict[str, Any]:
    """One ``Host`` like the general host of the ``Hosts_List`` example."""
    return {
        "id": f"{_cloud_id()}/clusters/{CLUSTER}/hosts/{name}",
        "name": name,
        "type": "Microsoft.AVS/privateClouds/clusters/hosts",
        "zones": ["1"],
        "sku": {"name": "av64"},
        "properties": {
            "kind": "General",
            "displayName": name,
            "moRefId": "host-209",
            "fqdn": name,
            "faultDomain": "1",
            "provisioningState": "Succeeded",
            **props,
        },
    }


def _example_hosts() -> list[dict[str, Any]]:
    """The three host shapes of the ``Hosts_List`` example."""
    in_maintenance = _host("esx02.avs.example.test", moRefId="host-128", maintenance="Replacement")
    del in_maintenance["properties"]["fqdn"]
    del in_maintenance["properties"]["faultDomain"]
    specialized = {
        "id": f"{_cloud_id()}/clusters/{CLUSTER}/hosts/esx03.avs.example.test",
        "name": "esx03.avs.example.test",
        "type": "Microsoft.AVS/privateClouds/clusters/hosts",
        "zones": ["1"],
        "properties": {"kind": "Specialized"},
    }
    return [_host("esx01.avs.example.test"), in_maintenance, specialized]


#: The ``Locations_CheckQuotaAvailability`` example answer.
_QUOTA_EXAMPLE: dict[str, Any] = {
    "hostsRemaining": {"AV20": 0, "AV36": 999},
    "quotaEnabled": "Enabled",
}


async def _call(op_id: str, params: dict[str, Any] | None = None) -> Any:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params or {},
    )


def _login(mock: respx.MockRouter) -> None:
    mock.post(token_url()).respond(200, json=token_answer(login_token()))


async def _query(handle_id: Any, spec: dict[str, Any]) -> dict[str, Any]:
    return await result_query_module.run_result_query(
        operator(), handle_id, ResultQuerySpec.model_validate(spec)
    )


_CLOUDS_URL = _url("/providers/Microsoft.AVS/privateClouds")
_CLUSTERS_URL = _url(f"/resourceGroups/{RG}/providers/Microsoft.AVS/privateClouds/{CLOUD}/clusters")
_HOSTS_URL = f"{_CLUSTERS_URL}/{CLUSTER}/hosts"
_QUOTA_URL = _url("/providers/Microsoft.AVS/locations/westeurope/checkQuotaAvailability")
_CLUSTER_PARAMS: dict[str, Any] = {"resource_group": RG, "private_cloud": CLOUD}
_HOST_PARAMS: dict[str, Any] = {**_CLUSTER_PARAMS, "cluster": CLUSTER}


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

_AVS_OP_IDS = {
    "azure.avs.privatecloud.list",
    "azure.avs.cluster.list",
    "azure.avs.host.list",
    "azure.avs.quota.show",
}


@pytest.mark.asyncio
async def test_the_avs_reads_are_stored_as_safe_typed_operations(
    registered: AzureConnector, session: AsyncSession
) -> None:
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.impl_id == AZURE_IMPL_ID,
                    EndpointDescriptor.op_id.startswith("azure.avs."),
                )
            )
        )
        .scalars()
        .all()
    )
    groups = (
        (
            await session.execute(
                select(OperationGroup).where(OperationGroup.impl_id == AZURE_IMPL_ID)
            )
        )
        .scalars()
        .all()
    )

    assert {row.op_id for row in rows} == _AVS_OP_IDS
    for row in rows:
        assert (row.source_kind, row.is_enabled) == ("typed", True)
        assert (row.safety_level, row.requires_approval) == ("safe", False)
        attr = row.op_id.removeprefix("azure.").replace(".", "_")
        assert row.handler_ref is not None and row.handler_ref.endswith(f"AvsOps.{attr}")
    avs = next(group for group in groups if group.group_key == "avs")
    assert avs.review_status == "enabled"
    assert avs.when_to_use == AZURE_TYPED_WHEN_TO_USE_BY_GROUP["avs"]


def test_every_avs_operation_is_a_read_with_guidance() -> None:
    assert {op.op_id for op in OPS} == _AVS_OP_IDS
    for op in OPS:
        assert write_word_in_op_id(op.op_id) is None
        assert op.group_key == "avs" and "read-only" in op.tags
        assert (op.safety_level, op.requires_approval) == ("safe", False)
        assert op.parameter_schema["additionalProperties"] is False
        assert op.llm_instructions is not None
        for key in ("when_to_call", "output_shape", "next_step"):
            assert str(op.llm_instructions[key]).strip()


#: The private cloud actions that return secrets: the vCenter and NSX
#: passwords (listAdminCredentials) and license keys (getVcfLicense and
#: licenses/{licenseName}/getProperties). MEHO never calls them.
_SECRET_ACTIONS = ("listadmincredentials", "getvcflicense", "getproperties")


def test_no_path_reads_the_private_cloud_passwords_or_license_keys() -> None:
    paths = {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str)
    }
    assert "AVS_PRIVATE_CLOUDS_PATH" in paths
    for name, path in paths.items():
        action = path.rstrip("/").rsplit("/", 1)[-1].lower()
        assert action not in _SECRET_ACTIONS, f"{name} calls {action}"
        assert not path.lower().endswith("listadmincredentials"), name


# ----------------------------------------------------------------------
# Private clouds
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_privatecloud_list_reads_the_spec_example_shapes(registered: AzureConnector) -> None:
    single = _private_cloud("avs-cloud-01")
    stretched = _private_cloud("avs-cloud-02", stretched=True)
    del stretched["properties"]["extendedNetworkBlocks"]
    del stretched["properties"]["vcfLicense"]
    stretched["identity"] = {"type": "None"}
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(_CLOUDS_URL).respond(200, json={"value": [single, stretched]})
        result = await _call("azure.avs.privatecloud.list")

    assert result.status == "ok", result.error
    assert result.handle is None
    assert arm.calls[0].request.url.params["api-version"] == AVS_VERSION
    first, second = result.result["value"]
    assert first == {
        "name": "avs-cloud-01",
        "resource_group": RG,
        "location": "westeurope",
        "id": _cloud_id("avs-cloud-01"),
        "sku": "AV36P",
        "provisioning_state": "Succeeded",
        "management_cluster_size": 3,
        "management_cluster_hosts": [
            "esx01.avs-cloud-01.avs.example.test",
            "esx02.avs-cloud-01.avs.example.test",
            "esx03.avs-cloud-01.avs.example.test",
        ],
        "network_block": "198.51.100.0/22",
        "extended_network_blocks": ["203.0.113.0/24"],
        "internet": "Disabled",
        "availability_strategy": "SingleZone",
        "availability_zone": 1,
        "secondary_zone": None,
        "identity_type": "SystemAssigned",
        "vcf_license_kind": "vcf5",
    }
    assert second["availability_strategy"] == "DualZone"
    assert (second["availability_zone"], second["secondary_zone"]) == (1, 2)
    assert second["extended_network_blocks"] == []
    assert second["vcf_license_kind"] is None
    assert second["identity_type"] == "None"
    assert {key: result.result[key] for key in ("truncated", "omission", "pages_read")} == {
        "truncated": False,
        "omission": None,
        "pages_read": 1,
    }


@pytest.mark.asyncio
async def test_privatecloud_list_of_a_subscription_without_private_clouds(
    registered: AzureConnector,
) -> None:
    """The shape the live proof expects on a subscription with no private cloud."""
    with respx.mock() as mock:
        _login(mock)
        mock.get(_CLOUDS_URL).respond(200, json={"value": []})
        result = await _call("azure.avs.privatecloud.list")

    assert result.status == "ok", result.error
    assert result.result == {
        "value": [],
        "truncated": False,
        "omission": None,
        "omission_kind": None,
        "pages_read": 1,
    }


@pytest.mark.asyncio
async def test_privatecloud_list_never_returns_a_password_or_license_key(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    """Even an answer that holds them keeps them out of the result and the stored rows."""
    clouds = [_cloud_with_secrets(f"avs-cloud-{n:02d}") for n in range(60)]
    with respx.mock() as mock:
        _login(mock)
        mock.get(_CLOUDS_URL).respond(200, json={"value": clouds})
        result = await _call("azure.avs.privatecloud.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    rows = (await _query(result.handle.handle_id, {"limit": 100}))["rows"]
    assert len(rows) == 60
    stored = b"".join(store.client.values.values()).decode()
    rendered = json.dumps(result.model_dump(mode="json")) + json.dumps(rows) + stored
    assert "avs-cloud-07" in rendered
    for secret in SECRETS:
        assert secret not in rendered


# ----------------------------------------------------------------------
# Clusters and hosts
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_list_reads_the_clusters_of_one_private_cloud(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(_CLUSTERS_URL).respond(
            200, json={"value": [_cluster("Cluster-2"), _cluster("Cluster-3", size=4)]}
        )
        result = await _call("azure.avs.cluster.list", _CLUSTER_PARAMS)

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == AVS_VERSION
    assert result.result["value"][0] == {
        "name": "Cluster-2",
        "id": f"{_cloud_id()}/clusters/Cluster-2",
        "sku": "AV64",
        "cluster_size": 3,
        "hosts": [
            "esx01.cluster-2.avs.example.test",
            "esx02.cluster-2.avs.example.test",
            "esx03.cluster-2.avs.example.test",
        ],
        "provisioning_state": "Succeeded",
    }
    assert result.result["value"][1]["cluster_size"] == 4
    assert (result.result["resource_group"], result.result["private_cloud"]) == (RG, CLOUD)
    assert result.result["pages_read"] == 1


@pytest.mark.asyncio
async def test_cluster_list_encodes_the_names_in_the_path(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(url__startswith=_url("/resourceGroups/")).respond(200, json={"value": []})
        result = await _call(
            "azure.avs.cluster.list",
            {"resource_group": "rg-(avs).1", "private_cloud": "avs_cloud.1"},
        )

    assert result.status == "ok", result.error
    raw_path = arm.calls[0].request.url.raw_path.decode().split("?")[0]
    assert raw_path == (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-%28avs%29.1"
        "/providers/Microsoft.AVS/privateClouds/avs_cloud.1/clusters"
    )
    assert result.result["resource_group"] == "rg-(avs).1"


@pytest.mark.asyncio
async def test_host_list_reads_the_hosts_of_one_cluster(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(_HOSTS_URL).respond(200, json={"value": _example_hosts()})
        result = await _call("azure.avs.host.list", _HOST_PARAMS)

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == AVS_VERSION
    general, in_maintenance, specialized = result.result["value"]
    assert general == {
        "name": "esx01.avs.example.test",
        "id": f"{_cloud_id()}/clusters/{CLUSTER}/hosts/esx01.avs.example.test",
        "display_name": "esx01.avs.example.test",
        "kind": "General",
        "provisioning_state": "Succeeded",
        "fqdn": "esx01.avs.example.test",
        "mo_ref_id": "host-209",
        "maintenance": None,
        "fault_domain": "1",
        "sku": "av64",
        "zones": ["1"],
    }
    assert in_maintenance["maintenance"] == "Replacement"
    assert in_maintenance["mo_ref_id"] == "host-128"
    assert (in_maintenance["fqdn"], in_maintenance["fault_domain"]) == (None, None)
    assert specialized["kind"] == "Specialized"
    assert (specialized["display_name"], specialized["sku"]) == (None, None)
    assert {key: result.result[key] for key in ("resource_group", "private_cloud", "cluster")} == {
        "resource_group": RG,
        "private_cloud": CLOUD,
        "cluster": CLUSTER,
    }


@pytest.mark.asyncio
async def test_a_missing_private_cloud_is_not_found(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_CLUSTERS_URL).respond(
            404, json={"error": {"code": "ResourceNotFound", "message": "not found"}}
        )
        result = await _call("azure.avs.cluster.list", _CLUSTER_PARAMS)

    assert result.status == "not_found", result.error


# ----------------------------------------------------------------------
# Lists over 50 rows: a link to the stored result
# ----------------------------------------------------------------------

_LIST_CASES = [
    pytest.param(
        "azure.avs.privatecloud.list", _CLOUDS_URL, {}, _private_cloud, {}, id="privatecloud"
    ),
    pytest.param(
        "azure.avs.cluster.list",
        _CLUSTERS_URL,
        _CLUSTER_PARAMS,
        _cluster,
        _CLUSTER_PARAMS,
        id="cluster",
    ),
    pytest.param("azure.avs.host.list", _HOSTS_URL, _HOST_PARAMS, _host, _HOST_PARAMS, id="host"),
]


@pytest.mark.parametrize(("op_id", "url", "params", "make", "context"), _LIST_CASES)
@pytest.mark.asyncio
async def test_a_list_over_50_rows_goes_behind_a_link_with_its_plain_values(
    registered: AzureConnector,
    store: MemoryResultStore,
    op_id: str,
    url: str,
    params: dict[str, Any],
    make: Callable[[str], dict[str, Any]],
    context: dict[str, Any],
) -> None:
    """60 objects over two pages: the real reduction stores the rows, the rest stays inline."""
    names = [f"example-{n:02d}" for n in range(60)]
    first = {
        "value": [make(name) for name in names[:40]],
        "nextLink": f"{url}?api-version={AVS_VERSION}&$skiptoken=page2",
    }
    second = {"value": [make(name) for name in names[40:]]}

    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(url).mock(
            side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
        )
        result = await _call(op_id, params)

    assert result.status == "ok", result.error
    assert arm.call_count == 2
    assert arm.calls[1].request.url.params["$skiptoken"] == "page2"
    assert result.handle is not None
    assert result.result["row_count"] == 60
    assert result.result["truncated"] is False
    assert result.result["omission"] is None
    assert result.result["omission_kind"] is None
    assert result.result["pages_read"] == 2
    for key, value in context.items():
        assert result.result[key] == value

    answer = await _query(
        result.handle.handle_id, {"filter": [{"field": "name", "op": "=", "value": names[7]}]}
    )
    (row,) = answer["rows"]
    assert row["name"] == names[7]
    assert row["id"] is not None and row["id"].endswith(f"/{names[7]}")


@pytest.mark.asyncio
async def test_a_list_stopped_by_a_limit_says_so_next_to_the_link(
    registered: AzureConnector, store: MemoryResultStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paging time limit runs out after page one: the omission survives the reduction."""
    clock = iter([0.0, 500.0])
    monkeypatch.setattr(_paging, "_monotonic", lambda: next(clock))
    page = {
        "value": [_host(f"esx{n:02d}.avs.example.test") for n in range(55)],
        "nextLink": f"{_HOSTS_URL}?api-version={AVS_VERSION}&$skiptoken=page2",
    }

    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(_HOSTS_URL).respond(200, json=page)
        result = await _call("azure.avs.host.list", _HOST_PARAMS)

    assert result.status == "ok", result.error
    assert arm.call_count == 1
    assert result.handle is not None
    assert result.result["row_count"] == 55
    assert result.result["truncated"] is True
    assert result.result["omission_kind"] == "time_limit"
    assert "Narrow the request" in result.result["omission"]
    assert result.result["pages_read"] == 1
    assert result.result["cluster"] == CLUSTER


# ----------------------------------------------------------------------
# Host quota
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_show_reads_the_host_quota_of_one_region(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.post(_QUOTA_URL).respond(200, json=_QUOTA_EXAMPLE)
        result = await _call("azure.avs.quota.show", {"location": "westeurope"})

    assert result.status == "ok", result.error
    assert result.result == {
        "location": "westeurope",
        "quota_enabled": "Enabled",
        "hosts_remaining": {"AV20": 0, "AV36": 999},
    }
    request = arm.calls[0].request
    assert request.method == "POST"
    assert request.url.params["api-version"] == AVS_VERSION
    assert request.content == b""


@pytest.mark.asyncio
async def test_quota_show_is_tried_again_after_a_server_error(
    registered: AzureConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quota check only reads, so a 5xx is retried like a GET."""
    monkeypatch.setattr(_http, "_sleep", AsyncMock())
    with respx.mock() as mock:
        _login(mock)
        arm = mock.post(_QUOTA_URL).mock(
            side_effect=[
                httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}}),
                httpx.Response(200, json=_QUOTA_EXAMPLE),
            ]
        )
        result = await _call("azure.avs.quota.show", {"location": "westeurope"})

    assert result.status == "ok", result.error
    assert arm.call_count == 2


def test_quota_answer_keeps_only_whole_host_counts() -> None:
    odd = {"hostsRemaining": {"AV36": "many", "AV52": True, "AV64": 2, "": 1}, "quotaEnabled": 5}

    assert quota_answer(odd, location="westeurope") == {
        "location": "westeurope",
        "quota_enabled": None,
        "hosts_remaining": {"AV64": 2},
    }
    assert quota_answer({"hostsRemaining": [3]}, location="westeurope")["hosts_remaining"] == {}


# ----------------------------------------------------------------------
# Errors and refused names
# ----------------------------------------------------------------------

_NOT_REGISTERED = {
    "error": {
        "code": "MissingSubscriptionRegistration",
        "message": "The subscription is not registered to use namespace 'Microsoft.AVS'.",
    }
}


@pytest.mark.parametrize(
    ("op_id", "method", "url", "params"),
    [
        pytest.param("azure.avs.privatecloud.list", "GET", _CLOUDS_URL, {}, id="privatecloud"),
        pytest.param(
            "azure.avs.quota.show", "POST", _QUOTA_URL, {"location": "westeurope"}, id="quota"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_subscription_without_the_avs_provider_gets_a_clear_message(
    registered: AzureConnector, op_id: str, method: str, url: str, params: dict[str, Any]
) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.request(method, url).respond(409, json=_NOT_REGISTERED)
        result = await _call(op_id, params)

    assert result.status == "error"
    assert arm.call_count == 1
    extras = result.extras or {}
    assert extras["exception_class"] == "AzureProviderNotRegisteredError"
    assert "MissingSubscriptionRegistration" in extras["exception_message"]
    assert "not registered" in extras["exception_message"]
    assert "MEHO never registers it" in extras["remediation"]


_REFUSED = [
    pytest.param("azure.avs.cluster.list", {"resource_group": "..", "private_cloud": CLOUD}),
    pytest.param("azure.avs.cluster.list", {"resource_group": "rg.", "private_cloud": CLOUD}),
    pytest.param("azure.avs.cluster.list", {"resource_group": "rg%2Fx", "private_cloud": CLOUD}),
    pytest.param("azure.avs.cluster.list", {"resource_group": RG, "private_cloud": ".."}),
    pytest.param("azure.avs.cluster.list", {"resource_group": RG, "private_cloud": "a/../b"}),
    pytest.param("azure.avs.cluster.list", {"resource_group": RG, "private_cloud": "pc\n"}),
    pytest.param("azure.avs.cluster.list", {"resource_group": RG, "private_cloud": "x" * 81}),
    pytest.param("azure.avs.cluster.list", {"resource_group": RG}),
    pytest.param("azure.avs.host.list", {**_CLUSTER_PARAMS, "cluster": "."}),
    pytest.param("azure.avs.host.list", {**_CLUSTER_PARAMS, "cluster": "c?x=1"}),
    pytest.param("azure.avs.host.list", {**_HOST_PARAMS, "subscription_id": SUBSCRIPTION_A}),
    pytest.param("azure.avs.quota.show", {"location": "West Europe"}),
    pytest.param("azure.avs.quota.show", {"location": "westeurope/../x"}),
    pytest.param("azure.avs.quota.show", {}),
]


@pytest.mark.parametrize(("op_id", "params"), _REFUSED)
@pytest.mark.asyncio
async def test_a_bad_parameter_is_refused_before_any_call(
    registered: AzureConnector, op_id: str, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.route(url__startswith=ARM_BASE).respond(200, json={})
        result = await _call(op_id, params)

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not arm.called and not login.called


@pytest.mark.parametrize(
    ("impl", "params"),
    [
        (avs_cluster_list_impl, {"resource_group": "..", "private_cloud": CLOUD}),
        (avs_cluster_list_impl, {"resource_group": RG, "private_cloud": "a/b"}),
        (avs_host_list_impl, {**_CLUSTER_PARAMS, "cluster": ".."}),
        (avs_host_list_impl, {**_CLUSTER_PARAMS, "cluster": "c#1"}),
        (avs_quota_show_impl, {"location": "westeurope\n"}),
    ],
)
@pytest.mark.asyncio
async def test_the_bodies_check_the_names_themselves(
    impl: Callable[..., Any], params: dict[str, Any]
) -> None:
    """A call that skips the dispatcher's schema check still sends nothing."""
    connector = AzureConnector(credentials_loader=CountingLoader())
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.route(url__startswith=ARM_BASE).respond(200, json={})
        with pytest.raises(ValueError, match="not a valid Azure name") as caught:
            await impl(connector, operator(), AzureTargetStub(), params)

    assert not arm.called
    for value in params.values():
        if value not in (RG, CLOUD):
            assert value not in str(caught.value)


# ----------------------------------------------------------------------
# Rows and schemas
# ----------------------------------------------------------------------

_ROW_BUILDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "azure.avs.privatecloud.list": private_cloud_row,
    "azure.avs.cluster.list": cluster_row,
    "azure.avs.host.list": host_row,
}


def test_every_row_lists_exactly_the_fields_of_its_schema() -> None:
    """The response schema and the row builder name the same fields."""
    by_id = {op.op_id: op for op in OPS}
    for op_id, build in _ROW_BUILDERS.items():
        schema = by_id[op_id].response_schema
        assert schema is not None
        assert set(build({})) == set(schema["properties"]["value"]["items"]["properties"]), op_id
    quota_schema = by_id["azure.avs.quota.show"].response_schema
    assert quota_schema is not None
    assert set(quota_answer({}, location="westeurope")) == set(quota_schema["properties"])


def test_the_spec_example_fixtures_fill_every_field() -> None:
    """Each row field has a value in at least one fixture shaped like Azure's examples."""
    fixtures = {
        "azure.avs.privatecloud.list": [
            _private_cloud("avs-cloud-01"),
            _private_cloud("avs-cloud-02", stretched=True),
        ],
        "azure.avs.cluster.list": [_cluster("Cluster-2")],
        "azure.avs.host.list": _example_hosts(),
    }
    for op_id, items in fixtures.items():
        rows = [_ROW_BUILDERS[op_id](item) for item in items]
        for field in rows[0]:
            assert any(row[field] not in (None, []) for row in rows), (op_id, field)
    quota = quota_answer(_QUOTA_EXAMPLE, location="westeurope")
    assert all(value not in (None, {}) for value in quota.values())


@pytest.mark.parametrize(("op_id", "url", "params", "make", "context"), _LIST_CASES)
@pytest.mark.asyncio
async def test_a_bare_object_gives_empty_fields_and_a_non_object_is_skipped(
    registered: AzureConnector,
    op_id: str,
    url: str,
    params: dict[str, Any],
    make: Callable[[str], dict[str, Any]],
    context: dict[str, Any],
) -> None:
    del make, context
    with respx.mock() as mock:
        _login(mock)
        mock.get(url).respond(200, json={"value": [{"name": "bare"}, "not-an-object", None]})
        result = await _call(op_id, params)

    assert result.status == "ok", result.error
    (row,) = result.result["value"]
    assert row["name"] == "bare"
    assert row["id"] is None
    for key, value in row.items():
        assert value in (None, []) or key == "name", (key, value)
