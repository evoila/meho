# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the network family of the Azure connector (``azure.network.*``).

Every test runs the real dispatcher (policy, audit, result reduction) with
only the Azure registrar run. Microsoft's login service and Azure Resource
Manager are mocked with respx. The answers follow the shapes of Azure's
API description files (``Microsoft.Network`` 2026-01-01, private DNS
2024-06-01). Every id, name, address and key here is made up; the
addresses come from the ranges reserved for documentation.
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
    _paging,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.network import OPS
from meho_backplane.connectors.azure.network_reads import network_vnet_show_impl
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    OperationGroup,
    Tenant,
)
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction.flight_recorder import SECRET_FAMILY_OMITTED_MARKER
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    MEHO_TENANT,
    SUBSCRIPTION_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)
from tests._result_store_helpers import MemoryResultStore

RG_HUB = "rg-hub"
RG_SPOKE = "rg-spoke"
NETWORK_VERSION = "2026-01-01"
PRIVATE_DNS_VERSION = "2024-06-01"

#: Made-up key values. None of them may reach a result or a stored trace.
SERVICE_KEY = "00000000-5e1c-4e00-8000-0000000000aa"
AUTHORIZATION_KEY = "fake-authorization-key-for-tests"
PEERING_SHARED_KEY = "fake-bgp-shared-key-for-tests"
ACTIVATION_KEY = "fake-activation-key-for-tests"
CIRCUIT_SECRETS = (SERVICE_KEY, AUTHORIZATION_KEY, PEERING_SHARED_KEY, ACTIVATION_KEY)


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
# Azure answers, shaped like the API description files
# ----------------------------------------------------------------------


def _url(suffix: str) -> str:
    return f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}{suffix}"


def _rid(kind: str, name: str, group: str = RG_HUB) -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{group}"
        f"/providers/Microsoft.Network/{kind}/{name}"
    )


def _subnet(vnet: str, name: str, **props: Any) -> dict[str, Any]:
    return {
        "id": f"{_rid('virtualNetworks', vnet)}/subnets/{name}",
        "name": name,
        "type": "Microsoft.Network/virtualNetworks/subnets",
        "properties": {"provisioningState": "Succeeded", **props},
    }


def _peering(name: str, remote: str, **props: Any) -> dict[str, Any]:
    return {
        "id": f"{_rid('virtualNetworks', 'vnet-hub')}/virtualNetworkPeerings/{name}",
        "name": name,
        "properties": {
            "remoteVirtualNetwork": {"id": _rid("virtualNetworks", remote, RG_SPOKE)},
            "peeringState": "Connected",
            "peeringSyncLevel": "FullyInSync",
            "allowVirtualNetworkAccess": True,
            "allowForwardedTraffic": True,
            "allowGatewayTransit": True,
            "useRemoteGateways": False,
            "remoteAddressSpace": {"addressPrefixes": ["203.0.113.0/25"]},
            "provisioningState": "Succeeded",
            **props,
        },
    }


def _vnet(
    name: str, *, subnets: list[Any] | None = None, peerings: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "id": _rid("virtualNetworks", name),
        "name": name,
        "type": "Microsoft.Network/virtualNetworks",
        "location": "westeurope",
        "properties": {
            "addressSpace": {"addressPrefixes": ["198.51.100.0/24"]},
            "dhcpOptions": {"dnsServers": ["198.51.100.4"]},
            "subnets": subnets if subnets is not None else [_subnet(name, "default")],
            "virtualNetworkPeerings": peerings or [],
            "resourceGuid": "00000000-0000-0000-0000-0000000000b1",
            "provisioningState": "Succeeded",
        },
    }


def _rule(name: str, priority: int, **props: Any) -> dict[str, Any]:
    return {
        "id": f"{_rid('networkSecurityGroups', 'nsg-web')}/securityRules/{name}",
        "name": name,
        "properties": {
            "protocol": "Tcp",
            "sourcePortRange": "*",
            "destinationPortRange": "443",
            "sourceAddressPrefix": "Internet",
            "destinationAddressPrefix": "*",
            "access": "Allow",
            "priority": priority,
            "direction": "Inbound",
            "provisioningState": "Succeeded",
            **props,
        },
    }


def _default_rules() -> list[dict[str, Any]]:
    """Azure's six default rules, as the API returns them."""
    defaults = [
        ("AllowVnetInBound", 65000, "Inbound", "Allow", "VirtualNetwork"),
        ("AllowAzureLoadBalancerInBound", 65001, "Inbound", "Allow", "AzureLoadBalancer"),
        ("DenyAllInBound", 65500, "Inbound", "Deny", "*"),
        ("AllowVnetOutBound", 65000, "Outbound", "Allow", "VirtualNetwork"),
        ("AllowInternetOutBound", 65001, "Outbound", "Allow", "*"),
        ("DenyAllOutBound", 65500, "Outbound", "Deny", "*"),
    ]
    return [
        _rule(
            name,
            priority,
            direction=direction,
            access=access,
            protocol="*",
            destinationPortRange="*",
            sourceAddressPrefix=source,
            description="Azure default rule",
        )
        for name, priority, direction, access, source in defaults
    ]


def _nsg(
    name: str, *, rules: list[Any] | None = None, subnets: int = 1, nics: int = 0
) -> dict[str, Any]:
    return {
        "id": _rid("networkSecurityGroups", name),
        "name": name,
        "type": "Microsoft.Network/networkSecurityGroups",
        "location": "westeurope",
        "properties": {
            "securityRules": rules if rules is not None else [_rule("allow-https", 100)],
            "defaultSecurityRules": _default_rules(),
            "subnets": [
                {"id": f"{_rid('virtualNetworks', 'vnet-hub')}/subnets/sn-{n}"}
                for n in range(subnets)
            ],
            "networkInterfaces": [
                {"id": _rid("networkInterfaces", f"nic-{n}")} for n in range(nics)
            ],
            "provisioningState": "Succeeded",
        },
    }


