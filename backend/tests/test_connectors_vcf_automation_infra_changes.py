# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA provider changes for regions and organization quotas.

Drives each change through the real dispatcher against a respx-mocked
appliance (made-up answers from ``tests/_vcfa_infra_support.py``) and pins,
per change: it waits for approval; the success path returns ``accepted`` with
the vendor's job id and the status read, never "done"; a vendor error reaches
the answer. Also: the vCenter certificate is trusted only with the
fingerprint the approver saw, and a half-added vCenter (``INITIAL``) is
switched on again after the trust; the NSX body always has a description; a
running job is not started twice; a ``202`` with an empty body works; a
region needs a Supervisor and a storage policy; storage requests carry the
``values`` wrapper; a quota without storage or VM classes warns; a step that
fails part-way lists what ran, what failed and what did not run; the
approved run stops when the object changed; credentials never reach the
answer, the log or the audit row.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vcf_automation import _lookups as lookups_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD
from tests._vcfa_infra_support import (
    CERT_FINGERPRINT,
    CERT_PEM,
    NSX_ID,
    NSX_MANAGERS,
    NSX_URL,
    REGION_ID,
    REGION_POLICIES,
    REGION_REF,
    REGIONS,
    RSP2_ID,
    SUPERVISORS,
    TASK_ID,
    TASK_LOCATION,
    TEST_CONNECTION,
    TRUSTED_CERTS,
    VC_ID,
    VC_URL,
    VC_UUID,
    VCENTERS,
    VDC_ID,
    VDC_POLICIES,
    VDC_SP_ID,
    VDCS,
    VM_CLASSES,
    VMC_ID,
    ZONES,
    approved_with,
    nsx_manager,
    probe,
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
    OPERATOR,
    ORG,
    PASSWORD,
    by_filter,
    mount_logins,
    page,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_VC_OP = "vcfa.provider.vcenter.register"
_NSX_OP = "vcfa.provider.nsx_manager.register"
_REGION_OP = "vcfa.provider.region.set"
_QUOTA_OP = "vcfa.provider.org_quota.set"
_VC_PARAMS = {
    "name": "vc-example",
    "url": "https://vc-example.test.invalid",
    "username": "svc-example",
    "password_secret_ref": "vcfa/vc-example",
}
_NSX_PARAMS = {
    "name": "nsx-example",
    "url": "https://nsx-example.test.invalid",
    "username": "svc-example",
    "password_secret_ref": "vcfa/nsx-example",
}
_REGION_PARAMS = {
    "name": "region-a",
    "nsx_manager": "nsx-example",
    "supervisors": ["sup-example"],
    "storage_policies": ["gold"],
}
_QUOTA_PARAMS: dict[str, Any] = {
    "org": "example-org",
    "region": "region-a",
    "name": "quota-a",
    "supervisors": ["sup-example"],
    "zones": [
        {
            "zone": "zone-a",
            "cpu_limit_mhz": 10000,
            "cpu_reservation_mhz": 0,
            "memory_limit_mib": 32768,
            "memory_reservation_mib": 0,
        }
    ],
}
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


@pytest.fixture(autouse=True)
def _vault_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the caller-identity Vault read behind ``password_secret_ref``."""

    async def _load(_target: Any, _operator: Any, *, mount: str = "secret") -> dict[str, Any]:
        return {"password": PASSWORD}

    monkeypatch.setattr(lookups_module, "load_vault_secret_data", _load)


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


async def _preview(op_id: str, params: dict[str, Any], connector: Any) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    effect = await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector,
            operator=OPERATOR,
            target=await resolved_target(),
            params=params,
            connector_id="vcfa-rest-9.0",
        )
    )
    assert effect is not None and "preview" in effect, effect
    preview: dict[str, Any] = effect["preview"]
    return preview


def _steps(result: dict[str, Any]) -> dict[str, str]:
    return {row["step"]: row["result"] for row in result["result"]["steps"]}


# ---------------------------------------------------------------------------
# Every change waits for approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (_VC_OP, _VC_PARAMS),
        (_NSX_OP, _NSX_PARAMS),
        (_REGION_OP, _REGION_PARAMS),
        (_QUOTA_OP, _QUOTA_PARAMS),
    ],
)
async def test_each_change_waits_for_approval(
    vcfa: VcfAutomationConnector, op_id: str, params: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(VCENTERS).respond(200, json=page([]))
        mock.get(NSX_MANAGERS).respond(200, json=page([nsx_manager()]))
        mock.get(SUPERVISORS).respond(200, json=page([supervisor()]))
        mock.get(REGIONS).respond(200, json=page([]))
        mock.post(TEST_CONNECTION).respond(200, json=probe("SUCCESS"))
        writes = [
            mock.post(VCENTERS).respond(202),
            mock.post(NSX_MANAGERS).respond(202),
            mock.post(REGIONS).respond(202),
            mock.post(VDCS).respond(202),
        ]
        result = await run(op_id, params, approved=False)
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not any(write.called for write in writes)


# ---------------------------------------------------------------------------
# vcfa.provider.vcenter.register
# ---------------------------------------------------------------------------


def _mount_vc(mock: respx.MockRouter, rows: list[dict[str, Any]], ssl: str) -> None:
    mount_logins(mock)
    mock.get(VCENTERS).respond(200, json=page(rows))
    mock.post(TEST_CONNECTION).respond(200, json=probe(ssl))


async def test_vcenter_untrusted_certificate_needs_an_approved_fingerprint(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_vc(mock, [], "ERROR_UNTRUSTED_CERTIFICATE")
        trust = mock.post(TRUSTED_CERTS).respond(201, json={"id": "cert-1"})
        add = mock.post(VCENTERS).respond(202, headers={"Location": TASK_LOCATION})
        result = await run(_VC_OP, _VC_PARAMS)
    out = result["result"]
    assert out["status"] == "certificate_not_trusted", out
    assert out["certificate"]["fingerprint_sha256"] == CERT_FINGERPRINT
    assert "_pem" not in out["certificate"]
    assert not trust.called and not add.called


async def test_vcenter_preview_shows_the_fingerprint_and_next_step(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_vc(mock, [], "ERROR_UNTRUSTED_CERTIFICATE")
        preview = await _preview(_VC_OP, _VC_PARAMS, vcfa)
    assert preview["would"] == "add"
    assert preview["certificate"]["fingerprint_sha256"] == CERT_FINGERPRINT
    assert preview["certificate"]["needs_trust"] is True
    assert PASSWORD not in json.dumps(preview)


async def test_vcenter_add_trusts_the_seen_certificate_and_returns_the_job(
    vcfa: VcfAutomationConnector,
) -> None:
    seen = {"would": "add", "certificate": {"fingerprint_sha256": CERT_FINGERPRINT}}
    with (
        respx.mock(base_url=BASE_URL, assert_all_called=False) as mock,
        approved_with(_VC_OP, seen),
        capture_logs() as logs,
    ):
        _mount_vc(mock, [], "ERROR_UNTRUSTED_CERTIFICATE")
        trust = mock.post(TRUSTED_CERTS).respond(201, json={"id": "cert-1"})
        add = mock.post(VCENTERS).respond(202, headers={"Location": TASK_LOCATION})
        result = await run(_VC_OP, _VC_PARAMS)
    out = result["result"]
    assert out["status"] == "accepted", out
    assert out["job"] == {
        "id": TASK_ID,
        "href": TASK_LOCATION,
        "status_read": "vcfa.provider.infra.list",
    }
    assert _steps(result) == {
        "trust_certificate": "done",
        "add": "started",
        "enable": "skipped",
        "refresh": "not_run",
    }
    assert json.loads(trust.calls.last.request.content)["certificate"] == CERT_PEM.strip()
    body = json.loads(add.calls.last.request.content)
    assert body["password"] == PASSWORD and body["isEnabled"] is True
    assert PASSWORD not in json.dumps(result)
    assert PASSWORD not in json.dumps(logs, default=str)


async def test_vcenter_other_certificate_than_approved_is_refused(
    vcfa: VcfAutomationConnector,
) -> None:
    seen = {"would": "add", "certificate": {"fingerprint_sha256": "AA:BB"}}
    with (
        respx.mock(base_url=BASE_URL, assert_all_called=False) as mock,
        approved_with(_VC_OP, seen),
    ):
        _mount_vc(mock, [], "ERROR_UNTRUSTED_CERTIFICATE")
        trust = mock.post(TRUSTED_CERTS).respond(201, json={"id": "cert-1"})
        add = mock.post(VCENTERS).respond(202)
        result = await run(_VC_OP, _VC_PARAMS)
    assert result["result"]["status"] == "precondition_failed"
    assert not trust.called and not add.called


async def test_half_added_vcenter_is_switched_on_after_the_trust(
    vcfa: VcfAutomationConnector,
) -> None:
    seen = {"would": "enable", "certificate": {"fingerprint_sha256": CERT_FINGERPRINT}}
    with (
        respx.mock(base_url=BASE_URL, assert_all_called=False) as mock,
        approved_with(_VC_OP, seen),
    ):
        _mount_vc(mock, [vcenter(state="INITIAL")], "ERROR_UNTRUSTED_CERTIFICATE")
        trust = mock.post(TRUSTED_CERTS).respond(201, json={"id": "cert-1"})
        put = mock.put(f"{VCENTERS}/{quoted(VC_ID)}").respond(
            202, headers={"Location": TASK_LOCATION}
        )
        result = await run(_VC_OP, _VC_PARAMS)
    assert result["result"]["status"] == "accepted", result
    assert _steps(result)["enable"] == "started"
    assert trust.called
    body = json.loads(put.calls.last.request.content)
    assert body["isEnabled"] is True and body["password"] == PASSWORD


async def test_connected_vcenter_is_refreshed(vcfa: VcfAutomationConnector) -> None:
    refresh_path = f"/api/admin/extension/vimServer/{VC_UUID}/action/refresh"
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_vc(mock, [vcenter()], "SUCCESS")
        refresh = mock.post(refresh_path).respond(
            202, json={"id": f"urn:vcloud:task:{TASK_ID}", "status": "queued"}
        )
        result = await run(_VC_OP, _VC_PARAMS)
    assert result["result"]["status"] == "accepted", result
    assert result["result"]["job"]["id"] == TASK_ID
    assert _steps(result)["refresh"] == "started"
    assert "version=40.0" in refresh.calls.last.request.headers["Accept"]


async def test_connecting_vcenter_starts_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_vc(mock, [vcenter(state="CONNECTING")], "SUCCESS")
        add = mock.post(VCENTERS).respond(202)
        result = await run(_VC_OP, _VC_PARAMS)
    assert result["result"]["status"] == "already_running"
    assert not add.called


async def test_vcenter_same_name_other_url_is_refused(vcfa: VcfAutomationConnector) -> None:
    other = {**_VC_PARAMS, "url": "https://vc-other.test.invalid"}
    read_secret = AsyncMock(return_value={"password": PASSWORD})
    with (
        respx.mock(base_url=BASE_URL, assert_all_called=False) as mock,
        approved_with(_VC_OP, {"would": "enable", "certificate": None}),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(lookups_module, "load_vault_secret_data", read_secret)
        _mount_vc(mock, [vcenter(state="DISCONNECTED")], "SUCCESS")
        trust = mock.post(TRUSTED_CERTS).respond(201)
        add = mock.post(VCENTERS).respond(202)
        put = mock.put(f"{VCENTERS}/{quoted(VC_ID)}").respond(202)
        probe_call = mock.post(TEST_CONNECTION)
        result = await run(_VC_OP, other)
    out = result["result"]
    assert out["status"] == "invalid_request", out
    assert VC_URL in out["guidance"] and "nothing was written" in out["guidance"]
    assert out["object"]["url"] == other["url"] and out["object"]["id"] is None
    assert not any(call.called for call in (trust, add, put, probe_call))
    read_secret.assert_not_called()


async def test_vcenter_preview_shows_the_vcenter_it_matched(
    vcfa: VcfAutomationConnector,
) -> None:
    other = {**_VC_PARAMS, "url": "https://vc-other.test.invalid"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_vc(mock, [vcenter(state="DISCONNECTED")], "SUCCESS")
        taken = await _preview(_VC_OP, other, vcfa)
        same = await _preview(_VC_OP, {**_VC_PARAMS, "url": f"{VC_URL}:443/"}, vcfa)
    assert taken["would"] == "name_taken"
    assert taken["current_url"] == VC_URL and taken["certificate"] is None
    assert same["would"] == "enable"
    assert same["current_url"] == VC_URL and same["current_state"] == "DISCONNECTED"


async def test_vcenter_add_failure_after_the_trust_lists_the_steps(
    vcfa: VcfAutomationConnector,
) -> None:
    seen = {"would": "add", "certificate": {"fingerprint_sha256": CERT_FINGERPRINT}}
    with (
        respx.mock(base_url=BASE_URL, assert_all_called=False) as mock,
        approved_with(_VC_OP, seen),
    ):
        _mount_vc(mock, [], "ERROR_UNTRUSTED_CERTIFICATE")
        mock.post(TRUSTED_CERTS).respond(201, json={"id": "cert-1"})
        mock.post(VCENTERS).respond(400, text=f"bad login for svc-example / {PASSWORD}")
        result = await run(_VC_OP, _VC_PARAMS)
    out = result["result"]
    assert out["status"] == "failed_part_way", out
    assert _steps(result) == {
        "trust_certificate": "done",
        "add": "failed",
        "enable": "not_run",
        "refresh": "not_run",
    }
    failed = next(row for row in out["steps"] if row["result"] == "failed")
    assert failed["http_status"] == 400
    assert "bad login" in failed["error"]
    assert PASSWORD not in json.dumps(result)


async def test_vcenter_credentials_stay_out_of_the_audit_row(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_vc(mock, [], "SUCCESS")
        mock.post(VCENTERS).respond(202, headers={"Location": TASK_LOCATION})
        result = await run(_VC_OP, _VC_PARAMS)
    assert result["result"]["status"] == "accepted"
    async with get_sessionmaker()() as session:
        rows = [
            row
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.payload.get("op_id") == _VC_OP
        ]
    assert rows
    assert all(PASSWORD not in json.dumps(row.payload, default=str) for row in rows)


# ---------------------------------------------------------------------------
# vcfa.provider.nsx_manager.register
# ---------------------------------------------------------------------------


async def test_nsx_add_always_sends_a_description(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(NSX_MANAGERS).respond(200, json=page([]))
        add = mock.post(NSX_MANAGERS).respond(202, headers={"Location": TASK_LOCATION})
        result = await run(_NSX_OP, _NSX_PARAMS)
    out = result["result"]
    assert out["status"] == "accepted", out
    assert out["job"]["id"] == TASK_ID
    body = json.loads(add.calls.last.request.content)
    assert body["description"] == ""
    assert body["password"] == PASSWORD
    assert PASSWORD not in json.dumps(result)


async def test_an_answer_with_the_created_object_gives_no_job_id(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(NSX_MANAGERS).respond(200, json=page([]))
        mock.post(NSX_MANAGERS).respond(
            201, json={"id": NSX_ID, "href": f"{BASE_URL}{NSX_MANAGERS}/{NSX_ID}"}
        )
        result = await run(_NSX_OP, _NSX_PARAMS)
    assert result["result"]["status"] == "accepted", result
    assert result["result"]["job"] == {
        "id": None,
        "href": None,
        "status_read": "vcfa.provider.infra.list",
    }


async def test_nsx_already_added_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(NSX_MANAGERS).respond(200, json=page([nsx_manager(status="CONFIGURING")]))
        add = mock.post(NSX_MANAGERS).respond(202)
        result = await run(_NSX_OP, _NSX_PARAMS)
    assert result["result"]["status"] == "already_running"
    assert not add.called


async def test_nsx_same_name_other_url_is_refused(vcfa: VcfAutomationConnector) -> None:
    other = {**_NSX_PARAMS, "url": "https://nsx-other.test.invalid"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(NSX_MANAGERS).respond(200, json=page([nsx_manager()]))
        add = mock.post(NSX_MANAGERS).respond(202)
        result = await run(_NSX_OP, other)
        preview = await _preview(_NSX_OP, other, vcfa)
    assert result["result"]["status"] == "invalid_request", result
    assert NSX_URL in result["result"]["guidance"]
    assert not add.called
    assert preview["would"] == "name_taken" and preview["current_url"] == NSX_URL


async def test_nsx_vendor_400_reaches_the_answer_without_vendor_text(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(NSX_MANAGERS).respond(200, json=page([]))
        mock.post(NSX_MANAGERS).respond(400, text="description is required")
        result = await run(_NSX_OP, _NSX_PARAMS)
    assert result["status"] == "error", result
    assert result["extras"]["http_status"] == 400
    # The op sends a secret, so the dispatcher keeps the vendor's own text out.
    assert result["extras"]["upstream_message"] == VENDOR_TEXT_WITHHELD
    assert PASSWORD not in json.dumps(result)


# ---------------------------------------------------------------------------
# vcfa.provider.region.set
# ---------------------------------------------------------------------------


def _mount_region(mock: respx.MockRouter, regions: list[dict[str, Any]]) -> None:
    mount_logins(mock)
    mock.get(REGIONS).respond(200, json=page(regions))
    mock.get(NSX_MANAGERS).respond(200, json=page([nsx_manager()]))
    mock.get(SUPERVISORS).respond(200, json=page([supervisor()]))


async def test_region_create_202_with_empty_body(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_region(mock, [])
        post = mock.post(REGIONS).respond(202)
        result = await run(_REGION_OP, _REGION_PARAMS)
    out = result["result"]
    assert out["status"] == "accepted", out
    assert out["job"] == {"id": None, "href": None, "status_read": "vcfa.provider.region.get"}
    assert "READY" in out["guidance"]
    body = json.loads(post.calls.last.request.content)
    assert body["nsxManager"]["id"] == nsx_manager()["id"]
    assert body["supervisors"] == [{"name": "sup-example", "id": supervisor()["supervisorId"]}]
    assert body["storagePolicies"] == ["gold"]


@pytest.mark.parametrize("empty", ["supervisors", "storage_policies"])
async def test_region_needs_a_supervisor_and_a_storage_policy(
    vcfa: VcfAutomationConnector, empty: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_region(mock, [])
        post = mock.post(REGIONS).respond(202)
        result = await run(_REGION_OP, {**_REGION_PARAMS, empty: []})
    assert result["extras"]["error_code"] == "invalid_params", result
    assert not post.called


async def test_region_vendor_400_text_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_region(mock, [])
        mock.post(REGIONS).respond(400, text="storage policy gold is not in every vCenter")
        result = await run(_REGION_OP, _REGION_PARAMS)
    assert result["status"] == "error", result
    assert result["extras"]["http_status"] == 400
    assert "not in every vCenter" in json.dumps(result)


async def test_region_with_a_running_job_starts_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_region(mock, [region(status="NOT_READY")])
        put = mock.put(f"{REGIONS}/{quoted(REGION_ID)}").respond(202)
        result = await run(_REGION_OP, {**_REGION_PARAMS, "storage_policies": ["silver"]})
    assert result["result"]["status"] == "already_running"
    assert not put.called


async def test_region_change_runs_only_on_the_state_the_approver_saw(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {**_REGION_PARAMS, "storage_policies": ["gold", "silver"]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_region(mock, [region()])
        put = mock.put(f"{REGIONS}/{quoted(REGION_ID)}").respond(
            202, headers={"Location": TASK_LOCATION}
        )
        preview = await _preview(_REGION_OP, params, vcfa)
        assert preview["would"] == "update"
        with approved_with(_REGION_OP, preview):
            result = await run(_REGION_OP, params)
        assert result["result"]["status"] == "accepted", result
        assert json.loads(put.calls.last.request.content)["storagePolicies"] == ["gold", "silver"]
        # The region changes after the preview: the run stops.
        mock.get(REGIONS).respond(200, json=page([region(policies=("bronze",))]))
        with approved_with(_REGION_OP, preview):
            stale = await run(_REGION_OP, params)
    assert stale["result"]["status"] == "precondition_failed"
    assert put.call_count == 1


async def test_region_unknown_nsx_manager_is_invalid_request(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_region(mock, [])
        post = mock.post(REGIONS).respond(202)
        result = await run(_REGION_OP, {**_REGION_PARAMS, "nsx_manager": "nsx-other"})
    assert result["result"]["status"] == "invalid_request"
    assert not post.called


# ---------------------------------------------------------------------------
# vcfa.provider.org_quota.set
# ---------------------------------------------------------------------------


def _mount_quota(mock: respx.MockRouter, vdcs: list[dict[str, Any]]) -> None:
    mount_logins(mock)
    mock.get("/cloudapi/1.0.0/orgs").mock(side_effect=by_filter({"name==example-org": [ORG]}))
    mock.get(REGIONS).respond(200, json=page([region()]))
    mock.get(SUPERVISORS).respond(200, json=page([supervisor(region=REGION_REF)]))
    mock.get(ZONES).respond(200, json=page([zone()]))
    mock.get(REGION_POLICIES).respond(
        200, json=page([region_policy(), region_policy(RSP2_ID, "silver")])
    )
    mock.get(VM_CLASSES).respond(200, json=page([vm_class()]))
    mock.get(VDCS).respond(200, json=page(vdcs))


async def test_new_quota_without_storage_or_vm_classes_warns(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [])
        post = mock.post(VDCS).respond(202, headers={"Location": TASK_LOCATION})
        result = await run(_QUOTA_OP, _QUOTA_PARAMS)
    out = result["result"]
    assert out["status"] == "accepted", out
    assert out["job"]["id"] == TASK_ID
    assert _steps(result) == {
        "quota": "started",
        "storage_policies": "skipped",
        "vm_classes": "skipped",
    }
    assert any("no storage policy" in warning for warning in out["warnings"])
    assert any("no VM classes" in warning for warning in out["warnings"])
    body = json.loads(post.calls.last.request.content)
    assert body["org"]["name"] == "example-org"
    assert body["zoneResourceAllocation"][0]["resourceAllocation"]["cpuLimitMHz"] == 10000


async def test_quota_storage_and_vm_classes_use_the_values_wrapper(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {
        **_QUOTA_PARAMS,
        "storage_policies": [
            {"name": "gold", "storage_limit_mib": 204800},
            {"name": "silver", "storage_limit_mib": 1024},
        ],
        "vm_classes": ["best-effort-small"],
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [vdc()])
        mock.get(VDC_POLICIES).respond(200, json=page([vdc_policy()]))
        mock.get(_VDC_CLASSES).respond(200, json=page([]))
        put_limit = mock.put(f"{VDC_POLICIES}/{quoted(VDC_SP_ID)}").respond(200, json={})
        add_policies = mock.post(VDC_POLICIES).respond(202, headers={"Location": TASK_LOCATION})
        put_classes = mock.put(_VDC_CLASSES).respond(200, json={"values": []})
        result = await run(_QUOTA_OP, params)
    out = result["result"]
    assert out["status"] == "accepted", out
    assert _steps(result) == {
        "quota": "skipped",
        "storage_policies": "started",
        "vm_classes": "done",
    }
    assert json.loads(put_limit.calls.last.request.content)["storageLimitMiB"] == 204800
    added = json.loads(add_policies.calls.last.request.content)
    assert list(added) == ["values"]
    assert added["values"][0]["regionStoragePolicy"]["id"] == RSP2_ID
    assert added["values"][0]["virtualDatacenter"]["id"] == VDC_ID
    assert json.loads(put_classes.calls.last.request.content) == {
        "values": [{"name": "best-effort-small", "id": VMC_ID}]
    }


async def test_quota_failure_part_way_lists_the_steps(vcfa: VcfAutomationConnector) -> None:
    params = {
        **_QUOTA_PARAMS,
        "zones": [{**_QUOTA_PARAMS["zones"][0], "cpu_limit_mhz": 20000}],
        "storage_policies": [{"name": "silver", "storage_limit_mib": 1024}],
        "vm_classes": ["best-effort-small"],
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [vdc()])
        mock.get(VDC_POLICIES).respond(200, json=page([vdc_policy()]))
        mock.get(_VDC_CLASSES).respond(200, json=page([]))
        mock.put(f"{VDCS}/{quoted(VDC_ID)}").respond(202, headers={"Location": TASK_LOCATION})
        mock.post(VDC_POLICIES).respond(400, text="storage request without values wrapper")
        put_classes = mock.put(_VDC_CLASSES).respond(200, json={})
        result = await run(_QUOTA_OP, params)
    out = result["result"]
    assert out["status"] == "failed_part_way", out
    assert _steps(result) == {
        "quota": "started",
        "storage_policies": "failed",
        "vm_classes": "not_run",
    }
    failed = next(row for row in out["steps"] if row["result"] == "failed")
    assert failed["http_status"] == 400
    assert "values wrapper" in failed["error"]
    assert next(row for row in out["steps"] if row["step"] == "quota")["job_id"] == TASK_ID
    assert not put_classes.called


async def test_quota_unchanged_writes_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [vdc()])
        mock.get(VDC_POLICIES).respond(200, json=page([vdc_policy()]))
        mock.get(_VDC_CLASSES).respond(200, json=page([vm_class()]))
        put = mock.put(f"{VDCS}/{quoted(VDC_ID)}").respond(202)
        result = await run(_QUOTA_OP, _QUOTA_PARAMS)
    assert result["result"]["status"] == "unchanged", result
    assert not put.called


async def test_quota_with_a_running_job_starts_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [vdc(status="NOT_READY")])
        put = mock.put(f"{VDCS}/{quoted(VDC_ID)}").respond(202)
        result = await run(_QUOTA_OP, _QUOTA_PARAMS)
    assert result["result"]["status"] == "already_running"
    assert not put.called


async def test_quota_change_stops_when_the_quota_changed_after_the_preview(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {**_QUOTA_PARAMS, "zones": [{**_QUOTA_PARAMS["zones"][0], "cpu_limit_mhz": 20000}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [vdc()])
        mock.get(VDC_POLICIES).respond(200, json=page([vdc_policy()]))
        mock.get(_VDC_CLASSES).respond(200, json=page([vm_class()]))
        put = mock.put(f"{VDCS}/{quoted(VDC_ID)}").respond(202)
        preview = await _preview(_QUOTA_OP, params, vcfa)
        assert preview["would"]["quota"] == "update"
        assert preview["state_hash"]
        mock.get(VDCS).respond(200, json=page([vdc(cpu_limit=15000)]))
        with approved_with(_QUOTA_OP, preview):
            result = await run(_QUOTA_OP, params)
    assert result["result"]["status"] == "precondition_failed"
    assert not put.called


async def test_quota_vendor_error_text_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [])
        mock.post(VDCS).respond(400, text="zone has not enough capacity")
        result = await run(_QUOTA_OP, _QUOTA_PARAMS)
    assert result["status"] == "error", result
    assert "not enough capacity" in json.dumps(result)


async def test_quota_unknown_zone_is_invalid_request(vcfa: VcfAutomationConnector) -> None:
    params = {**_QUOTA_PARAMS, "zones": [{**_QUOTA_PARAMS["zones"][0], "zone": "zone-x"}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_quota(mock, [])
        post = mock.post(VDCS).respond(202)
        result = await run(_QUOTA_OP, params)
    assert result["result"]["status"] == "invalid_request"
    assert "zone-x" in result["result"]["guidance"]
    assert not post.called
