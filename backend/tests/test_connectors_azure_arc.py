# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure arc family: Arc machines, stored patching results and the
approved patch assessment.

The dispatch tests run the real dispatcher (parameter check, approval, audit,
result reduction) with only the Azure registrar run. Microsoft's login service
and Azure Resource Manager are mocked with respx. Every id, name and secret
here is made up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    _http,
    _paging,
    _polling,
    arc,
    arc_reads,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.arc_assess import checked_machine
from meho_backplane.connectors.azure.arc_reads import ASSESSMENT_QUERY
from meho_backplane.connectors.azure.arc_rows import (
    assessment_result,
    class_counts,
    installation_row,
    machine_of_result,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
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
from tests._result_store_helpers import MemoryResultStore

GROUP = "rg-servers-example"
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
PRINCIPAL_ID = "cccccccc-0000-0000-0000-00000000e001"
PUBLIC_KEY = "example-public-key-of-the-agent"
TPM_CERTIFICATE = "example-tpm-certificate-of-the-agent"
PROXY_URL = "http://proxy.example.test:3128"
SECRET = "Bearer s3cr3tTOKENvalue" + "A" * 80

GRAPH_URL = f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources"
MACHINES_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.HybridCompute/machines"
)

ARC_OPS = (
    "azure.arc.machine.list",
    "azure.arc.machine.show",
    "azure.patching.assessment.list",
    "azure.patching.installation.list",
    "azure.patching.assessment.start",
)


def _vm_id(name: str, subscription: str = SUBSCRIPTION_A) -> str:
    return (
        f"/subscriptions/{subscription}/resourceGroups/{GROUP}"
        + f"/providers/Microsoft.Compute/virtualMachines/{name}"
    )


def _arc_id(name: str, subscription: str = SUBSCRIPTION_A, group: str = GROUP) -> str:
    return (
        f"/subscriptions/{subscription}/resourceGroups/{group}"
        + f"/providers/Microsoft.HybridCompute/machines/{name}"
    )


VM_ID = _vm_id("vm-app-01")
ARC_ID = _arc_id("arc-server-01")
VM_ASSESS_URL = f"{ARM_BASE}{VM_ID}/assessPatches"
ARC_ASSESS_URL = f"{ARM_BASE}{ARC_ID}/assessPatches"
VM_RESULT_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Compute/locations/"
    + "westeurope/operations/33333333-0000-0000-0000-00000000f001"
    + "?monitor=true&api-version=2026-04-01"
)
VM_STATUS_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Compute/locations/"
    + "westeurope/operations/33333333-0000-0000-0000-00000000f001?api-version=2026-04-01"
)
ARC_RESULT_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.HybridCompute/locations/"
    + "westeurope/operationResults/33333333-0000-0000-0000-00000000f002?api-version=2026-09-01"
)


# ----------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------


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


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    fake = _Clock()
    for module in (_http, _paging, _polling):
        monkeypatch.setattr(module, "_monotonic", fake.monotonic)
    for module in (_http, _polling):
        monkeypatch.setattr(module, "_sleep", fake.sleep)
    monkeypatch.setattr(arc_reads, "_now", lambda: NOW)
    yield fake


@pytest.fixture
async def azure(monkeypatch: pytest.MonkeyPatch) -> AzureConnector:
    """Register the Azure operations; return the dispatcher's connector instance."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """The real result reducer over an in-memory store that result_query reads."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


async def _call(
    op_id: str, params: dict[str, Any] | None = None, *, approved: bool = False
) -> OperationResult:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params or {},
        _approved=approved,
    )


async def _rows(result: OperationResult, query: dict[str, Any]) -> list[dict[str, Any]]:
    assert result.handle is not None
    spec = ResultQuerySpec.model_validate(query)
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, spec)
    rows: list[dict[str, Any]] = answer["rows"]
    return rows


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


def _machine(
    name: str, status: str = "Connected", *, os_type: str = "linux", group: str = GROUP
) -> dict[str, Any]:
    """One Azure Arc ``Machine`` as Azure returns it, with fields MEHO must not pass on."""
    return {
        "id": _arc_id(name, group=group),
        "name": name,
        "type": "Microsoft.HybridCompute/machines",
        "location": "westeurope",
        "kind": "VMware",
        "identity": {"type": "SystemAssigned", "principalId": PRINCIPAL_ID, "tenantId": TENANT_A},
        "tags": {"owner": "team-a"},
        "properties": {
            "status": status,
            "lastStatusChange": "2026-10-27T08:00:00Z",
            "agentVersion": "1.50.00000.0000",
            "osName": os_type,
            "osVersion": "6.8.0",
            "osType": os_type,
            "osSku": "Example Server 24",
            "osEdition": "server",
            "provisioningState": "Succeeded",
            "displayName": name,
            "machineFqdn": f"{name}.example.test",
            "domainName": None,
            "vmId": "44444444-0000-0000-0000-00000000a001",
            "clientPublicKey": PUBLIC_KEY,
            "tpmEkCertificate": TPM_CERTIFICATE,
            "agentConfiguration": {"proxyUrl": PROXY_URL, "incomingConnectionsPorts": ["22"]},
            "osProfile": {
                "computerName": name,
                "linuxConfiguration": {
                    "patchSettings": {
                        "assessmentMode": "AutomaticByPlatform",
                        "patchMode": "AutomaticByPlatform",
                    }
                },
                "windowsConfiguration": {
                    "patchSettings": {"assessmentMode": "ImageDefault", "patchMode": "Manual"}
                },
            },
            "serviceStatuses": {
                "extensionService": {"status": "Running", "startupType": "Automatic"},
                "guestConfigurationService": {"status": "Stopped", "startupType": "Manual"},
            },
            "errorDetails": [],
        },
    }


