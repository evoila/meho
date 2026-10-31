# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA provider reads for regions and organization quotas, and the op metadata.

Drives the reads through the real dispatcher against a respx-mocked
appliance (made-up answers from ``tests/_vcfa_infra_support.py``) and pins:
the risk level and approval flag of all seven ops; the path form per version
(``/cloudapi/vcf/`` on 9.0, ``/cloudapi/v1/`` on 9.1 and 9.1.1); the checks
on values that go into a URL path; the flat infra list without any echoed
password; the job state of a status read (running, failed with the vendor's
text); a new login when the session expired between start and status read;
``not_reachable`` with the job id and the time when the system does not
answer; the region detail; and the quota rows with their warnings.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import VCFA_INFRA_OPS, VcfAutomationConnector
from meho_backplane.connectors.vcf_automation._infra_paths import (
    INFRA_VDC_VM_CLASSES_PATH,
    provider_vcf_path,
    safe_segment,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from tests._vcfa_infra_support import (
    NSX_MANAGERS,
    REGION_POLICIES,
    REGION_REF,
    REGIONS,
    SUPERVISORS,
    TASK,
    TASK_ID,
    VCENTERS,
    VDC_ID,
    VDC_POLICIES,
    VDCS,
    VM_CLASSES,
    ZONES,
    nsx_manager,
    quoted,
    region,
    region_policy,
    supervisor,
    vcenter,
    vdc,
    vdc_policy,
    vm_class,
    zone,
)
from tests._vcfa_provisioning_support import (
    BASE_URL,
    PROVIDER_JWT,
    mount_logins,
    page,
    run,
    seed_target,
    wire_connector,
)

_VDC_CLASSES = f"{VDCS}/{quoted(VDC_ID)}/virtualMachineClasses"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _mount_infra(mock: respx.MockRouter) -> None:
    mock.get(VCENTERS).respond(200, json=page([vcenter()]))
    mock.get(NSX_MANAGERS).respond(200, json=page([nsx_manager()]))
    mock.get(SUPERVISORS).respond(200, json=page([supervisor(), supervisor(region=REGION_REF)]))


# ---------------------------------------------------------------------------
# Metadata, path form, path checks
# ---------------------------------------------------------------------------


def test_risk_levels_and_approval() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_INFRA_OPS} == {
        "vcfa.provider.infra.list": ("safe", False),
        "vcfa.provider.region.get": ("safe", False),
        "vcfa.provider.org_quota.list": ("safe", False),
        "vcfa.provider.vcenter.register": ("dangerous", True),
        "vcfa.provider.nsx_manager.register": ("dangerous", True),
        "vcfa.provider.region.set": ("dangerous", True),
        "vcfa.provider.org_quota.set": ("dangerous", True),
    }


