# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure Migrate reads (group ``migrate``).

Azure Resource Manager and Microsoft's login service are mocked with respx.
The answers are shaped like the examples in Azure's pinned API description
files (Microsoft.Migrate 2024-01-15, Microsoft.OffAzure 2023-06-06), with
made-up names and ids. The tests run the real paging helper, the token
cache, the real dispatcher and the real result reduction with the real
result store on a fake Valkey.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_VERSION,
    AzureConnector,
    _http,
    _paging,
    migrate_reads,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure._errors import AzureNotFoundError
from meho_backplane.connectors.azure.migrate import OPS, WHEN_TO_USE, MigrateOps
from meho_backplane.connectors.azure.migrate_reads import MAX_GROUPS
from meho_backplane.connectors.azure.migrate_rows import (
    machine_origin,
    migrate_name_param,
    resource_group_param,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import run_result_query
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_HOST,
    CLIENT_A,
    OBJECT_A,
    SUBSCRIPTION_A,
    SUBSCRIPTION_B,
    TENANT_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

SUB = SUBSCRIPTION_A
RG = "rg-migrate-example"
PROJECT = "example-assessment-project"
MIGRATE_PROJECT = "example-migrate-project"
GROUP = "example-group"
ASSESSMENT = "example-assessment"
WHEN = "2026-09-26T09:14:05.7322406Z"

RG_PATH = f"/subscriptions/{SUB}/resourceGroups/{RG}"
PROJECT_PATH = f"{RG_PATH}/providers/Microsoft.Migrate/assessmentProjects/{PROJECT}"
OFFAZURE_PATH = f"{RG_PATH}/providers/Microsoft.OffAzure"
GROUPS_PATH = f"{PROJECT_PATH}/groups"
ASSESSMENT_PATH = f"{GROUPS_PATH}/{GROUP}/assessments/{ASSESSMENT}"

#: Values that must never leave MEHO: the spec examples carry them in sites.
RAW_CERT = "raw-cert-data-must-not-leave"
KEY_VAULT_URI = "https://example-vault.vault.azure.invalid/"

MIGRATE_PARAMS = {"resource_group": RG, "project": PROJECT}
ASSESSMENT_PARAMS = {**MIGRATE_PARAMS, "group": GROUP, "assessment": ASSESSMENT}


# ----------------------------------------------------------------------
# Answers shaped like the spec examples, with made-up values
# ----------------------------------------------------------------------


def _system_data() -> dict[str, Any]:
    return {
        "createdBy": "example-user",
        "createdByType": "User",
        "createdAt": WHEN,
        "lastModifiedBy": "example-user",
        "lastModifiedByType": "User",
        "lastModifiedAt": WHEN,
    }


def _project(name: str = PROJECT, rg: str = RG) -> dict[str, Any]:
    """AssessmentProjectsOperations_ListBySubscription_MaximumSet_Gen, one value."""
    solution = (
        f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Migrate/"
        f"MigrateProjects/{MIGRATE_PROJECT}/Solutions/Servers-Assessment-ServerAssessment"
    )
    return {
        "properties": {
            "createdTimestamp": WHEN,
            "updatedTimestamp": WHEN,
            "serviceEndpoint": "https://asm.example.invalid/",
            "assessmentSolutionId": solution,
            "projectStatus": "Active",
            "customerWorkspaceId": None,
            "customerWorkspaceLocation": None,
            "publicNetworkAccess": "Disabled",
            "privateEndpointConnections": [],
            "customerStorageAccountArmId": None,
            "provisioningState": "Succeeded",
        },
        "tags": {"Migrate Project": MIGRATE_PROJECT},
        "location": "westeurope",
        "id": f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Migrate/"
        f"assessmentprojects/{name}",
        "name": name,
        "type": "Microsoft.Migrate/assessmentprojects",
        "systemData": _system_data(),
    }


def _site(site_type: str, name: str) -> dict[str, Any]:
    """SitesController / HypervSites / ServerSitesController examples, one value."""
    return {
        "properties": {
            "masterSiteId": f"{OFFAZURE_PATH}/MasterSites/example-master",
            "servicePrincipalIdentityDetails": {
                "tenantId": TENANT_A,
                "applicationId": CLIENT_A,
                "objectId": OBJECT_A,
                "audience": "example-audience",
                "aadAuthority": "https://login.example.invalid/",
                "rawCertData": RAW_CERT,
            },
            "agentDetails": {
                "id": "example-agent",
                "version": "1.2.3",
                "lastHeartBeatUtc": WHEN,
                "keyVaultUri": KEY_VAULT_URI,
                "keyVaultId": f"{RG_PATH}/providers/Microsoft.KeyVault/vaults/example-vault",
            },
            "applianceName": "example-appliance",
            "discoverySolutionId": "example-discovery-solution",
            "serviceEndpoint": "https://agent.example.invalid/",
            "provisioningState": "Created",
        },
        "eTag": "example-etag",
        "location": "westeurope",
        "id": f"{OFFAZURE_PATH}/{site_type}/{name}",
        "name": name,
        "type": f"Microsoft.OffAzure/{site_type}",
        "systemData": _system_data(),
    }


def _import_site(name: str) -> dict[str, Any]:
    """ImportSitesController_ListByResourceGroup_MaximumSet_Gen, one value."""
    return {
        "properties": {
            "discoverySolutionId": "example-discovery-solution",
            "masterSiteId": f"{OFFAZURE_PATH}/MasterSites/example-master",
            "serviceEndpoint": "https://import.example.invalid/",
            "provisioningState": "Created",
        },
        "location": "westeurope",
        "id": f"{OFFAZURE_PATH}/importSites/{name}",
        "name": name,
        "type": "Microsoft.OffAzure/importSites",
        "systemData": _system_data(),
    }


def _master_site(name: str) -> dict[str, Any]:
    """MasterSitesController_ListByResourceGroup_MaximumSet_Gen, one value."""
    return {
        "properties": {
            "publicNetworkAccess": "NotSpecified",
            "allowMultipleSites": True,
            "sites": ["example-vmware-site"],
            "customerStorageAccountArmId": None,
            "privateEndpointConnections": [],
            "nestedSites": [],
            "provisioningState": "Created",
        },
        "location": "westeurope",
        "id": f"{OFFAZURE_PATH}/MasterSites/{name}",
        "name": name,
        "type": "Microsoft.OffAzure/MasterSites",
        "systemData": _system_data(),
    }


def _discovered(site_type: str, site: str, n: int) -> str:
    """The ID of a discovered machine, lower case like the spec example."""
    return (
        f"/subscriptions/{SUB}/resourcegroups/{RG}/providers/microsoft.offazure/"
        f"{site_type}/{site}/machines/example-vm-{n}"
    )


def _guid(n: int) -> str:
    return f"55555555-0000-0000-0000-{n:012d}"


def _machine(n: int, discovery: str | None) -> dict[str, Any]:
    """MachinesOperations_ListByAssessmentProject_MaximumSet_Gen, one value."""
    return {
        "id": f"{PROJECT_PATH}/machines/{_guid(n)}",
        "name": _guid(n),
        "systemData": _system_data(),
        "type": "Microsoft.Migrate/assessmentprojects/machines",
        "properties": {
            "workloadSummary": {"oracleInstances": 0, "springApps": 0},
            "errors": [],
            "hostProcessor": None,
            "discoveryMachineArmId": discovery,
            "datacenterManagementServerName": "vcenter.example.invalid",
            "bootType": "BIOS",
            "displayName": f"server-{n:02d}",
            "megabytesOfMemory": 30720.0,
            "numberOfCores": 8,
            "operatingSystemType": "windowsGuest",
            "operatingSystemName": "Microsoft Windows Server 2016 (64-bit)",
            "operatingSystemVersion": None,
            "description": "",
            "createdTimestamp": WHEN,
            "disks": {"disk-0": {"gigabytesAllocated": 150.0, "displayName": "scsi0:0"}},
            "groups": [f"{GROUPS_PATH}/{GROUP}", f"{GROUPS_PATH}/other-group"],
            "networkAdapters": {},
            "sqlInstances": [],
            "webApplications": [],
            "updatedTimestamp": WHEN,
        },
    }


def _group(name: str, group_type: str = "Default") -> dict[str, Any]:
    """GroupsOperations_ListByAssessmentProject_MaximumSet_Gen, one value."""
    return {
        "properties": {
            "groupStatus": "Completed",
            "machineCount": 20,
            "assessments": [f"{GROUPS_PATH}/{name}/assessments/{ASSESSMENT}"],
            "supportedAssessmentTypes": ["MachineAssessment", "AvsAssessment"],
            "areAssessmentsRunning": False,
            "createdTimestamp": WHEN,
            "updatedTimestamp": WHEN,
            "groupType": group_type,
            "provisioningState": "Succeeded",
        },
        "id": f"{GROUPS_PATH}/{name}",
        "name": name,
        "type": "Microsoft.Migrate/assessmentprojects/groups",
        "systemData": _system_data(),
    }


def _assessment(name: str = ASSESSMENT, group: str = GROUP) -> dict[str, Any]:
    """AssessmentsOperations_ListByGroup_MaximumSet_Gen, one value."""
    return {
        "properties": {
            "assessmentErrorSummary": {},
            "costComponents": [{"name": "MonthlySecurityCost", "value": 44.64}],
            "azurePricingTier": "Standard",
            "azureStorageRedundancy": "LocallyRedundant",
            "reservedInstance": "RI3Year",
            "azureHybridUseBenefit": "Yes",
            "monthlyComputeCost": 10,
            "monthlyBandwidthCost": 21,
            "monthlyStorageCost": 20,
            "suitabilitySummary": {"suitable": 3},
            "numberOfMachines": 3,
            "groupType": "Default",
            "assessmentType": "MachineAssessment",
            "azureLocation": "WestEurope",
            "currency": "EUR",
            "scalingFactor": 1.3,
            "percentile": "Percentile95",
            "timeRange": "Day",
            "stage": "InProgress",
            "sizingCriterion": "PerformanceBased",
            "confidenceRatingInPercentage": 28,
            "pricesTimestamp": "2026-08-16T00:00:00",
            "createdTimestamp": WHEN,
            "updatedTimestamp": WHEN,
            "status": "Completed",
            "provisioningState": "Succeeded",
        },
        "id": f"{GROUPS_PATH}/{group}/assessments/{name}",
        "name": name,
        "type": "Microsoft.Migrate/assessmentprojects/groups/assessments",
        "systemData": _system_data(),
    }


def _assessed(n: int, discovery: str, suitability: str = "Suitable") -> dict[str, Any]:
    """AssessedMachinesOperations_ListByAssessment_MaximumSet_Gen, one value."""
    return {
        "properties": {
            "errors": [],
            "disks": {},
            "costComponents": [{"name": "MonthlySecurityCost", "value": 14.88}],
            "monthlyBandwidthCost": 4,
            "monthlyStorageCost": 10,
            "networkAdapters": {},
            "recommendedSize": "Standard_F4s_v2",
            "numberOfCoresForRecommendedSize": 8,
            "megabytesOfMemoryForRecommendedSize": 27,
            "monthlyComputeCostForRecommendedSize": 61.5,
            "suitabilityExplanation": "NotApplicable",
            "suitabilityDetail": "PercentageOfCoresUtilizedMissing",
            "type": "AssessedMachine",
            "bootType": "BIOS",
            "operatingSystemName": "Microsoft Windows Server 2016 or later (64-bit)",
            "displayName": f"server-{n:02d}",
            "datacenterMachineArmId": discovery,
            "megabytesOfMemory": 21,
            "numberOfCores": 7,
            "confidenceRatingInPercentage": 14,
            "percentageCoresUtilization": 24,
            "percentageMemoryUtilization": 5,
            "suitability": suitability,
        },
        "id": f"{ASSESSMENT_PATH}/assessedMachines/{_guid(n)}",
        "name": _guid(n),
        "type": "Microsoft.Migrate/assessmentprojects/groups/assessments/assessedMachines",
        "systemData": _system_data(),
    }


def _page(values: list[dict[str, Any]], next_link: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"value": values}
    if next_link is not None:
        body["nextLink"] = next_link
    return body


def _ok(values: list[dict[str, Any]], next_link: str | None = None) -> httpx.Response:
    return httpx.Response(200, json=_page(values, next_link))


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "Example message."}})