def _assessment(
    machine_id: str,
    counts: dict[str, int] | None = None,
    *,
    status: str = "Succeeded",
    reboot: bool = False,
) -> dict[str, Any]:
    """One stored assessment row of Resource Graph, as the fixed query projects it."""
    kind = "virtualmachines" if "virtualMachines" in machine_id else "machines"
    provider = "compute" if kind == "virtualmachines" else "hybridcompute"
    properties: dict[str, Any] = {
        "status": status,
        "rebootPending": reboot,
        "patchServiceUsed": "APT",
        "osType": "Linux",
        "startDateTime": "2026-10-27T09:58:00Z",
        "lastModifiedDateTime": "2026-10-27T10:00:00Z",
        "startedBy": "Platform",
        "errorDetails": None,
    }
    if counts is not None:
        properties["availablePatchCountByClassification"] = counts
    return {
        "id": f"{machine_id}/patchAssessmentResults/latest",
        "type": f"microsoft.{provider}/{kind}/patchassessmentresults",
        "location": "westeurope",
        "properties": properties,
    }


def _installation(machine_id: str, run: int, *, status: str = "Succeeded") -> dict[str, Any]:
    """One stored installation run of Resource Graph, as the fixed query projects it."""
    run_id = f"55555555-0000-0000-0000-{run:012d}"
    return {
        "id": f"{machine_id}/patchInstallationResults/{run_id}",
        "type": "microsoft.compute/virtualmachines/patchinstallationresults",
        "location": "westeurope",
        "name": run_id,
        "properties": {
            "installationActivityId": run_id,
            "status": status,
            "startDateTime": "2026-10-26T02:00:00Z",
            "lastModifiedDateTime": "2026-10-26T02:30:00Z",
            "startedBy": "Platform",
            "installedPatchCount": 4,
            "failedPatchCount": 1 if status == "Failed" else 0,
            "pendingPatchCount": 0,
            "excludedPatchCount": 2,
            "notSelectedPatchCount": 3,
            "rebootStatus": "Completed",
            "maintenanceWindowExceeded": "False",
            "osType": "Windows",
            "errorDetails": None,
        },
    }


def _graph(rows: list[Any], token: str | None = None, **extra: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "data": rows,
        "count": len(rows),
        "totalRecords": len(rows),
        "resultTruncated": "false",
        **extra,
    }
    if token is not None:
        answer["$skipToken"] = token
    return answer


def _bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


#: The answer of a finished check on an Azure VM (Microsoft's example shape).
VM_CHECK = {
    "status": "Succeeded",
    "assessmentActivityId": "66666666-0000-0000-0000-00000000c001",
    "rebootPending": True,
    "criticalAndSecurityPatchCount": 1,
    "otherPatchCount": 2,
    "startDateTime": "2026-10-27T12:01:00Z",
    "availablePatches": [
        {"patchId": "p1", "name": "Example security update", "classifications": ["Security"]},
        {"patchId": "p2", "name": "Example definition update", "classifications": ["Definition"]},
        {"patchId": "p3", "name": "Example rollup", "classifications": ["UpdateRollUp"]},
    ],
    "error": None,
}

#: The answer of a finished check on an Arc machine (Microsoft's example shape).
ARC_CHECK = {
    "status": "Succeeded",
    "assessmentActivityId": "66666666-0000-0000-0000-00000000c002",
    "rebootPending": False,
    "availablePatchCountByClassification": {
        "critical": 2,
        "security": 3,
        "definition": 0,
        "updateRollup": 1,
        "updates": 4,
    },
    "startDateTime": "2026-10-27T12:01:00Z",
    "lastModifiedDateTime": "2026-10-27T12:03:00Z",
    "startedBy": "User",
    "osType": "Windows",
    "errorDetails": None,
}


# ----------------------------------------------------------------------
# The operation table
# ----------------------------------------------------------------------


def test_the_family_is_wired_into_the_connector_table() -> None:
    table = {op.op_id: op for op in AZURE_TYPED_OPS}
    assert set(ARC_OPS) <= set(table)
    assert [op.op_id for op in arc.OPS] == list(ARC_OPS)
    assert AZURE_TYPED_WHEN_TO_USE_BY_GROUP["arc"] == arc.WHEN_TO_USE["arc"]
    assert issubclass(AzureConnector, arc.ArcOps)
    for op in arc.OPS:
        assert op.group_key == "arc"
        if op.op_id == "azure.patching.assessment.start":
            assert (op.safety_level, op.requires_approval) == ("caution", True)
            assert op.llm_instructions is not None
            assert "approve" in str(op.llm_instructions["preconditions"])
        else:
            assert (op.safety_level, op.requires_approval) == ("safe", False)
            assert "read-only" in op.tags
            assert write_word_in_op_id(op.op_id) is None