async def test_ops_register_as_typed_rows_in_their_group(vcfa: VcfAutomationConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id.in_([op.op_id for op in VCFA_INFRA_OPS])
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    assert set(by_op) == {op.op_id for op in VCFA_INFRA_OPS}
    assert all(row.source_kind == "typed" for row in by_op.values())
    assert by_op["vcfa.provider.org_quota.set"].requires_approval is True


@pytest.mark.parametrize(
    ("version", "prefix"),
    [
        ("9.0", "/cloudapi/vcf/"),
        ("9.1", "/cloudapi/v1/"),
        ("9.1.1", "/cloudapi/v1/"),
        (None, "/cloudapi/vcf/"),
    ],
)
def test_path_form_follows_the_target_version(version: str | None, prefix: str) -> None:
    target = SimpleNamespace(fingerprint={"version": version} if version else None, version=None)
    assert provider_vcf_path(target, VDCS) == prefix + "virtualDatacenters"
    # Paths outside /cloudapi/vcf/ do not move.
    assert provider_vcf_path(target, VCENTERS) == VCENTERS


@pytest.mark.parametrize("bad", ["", ".", "..", "  "])
def test_url_path_values_refuse_dot_segments(bad: str) -> None:
    with pytest.raises(ValueError, match="not a valid id"):
        safe_segment(bad, "quota id")


def test_url_path_values_are_percent_encoded() -> None:
    assert safe_segment("a/b?c#d", "quota id") == "a%2Fb%3Fc%23d"
    path = INFRA_VDC_VM_CLASSES_PATH.format(vdcUrn=safe_segment(VDC_ID, "quota id"))
    assert path == _VDC_CLASSES


# ---------------------------------------------------------------------------
# vcfa.provider.infra.list
# ---------------------------------------------------------------------------


async def test_infra_list_is_one_flat_list_without_passwords(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_infra(mock)
        result = await run("vcfa.provider.infra.list", {})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["state"] == "ok" and out["reachable"] is True and out["job"] is None
    kinds = [(row["kind"], row["state"]) for row in out["items"]]
    assert kinds == [
        ("vcenter", "CONNECTED"),
        ("nsx_manager", "REALIZED"),
        ("supervisor", "free"),
        ("supervisor", "in_region"),
    ]
    assert out["items"][3]["region"] == REGION_REF
    assert "vendor-echo-sentinel" not in json.dumps(result)


@pytest.mark.parametrize(
    ("task", "state", "error"),
    [
        (
            {"status": "running", "progress": 40, "operationName": "vimServerCreate"},
            "running",
            None,
        ),
        (
            {"status": "error", "error": {"message": "vendor says the host is busy"}},
            "failed",
            "vendor says the host is busy",
        ),
        ({"status": "success", "progress": 100}, "succeeded", None),
    ],
)
async def test_status_read_gives_the_job_state(
    vcfa: VcfAutomationConnector, task: dict[str, Any], state: str, error: str | None
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_infra(mock)
        task_read = mock.get(TASK).respond(200, json=task)
        result = await run("vcfa.provider.infra.list", {"job_id": TASK_ID})
    job = result["result"]["job"]
    assert job["id"] == TASK_ID
    assert job["state"] == state
    assert job["error"] == error
    assert job["progress"] == task.get("progress")
    # The legacy task read carries the classic Accept header.
    assert "version=40.0" in task_read.calls.last.request.headers["Accept"]


async def test_status_read_logs_in_again_after_the_session_expired(
    vcfa: VcfAutomationConnector,
) -> None:
    answers = iter([httpx.Response(401), httpx.Response(200, json={"status": "running"})])
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": PROVIDER_JWT}
        )
        _mount_infra(mock)
        mock.get(TASK).mock(side_effect=lambda _request: next(answers))
        result = await run("vcfa.provider.infra.list", {"job_id": TASK_ID})
    assert result["result"]["job"]["state"] == "running"
    assert login.call_count == 2


async def test_status_read_says_not_reachable_with_job_id_and_time(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(VCENTERS).mock(side_effect=httpx.ConnectError("no route"))
        result = await run("vcfa.provider.infra.list", {"job_id": TASK_ID})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["state"] == "not_reachable"
    assert out["reachable"] is False
    assert out["job"] == {"id": TASK_ID, "state": "not_reachable"}
    assert out["checked_at"]
    assert "not a failure" in out["guidance"]


async def test_status_read_503_is_not_reachable(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(VCENTERS).respond(503, text="upgrading")
        result = await run("vcfa.provider.infra.list", {})
    assert result["result"]["state"] == "not_reachable"


# ---------------------------------------------------------------------------
# vcfa.provider.region.get
# ---------------------------------------------------------------------------


async def test_region_get_reads_zones_policies_and_vm_classes(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        regions = mock.get(REGIONS).respond(200, json=page([region()]))
        zones = mock.get(ZONES).respond(200, json=page([zone()]))
        mock.get(REGION_POLICIES).respond(200, json=page([region_policy()]))
        mock.get(VM_CLASSES).respond(200, json=page([vm_class()]))
        result = await run("vcfa.provider.region.get", {"region": "REGION-A"})
    out = result["result"]
    assert out["state"] == "ok", out
    assert out["region"]["status"] == "READY"
    assert out["region"]["storagePolicies"] == ["gold"]
    assert out["zones"][0]["name"] == "zone-a"
    assert out["storage_policies"][0]["id"] == region_policy()["id"]
    assert out["vm_classes"][0]["name"] == "best-effort-small"
    assert regions.calls.last.request.url.params["filter"] == "name==REGION-A"
    assert zones.calls.last.request.url.params["filter"] == f"region.id=={region()['id']}"


async def test_region_get_unknown_name_is_not_found(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(REGIONS).respond(200, json=page([]))
        result = await run("vcfa.provider.region.get", {"region": "region-x"})
    assert result["result"]["state"] == "not_found"
    assert result["result"]["region"] is None


# ---------------------------------------------------------------------------
# vcfa.provider.org_quota.list
# ---------------------------------------------------------------------------


async def test_org_quota_list_rows_and_warnings(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(VDCS).respond(200, json=page([vdc()]))
        mock.get(VDC_POLICIES).respond(200, json=page([vdc_policy()]))
        mock.get(_VDC_CLASSES).respond(200, json=page([]))
        result = await run("vcfa.provider.org_quota.list", {"org": "example-org"})
    rows = result["result"]["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["zones"][0]["cpu_limit_mhz"] == 10000
    assert row["storage_policies"][0]["storage_limit_mib"] == 102400
    assert row["vm_classes"] == []
    assert any("no VM classes" in warning for warning in row["warnings"])
    assert not any("no storage policy" in warning for warning in row["warnings"])


async def test_org_quota_list_filters_by_org(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(VDCS).respond(200, json=page([vdc()]))
        mock.get(VDC_POLICIES).respond(200, json=page([]))
        result = await run("vcfa.provider.org_quota.list", {"org": "other-org"})
    assert result["result"]["items"] == []