def _connector() -> AzureConnector:
    return AzureConnector(credentials_loader=CountingLoader())


def _route(mock: respx.MockRouter, path: str) -> respx.Route:
    return mock.get(host=ARM_HOST, path=path)


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


# ----------------------------------------------------------------------
# Names and sources
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("site_type", "source", "kind"),
    [
        ("vmwaresites", "appliance", "vmware"),
        ("VMwareSites", "appliance", "vmware"),
        ("hypervsites", "appliance", "hyperv"),
        ("serversites", "appliance", "server"),
        ("importsites", "import", "import"),
    ],
)
def test_the_source_comes_from_the_kind_of_site(site_type: str, source: str, kind: str) -> None:
    origin = machine_origin(_discovered(site_type, "site-a", 1))

    assert origin == {"source": source, "site_kind": kind, "site": "site-a"}


@pytest.mark.parametrize(
    "discovery",
    [
        None,
        "",
        "not an id",
        f"/subscriptions/{SUB}/resourcegroups/{RG}/providers/microsoft.offazure/mastersites/m",
        f"/subscriptions/{SUB}/resourcegroups/{RG}/providers/microsoft.compute/vmwaresites/s",
        # A resource group named like a site type must not count as a site.
        f"/subscriptions/{SUB}/resourcegroups/vmwaresites/providers/microsoft.compute/x/y",
    ],
)
def test_an_id_without_a_known_site_gives_unknown(discovery: str | None) -> None:
    assert machine_origin(discovery) == {"source": "unknown", "site_kind": None, "site": None}