def test_every_result_objects_hint_names_an_object_of_the_answer() -> None:
    hinted = {
        op.op_id: op.llm_instructions["result_objects"]["objects"]
        for op in arc.OPS
        if op.llm_instructions and "result_objects" in op.llm_instructions
    }
    assert set(hinted) == {
        "azure.arc.machine.list",
        "azure.patching.assessment.list",
        "azure.patching.installation.list",
    }
    for op in arc.OPS:
        for key, fields in hinted.get(op.op_id, {}).items():
            assert op.response_schema is not None
            declared = op.response_schema["properties"][key]
            assert declared["type"] == "object"
            assert fields == list(declared["properties"])


# ----------------------------------------------------------------------
# Arc machines
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_machine_list_follows_the_next_link_and_passes_on_only_listed_fields(
    azure: AzureConnector,
) -> None:
    first = {
        "value": [_machine("arc-01"), _machine("arc-02", "Disconnected")],
        "nextLink": f"{MACHINES_URL}?api-version=2026-09-01&$skiptoken=page2",
    }
    second = {"value": [_machine("arc-03", "Error"), "not-a-machine"]}
    with respx.mock() as mock:
        _login(mock)
        route = mock.get(MACHINES_URL).mock(
            side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
        )
        result = await _call("azure.arc.machine.list")

    assert result.status == "ok", result.error
    assert route.call_count == 2
    assert route.calls[0].request.url.params["api-version"] == "2026-09-01"
    assert route.calls[1].request.url.params["$skiptoken"] == "page2"
    answer = result.result
    assert answer["value"][0] == {
        "name": "arc-01",
        "resource_group": GROUP,
        "location": "westeurope",
        "id": _arc_id("arc-01"),
        "kind": "VMware",
        "status": "Connected",
        "last_status_change": "2026-10-27T08:00:00Z",
        "agent_version": "1.50.00000.0000",
        "os_name": "linux",
        "os_version": "6.8.0",
        "os_type": "linux",
        "os_sku": "Example Server 24",
        "provisioning_state": "Succeeded",
        "tags": {"owner": "team-a"},
    }
    assert [row["name"] for row in answer["value"]] == ["arc-01", "arc-02", "arc-03"]
    assert answer["summary"] == {
        "machines": 3,
        "connected": 1,
        "disconnected": 1,
        "error": 1,
        "awaiting_connection": 0,
        "other_status": 0,
    }
    assert (answer["truncated"], answer["pages_read"]) == (False, 2)
    text = json.dumps(answer)
    for withheld in (PROXY_URL, PUBLIC_KEY, TPM_CERTIFICATE, PRINCIPAL_ID):
        assert withheld not in text