def _load_balancer(name: str, *, sku: str = "Standard", public: bool = True) -> dict[str, Any]:
    frontend: dict[str, Any] = (
        {"publicIPAddress": {"id": _rid("publicIPAddresses", f"pip-{name}")}}
        if public
        else {"privateIPAddress": "198.51.100.10", "privateIPAllocationMethod": "Static"}
    )
    return {
        "id": _rid("loadBalancers", name),
        "name": name,
        "location": "westeurope",
        "sku": {"name": sku, "tier": "Regional"},
        "properties": {
            "frontendIPConfigurations": [{"name": "fe-1", "properties": frontend}],
            "backendAddressPools": [{"name": "pool-1"}, {"name": "pool-2"}],
            "loadBalancingRules": [{"name": "rule-1"}],
            "probes": [{"name": "probe-1"}],
            "inboundNatRules": [],
            "outboundRules": [{"name": "out-1"}],
            "provisioningState": "Succeeded",
        },
    }


def _public_ip(
    name: str, *, used_by: str | None = None, nat: str | None = None, **extra: Any
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "publicIPAllocationMethod": "Static",
        "publicIPAddressVersion": "IPv4",
        "ipAddress": "192.0.2.10",
        "idleTimeoutInMinutes": 4,
        "dnsSettings": {"domainNameLabel": name, "fqdn": f"{name}.example.test"},
        "provisioningState": "Succeeded",
    }
    if used_by is not None:
        props["ipConfiguration"] = {"id": used_by}
    if nat is not None:
        props["natGateway"] = {"id": _rid("natGateways", nat)}
    return {
        "id": _rid("publicIPAddresses", name),
        "name": name,
        "location": "westeurope",
        "sku": {"name": "Standard", "tier": "Regional"},
        "zones": ["1", "2", "3"],
        "properties": props,
        **extra,
    }


def _gateway(name: str) -> dict[str, Any]:
    return {
        "id": _rid("virtualNetworkGateways", name),
        "name": name,
        "location": "westeurope",
        "properties": {
            "gatewayType": "Vpn",
            "vpnType": "RouteBased",
            "vpnGatewayGeneration": "Generation2",
            "enableBgp": True,
            "activeActive": False,
            "sku": {"name": "VpnGw2AZ", "tier": "VpnGw2AZ", "capacity": 2},
            "bgpSettings": {"asn": 65515, "bgpPeeringAddress": "198.51.100.30", "peerWeight": 0},
            "ipConfigurations": [
                {
                    "name": "default",
                    "properties": {
                        "privateIPAllocationMethod": "Dynamic",
                        "subnet": {
                            "id": f"{_rid('virtualNetworks', 'vnet-hub')}/subnets/GatewaySubnet"
                        },
                        "publicIPAddress": {"id": _rid("publicIPAddresses", f"pip-{name}")},
                    },
                }
            ],
            "vpnClientConfiguration": {"vpnClientProtocols": ["OpenVPN"]},
            "provisioningState": "Succeeded",
        },
    }


def _circuit(name: str) -> dict[str, Any]:
    """An ExpressRoute circuit with the keys Azure puts into its answer."""
    return {
        "id": _rid("expressRouteCircuits", name),
        "name": name,
        "location": "westeurope",
        "sku": {"name": "Standard_MeteredData", "tier": "Standard", "family": "MeteredData"},
        "properties": {
            "serviceKey": SERVICE_KEY,
            "authorizationKey": AUTHORIZATION_KEY,
            "activationKey": ACTIVATION_KEY,
            "authorizations": [
                {"name": "auth-1", "properties": {"authorizationKey": AUTHORIZATION_KEY}}
            ],
            "peerings": [
                {
                    "name": "AzurePrivatePeering",
                    "properties": {"sharedKey": PEERING_SHARED_KEY, "peerASN": 65001},
                }
            ],
            "serviceProviderProperties": {
                "serviceProviderName": "Example Provider",
                "peeringLocation": "Example City",
                "bandwidthInMbps": 200,
            },
            "circuitProvisioningState": "Enabled",
            "serviceProviderProvisioningState": "Provisioned",
            "serviceProviderNotes": "made-up notes",
            "globalReachEnabled": False,
            "provisioningState": "Succeeded",
        },
    }


def _route_table(name: str, *, routes: list[Any] | None = None) -> dict[str, Any]:
    default_routes = [
        {
            "name": "to-firewall",
            "properties": {
                "addressPrefix": "0.0.0.0/0",
                "nextHopType": "VirtualAppliance",
                "nextHopIpAddress": "198.51.100.68",
            },
        }
    ]
    return {
        "id": _rid("routeTables", name),
        "name": name,
        "location": "westeurope",
        "properties": {
            "routes": routes if routes is not None else default_routes,
            "subnets": [{"id": f"{_rid('virtualNetworks', 'vnet-spoke')}/subnets/sn-app"}],
            "disableBgpRoutePropagation": True,
            "provisioningState": "Succeeded",
        },
    }