def test_a_resource_group_named_like_a_site_type_does_not_confuse_the_source() -> None:
    discovery = (
        f"/subscriptions/{SUB}/resourcegroups/importsites/providers/microsoft.offazure/"
        "vmwaresites/site-b/machines/m1"
    )

    assert machine_origin(discovery) == {
        "source": "appliance",
        "site_kind": "vmware",
        "site": "site-b",
    }


@pytest.mark.parametrize(
    "bad", ["..", ".", "a/b", "a?b", "a#b", "a%2Fb", "a\\b", "name\n", "a:b", "", "x" * 261, 7]
)
def test_a_name_that_could_change_the_path_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="project must be"):
        migrate_name_param({"project": bad}, "project")


@pytest.mark.parametrize("bad", ["rg.", "a/b", "rg\n", "rg name", "", "r" * 91, "..", None])
def test_a_bad_resource_group_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="resource_group must be"):
        resource_group_param({"resource_group": bad})


@pytest.mark.parametrize("good", ["rg-migrate", "RG_1", "rg.(prod)", "Ünïcode-rg"])
def test_a_usual_resource_group_passes(good: str) -> None:
    assert resource_group_param({"resource_group": good}) == good


@pytest.mark.parametrize("good", ["project1", "my project", "name.with.dots", "...x"])
def test_a_usual_migrate_name_passes(good: str) -> None:
    assert migrate_name_param({"group": good}, "group") == good


