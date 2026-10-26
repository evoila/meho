# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA tenant ops for a project's own network and its first deployment.

Drives each op through the real dispatcher (approval already granted) against
a respx-mocked appliance. Pins the wire calls (path, ``apiVersion``, body), the
tenant login on the blueprint and deployment APIs, the "safe to run twice"
answers (``unchanged``, nothing written), and the project quota and network
rule on ``vcfa.tenant.project.create``. All names are synthetic.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_DEPLOY_OPS,
    VCFA_IMPL_ID,
    VCFA_NETWORK_OPS,
    VCFA_PRODUCT,
    VCFA_VERSION,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TENANT_TOKEN,
    mount_logins,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_API_VERSION = "2021-07-15"
_NETWORKS = "/iaas/api/fabric-networks-vsphere"
_NETWORK = f"{_NETWORKS}/net-1"
_PROFILES = "/iaas/api/network-profiles"
_PROJECTS = "/iaas/api/projects"
_DEPLOYMENTS = "/iaas/api/deployments"
_BP_REQUESTS = "/blueprint/api/blueprint-requests"

_EXPECTED_TIERS: dict[str, tuple[str, bool]] = {
    "vcfa.tenant.fabric_network.list": ("safe", False),
    "vcfa.tenant.fabric_compute.list": ("safe", False),
    "vcfa.tenant.region.list": ("safe", False),
    "vcfa.tenant.zone.list": ("safe", False),
    "vcfa.tenant.fabric_network.update": ("caution", True),
    "vcfa.tenant.network_profile.create": ("caution", True),
    "vcfa.tenant.blueprint_request.create": ("caution", True),
    "vcfa.tenant.blueprint_request.get": ("safe", False),
    "vcfa.tenant.deployment.delete": ("dangerous", True),
}


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# ---------------------------------------------------------------------------
# Registration and routing
# ---------------------------------------------------------------------------


def test_tiers() -> None:
    ops = (*VCFA_NETWORK_OPS, *VCFA_DEPLOY_OPS)
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in ops} == _EXPECTED_TIERS


async def test_ops_register_as_typed_rows() -> None:
    await VcfAutomationConnector.register_typed_operations()
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VCFA_PRODUCT,
                    EndpointDescriptor.version == VCFA_VERSION,
                    EndpointDescriptor.impl_id == VCFA_IMPL_ID,
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    for op_id, (safety, approval) in _EXPECTED_TIERS.items():
        assert by_op[op_id].source_kind == "typed"
        assert (by_op[op_id].safety_level, by_op[op_id].requires_approval) == (safety, approval)


@pytest.mark.parametrize(
    ("path", "plane"),
    [
        ("/iaas/api/zones", "tenant"),
        ("/blueprint/api/blueprint-requests", "tenant"),
        ("/deployment/api/deployments/{deploymentId}", "tenant"),
        ("/cloudapi/1.0.0/orgs", "provider"),
        ("/blueprint", "provider"),
    ],
)
def test_blueprint_and_deployment_apis_use_the_tenant_login(path: str, plane: str) -> None:
    assert plane_for_path(path) == plane


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_fabric_network_list_sends_api_version_and_filter(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_NETWORKS).respond(
            200, json={"content": [{"id": "net-1", "name": "pg-team-a"}], "totalElements": 1}
        )
        result = await run("vcfa.tenant.fabric_network.list", {"$filter": "name eq 'pg-team-a'"})
    assert result["status"] == "ok", result
    assert result["result"]["content"][0]["id"] == "net-1"
    request = route.calls.last.request
    assert request.url.params["apiVersion"] == _API_VERSION
    assert request.url.params["$filter"] == "name eq 'pg-team-a'"
    assert request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"