def _zone(name: str) -> dict[str, Any]:
    return {
        "id": _rid("privateDnsZones", name),
        "name": name,
        "type": "Microsoft.Network/privateDnsZones",
        "location": "global",
        "etag": "00000000-0000-0000-0000-0000000000e1",
        "properties": {
            "maxNumberOfRecordSets": 25000,
            "numberOfRecordSets": 3,
            "maxNumberOfVirtualNetworkLinks": 1000,
            "numberOfVirtualNetworkLinks": 2,
            "maxNumberOfVirtualNetworkLinksWithRegistration": 100,
            "numberOfVirtualNetworkLinksWithRegistration": 1,
            "provisioningState": "Succeeded",
            "internalId": "made-up-internal-id",
        },
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


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

_NETWORK_OP_IDS = {
    "azure.network.vnet.list",
    "azure.network.vnet.show",
    "azure.network.nsg.list",
    "azure.network.nsg.show",
    "azure.network.lb.list",
    "azure.network.publicip.list",
    "azure.network.gateway.list",
    "azure.network.expressroute.list",
    "azure.network.routetable.list",
    "azure.network.privatedns.list",
}


@pytest.mark.asyncio
async def test_the_network_reads_are_stored_as_safe_typed_operations(
    registered: AzureConnector, session: AsyncSession
) -> None:
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.impl_id == AZURE_IMPL_ID,
                    EndpointDescriptor.op_id.startswith("azure.network."),
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

    assert {row.op_id for row in rows} == _NETWORK_OP_IDS
    for row in rows:
        assert (row.source_kind, row.is_enabled) == ("typed", True)
        assert (row.safety_level, row.requires_approval) == ("safe", False)
        attr = row.op_id.removeprefix("azure.").replace(".", "_")
        assert row.handler_ref is not None and row.handler_ref.endswith(f"NetworkOps.{attr}")
    network = next(group for group in groups if group.group_key == "network")
    assert network.review_status == "enabled"
    assert network.when_to_use == AZURE_TYPED_WHEN_TO_USE_BY_GROUP["network"]


def test_every_network_operation_is_a_read_with_guidance() -> None:
    assert {op.op_id for op in OPS} == _NETWORK_OP_IDS
    for op in OPS:
        assert write_word_in_op_id(op.op_id) is None
        assert op.group_key == "network" and "read-only" in op.tags
        assert op.parameter_schema["additionalProperties"] is False
        assert op.llm_instructions is not None
        for key in ("when_to_call", "output_shape", "next_step"):
            assert str(op.llm_instructions[key]).strip()


# ----------------------------------------------------------------------
# Lists: rows inline, and a link to the stored result over 50 rows
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vnet_list_shows_ranges_dns_and_counts(registered: AzureConnector) -> None:
    hub = _vnet(
        "vnet-hub",
        subnets=[_subnet("vnet-hub", "GatewaySubnet"), _subnet("vnet-hub", "sn-shared")],
        peerings=[_peering("hub-to-spoke", "vnet-spoke")],
    )
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(_url("/providers/Microsoft.Network/virtualNetworks")).respond(
            200, json={"value": [hub]}
        )
        result = await _call("azure.network.vnet.list")

    assert result.status == "ok", result.error
    assert result.handle is None
    assert arm.calls[0].request.url.params["api-version"] == NETWORK_VERSION
    assert result.result == {
        "value": [
            {
                "name": "vnet-hub",
                "resource_group": RG_HUB,
                "location": "westeurope",
                "id": _rid("virtualNetworks", "vnet-hub"),
                "address_prefixes": ["198.51.100.0/24"],
                "dns_servers": ["198.51.100.4"],
                "subnet_count": 2,
                "peering_count": 1,
                "provisioning_state": "Succeeded",
            }
        ],
        "truncated": False,
        "omission": None,
        "omission_kind": None,
        "pages_read": 1,
    }


_LIST_CASES = [
    pytest.param("azure.network.vnet.list", "virtualNetworks", _vnet, id="vnet"),
    pytest.param("azure.network.nsg.list", "networkSecurityGroups", _nsg, id="nsg"),
    pytest.param("azure.network.lb.list", "loadBalancers", _load_balancer, id="lb"),
    pytest.param("azure.network.publicip.list", "publicIPAddresses", _public_ip, id="publicip"),
    pytest.param("azure.network.gateway.list", "virtualNetworkGateways", _gateway, id="gateway"),
    pytest.param(
        "azure.network.expressroute.list", "expressRouteCircuits", _circuit, id="expressroute"
    ),
    pytest.param("azure.network.routetable.list", "routeTables", _route_table, id="routetable"),
    pytest.param("azure.network.privatedns.list", "privateDnsZones", _zone, id="privatedns"),
]


def _list_url(op_id: str, kind: str) -> str:
    if op_id == "azure.network.gateway.list":
        return _url(f"/resourceGroups/{RG_HUB}/providers/Microsoft.Network/{kind}")
    return _url(f"/providers/Microsoft.Network/{kind}")


def _list_params(op_id: str) -> dict[str, Any]:
    return {"resource_group": RG_HUB} if op_id == "azure.network.gateway.list" else {}


@pytest.mark.parametrize(("op_id", "kind", "make"), _LIST_CASES)
@pytest.mark.asyncio
async def test_a_list_over_50_rows_goes_behind_a_link_with_its_paging_fields(
    registered: AzureConnector,
    store: MemoryResultStore,
    op_id: str,
    kind: str,
    make: Callable[[str], dict[str, Any]],
) -> None:
    """60 objects over two pages: the real reduction stores the rows, the rest stays inline."""
    names = [f"example-{n:02d}" for n in range(60)]
    url = _list_url(op_id, kind)
    first = {
        "value": [make(name) for name in names[:40]],
        "nextLink": f"{url}?api-version={NETWORK_VERSION}&$skiptoken=page2",
    }
    second = {"value": [make(name) for name in names[40:]]}

    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(url).mock(
            side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
        )
        result = await _call(op_id, _list_params(op_id))

    assert result.status == "ok", result.error
    assert arm.call_count == 2
    assert arm.calls[1].request.url.params["$skiptoken"] == "page2"
    assert result.handle is not None
    assert result.result["row_count"] == 60
    assert result.result["truncated"] is False
    assert result.result["omission"] is None
    assert result.result["omission_kind"] is None
    assert result.result["pages_read"] == 2

    answer = await _query(
        result.handle.handle_id, {"filter": [{"field": "name", "op": "=", "value": names[7]}]}
    )
    (row,) = answer["rows"]
    assert row["name"] == names[7]
    assert row["resource_group"] == RG_HUB
    assert row["id"] == _rid(kind, names[7])


@pytest.mark.asyncio
async def test_a_list_stopped_by_a_limit_says_so_next_to_the_link(
    registered: AzureConnector, store: MemoryResultStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paging time limit runs out after page one: the omission survives the reduction."""
    clock = iter([0.0, 500.0])
    monkeypatch.setattr(_paging, "_monotonic", lambda: next(clock))
    url = _list_url("azure.network.publicip.list", "publicIPAddresses")
    page = {
        "value": [_public_ip(f"pip-{n:02d}") for n in range(55)],
        "nextLink": f"{url}?api-version={NETWORK_VERSION}&$skiptoken=page2",
    }

    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(url).respond(200, json=page)
        result = await _call("azure.network.publicip.list")

    assert result.status == "ok", result.error
    assert arm.call_count == 1
    assert result.handle is not None
    assert result.result["row_count"] == 55
    assert result.result["truncated"] is True
    assert result.result["omission_kind"] == "time_limit"
    assert "Narrow the request" in result.result["omission"]
    assert result.result["pages_read"] == 1


# ----------------------------------------------------------------------
# One virtual network
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vnet_show_returns_subnets_and_peerings(registered: AzureConnector) -> None:
    hub = _vnet(
        "vnet-hub",
        subnets=[
            _subnet(
                "vnet-hub",
                "sn-app",
                addressPrefix="198.51.100.0/26",
                networkSecurityGroup={"id": _rid("networkSecurityGroups", "nsg-app")},
                routeTable={"id": _rid("routeTables", "rt-app")},
                natGateway={"id": _rid("natGateways", "nat-1")},
                delegations=[
                    {"name": "d-1", "properties": {"serviceName": "Microsoft.Web/serverFarms"}}
                ],
                ipConfigurations=[
                    {"id": f"{_rid('networkInterfaces', 'nic-1')}/ipConfigurations/c"}
                ],
            ),
            _subnet("vnet-hub", "sn-dual", addressPrefixes=["198.51.100.64/26", "2001:db8::/64"]),
        ],
        peerings=[
            _peering("hub-to-spoke", "vnet-spoke"),
            _peering(
                "hub-to-old",
                "vnet-old",
                peeringState="Disconnected",
                peeringSyncLevel="RemoteNotInSync",
                allowGatewayTransit=False,
                useRemoteGateways=True,
                remoteAddressSpace={},
                remoteVirtualNetworkAddressSpace={"addressPrefixes": ["203.0.113.128/25"]},
            ),
        ],
    )
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(
            _url(f"/resourceGroups/{RG_HUB}/providers/Microsoft.Network/virtualNetworks/vnet-hub")
        ).respond(200, json=hub)
        result = await _call(
            "azure.network.vnet.show", {"resource_group": RG_HUB, "name": "vnet-hub"}
        )

    assert result.status == "ok", result.error
    assert result.handle is None
    assert arm.calls[0].request.url.params["api-version"] == NETWORK_VERSION
    shown = result.result
    assert (shown["name"], shown["resource_group"], shown["subnet_count"]) == (
        "vnet-hub",
        RG_HUB,
        2,
    )
    assert shown["subnets"] == [
        {
            "name": "sn-app",
            "address_prefixes": ["198.51.100.0/26"],
            "network_security_group_id": _rid("networkSecurityGroups", "nsg-app"),
            "route_table_id": _rid("routeTables", "rt-app"),
            "nat_gateway_id": _rid("natGateways", "nat-1"),
            "delegations": ["Microsoft.Web/serverFarms"],
        },
        {
            "name": "sn-dual",
            "address_prefixes": ["198.51.100.64/26", "2001:db8::/64"],
            "network_security_group_id": None,
            "route_table_id": None,
            "nat_gateway_id": None,
            "delegations": [],
        },
    ]
    assert shown["peerings"] == [
        {
            "name": "hub-to-spoke",
            "remote_virtual_network_id": _rid("virtualNetworks", "vnet-spoke", RG_SPOKE),
            "peering_state": "Connected",
            "peering_sync_level": "FullyInSync",
            "allow_virtual_network_access": True,
            "allow_forwarded_traffic": True,
            "allow_gateway_transit": True,
            "use_remote_gateways": False,
            "remote_address_prefixes": ["203.0.113.0/25"],
        },
        {
            "name": "hub-to-old",
            "remote_virtual_network_id": _rid("virtualNetworks", "vnet-old", RG_SPOKE),
            "peering_state": "Disconnected",
            "peering_sync_level": "RemoteNotInSync",
            "allow_virtual_network_access": True,
            "allow_forwarded_traffic": True,
            "allow_gateway_transit": False,
            "use_remote_gateways": True,
            "remote_address_prefixes": ["203.0.113.128/25"],
        },
    ]
    assert "ipConfigurations" not in json.dumps(shown)


@pytest.mark.asyncio
async def test_a_large_vnet_is_stored_whole_and_read_by_path(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    subnets = [
        _subnet("vnet-hub", f"sn-{n:03d}", addressPrefix="198.51.100.0/28") for n in range(120)
    ]
    peerings = [_peering(f"to-spoke-{n:02d}", f"vnet-spoke-{n:02d}") for n in range(12)]
    with respx.mock() as mock:
        _login(mock)
        mock.get(url__startswith=_url("/resourceGroups/")).respond(
            200, json=_vnet("vnet-hub", subnets=subnets, peerings=peerings)
        )
        result = await _call(
            "azure.network.vnet.show", {"resource_group": RG_HUB, "name": "vnet-hub"}
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["result_record"] == "document"
    assert result.result["name"] == "vnet-hub"
    assert result.result["subnet_count"] == 120
    assert result.result["peering_count"] == 12

    peering_page = await _query(result.handle.handle_id, {"path": "/peerings"})
    assert peering_page["total_rows"] == 12
    assert peering_page["rows"][0]["name"] == "to-spoke-00"
    subnet_page = await _query(result.handle.handle_id, {"path": "/subnets"})
    assert subnet_page["total_rows"] == 120


@pytest.mark.asyncio
async def test_vnet_show_encodes_the_names_in_the_path(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(url__startswith=_url("/resourceGroups/")).respond(
            200, json=_vnet("vnet.hub_1")
        )
        result = await _call(
            "azure.network.vnet.show", {"resource_group": "rg-(hub).1", "name": "vnet.hub_1"}
        )

    assert result.status == "ok", result.error
    raw_path = arm.calls[0].request.url.raw_path.decode()
    assert "/resourceGroups/rg-%28hub%29.1/providers/" in raw_path
    assert raw_path.split("?")[0].endswith("/virtualNetworks/vnet.hub_1")


@pytest.mark.parametrize(
    "params",
    [
        {"resource_group": "..", "name": "vnet-hub"},
        {"resource_group": "rg-hub.", "name": "vnet-hub"},
        {"resource_group": "rg/../other", "name": "vnet-hub"},
        {"resource_group": "rg-hub?x=1", "name": "vnet-hub"},
        {"resource_group": "rg-hub\n", "name": "vnet-hub"},
        {"resource_group": "rg%2Fhub", "name": "vnet-hub"},
        {"resource_group": "", "name": "vnet-hub"},
        {"resource_group": RG_HUB, "name": ".."},
        {"resource_group": RG_HUB, "name": "-vnet"},
        {"resource_group": RG_HUB, "name": "vnet-"},
        {"resource_group": RG_HUB, "name": "vnet/../../x"},
        {"resource_group": RG_HUB, "name": "v"},
        {"resource_group": RG_HUB},
        {"resource_group": RG_HUB, "name": "vnet-hub", "subscription_id": SUBSCRIPTION_A},
    ],
)
@pytest.mark.asyncio
async def test_vnet_show_refuses_a_bad_name_before_any_call(
    registered: AzureConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={})
        result = await _call("azure.network.vnet.show", params)

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not arm.called and not login.called


@pytest.mark.parametrize("bad", ["..", "rg-hub.", "a/b", "rg-hub\n", "rg#1"])
@pytest.mark.asyncio
async def test_the_body_checks_the_names_itself(bad: str) -> None:
    """A call that skips the dispatcher's schema check still sends nothing."""
    connector = AzureConnector(credentials_loader=CountingLoader())
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={})
        with pytest.raises(ValueError, match="not a valid Azure name"):
            await network_vnet_show_impl(
                connector, operator(), AzureTargetStub(), {"resource_group": bad, "name": "vnet-1"}
            )

    assert not arm.called


@pytest.mark.asyncio
async def test_a_missing_network_is_not_found(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(url__startswith=_url("/resourceGroups/")).respond(
            404, json={"error": {"code": "ResourceNotFound", "message": "not found"}}
        )
        result = await _call(
            "azure.network.vnet.show", {"resource_group": RG_HUB, "name": "vnet-gone"}
        )

    assert result.status == "not_found", result.error


# ----------------------------------------------------------------------
# Network security groups
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nsg_list_counts_rules_and_users(registered: AzureConnector) -> None:
    group = _nsg("nsg-web", rules=[_rule("a", 100), _rule("b", 110)], subnets=2, nics=3)
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/networkSecurityGroups")).respond(
            200, json={"value": [group]}
        )
        result = await _call("azure.network.nsg.list")

    assert result.status == "ok", result.error
    assert result.result["value"] == [
        {
            "name": "nsg-web",
            "resource_group": RG_HUB,
            "location": "westeurope",
            "id": _rid("networkSecurityGroups", "nsg-web"),
            "custom_rule_count": 2,
            "subnet_count": 2,
            "network_interface_count": 3,
            "provisioning_state": "Succeeded",
        }
    ]


@pytest.mark.asyncio
async def test_nsg_show_lists_custom_and_default_rules_in_order(
    registered: AzureConnector,
) -> None:
    asg = _rid("applicationSecurityGroups", "asg-web")
    rules = [
        _rule(
            "allow-out-dns", 200, direction="Outbound", protocol="Udp", destinationPortRange="53"
        ),
        _rule(
            "allow-web",
            110,
            protocol="*",
            sourcePortRange="",
            sourcePortRanges=["1024-65535"],
            destinationPortRange="",
            destinationPortRanges=["80", "443"],
            sourceAddressPrefix="",
            sourceAddressPrefixes=["192.0.2.0/24", "198.51.100.0/24"],
            destinationAddressPrefix="",
            destinationApplicationSecurityGroups=[{"id": asg}],
            description="Web traffic from the example ranges",
        ),
        _rule("deny-rdp", 100, access="Deny", destinationPortRange="3389"),
    ]
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(
            _url(
                f"/resourceGroups/{RG_HUB}/providers/Microsoft.Network/networkSecurityGroups/nsg-web"
            )
        ).respond(200, json=_nsg("nsg-web", rules=rules, subnets=1, nics=2))
        result = await _call(
            "azure.network.nsg.show", {"resource_group": RG_HUB, "name": "nsg-web"}
        )

    assert result.status == "ok", result.error
    assert result.handle is None
    assert arm.calls[0].request.url.params["api-version"] == NETWORK_VERSION
    group = result.result["network_security_group"]
    assert group == {
        "name": "nsg-web",
        "resource_group": RG_HUB,
        "location": "westeurope",
        "id": _rid("networkSecurityGroups", "nsg-web"),
        "custom_rule_count": 3,
        "default_rule_count": 6,
        "subnet_count": 1,
        "network_interface_count": 2,
        "subnet_ids": [f"{_rid('virtualNetworks', 'vnet-hub')}/subnets/sn-0"],
        "provisioning_state": "Succeeded",
    }
    order = [(rule["direction"], rule["priority"], rule["kind"]) for rule in result.result["value"]]
    assert order == [
        ("Inbound", 100, "custom"),
        ("Inbound", 110, "custom"),
        ("Inbound", 65000, "default"),
        ("Inbound", 65001, "default"),
        ("Inbound", 65500, "default"),
        ("Outbound", 200, "custom"),
        ("Outbound", 65000, "default"),
        ("Outbound", 65001, "default"),
        ("Outbound", 65500, "default"),
    ]
    web = result.result["value"][1]
    assert web == {
        "name": "allow-web",
        "kind": "custom",
        "direction": "Inbound",
        "priority": 110,
        "access": "Allow",
        "protocol": "*",
        "source_prefixes": ["192.0.2.0/24", "198.51.100.0/24"],
        "source_ports": ["1024-65535"],
        "destination_prefixes": [],
        "destination_ports": ["80", "443"],
        "source_application_security_group_ids": [],
        "destination_application_security_group_ids": [asg],
        "description": "Web traffic from the example ranges",
    }
    deny = result.result["value"][0]
    assert (deny["access"], deny["source_prefixes"], deny["destination_ports"]) == (
        "Deny",
        ["Internet"],
        ["3389"],
    )


@pytest.mark.asyncio
async def test_a_large_nsg_keeps_the_group_next_to_the_link(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    """60 custom rules plus 6 default: the rules are stored, the group object stays inline."""
    rules = [
        _rule(f"rule-{n:02d}", 100 + n, access="Allow" if n % 2 else "Deny") for n in range(60)
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.get(url__startswith=_url("/resourceGroups/")).respond(
            200, json=_nsg("nsg-big", rules=rules, subnets=2, nics=5)
        )
        result = await _call(
            "azure.network.nsg.show", {"resource_group": RG_HUB, "name": "nsg-big"}
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 66
    assert result.result["network_security_group"] == {
        "name": "nsg-big",
        "resource_group": RG_HUB,
        "id": _rid("networkSecurityGroups", "nsg-big"),
        "custom_rule_count": 60,
        "default_rule_count": 6,
        "subnet_count": 2,
        "network_interface_count": 5,
        "subnet_ids": [
            f"{_rid('virtualNetworks', 'vnet-hub')}/subnets/sn-0",
            f"{_rid('virtualNetworks', 'vnet-hub')}/subnets/sn-1",
        ],
    }

    allowed = await _query(
        result.handle.handle_id,
        {
            "filter": [
                {"field": "direction", "op": "=", "value": "Inbound"},
                {"field": "access", "op": "=", "value": "Allow"},
                {"field": "kind", "op": "=", "value": "custom"},
            ]
        },
    )
    assert len(allowed["rows"]) == 30
    whole = await _query(result.handle.handle_id, {"path": "/network_security_group"})
    assert whole["value"]["location"] == "westeurope"
    assert whole["value"]["provisioning_state"] == "Succeeded"


# ----------------------------------------------------------------------
# Load balancers, public IP addresses, gateways
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lb_list_shows_sku_front_ends_and_counts(registered: AzureConnector) -> None:
    balancers = [_load_balancer("lb-public"), _load_balancer("lb-old", sku="Basic", public=False)]
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/loadBalancers")).respond(
            200, json={"value": balancers}
        )
        result = await _call("azure.network.lb.list")

    assert result.status == "ok", result.error
    public, internal = result.result["value"]
    assert public == {
        "name": "lb-public",
        "resource_group": RG_HUB,
        "location": "westeurope",
        "id": _rid("loadBalancers", "lb-public"),
        "sku": "Standard",
        "sku_tier": "Regional",
        "frontend_count": 1,
        "public_ip_address_ids": [_rid("publicIPAddresses", "pip-lb-public")],
        "private_ip_addresses": [],
        "backend_pool_count": 2,
        "rule_count": 1,
        "probe_count": 1,
        "inbound_nat_rule_count": 0,
        "outbound_rule_count": 1,
        "provisioning_state": "Succeeded",
    }
    assert (internal["sku"], internal["public_ip_address_ids"]) == ("Basic", [])
    assert internal["private_ip_addresses"] == ["198.51.100.10"]


@pytest.mark.asyncio
async def test_publicip_list_names_the_resource_that_uses_each_address(
    registered: AzureConnector,
) -> None:
    nic_config = f"{_rid('networkInterfaces', 'nic-web')}/ipConfigurations/ipconfig1"
    lb_frontend = f"{_rid('loadBalancers', 'lb-public')}/frontendIPConfigurations/fe-1"
    addresses = [
        _public_ip("pip-nic", used_by=nic_config),
        _public_ip("pip-lb", used_by=lb_frontend),
        _public_ip("pip-nat", nat="nat-1"),
        _public_ip("pip-free"),
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/publicIPAddresses")).respond(
            200, json={"value": addresses}
        )
        result = await _call("azure.network.publicip.list")

    assert result.status == "ok", result.error
    rows = {row["name"]: row for row in result.result["value"]}
    assert rows["pip-nic"] == {
        "name": "pip-nic",
        "resource_group": RG_HUB,
        "location": "westeurope",
        "id": _rid("publicIPAddresses", "pip-nic"),
        "ip_address": "192.0.2.10",
        "ip_version": "IPv4",
        "allocation": "Static",
        "sku": "Standard",
        "sku_tier": "Regional",
        "zones": ["1", "2", "3"],
        "attached_to": _rid("networkInterfaces", "nic-web"),
        "attached_type": "Microsoft.Network/networkInterfaces",
        "dns_fqdn": "pip-nic.example.test",
        "provisioning_state": "Succeeded",
    }
    assert (rows["pip-lb"]["attached_to"], rows["pip-lb"]["attached_type"]) == (
        _rid("loadBalancers", "lb-public"),
        "Microsoft.Network/loadBalancers",
    )
    assert (rows["pip-nat"]["attached_to"], rows["pip-nat"]["attached_type"]) == (
        _rid("natGateways", "nat-1"),
        "Microsoft.Network/natGateways",
    )
    assert (rows["pip-free"]["attached_to"], rows["pip-free"]["attached_type"]) == (None, None)


@pytest.mark.asyncio
async def test_gateway_list_reads_one_resource_group(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(
            _url(f"/resourceGroups/{RG_HUB}/providers/Microsoft.Network/virtualNetworkGateways")
        ).respond(200, json={"value": [_gateway("vpngw-hub")]})
        result = await _call("azure.network.gateway.list", {"resource_group": RG_HUB})

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == NETWORK_VERSION
    assert result.result["value"] == [
        {
            "name": "vpngw-hub",
            "resource_group": RG_HUB,
            "location": "westeurope",
            "id": _rid("virtualNetworkGateways", "vpngw-hub"),
            "gateway_type": "Vpn",
            "vpn_type": "RouteBased",
            "sku": "VpnGw2AZ",
            "generation": "Generation2",
            "bgp_enabled": True,
            "bgp_asn": 65515,
            "active_active": False,
            "virtual_network_id": _rid("virtualNetworks", "vnet-hub"),
            "public_ip_address_ids": [_rid("publicIPAddresses", "pip-vpngw-hub")],
            "provisioning_state": "Succeeded",
        }
    ]


@pytest.mark.asyncio
async def test_gateway_list_needs_a_resource_group(registered: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.get(url__startswith=ARM_BASE).respond(200, json={"value": []})
        result = await _call("azure.network.gateway.list")

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not arm.called


@pytest.mark.asyncio
async def test_an_empty_resource_group_has_no_gateway(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(url__startswith=_url("/resourceGroups/")).respond(200, json={"value": []})
        result = await _call("azure.network.gateway.list", {"resource_group": RG_SPOKE})

    assert result.status == "ok", result.error
    assert result.result["value"] == []
    assert result.result["truncated"] is False


# ----------------------------------------------------------------------
# ExpressRoute circuits: the keys never leave
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expressroute_list_shows_provider_bandwidth_and_state_without_keys(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/expressRouteCircuits")).respond(
            200, json={"value": [_circuit("er-primary")]}
        )
        result = await _call("azure.network.expressroute.list")

    assert result.status == "ok", result.error
    assert result.result["value"] == [
        {
            "name": "er-primary",
            "resource_group": RG_HUB,
            "location": "westeurope",
            "id": _rid("expressRouteCircuits", "er-primary"),
            "provider": "Example Provider",
            "peering_location": "Example City",
            "bandwidth_mbps": 200,
            "bandwidth_gbps": None,
            "sku_tier": "Standard",
            "sku_family": "MeteredData",
            "circuit_state": "Enabled",
            "provider_state": "Provisioned",
            "global_reach_enabled": False,
            "peering_count": 1,
            "provisioning_state": "Succeeded",
        }
    ]
    rendered = json.dumps(result.model_dump(mode="json"))
    for secret in CIRCUIT_SECRETS:
        assert secret not in rendered


@pytest.mark.asyncio
async def test_an_expressroute_list_error_keeps_azure_text_out(registered: AzureConnector) -> None:
    """The keys-withheld tag also hides Azure's error text; the code and the hint stay."""
    refusal = {"error": {"code": "AuthorizationFailed", "message": "made-up refusal text"}}
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/expressRouteCircuits")).respond(
            403, json=refusal
        )
        result = await _call("azure.network.expressroute.list")

    assert result.status == "error"
    assert result.extras is not None and result.extras["http_status"] == 403
    assert "role" in result.extras["remediation"]
    assert "made-up refusal text" not in json.dumps(result.model_dump(mode="json"))


async def _trace_spans(audit_id: Any) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = await s.execute(
            select(DispatchTraceSpan)
            .where(DispatchTraceSpan.trace_id == trace.id)
            .order_by(DispatchTraceSpan.seq.asc())
        )
        return list(spans.scalars().all())


@pytest.mark.asyncio
async def test_the_trace_of_expressroute_list_keeps_no_body(
    registered: AzureConnector, session: AsyncSession
) -> None:
    """The flight recorder is on: the stored trace holds no key of the circuit."""
    session.add(
        Tenant(id=MEHO_TENANT, slug="azure-net", name="azure-net", flight_recorder_enabled=True)
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/expressRouteCircuits")).respond(
            200, json={"value": [_circuit("er-primary")]}
        )
        result = await _call("azure.network.expressroute.list")

    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _trace_spans(result.audit_id)
    vendor_calls = [span for span in spans if span.span_kind == "vendor_call"]
    assert vendor_calls
    arm_call = next(span for span in vendor_calls if "expressRouteCircuits" in span.name)
    assert arm_call.attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER
    rendered = json.dumps([f"{span.name}|{span.attributes}" for span in spans], default=str)
    for secret in CIRCUIT_SECRETS:
        assert secret not in rendered


# ----------------------------------------------------------------------
# Route tables and private DNS zones
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routetable_list_shows_every_route(registered: AzureConnector) -> None:
    table = _route_table(
        "rt-spoke",
        routes=[
            {
                "name": "to-firewall",
                "properties": {
                    "addressPrefix": "0.0.0.0/0",
                    "nextHopType": "VirtualAppliance",
                    "nextHopIpAddress": "198.51.100.68",
                },
            },
            {
                "name": "drop-test",
                "properties": {"addressPrefix": "192.0.2.0/24", "nextHopType": "None"},
            },
        ],
    )
    with respx.mock() as mock:
        _login(mock)
        mock.get(_url("/providers/Microsoft.Network/routeTables")).respond(
            200, json={"value": [table]}
        )
        result = await _call("azure.network.routetable.list")

    assert result.status == "ok", result.error
    assert result.result["value"] == [
        {
            "name": "rt-spoke",
            "resource_group": RG_HUB,
            "location": "westeurope",
            "id": _rid("routeTables", "rt-spoke"),
            "bgp_route_propagation_disabled": True,
            "subnet_count": 1,
            "route_count": 2,
            "routes": [
                {
                    "name": "to-firewall",
                    "address_prefix": "0.0.0.0/0",
                    "next_hop_type": "VirtualAppliance",
                    "next_hop_ip_address": "198.51.100.68",
                },
                {
                    "name": "drop-test",
                    "address_prefix": "192.0.2.0/24",
                    "next_hop_type": "None",
                    "next_hop_ip_address": None,
                },
            ],
            "provisioning_state": "Succeeded",
        }
    ]


@pytest.mark.asyncio
async def test_privatedns_list_counts_records_and_links(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(_url("/providers/Microsoft.Network/privateDnsZones")).respond(
            200, json={"value": [_zone("privatelink.blob.core.windows.net")]}
        )
        result = await _call("azure.network.privatedns.list")

    assert result.status == "ok", result.error
    assert arm.calls[0].request.url.params["api-version"] == PRIVATE_DNS_VERSION
    assert result.result["value"] == [
        {
            "name": "privatelink.blob.core.windows.net",
            "resource_group": RG_HUB,
            "location": "global",
            "id": _rid("privateDnsZones", "privatelink.blob.core.windows.net"),
            "record_set_count": 3,
            "virtual_network_link_count": 2,
            "registration_link_count": 1,
            "provisioning_state": "Succeeded",
        }
    ]


# ----------------------------------------------------------------------
# Odd answers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "kind", "make"), _LIST_CASES)
@pytest.mark.asyncio
async def test_a_bare_object_gives_empty_fields_and_a_non_object_is_skipped(
    registered: AzureConnector, op_id: str, kind: str, make: Callable[[str], dict[str, Any]]
) -> None:
    del make
    with respx.mock() as mock:
        _login(mock)
        mock.get(_list_url(op_id, kind)).respond(
            200, json={"value": [{"name": "bare"}, "not-an-object", None]}
        )
        result = await _call(op_id, _list_params(op_id))

    assert result.status == "ok", result.error
    (row,) = result.result["value"]
    assert row["name"] == "bare"
    assert row["resource_group"] is None and row["id"] is None
    for key, value in row.items():
        assert value in (None, [], 0) or key == "name", (key, value)


def test_every_network_row_lists_exactly_the_fields_of_its_schema() -> None:
    """The response schema and the row builder name the same fields."""
    from meho_backplane.connectors.azure import network_rows

    builders = {
        "azure.network.vnet.list": network_rows.vnet_row,
        "azure.network.nsg.list": network_rows.nsg_row,
        "azure.network.lb.list": network_rows.load_balancer_row,
        "azure.network.publicip.list": network_rows.public_ip_row,
        "azure.network.gateway.list": network_rows.gateway_row,
        "azure.network.expressroute.list": network_rows.express_route_row,
        "azure.network.routetable.list": network_rows.route_table_row,
        "azure.network.privatedns.list": network_rows.private_zone_row,
    }
    by_id = {op.op_id: op for op in OPS}
    for op_id, build in builders.items():
        schema = by_id[op_id].response_schema
        assert schema is not None
        row_fields = set(schema["properties"]["value"]["items"]["properties"])
        assert set(build({})) == row_fields, op_id
    vnet_schema = by_id["azure.network.vnet.show"].response_schema
    assert vnet_schema is not None
    assert set(network_rows.vnet_detail({})) == set(vnet_schema["properties"])
    nsg_schema = by_id["azure.network.nsg.show"].response_schema
    assert nsg_schema is not None
    shown = network_rows.nsg_detail({})
    group_fields = nsg_schema["properties"]["network_security_group"]["properties"]
    assert set(shown["network_security_group"]) == set(group_fields)