@pytest.mark.parametrize("bad", ["../other", "a/b", "a?b", "a%2Fb", ".."])
def test_the_parameter_schema_refuses_the_same_names(bad: str) -> None:
    schema = next(op for op in OPS if op.op_id == "azure.migrate.machine.list").parameter_schema

    assert validate_params(schema, {"resource_group": RG, "project": bad})
    assert not validate_params(schema, {"resource_group": RG, "project": PROJECT})


# ----------------------------------------------------------------------
# Each read, through the connector, the paging helper and the token cache
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_list_follows_every_page_with_one_login() -> None:
    connector = _connector()
    next_link = (
        f"{ARM_BASE}/subscriptions/{SUB}/providers/Microsoft.Migrate/assessmentProjects?page=2"
    )

    with respx.mock() as mock:
        login = _login(mock)
        arm = _route(
            mock, f"/subscriptions/{SUB}/providers/Microsoft.Migrate/assessmentProjects"
        ).mock(side_effect=[_ok([_project()], next_link), _ok([_project("p2", "rg-two")])])
        result = await connector.migrate_project_list(operator(), AzureTargetStub(), {})

    assert login.call_count == 1
    first, second = (call.request.url for call in arm.calls)
    assert first.params["api-version"] == "2024-01-15"
    assert str(second) == next_link
    assert result["truncated"] is False
    assert result["omissions"] == {"count": 0, "items": []}
    first_row, second_row = result["value"]
    assert first_row == {
        "name": PROJECT,
        "id": f"{RG_PATH}/providers/Microsoft.Migrate/assessmentprojects/{PROJECT}",
        "resource_group": RG,
        "location": "westeurope",
        "status": "Active",
        "migrate_project": MIGRATE_PROJECT,
        "migrate_project_id": f"{RG_PATH}/providers/Microsoft.Migrate/MigrateProjects/"
        f"{MIGRATE_PROJECT}",
        "created": WHEN,
        "updated": WHEN,
    }
    assert second_row["resource_group"] == "rg-two"


@pytest.mark.asyncio
async def test_site_list_reads_all_five_kinds_and_names_each() -> None:
    with respx.mock() as mock:
        _login(mock)
        routes = {
            "vmwareSites": _route(mock, f"{OFFAZURE_PATH}/vmwareSites").mock(
                return_value=_ok([_site("VMwareSites", "example-vmware-site")])
            ),
            "hypervSites": _route(mock, f"{OFFAZURE_PATH}/hypervSites").mock(
                return_value=_ok([_site("HyperVSites", "example-hyperv-site")])
            ),
            "serverSites": _route(mock, f"{OFFAZURE_PATH}/serverSites").mock(return_value=_ok([])),
            "importSites": _route(mock, f"{OFFAZURE_PATH}/importSites").mock(
                return_value=_ok([_import_site("example-import-site")])
            ),
            "masterSites": _route(mock, f"{OFFAZURE_PATH}/masterSites").mock(
                return_value=_ok([_master_site("example-master")])
            ),
        }
        result = await _connector().migrate_site_list(
            operator(), AzureTargetStub(), {"resource_group": RG}
        )

    for route in routes.values():
        assert route.calls.last.request.url.params["api-version"] == "2023-06-06"
    by_kind = {row["kind"]: row for row in result["value"]}
    assert set(by_kind) == {"vmware", "hyperv", "import", "master"}
    assert by_kind["vmware"]["source"] == "appliance"
    assert by_kind["vmware"]["appliance_name"] == "example-appliance"
    assert by_kind["vmware"]["agent_last_heartbeat"] == WHEN
    assert by_kind["vmware"]["master_site"] == "example-master"
    assert by_kind["import"]["source"] == "import"
    assert by_kind["import"]["appliance_name"] is None
    assert by_kind["master"]["source"] is None
    assert result["kind_counts"] == {
        "vmware": 1,
        "hyperv": 1,
        "server": 0,
        "import": 1,
        "master": 1,
    }
    assert result["truncated"] is False
    text = json.dumps(result)
    assert RAW_CERT not in text
    assert KEY_VAULT_URI not in text
    assert "example.invalid" not in text