@pytest.mark.asyncio
async def test_machine_list_over_50_rows_keeps_its_summary_next_to_the_stored_result(
    azure: AzureConnector, store: MemoryResultStore
) -> None:
    machines = [
        _machine(f"arc-{n:02d}", "Connected" if n < 50 else "Disconnected") for n in range(60)
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.get(MACHINES_URL).respond(200, json={"value": machines})
        result = await _call("azure.arc.machine.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["summary"] == {
        "machines": 60,
        "connected": 50,
        "disconnected": 10,
        "error": 0,
        "awaiting_connection": 0,
        "other_status": 0,
    }
    assert (summary["truncated"], summary["pages_read"]) == (False, 1)
    rows = await _rows(
        result, {"filter": [{"field": "status", "op": "=", "value": "Disconnected"}]}
    )
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_machine_show_reads_one_machine_with_its_details(azure: AzureConnector) -> None:
    machine = _machine("arc-server-01")
    machine["properties"]["errorDetails"] = [
        {"code": "AgentError", "message": f"Agent could not reach Azure. {SECRET}"}
    ]
    with respx.mock() as mock:
        _login(mock)
        route = mock.get(f"{ARM_BASE}{ARC_ID}").respond(200, json=machine)
        result = await _call(
            "azure.arc.machine.show", {"resource_group": GROUP, "name": "arc-server-01"}
        )

    assert result.status == "ok", result.error
    assert route.calls[0].request.url.params["api-version"] == "2026-09-01"
    detail = result.result
    assert detail["name"] == "arc-server-01"
    assert detail["status"] == "Connected"
    assert detail["computer_name"] == "arc-server-01"
    assert detail["machine_fqdn"] == "arc-server-01.example.test"
    assert detail["os_edition"] == "server"
    # A Linux machine: the patch settings come from its Linux configuration.
    assert (detail["assessment_mode"], detail["patch_mode"]) == (
        "AutomaticByPlatform",
        "AutomaticByPlatform",
    )
    assert detail["extension_service"] == "Running"
    assert detail["guest_configuration_service"] == "Stopped"
    assert detail["errors"][0]["code"] == "AgentError"
    text = json.dumps(detail)
    assert "s3cr3t" not in text
    for withheld in (PROXY_URL, PUBLIC_KEY, TPM_CERTIFICATE, PRINCIPAL_ID):
        assert withheld not in text


@pytest.mark.asyncio
async def test_machine_show_of_a_windows_machine_reads_its_windows_settings(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(f"{ARM_BASE}{ARC_ID}").respond(
            200, json=_machine("arc-server-01", os_type="windows")
        )
        result = await _call(
            "azure.arc.machine.show", {"resource_group": GROUP, "name": "arc-server-01"}
        )

    assert result.status == "ok", result.error
    assert (result.result["assessment_mode"], result.result["patch_mode"]) == (
        "ImageDefault",
        "Manual",
    )


@pytest.mark.asyncio
async def test_machine_show_encodes_special_characters_of_the_resource_group(
    azure: AzureConnector,
) -> None:
    group = "rg(servers)"
    with respx.mock() as mock:
        _login(mock)
        route = mock.get(url__startswith=f"{ARM_BASE}/subscriptions/").respond(
            200, json=_machine("arc-server-01", group=group)
        )
        result = await _call(
            "azure.arc.machine.show", {"resource_group": group, "name": "arc-server-01"}
        )

    assert result.status == "ok", result.error
    assert (
        route.calls[0]
        .request.url.raw_path.decode()
        .startswith(f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg%28servers%29/providers/")
    )
    assert result.result["resource_group"] == group


@pytest.mark.parametrize(
    "params",
    [
        {"resource_group": GROUP, "name": ".."},
        {"resource_group": GROUP, "name": "a/b"},
        {"resource_group": GROUP, "name": "a?b"},
        {"resource_group": GROUP, "name": "a%2Fb"},
        {"resource_group": GROUP, "name": "x" * 55},
        {"resource_group": "..", "name": "arc-server-01"},
        {"resource_group": "rg/other", "name": "arc-server-01"},
        {"resource_group": GROUP, "name": "arc-server-01\n"},
        {"resource_group": GROUP},
    ],
)
@pytest.mark.asyncio
async def test_machine_show_refuses_a_bad_name_before_any_request(
    azure: AzureConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call("azure.arc.machine.show", params)

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not login.called


@pytest.mark.asyncio
async def test_the_body_checks_the_names_again(azure: AzureConnector) -> None:
    """A direct call that skips the parameter schema is refused too, before any request."""
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        with pytest.raises(ValueError, match="not a valid Azure name"):
            await azure.arc_machine_show(
                operator(), AzureTargetStub(), {"resource_group": GROUP, "name": "../x"}
            )

    assert not login.called


@pytest.mark.asyncio
async def test_machine_show_of_a_missing_machine_is_not_found(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(f"{ARM_BASE}{ARC_ID}").respond(
            404, json={"error": {"code": "ResourceNotFound", "message": "Not found."}}
        )
        result = await _call(
            "azure.arc.machine.show", {"resource_group": GROUP, "name": "arc-server-01"}
        )

    assert result.status == "not_found"


# ----------------------------------------------------------------------
# Stored update checks
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessment_list_asks_resource_graph_for_the_target_subscription_only(
    azure: AzureConnector,
) -> None:
    rows = [
        _assessment(VM_ID, {"critical": 1, "security": 2, "other": 3}, reboot=True),
        _assessment(ARC_ID, {"security": 1, "updateRollup": 2, "definition": 4}),
        _assessment(_vm_id("vm-elsewhere", SUBSCRIPTION_B), {"critical": 9}),
        "not-a-row",
    ]
    with respx.mock() as mock:
        _login(mock)
        route = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(rows))
        result = await _call("azure.patching.assessment.list")

    assert result.status == "ok", result.error
    body = _bodies(route)[0]
    assert body == {
        "query": ASSESSMENT_QUERY,
        "subscriptions": [SUBSCRIPTION_A],
        "options": {"resultFormat": "objectArray"},
    }
    assert route.calls[0].request.url.params["api-version"] == "2024-04-01"
    for word in ("take", "limit", "sample"):
        assert f" {word} " not in f" {body['query']} "
    answer = result.result
    assert answer["value"] == [
        {
            "machine_name": "vm-app-01",
            "machine_id": VM_ID,
            "machine_type": "Microsoft.Compute/virtualMachines",
            "resource_group": GROUP,
            "location": "westeurope",
            "os_type": "Linux",
            "status": "Succeeded",
            "assessed_at": "2026-10-27T10:00:00Z",
            "started_by": "Platform",
            "reboot_pending": True,
            "critical": 1,
            "security": 2,
            "other": 3,
            "patch_service": "APT",
            "error": None,
        },
        {
            "machine_name": "arc-server-01",
            "machine_id": ARC_ID,
            "machine_type": "Microsoft.HybridCompute/machines",
            "resource_group": GROUP,
            "location": "westeurope",
            "os_type": "Linux",
            "status": "Succeeded",
            "assessed_at": "2026-10-27T10:00:00Z",
            "started_by": "Platform",
            "reboot_pending": False,
            "critical": 0,
            "security": 1,
            "other": 6,
            "patch_service": "APT",
            "error": None,
        },
    ]
    assert answer["summary"] == {
        "machines": 2,
        "needing_critical_or_security": 2,
        "critical_patches": 1,
        "security_patches": 3,
        "other_patches": 9,
        "reboot_pending": 1,
        "failed_assessments": 0,
    }
    assert SUBSCRIPTION_B not in json.dumps(answer)


@pytest.mark.asyncio
async def test_assessment_list_follows_the_skip_token(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        route = mock.post(url__startswith=GRAPH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_graph([_assessment(VM_ID, {})], token="page2")),
                httpx.Response(200, json=_graph([_assessment(ARC_ID, {})])),
            ]
        )
        result = await _call("azure.patching.assessment.list")

    assert result.status == "ok", result.error
    first, second = _bodies(route)
    assert second["options"] == {"resultFormat": "objectArray", "$skipToken": "page2"}
    assert (second["query"], second["subscriptions"]) == (first["query"], [SUBSCRIPTION_A])
    assert [row["machine_name"] for row in result.result["value"]] == ["vm-app-01", "arc-server-01"]
    assert result.result["pages_read"] == 2


@pytest.mark.asyncio
async def test_missing_counts_are_unknown_not_zero(azure: AzureConnector) -> None:
    failed = _assessment(VM_ID, None, status="Failed")
    failed["properties"]["errorDetails"] = {"code": "AssessmentFailed", "message": SECRET}
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([failed]))
        result = await _call("azure.patching.assessment.list")

    assert result.status == "ok", result.error
    row = result.result["value"][0]
    assert (row["critical"], row["security"], row["other"]) == (None, None, None)
    assert row["error"]["code"] == "AssessmentFailed"
    assert "s3cr3t" not in json.dumps(row)
    assert result.result["summary"]["failed_assessments"] == 1
    assert result.result["summary"]["needing_critical_or_security"] == 0


@pytest.mark.asyncio
async def test_a_result_cut_by_resource_graph_is_named(azure: AzureConnector) -> None:
    answer = _graph([_assessment(VM_ID, {})], resultTruncated="true", totalRecords=5)
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=answer)
        result = await _call("azure.patching.assessment.list")

    assert result.status == "ok", result.error
    assert result.result["truncated"] is True
    assert result.result["omission_kind"] == "result_truncated"


@pytest.mark.asyncio
async def test_assessment_list_over_50_rows_keeps_its_summary_next_to_the_stored_result(
    azure: AzureConnector, store: MemoryResultStore
) -> None:
    rows = [
        _assessment(_vm_id(f"vm-{n:02d}"), {"critical": 1 if n < 5 else 0, "security": 0})
        for n in range(60)
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(rows))
        result = await _call("azure.patching.assessment.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["summary"] == {
        "machines": 60,
        "needing_critical_or_security": 5,
        "critical_patches": 5,
        "security_patches": 0,
        "other_patches": 0,
        "reboot_pending": 0,
        "failed_assessments": 0,
    }
    assert "result_object_truncations" not in summary
    needing = await _rows(result, {"filter": [{"field": "critical", "op": ">", "value": 0}]})
    assert sorted(row["machine_name"] for row in needing) == [f"vm-{n:02d}" for n in range(5)]


# ----------------------------------------------------------------------
# Stored installation runs
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_installation_list_reads_the_last_30_days_by_default(
    azure: AzureConnector,
) -> None:
    runs = [_installation(VM_ID, 2, status="Failed"), _installation(VM_ID, 1)]
    with respx.mock() as mock:
        _login(mock)
        route = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(runs))
        result = await _call("azure.patching.installation.list")

    assert result.status == "ok", result.error
    body = _bodies(route)[0]
    assert body["subscriptions"] == [SUBSCRIPTION_A]
    assert body["query"].startswith("patchinstallationresources")
    assert "datetime(2026-09-27T12:00:00Z)" in body["query"]
    assert "order by lastModified desc" in body["query"]
    answer = result.result
    assert answer["window"] == {
        "days": 30,
        "from": "2026-09-27T12:00:00Z",
        "to": "2026-10-27T12:00:00Z",
    }
    assert answer["value"][0] == {
        "machine_name": "vm-app-01",
        "machine_id": VM_ID,
        "machine_type": "Microsoft.Compute/virtualMachines",
        "resource_group": GROUP,
        "location": "westeurope",
        "os_type": "Windows",
        "run_id": "55555555-0000-0000-0000-000000000002",
        "status": "Failed",
        "started_at": "2026-10-26T02:00:00Z",
        "last_modified": "2026-10-26T02:30:00Z",
        "started_by": "Platform",
        "installed": 4,
        "failed": 1,
        "pending": 0,
        "excluded": 2,
        "not_selected": 3,
        "reboot_status": "Completed",
        "maintenance_window_exceeded": False,
        "error": None,
    }
    assert answer["summary"] == {
        "runs": 2,
        "machines": 1,
        "failed_runs": 1,
        "installed_patches": 8,
        "failed_patches": 1,
    }