@pytest.mark.parametrize(
    ("op_id", "path"),
    [
        ("vcfa.tenant.fabric_compute.list", "/iaas/api/fabric-computes"),
        ("vcfa.tenant.region.list", "/iaas/api/regions"),
        ("vcfa.tenant.zone.list", "/iaas/api/zones"),
    ],
)
async def test_other_lists_send_api_version(
    vcfa: VcfAutomationConnector, op_id: str, path: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(path).respond(200, json={"content": [], "totalElements": 0})
        result = await run(op_id, {"$top": 5})
    assert result["status"] == "ok", result
    assert route.calls.last.request.url.params["apiVersion"] == _API_VERSION
    assert route.calls.last.request.url.params["$top"] == "5"


# ---------------------------------------------------------------------------
# vSphere network change
# ---------------------------------------------------------------------------

_CURRENT_NETWORK = {
    "id": "net-1",
    "name": "pg-team-a",
    "cidr": "<old-cidr>",
    "domain": "<dns-domain>",
    "tags": [{"key": "env", "value": "lab"}],
}


async def test_fabric_network_update_sends_only_changes_and_keeps_tags(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_NETWORK).respond(200, json=_CURRENT_NETWORK)
        patch = mock.patch(_NETWORK).respond(200, json={**_CURRENT_NETWORK, "cidr": "<cidr>"})
        result = await run(
            "vcfa.tenant.fabric_network.update",
            {
                "id": "net-1",
                "cidr": "<cidr>",
                "default_gateway": "<gateway>",
                "dns_server_addresses": ["<dns-1>"],
                "domain": "<dns-domain>",
                "add_tags": [{"key": "project", "value": "team-a"}],
            },
        )
    out = result["result"]
    assert out["status"] == "updated", out
    assert out["changed"] == ["cidr", "defaultGateway", "dnsServerAddresses", "tags"]
    assert patch.calls.last.request.url.params["apiVersion"] == _API_VERSION
    assert _body(patch) == {
        "cidr": "<cidr>",
        "defaultGateway": "<gateway>",
        "dnsServerAddresses": ["<dns-1>"],
        "tags": [{"key": "env", "value": "lab"}, {"key": "project", "value": "team-a"}],
    }


async def test_fabric_network_update_unchanged_writes_nothing(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_NETWORK).respond(200, json=_CURRENT_NETWORK)
        patch = mock.patch(_NETWORK).respond(200, json={})
        result = await run(
            "vcfa.tenant.fabric_network.update",
            {"id": "net-1", "domain": "<dns-domain>", "add_tags": [{"key": "env", "value": "lab"}]},
        )
    assert result["result"]["status"] == "unchanged"
    assert not patch.called


async def test_fabric_network_update_without_a_field_is_invalid_request(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(_NETWORK).respond(200, json=_CURRENT_NETWORK)
        result = await run("vcfa.tenant.fabric_network.update", {"id": "net-1"})
    assert result["result"]["status"] == "invalid_request"
    assert not read.called


# ---------------------------------------------------------------------------
# Network profile
# ---------------------------------------------------------------------------


async def test_network_profile_create_created(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(_PROFILES).respond(200, json={"content": []})
        post = mock.post(_PROFILES).respond(201, json={"id": "np-1", "name": "np-team-a"})
        result = await run(
            "vcfa.tenant.network_profile.create",
            {
                "name": "np-team-a",
                "region_id": "region-1",
                "fabric_network_ids": ["net-1"],
                "tags": [{"key": "project", "value": "team-a"}],
            },
        )
    out = result["result"]
    assert out["status"] == "created", out
    assert out["network_profile"]["id"] == "np-1"
    assert listing.calls.last.request.url.params["$filter"] == "name eq 'np-team-a'"
    assert post.calls.last.request.url.params["apiVersion"] == _API_VERSION
    assert _body(post) == {
        "name": "np-team-a",
        "regionId": "region-1",
        "fabricNetworkIds": ["net-1"],
        "tags": [{"key": "project", "value": "team-a"}],
    }


async def test_network_profile_create_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROFILES).respond(200, json={"content": [{"id": "np-1", "name": "NP-Team-A"}]})
        post = mock.post(_PROFILES).respond(201, json={})
        result = await run(
            "vcfa.tenant.network_profile.create", {"name": "np-team-a", "region_id": "region-1"}
        )
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["network_profile"]["id"] == "np-1"
    assert not post.called


# ---------------------------------------------------------------------------
# Project quota and network rule
# ---------------------------------------------------------------------------


async def test_project_create_sends_quota_and_network_rule(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROJECTS).respond(200, json={"content": []})
        post = mock.post(_PROJECTS).respond(201, json={"id": "p-1", "name": "team-a"})
        result = await run(
            "vcfa.tenant.project.create",
            {
                "name": "team-a",
                "zones": [
                    {"zone_id": "zone-1", "priority": 1, "max_instances": 10, "cpu_limit": 20},
                    {"zone_id": "zone-2", "memory_limit_mb": 4096, "storage_limit_gb": 100},
                ],
                "network_constraints": [
                    {"expression": "project:team-a"},
                    {"expression": "!env:prod", "mandatory": False},
                ],
            },
        )
    assert result["result"]["status"] == "created", result
    assert _body(post) == {
        "name": "team-a",
        "zoneAssignmentConfigurations": [
            {"zoneId": "zone-1", "priority": 1, "maxNumberInstances": 10, "cpuLimit": 20},
            {"zoneId": "zone-2", "memoryLimitMB": 4096, "storageLimitGB": 100},
        ],
        "constraints": {
            "network": [
                {"expression": "project:team-a", "mandatory": True},
                {"expression": "!env:prod", "mandatory": False},
            ]
        },
    }


# ---------------------------------------------------------------------------
# Deploy request, wait, delete
# ---------------------------------------------------------------------------


async def test_blueprint_request_plan_uses_the_tenant_login(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup = mock.get(_DEPLOYMENTS).respond(200, json={"content": []})
        post = mock.post(_BP_REQUESTS).respond(
            202, json={"id": "req-1", "status": "CREATED", "plan": True, "projectId": "p-1"}
        )
        result = await run(
            "vcfa.tenant.blueprint_request.create",
            {
                "project_id": "p-1",
                "blueprint_id": "bp-1",
                "deployment_name": "team-a-first-vm",
                "inputs": {"size": "small"},
                "plan": True,
            },
        )
    out = result["result"]
    assert out["status"] == "submitted", out
    assert out["request"]["id"] == "req-1"
    assert "placement" in out["guidance"]
    assert not lookup.called
    assert post.calls.last.request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"
    assert _body(post) == {
        "projectId": "p-1",
        "blueprintId": "bp-1",
        "deploymentName": "team-a-first-vm",
        "inputs": {"size": "small"},
        "plan": True,
    }


async def test_blueprint_request_existing_deployment_name_is_unchanged(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup = mock.get(_DEPLOYMENTS).respond(
            200, json={"content": [{"id": "d-1", "name": "team-a-first-vm", "projectId": "p-1"}]}
        )
        post = mock.post(_BP_REQUESTS).respond(202, json={})
        result = await run(
            "vcfa.tenant.blueprint_request.create",
            {
                "project_id": "p-1",
                "content": "formatVersion: 1",
                "deployment_name": "team-a-first-vm",
            },
        )
    out = result["result"]
    assert out["status"] == "unchanged", out
    assert out["deployment"]["id"] == "d-1"
    assert lookup.calls.last.request.url.params["apiVersion"] == _API_VERSION
    assert (
        lookup.calls.last.request.url.params["$filter"]
        == "name eq 'team-a-first-vm' and projectId eq 'p-1'"
    )
    assert not post.called


async def test_blueprint_request_same_name_in_another_project_is_submitted(
    vcfa: VcfAutomationConnector,
) -> None:
    """A deployment of that name in another project does not block this project."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_DEPLOYMENTS).respond(
            200, json={"content": [{"id": "d-9", "name": "team-a-first-vm", "projectId": "p-2"}]}
        )
        post = mock.post(_BP_REQUESTS).respond(
            202, json={"id": "req-2", "status": "CREATED", "plan": False, "projectId": "p-1"}
        )
        result = await run(
            "vcfa.tenant.blueprint_request.create",
            {
                "project_id": "p-1",
                "content": "formatVersion: 1",
                "deployment_name": "team-a-first-vm",
            },
        )
    out = result["result"]
    assert out["status"] == "submitted", out
    assert out["request"]["id"] == "req-2"
    assert _body(post)["projectId"] == "p-1"


@pytest.mark.parametrize(
    "template",
    [{}, {"blueprint_id": "bp-1", "content": "formatVersion: 1"}],
    ids=["neither", "both"],
)
async def test_blueprint_request_needs_exactly_one_template(
    vcfa: VcfAutomationConnector, template: dict[str, str]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_BP_REQUESTS).respond(202, json={})
        result = await run(
            "vcfa.tenant.blueprint_request.create", {"project_id": "p-1", **template}
        )
    assert result["result"]["status"] == "invalid_request"
    assert not post.called


async def test_blueprint_request_get_quotes_the_id(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(f"{_BP_REQUESTS}/req%2F1").respond(
            200, json={"id": "req/1", "status": "FINISHED", "deploymentId": "d-1"}
        )
        result = await run("vcfa.tenant.blueprint_request.get", {"id": "req/1"})
    assert result["result"]["status"] == "FINISHED", result
    assert route.called


async def test_deployment_delete_submitted(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.delete("/deployment/api/deployments/d-1").respond(
            200, json={"id": "r-1", "name": "Delete", "status": "PENDING", "deploymentId": "d-1"}
        )
        result = await run("vcfa.tenant.deployment.delete", {"id": "d-1"})
    out = result["result"]
    assert out["status"] == "submitted", out
    assert out["request"] == {
        "id": "r-1",
        "name": "Delete",
        "status": "PENDING",
        "deploymentId": "d-1",
    }
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"


async def test_deployment_delete_unknown_id_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.delete("/deployment/api/deployments/d-1").respond(404, json={"message": "Not found"})
        result = await run("vcfa.tenant.deployment.delete", {"id": "d-1"})
    assert result["result"]["status"] == "unchanged", result


async def test_deployment_delete_conflict_is_connector_error(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.delete("/deployment/api/deployments/d-1").respond(409, json={"message": "In progress"})
        result = await run("vcfa.tenant.deployment.delete", {"id": "d-1"})
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 409


async def test_deployment_delete_preview_names_the_deployment(
    vcfa: VcfAutomationConnector,
) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.deployment.delete"]
    ctx: Any = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=await resolved_target(),
        params={"id": "d-1"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(f"{_DEPLOYMENTS}/d-1").respond(
            200, json={"id": "d-1", "name": "team-a-first-vm", "projectId": "p-1"}
        )
        preview = await builder(ctx)
    assert preview == {
        "action": "delete_deployment",
        "id": "d-1",
        "name": "team-a-first-vm",
        "project_id": "p-1",
    }
    assert read.calls.last.request.url.params["apiVersion"] == _API_VERSION