@pytest.mark.asyncio
async def test_a_site_kind_that_cannot_be_read_is_named_and_its_count_is_unknown() -> None:
    with respx.mock() as mock:
        _login(mock)
        _route(mock, f"{OFFAZURE_PATH}/vmwareSites").mock(
            return_value=_error(403, "AuthorizationFailed")
        )
        for kind in ("hypervSites", "serverSites", "masterSites"):
            _route(mock, f"{OFFAZURE_PATH}/{kind}").mock(return_value=_ok([]))
        _route(mock, f"{OFFAZURE_PATH}/importSites").mock(
            return_value=_ok([_import_site("example-import-site")])
        )
        result = await _connector().migrate_site_list(
            operator(), AzureTargetStub(), {"resource_group": RG}
        )

    assert [row["name"] for row in result["value"]] == ["example-import-site"]
    assert result["kind_counts"]["vmware"] is None
    assert result["kind_counts"]["import"] == 1
    assert result["truncated"] is True
    (item,) = result["omissions"]["items"]
    assert item["what"] == "vmware sites"
    assert item["reason"] == "no_permission"
    assert "AuthorizationFailed" in item["detail"]
    assert "Example message." not in item["detail"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_error(404, "ResourceNotFound"), "not_found"),
        (_error(409, "MissingSubscriptionRegistration"), "provider_not_registered"),
        (
            httpx.Response(
                429,
                headers={"Retry-After": "1"},
                json={"error": {"code": "TooManyRequests", "message": "Example message."}},
            ),
            "throttled",
        ),
        (_error(500, "InternalServerError"), "read_failed"),
        (httpx.ConnectError("Example message."), "read_failed"),
    ],
    ids=["404", "not-registered", "429", "500", "lost-connection"],
)
@pytest.mark.asyncio
async def test_each_error_of_one_site_kind_gets_its_reason(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception, reason: str
) -> None:
    """Every omission reason the docs name, with MEHO's own text and never Azure's."""
    monkeypatch.setattr(_http, "_sleep", AsyncMock())
    with respx.mock() as mock:
        _login(mock)
        failing = _route(mock, f"{OFFAZURE_PATH}/vmwareSites")
        if isinstance(answer, Exception):
            failing.mock(side_effect=answer)
        else:
            failing.mock(return_value=answer)
        for kind in ("hypervSites", "serverSites", "importSites", "masterSites"):
            _route(mock, f"{OFFAZURE_PATH}/{kind}").mock(return_value=_ok([]))
        result = await _connector().migrate_site_list(
            operator(), AzureTargetStub(), {"resource_group": RG}
        )

    (item,) = result["omissions"]["items"]
    assert item["what"] == "vmware sites"
    assert item["reason"] == reason
    assert "Example message." not in item["detail"]
    assert result["kind_counts"]["vmware"] is None
    assert result["kind_counts"]["hyperv"] == 0
    if isinstance(answer, Exception):
        assert item["detail"] == "Azure did not answer (ConnectError). Try again later."


@pytest.mark.asyncio
async def test_a_next_link_outside_the_subscription_is_refused_and_named() -> None:
    elsewhere = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_B}/resourceGroups/{RG}/providers/x?p=2"
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        for kind in ("vmwareSites", "hypervSites", "serverSites", "masterSites"):
            _route(mock, f"{OFFAZURE_PATH}/{kind}").mock(return_value=_ok([]))
        _route(mock, f"{OFFAZURE_PATH}/importSites").mock(
            return_value=_ok([_import_site("first-page-site")], elsewhere)
        )
        other = mock.get(url__startswith=f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_B}")
        result = await _connector().migrate_site_list(
            operator(), AzureTargetStub(), {"resource_group": RG}
        )

    assert not other.called
    assert result["kind_counts"]["import"] is None
    (item,) = result["omissions"]["items"]
    assert item["what"] == "import sites"
    assert item["reason"] == "read_failed"
    assert "outside the target's subscription" in item["detail"]


@pytest.mark.asyncio
async def test_machine_list_keeps_appliance_and_imported_machines_apart() -> None:
    machines = [
        _machine(4, _discovered("importsites", "example-import-site", 4)),
        _machine(2, _discovered("hypervsites", "example-hyperv-site", 2)),
        _machine(5, None),
        _machine(1, _discovered("vmwaresites", "example-vmware-site", 1)),
        _machine(3, _discovered("serversites", "example-server-site", 3)),
    ]
    with respx.mock() as mock:
        _login(mock)
        arm = _route(mock, f"{PROJECT_PATH}/machines").mock(return_value=_ok(machines))
        result = await _connector().migrate_machine_list(
            operator(), AzureTargetStub(), MIGRATE_PARAMS
        )

    assert arm.calls.last.request.url.params["api-version"] == "2024-01-15"
    rows = result["value"]
    assert [(row["display_name"], row["source"]) for row in rows] == [
        ("server-01", "appliance"),
        ("server-02", "appliance"),
        ("server-03", "appliance"),
        ("server-04", "import"),
        ("server-05", "unknown"),
    ]
    assert result["source_counts"] == {"appliance": 3, "import": 1, "unknown": 1}
    imported = rows[3]
    assert imported["site_kind"] == "import"
    assert imported["site"] == "example-import-site"
    assert imported["id"] == f"{PROJECT_PATH}/machines/{_guid(4)}"
    assert imported["name"] == _guid(4)
    assert imported["groups"] == [GROUP, "other-group"]
    assert imported["cores"] == 8 and imported["memory_mb"] == 30720.0
    assert "vcenter.example.invalid" not in json.dumps(result)


@pytest.mark.asyncio
async def test_group_list_shapes_each_group() -> None:
    with respx.mock() as mock:
        _login(mock)
        _route(mock, GROUPS_PATH).mock(
            return_value=_ok([_group(GROUP), _group("imported", "Import")])
        )
        result = await _connector().migrate_group_list(
            operator(), AzureTargetStub(), MIGRATE_PARAMS
        )

    first, second = result["value"]
    assert first == {
        "name": GROUP,
        "id": f"{GROUPS_PATH}/{GROUP}",
        "status": "Completed",
        "type": "Default",
        "machine_count": 20,
        "assessments": [ASSESSMENT],
        "assessment_types": ["MachineAssessment", "AvsAssessment"],
        "assessments_running": False,
        "created": WHEN,
        "updated": WHEN,
    }
    assert second["type"] == "Import"