@pytest.mark.asyncio
async def test_installation_list_reads_the_days_asked_for(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        route = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call("azure.patching.installation.list", {"days": 7})

    assert result.status == "ok", result.error
    assert "datetime(2026-10-20T12:00:00Z)" in _bodies(route)[0]["query"]
    assert result.result["window"]["days"] == 7
    assert result.result["value"] == []
    assert result.result["summary"]["runs"] == 0


@pytest.mark.parametrize("days", [0, 31, -1, True, "7", 2.5])
@pytest.mark.asyncio
async def test_installation_list_refuses_other_days_before_any_request(
    azure: AzureConnector, days: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call("azure.patching.installation.list", {"days": days})

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params")
    assert not login.called


@pytest.mark.asyncio
async def test_the_body_checks_days_again(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        with pytest.raises(ValueError, match="days must be"):
            await azure.patching_installation_list(operator(), AzureTargetStub(), {"days": 90})

    assert not login.called


@pytest.mark.asyncio
async def test_installation_list_over_50_rows_keeps_window_and_summary(
    azure: AzureConnector, store: MemoryResultStore
) -> None:
    runs = [_installation(_vm_id(f"vm-{n % 6:02d}"), n) for n in range(60)]
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(runs))
        result = await _call("azure.patching.installation.list", {"days": 14})

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["window"] == {
        "days": 14,
        "from": "2026-10-13T12:00:00Z",
        "to": "2026-10-27T12:00:00Z",
    }
    assert summary["summary"] == {
        "runs": 60,
        "machines": 6,
        "failed_runs": 0,
        "installed_patches": 240,
        "failed_patches": 0,
    }
    rows = await _rows(result, {"filter": [{"field": "machine_name", "op": "=", "value": "vm-00"}]})
    assert len(rows) == 10


# ----------------------------------------------------------------------
# The approved patch assessment
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_check_waits_for_approval(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call("azure.patching.assessment.start", {"machine_id": VM_ID})

    assert result.status == "awaiting_approval", result
    assert not login.called


@pytest.mark.asyncio
async def test_a_check_on_a_vm_follows_the_location_url_to_the_result(
    azure: AzureConnector, clock: _Clock
) -> None:
    accepted = httpx.Response(
        202,
        headers={
            "Location": VM_RESULT_URL,
            "Azure-AsyncOperation": VM_STATUS_URL,
            "Retry-After": "20",
        },
    )
    with respx.mock() as mock:
        _login(mock)
        start = mock.post(VM_ASSESS_URL).mock(return_value=accepted)
        poll = mock.get(VM_RESULT_URL).mock(
            side_effect=[
                httpx.Response(202, headers={"Retry-After": "30"}),
                httpx.Response(200, json=VM_CHECK),
            ]
        )
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "ok", result.error
    assert start.call_count == 1
    request = start.calls[0].request
    assert request.url.params["api-version"] == "2026-04-01"
    assert request.content == b""
    assert poll.call_count == 2
    assert clock.sleeps == [20.0, 30.0]
    answer = result.result
    assert answer["machine_id"] == VM_ID
    assert answer["machine_type"] == "Microsoft.Compute/virtualMachines"
    assert (answer["status"], answer["done"], answer["error"]) == ("Succeeded", True, None)
    assert answer["assessment"] == {
        "status": "Succeeded",
        "started_at": "2026-10-27T12:01:00Z",
        "last_modified": None,
        "reboot_pending": True,
        "os_type": None,
        "critical": 0,
        "security": 1,
        "other": 2,
        "critical_and_security": 1,
        "by_class": {"Security": 1, "Definition": 1, "UpdateRollUp": 1},
        "activity_id": "66666666-0000-0000-0000-00000000c001",
        "error": None,
    }
    assert answer["operation"]["poll_kind"] == "location"
    assert answer["operation"]["poll_url"] == VM_RESULT_URL
    assert answer["operation"]["resource_id"] == VM_ID


@pytest.mark.asyncio
async def test_a_check_on_an_arc_machine_returns_counts_by_class(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        start = mock.post(ARC_ASSESS_URL).respond(202, headers={"Location": ARC_RESULT_URL})
        mock.get(ARC_RESULT_URL).respond(200, json=ARC_CHECK)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": ARC_ID}, approved=True
        )

    assert result.status == "ok", result.error
    assert start.calls[0].request.url.params["api-version"] == "2026-09-01"
    answer = result.result
    assert answer["machine_type"] == "Microsoft.HybridCompute/machines"
    assert answer["done"] is True
    assessment = answer["assessment"]
    assert (assessment["critical"], assessment["security"], assessment["other"]) == (2, 3, 5)
    assert assessment["critical_and_security"] == 5
    assert assessment["by_class"] == ARC_CHECK["availablePatchCountByClassification"]
    assert (assessment["os_type"], assessment["last_modified"]) == (
        "Windows",
        "2026-10-27T12:03:00Z",
    )
    assert assessment["reboot_pending"] is False


@pytest.mark.asyncio
async def test_when_the_wait_runs_out_the_reference_reads_the_outcome_later(
    azure: AzureConnector,
) -> None:
    """The restart-safe path: no second check, only a later read of the same operation."""
    with respx.mock() as mock:
        _login(mock)
        start = mock.post(VM_ASSESS_URL).respond(
            202, headers={"Location": VM_RESULT_URL, "Retry-After": "120"}
        )
        poll = mock.get(VM_RESULT_URL).mock(
            side_effect=[
                httpx.Response(202, headers={"Retry-After": "120"}),
                httpx.Response(202, headers={"Retry-After": "120"}),
                httpx.Response(200, json=VM_CHECK),
            ]
        )
        waited = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )
        assert waited.status == "ok", waited.error
        assert (waited.result["done"], waited.result["assessment"]) == (False, None)
        reference = waited.result["operation"]
        assert reference["poll_url"] == VM_RESULT_URL

        # A restart: every cached connector, secret and token is gone.
        reset_handler_cache()
        instance = get_or_create_connector_instance(AzureConnector)
        assert isinstance(instance, AzureConnector)
        instance._credentials_loader = CountingLoader()
        later = await _call("azure.operation.status", {"operation": reference})

    assert later.status == "ok", later.error
    assert (later.result["status"], later.result["done"]) == ("Succeeded", True)
    assert later.result["result"]["criticalAndSecurityPatchCount"] == 1
    assert start.call_count == 1
    assert poll.call_count == 3


@pytest.mark.asyncio
async def test_a_failed_poll_returns_the_reference_instead_of_an_error(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        start = mock.post(VM_ASSESS_URL).respond(202, headers={"Location": VM_RESULT_URL})
        mock.get(VM_RESULT_URL).respond(503, json={"error": {"code": "ServerBusy"}})
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "ok", result.error
    answer = result.result
    assert (answer["status"], answer["done"], answer["assessment"]) == ("Unknown", False, None)
    assert answer["error"]["code"] == "AzureServerError"
    assert "azure.operation.status" in answer["error"]["message"]
    assert answer["operation"]["poll_url"] == VM_RESULT_URL
    assert start.call_count == 1


@pytest.mark.asyncio
async def test_a_failed_check_reports_azures_error_without_secrets(
    azure: AzureConnector,
) -> None:
    failed = {
        **ARC_CHECK,
        "status": "Failed",
        "errorDetails": {"code": "PatchAssessmentFailed", "message": f"Agent said: {SECRET}"},
    }
    with respx.mock() as mock:
        _login(mock)
        mock.post(ARC_ASSESS_URL).respond(202, headers={"Location": ARC_RESULT_URL})
        mock.get(ARC_RESULT_URL).respond(200, json=failed)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": ARC_ID}, approved=True
        )

    assert result.status == "ok", result.error
    assessment = result.result["assessment"]
    assert assessment["status"] == "Failed"
    assert assessment["error"]["code"] == "PatchAssessmentFailed"
    assert "s3cr3t" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_a_failed_operation_reports_azures_error(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(VM_ASSESS_URL).respond(202, headers={"Location": VM_RESULT_URL})
        mock.get(VM_RESULT_URL).respond(
            409, json={"error": {"code": "OperationNotAllowed", "message": "The VM is stopped."}}
        )
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "ok", result.error
    answer = result.result
    assert (answer["status"], answer["done"], answer["assessment"]) == ("Failed", True, None)
    assert answer["error"] == {"code": "OperationNotAllowed", "message": "The VM is stopped."}


@pytest.mark.asyncio
async def test_the_check_is_not_sent_again_after_a_server_error(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        start = mock.post(VM_ASSESS_URL).respond(500, json={"error": {"code": "InternalError"}})
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "error"
    assert result.extras["exception_class"] == "AzureServerError"
    assert start.call_count == 1


@pytest.mark.asyncio
async def test_an_answer_with_the_result_needs_no_polling(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(ARC_ASSESS_URL).respond(200, json=ARC_CHECK)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": ARC_ID}, approved=True
        )

    assert result.status == "ok", result.error
    answer = result.result
    assert (answer["status"], answer["done"], answer["operation"]) == ("Succeeded", True, None)
    assert answer["assessment"]["critical"] == 2


@pytest.mark.asyncio
async def test_an_accepted_check_without_a_poll_url_says_so(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(VM_ASSESS_URL).respond(202)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "ok", result.error
    answer = result.result
    assert (answer["status"], answer["done"], answer["operation"]) == ("Accepted", False, None)
    assert answer["assessment"] is None


@pytest.mark.asyncio
async def test_without_location_the_async_operation_url_is_polled(azure: AzureConnector) -> None:
    status = {"status": "Succeeded", "properties": {"output": VM_CHECK}}
    with respx.mock() as mock:
        _login(mock)
        mock.post(VM_ASSESS_URL).respond(202, headers={"Azure-AsyncOperation": VM_STATUS_URL})
        mock.get(VM_STATUS_URL).respond(200, json=status)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "ok", result.error
    answer = result.result
    assert answer["operation"]["poll_kind"] == "azure-asyncoperation"
    assert answer["assessment"]["critical_and_security"] == 1


@pytest.mark.asyncio
async def test_a_poll_url_outside_the_subscription_is_refused(azure: AzureConnector) -> None:
    elsewhere = VM_RESULT_URL.replace(SUBSCRIPTION_A, SUBSCRIPTION_B)
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.post(VM_ASSESS_URL).respond(202, headers={"Location": elsewhere})
        poll = mock.get(elsewhere).respond(200, json=VM_CHECK)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": VM_ID}, approved=True
        )

    assert result.status == "error"
    assert result.extras["exception_class"] == "AzureUrlRefusedError"
    assert not poll.called


@pytest.mark.parametrize(
    "machine_id",
    [
        _vm_id("vm-app-01", SUBSCRIPTION_B),
        _arc_id("arc-server-01", SUBSCRIPTION_B),
        VM_ID.replace("Microsoft.Compute/virtualMachines", "Microsoft.Storage/storageAccounts"),
        f"{VM_ID}/extensions/example",
        VM_ID.rsplit("/", 1)[0],
        f"{VM_ID}/",
        VM_ID.replace("vm-app-01", ".."),
        VM_ID.replace("vm-app-01", "vm?x=1"),
        VM_ID.replace("vm-app-01", "vm%2F.."),
        VM_ID.replace(GROUP, ".."),
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}",
    ],
)
@pytest.mark.asyncio
async def test_a_machine_outside_the_target_is_refused_before_any_call(
    azure: AzureConnector, machine_id: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call(
            "azure.patching.assessment.start", {"machine_id": machine_id}, approved=True
        )

    assert result.status == "error"
    assert result.extras["exception_class"] == "ValueError"
    assert "MEHO sent nothing" in result.extras["exception_message"]
    assert not login.called


def test_a_machine_id_in_any_letter_case_is_accepted() -> None:
    machine = checked_machine(AzureTargetStub(), VM_ID.lower().replace(GROUP, "RG-Servers"))
    assert machine.machine_type == "Microsoft.Compute/virtualMachines"
    assert machine.resource_id == (
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/RG-Servers"
        + "/providers/Microsoft.Compute/virtualMachines/vm-app-01"
    )
    assert machine.path == f"{machine.resource_id}/assessPatches"
    assert machine.api_version == "2026-04-01"
    arc_machine = checked_machine(AzureTargetStub(), ARC_ID.upper())
    assert arc_machine.machine_type == "Microsoft.HybridCompute/machines"
    assert arc_machine.api_version == "2026-09-01"


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("by_class", "expected"),
    [
        (None, {"critical": None, "security": None, "other": None}),
        ({}, {"critical": 0, "security": 0, "other": 0}),
        (
            {"critical": 1, "security": 2, "updates": 3, "other": 4},
            {"critical": 1, "security": 2, "other": 7},
        ),
        (
            {"Critical": 2, "tools": "3", "definition": -1},
            {"critical": 2, "security": 0, "other": 0},
        ),
    ],
)
def test_class_counts_add_up_every_other_class(
    by_class: object, expected: dict[str, int | None]
) -> None:
    assert class_counts(by_class) == expected


@pytest.mark.parametrize(
    ("row_id", "expected_name"),
    [
        (f"{VM_ID}/patchAssessmentResults/latest", "vm-app-01"),
        (f"{ARC_ID}/patchInstallationResults/run-1", "arc-server-01"),
        (f"{VM_ID.lower()}/patchassessmentresults/latest", "vm-app-01"),
        (f"{_vm_id('vm-b', SUBSCRIPTION_B)}/patchAssessmentResults/latest", None),
        (f"/subscriptions/{SUBSCRIPTION_A}/patchAssessmentResults/latest", None),
        (VM_ID, None),
        (None, None),
    ],
)
def test_the_machine_is_read_from_the_result_id(row_id: object, expected_name: str | None) -> None:
    machine = machine_of_result({"id": row_id}, SUBSCRIPTION_A)
    if expected_name is None:
        assert machine is None
    else:
        assert machine is not None
        assert machine["machine_name"] == expected_name


def test_values_of_the_wrong_type_become_null() -> None:
    run = _installation(VM_ID, 1)
    run["properties"].update(
        {"installedPatchCount": "4", "failedPatchCount": -1, "maintenanceWindowExceeded": "True"}
    )
    row = installation_row(run, SUBSCRIPTION_A)
    assert row is not None
    assert (row["installed"], row["failed"], row["maintenance_window_exceeded"]) == (
        None,
        None,
        True,
    )


def test_an_empty_assessment_answer_has_unknown_counts() -> None:
    result = assessment_result(None)
    assert result["status"] is None
    assert (result["critical"], result["security"], result["by_class"]) == (None, None, None)
    assert result["error"] is None