@pytest.mark.asyncio
async def test_assessment_list_with_a_group_reads_only_that_group() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        groups = _route(mock, GROUPS_PATH)
        arm = _route(mock, f"{GROUPS_PATH}/{GROUP}/assessments").mock(
            return_value=_ok([_assessment()])
        )
        result = await _connector().migrate_assessment_list(
            operator(), AzureTargetStub(), {**MIGRATE_PARAMS, "group": GROUP}
        )

    assert not groups.called
    assert arm.call_count == 1
    (row,) = result["value"]
    assert row["name"] == ASSESSMENT
    assert row["group"] == GROUP
    assert row["status"] == "Completed"
    assert row["sizing_criterion"] == "PerformanceBased"
    assert row["region"] == "WestEurope"
    assert row["currency"] == "EUR"
    assert row["readiness_summary"] == {"suitable": 3}
    assert row["monthly_compute_cost"] == 10
    assert row["comfort_factor"] == 1.3
    assert row["created"] == WHEN and row["updated"] == WHEN


@pytest.mark.asyncio
async def test_assessment_list_without_a_group_reads_every_group_and_names_the_gaps() -> None:
    groups = [_group("g1"), _group("g2"), _group("../other")]
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _route(mock, GROUPS_PATH).mock(return_value=_ok(groups))
        _route(mock, f"{GROUPS_PATH}/g1/assessments").mock(
            return_value=_ok([_assessment("a1", "g1")])
        )
        _route(mock, f"{GROUPS_PATH}/g2/assessments").mock(
            return_value=_error(403, "AuthorizationFailed")
        )
        result = await _connector().migrate_assessment_list(
            operator(), AzureTargetStub(), MIGRATE_PARAMS
        )
        paths = [call.request.url.path for call in mock.calls if call.request.url.host == ARM_HOST]

    assert [(row["group"], row["name"]) for row in result["value"]] == [("g1", "a1")]
    assert all("other" not in path for path in paths)
    reasons = {item["reason"]: item for item in result["omissions"]["items"]}
    assert set(reasons) == {"name_not_usable", "no_permission"}
    assert reasons["no_permission"]["what"] == "assessments of group g2"
    assert "../other" not in json.dumps(result["omissions"])
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_assessment_list_reads_at_most_the_group_limit() -> None:
    groups = [_group(f"g{n:03d}") for n in range(MAX_GROUPS + 1)]
    with respx.mock() as mock:
        _login(mock)
        _route(mock, GROUPS_PATH).mock(return_value=_ok(groups))
        reads = mock.get(url__regex=rf"^{ARM_BASE}{GROUPS_PATH}/g\d+/assessments\?").mock(
            return_value=_ok([])
        )
        result = await _connector().migrate_assessment_list(
            operator(), AzureTargetStub(), MIGRATE_PARAMS
        )

    assert reads.call_count == MAX_GROUPS
    (item,) = result["omissions"]["items"]
    assert item["reason"] == "group_limit"
    assert f"first {MAX_GROUPS} of {MAX_GROUPS + 1}" in item["detail"]


@pytest.mark.asyncio
async def test_a_read_of_many_groups_stops_starting_new_reads_after_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([0.0, 0.0, 200.0, 200.0])
    monkeypatch.setattr(migrate_reads, "_monotonic", lambda: next(clock))
    with respx.mock() as mock:
        _login(mock)
        _route(mock, GROUPS_PATH).mock(return_value=_ok([_group("g1"), _group("g2"), _group("g3")]))
        reads = _route(mock, f"{GROUPS_PATH}/g1/assessments").mock(
            return_value=_ok([_assessment("a1", "g1")])
        )
        result = await _connector().migrate_assessment_list(
            operator(), AzureTargetStub(), MIGRATE_PARAMS
        )

    assert reads.call_count == 1
    assert [row["name"] for row in result["value"]] == ["a1"]
    assert [item["what"] for item in result["omissions"]["items"]] == [
        "assessments of group g2",
        "assessments of group g3",
    ]
    assert {item["reason"] for item in result["omissions"]["items"]} == {"time_limit"}
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_a_group_that_does_not_exist_returns_the_error_as_it_is() -> None:
    with respx.mock() as mock:
        _login(mock)
        _route(mock, f"{GROUPS_PATH}/missing/assessments").mock(
            return_value=_error(404, "ResourceNotFound")
        )
        with pytest.raises(
            AzureNotFoundError, match="no such subscription, resource group or resource"
        ):
            await _connector().migrate_assessment_list(
                operator(), AzureTargetStub(), {**MIGRATE_PARAMS, "group": "missing"}
            )


@pytest.mark.asyncio
async def test_assessment_machine_list_returns_readiness_cost_and_source() -> None:
    no_utilization = _assessed(
        3, _discovered("importsites", "example-import-site", 3), "NotSuitable"
    )
    del no_utilization["properties"]["percentageCoresUtilization"]
    machines = [
        _assessed(1, _discovered("vmwaresites", "example-vmware-site", 1)),
        _assessed(2, _discovered("vmwaresites", "example-vmware-site", 2), "ConditionallySuitable"),
        no_utilization,
        _assessed(4, "/somewhere/else", "SomethingNew"),
    ]
    with respx.mock() as mock:
        _login(mock)
        summary = _route(mock, ASSESSMENT_PATH).mock(
            return_value=httpx.Response(200, json=_assessment())
        )
        arm = _route(mock, f"{ASSESSMENT_PATH}/assessedMachines").mock(return_value=_ok(machines))
        result = await _connector().migrate_assessment_machine_list(
            operator(), AzureTargetStub(), ASSESSMENT_PARAMS
        )

    assert summary.call_count == 1 and arm.call_count == 1
    assert arm.calls.last.request.url.params["api-version"] == "2024-01-15"
    assert result["assessment"]["currency"] == "EUR"
    assert result["assessment"]["sizing_criterion"] == "PerformanceBased"
    assert result["assessment"]["group"] == GROUP
    first = result["value"][0]
    assert first["readiness"] == "Suitable"
    assert first["recommended_size"] == "Standard_F4s_v2"
    assert first["monthly_compute_cost"] == 61.5
    assert first["monthly_storage_cost"] == 10
    assert first["monthly_bandwidth_cost"] == 4
    assert first["source"] == "appliance"
    imported = next(row for row in result["value"] if row["source"] == "import")
    assert imported["cpu_utilization_percent"] is None
    assert imported["memory_utilization_percent"] == 5
    assert result["source_counts"] == {"appliance": 2, "import": 1, "unknown": 1}
    assert result["readiness_counts"] == {
        "Suitable": 1,
        "ConditionallySuitable": 1,
        "NotSuitable": 1,
        "ReadinessUnknown": 0,
        "Unknown": 0,
        "Other": 1,
    }


@pytest.mark.asyncio
async def test_a_name_with_a_line_end_is_refused_by_the_body_before_any_call() -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        with pytest.raises(ValueError, match="resource_group must be"):
            await _connector().migrate_machine_list(
                operator(), AzureTargetStub(), {"resource_group": "rg\n", "project": PROJECT}
            )

    assert not login.called


# ----------------------------------------------------------------------
# Through the dispatcher: registration, parameter check, result handles
# ----------------------------------------------------------------------


class _FakeValkey:
    """In-memory async Valkey stand-in for the result handle store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[ResultHandleStore]:
    """Register the Azure connector and use the real reducer with an in-memory store."""
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
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
    store = ResultHandleStore(_FakeValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "meho_backplane.operations.result_query.get_result_handle_store", lambda: store
    )
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))
    yield store
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


async def _registered() -> None:
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()


async def _dispatch(op_id: str, params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params,
    )


@pytest.mark.asyncio
async def test_every_migrate_read_is_registered_safe_with_its_group(_dispatch_env: Any) -> None:
    await register_azure_typed_operations()

    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == AZURE_IMPL_ID)
                )
            ).scalars()
            if row.op_id.startswith("azure.migrate.")
        }
        group = (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.impl_id == AZURE_IMPL_ID, OperationGroup.group_key == "migrate"
                )
            )
        ).scalar_one()

    assert set(rows) == {op.op_id for op in OPS}
    assert group.when_to_use == WHEN_TO_USE["migrate"]
    assert group.review_status == "enabled"
    for op in OPS:
        row = rows[op.op_id]
        assert row.safety_level == "safe" and row.requires_approval is False
        assert row.handler_ref is not None
        assert row.handler_ref.endswith(f"MigrateOps.{op.handler_attr}")
        hinted = row.llm_instructions["result_objects"]["objects"]
        assert set(hinted) <= set(op.response_schema["properties"])  # type: ignore[index]
        assert getattr(MigrateOps, op.handler_attr) is getattr(AzureConnector, op.handler_attr)


@pytest.mark.asyncio
async def test_a_bad_name_is_refused_before_anything_is_sent(_dispatch_env: Any) -> None:
    await _registered()

    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _dispatch(
            "azure.migrate.machine.list", {"resource_group": RG, "project": "../../other"}
        )

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert not login.called


@pytest.mark.asyncio
async def test_a_missing_resource_group_comes_back_as_not_found(_dispatch_env: Any) -> None:
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        mock.get(url__startswith=f"{ARM_BASE}{OFFAZURE_PATH}/").mock(
            return_value=_error(404, "ResourceGroupNotFound")
        )
        result = await _dispatch("azure.migrate.site.list", {"resource_group": RG})

    assert result.status == "not_found"
    assert result.extras["error_code"] == "not_found"


def _sixty_routes(mock: respx.MockRouter, op_id: str) -> dict[str, Any]:
    """Mock answers with 60 rows for *op_id*; return the params of the call."""
    many = range(60)
    if op_id == "azure.migrate.project.list":
        _route(mock, f"/subscriptions/{SUB}/providers/Microsoft.Migrate/assessmentProjects").mock(
            return_value=_ok([_project(f"p{n:02d}") for n in many])
        )
        return {}
    if op_id == "azure.migrate.site.list":
        _route(mock, f"{OFFAZURE_PATH}/vmwareSites").mock(
            return_value=_error(403, "AuthorizationFailed")
        )
        for kind in ("hypervSites", "serverSites", "masterSites"):
            _route(mock, f"{OFFAZURE_PATH}/{kind}").mock(return_value=_ok([]))
        _route(mock, f"{OFFAZURE_PATH}/importSites").mock(
            return_value=_ok([_import_site(f"site-{n:02d}") for n in many])
        )
        return {"resource_group": RG}
    if op_id == "azure.migrate.machine.list":
        _route(mock, f"{PROJECT_PATH}/machines").mock(
            return_value=_ok(
                [_machine(n, _discovered("vmwaresites", "s", n)) for n in range(40)]
                + [_machine(n, _discovered("importsites", "i", n)) for n in range(40, 60)]
            )
        )
        return MIGRATE_PARAMS
    if op_id == "azure.migrate.group.list":
        _route(mock, GROUPS_PATH).mock(return_value=_ok([_group(f"g{n:02d}") for n in many]))
        return MIGRATE_PARAMS
    if op_id == "azure.migrate.assessment.list":
        _route(mock, f"{GROUPS_PATH}/{GROUP}/assessments").mock(
            return_value=_ok([_assessment(f"a{n:02d}") for n in many])
        )
        return {**MIGRATE_PARAMS, "group": GROUP}
    _route(mock, ASSESSMENT_PATH).mock(return_value=httpx.Response(200, json=_assessment()))
    _route(mock, f"{ASSESSMENT_PATH}/assessedMachines").mock(
        return_value=_ok(
            [_assessed(n, _discovered("vmwaresites", "s", n)) for n in range(45)]
            + [
                _assessed(n, _discovered("importsites", "i", n), "NotSuitable")
                for n in range(45, 60)
            ]
        )
    )
    return ASSESSMENT_PARAMS


#: The blocks each operation must keep next to its stored-result link.
_EXPECTED_BLOCKS: dict[str, dict[str, Any]] = {
    "azure.migrate.project.list": {},
    "azure.migrate.site.list": {
        "kind_counts": {"vmware": None, "hyperv": 0, "server": 0, "import": 60, "master": 0}
    },
    "azure.migrate.machine.list": {"source_counts": {"appliance": 40, "import": 20, "unknown": 0}},
    "azure.migrate.group.list": {},
    "azure.migrate.assessment.list": {},
    "azure.migrate.assessment.machine.list": {
        "source_counts": {"appliance": 45, "import": 15, "unknown": 0},
        "readiness_counts": {
            "Suitable": 45,
            "ConditionallySuitable": 0,
            "NotSuitable": 15,
            "ReadinessUnknown": 0,
            "Unknown": 0,
            "Other": 0,
        },
    },
}


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_BLOCKS))
@pytest.mark.asyncio
async def test_a_long_list_keeps_its_blocks_next_to_the_stored_result(
    _dispatch_env: ResultHandleStore, op_id: str
) -> None:
    """The real reducer stores the 60 rows and keeps every block inline and stored."""
    await _registered()

    with respx.mock() as mock:
        _login(mock)
        params = _sixty_routes(mock, op_id)
        result = await _dispatch(op_id, params)

    assert result.status == "ok", result.error
    assert result.handle is not None, "60 rows should come back as a stored result"
    summary = result.result
    assert summary["row_count"] == 60
    expected_omissions = 1 if op_id == "azure.migrate.site.list" else 0
    assert summary["omissions"]["count"] == expected_omissions
    assert summary["truncated"] is (expected_omissions > 0)
    for key, value in _EXPECTED_BLOCKS[op_id].items():
        assert summary[key] == value, key
    if op_id == "azure.migrate.assessment.machine.list":
        assert summary["assessment"]["currency"] == "EUR"
        assert summary["assessment"]["status"] == "Completed"
        assert summary["assessment"]["sizing_criterion"] == "PerformanceBased"
    stored = await run_result_query(
        operator(), result.handle.handle_id, ResultQuerySpec(path="/omissions")
    )
    assert stored["value"]["count"] == expected_omissions
    if expected_omissions:
        assert stored["value"]["items"][0]["reason"] == "no_permission"
        assert summary["omissions"]["items"][0]["what"] == "vmware sites"


@pytest.mark.asyncio
async def test_a_limit_on_a_long_list_stays_next_to_the_stored_result(
    _dispatch_env: ResultHandleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paging limit cuts the machines; the reason survives at the stored result."""
    ticks = iter(range(0, 10_000, 200))
    monkeypatch.setattr(_paging, "_monotonic", lambda: float(next(ticks)))
    await _registered()
    next_link = f"{ARM_BASE}{PROJECT_PATH}/machines?api-version=2024-01-15&continuationToken=2"

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _route(mock, f"{PROJECT_PATH}/machines").mock(
            side_effect=[
                _ok(
                    [_machine(n, _discovered("importsites", "i", n)) for n in range(55)], next_link
                ),
                _ok([_machine(99, None)]),
            ]
        )
        result = await _dispatch("azure.migrate.machine.list", MIGRATE_PARAMS)

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["truncated"] is True
    (item,) = summary["omissions"]["items"]
    assert item["what"] == "machines"
    assert item["reason"] == "time_limit"
    assert summary["source_counts"] == {"appliance": 0, "import": 55, "unknown": 0}
