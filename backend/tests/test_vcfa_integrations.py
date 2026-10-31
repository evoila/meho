# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Automation integration ops: registration, the list, and the add or change.

Drives the ``vcfa.tenant`` ids through the real dispatcher against a
respx-mocked appliance. The ops are written once and registered on both
Automation connectors; ``test_connectors_vra8_integrations.py`` covers the
Aria Automation 8 side. All names, ids and secrets are made up.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vmware_shared.vcfa_integration_ops import INTEGRATION_OPS
from meho_backplane.connectors.vra8 import register_vra8_integration_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from tests import _vcfa_integration_support as _support
from tests._vcfa_integration_support import (
    INTEGRATIONS,
    SECRET_REF,
    SOURCES,
    SYNC_HISTORY,
    TIERS,
    TRACKER,
    VAULT_VALUE,
    body,
    mount_logins_full,
    no_secret,
    tracker,
)
from tests._vcfa_provisioning_support import BASE_URL, run

# Fixtures shared through the support module.
_env_and_caches = _support._env_and_caches
vault = _support.vault
vcfa = _support.vcfa


# ---------------------------------------------------------------------------
# Registration on both connectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("impl_id", "prefix", "group"),
    [
        ("vcfa-rest", "vcfa.tenant", "vcfa-tenant-integrations"),
        ("vcfa-vra8", "vcfa.vra8", "vcfa-vra8-integrations"),
    ],
)
async def test_both_connectors_register_the_five_ops(
    vcfa: VcfAutomationConnector, impl_id: str, prefix: str, group: str
) -> None:
    await register_vra8_integration_operations()
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == impl_id)
                )
            )
            .scalars()
            .all()
        )
        groups = (
            (await session.execute(select(OperationGroup).where(OperationGroup.group_key == group)))
            .scalars()
            .all()
        )
    by_id = {row.op_id: row for row in rows}
    for name, (safety, approval) in TIERS.items():
        row = by_id[f"{prefix}.{name}"]
        assert row.source_kind == "typed"
        assert row.safety_level == safety, name
        assert row.requires_approval is approval, name
        assert f"{prefix}." in str(row.llm_instructions)
        assert "{ns}" not in str(row.llm_instructions) + row.summary + row.description
    assert len(groups) == 1
    assert f"{prefix}.integration.set" in (groups[0].when_to_use or "")


def test_every_op_path_is_on_the_tenant_plane() -> None:
    assert {plane_for_path(spec.path) for spec in INTEGRATION_OPS} == {"tenant"}
    assert plane_for_path("/content/api/sources") == "tenant"
    assert plane_for_path("/provisioning/ipam/api/providers") == "tenant"


# ---------------------------------------------------------------------------
# integration.list
# ---------------------------------------------------------------------------

_VRO = {
    "id": "int-vro",
    "name": "<orchestrator>",
    "integrationType": "vro",
    "integrationProperties": {"hostName": "https://<orchestrator-host>", "privateKey": "x"},
    "customProperties": {"isExternal": "true"},
}
_IPAM = {
    "id": "int-ipam",
    "name": "<ipam>",
    "integrationType": "ipam",
    "integrationProperties": {"providerId": "prov-1", "certificate": "-----BEGIN CERT"},
}


async def test_list_returns_flat_rows_without_secrets(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        route = mock.get(INTEGRATIONS).respond(200, json={"content": [_VRO, _IPAM]})
        result = await run("vcfa.tenant.integration.list", {"integration_type": "ipam"})
    assert result["status"] == "ok", result
    data = result["result"]
    assert data["view"] == "integrations"
    assert data["total"] == 1
    assert data["rows"][0]["id"] == "int-ipam"
    assert data["rows"][0]["has_certificate"] is True
    assert "certificate" not in data["rows"][0]["integration_properties"]
    assert route.calls.last.request.url.params["apiVersion"] == "2021-07-15"


async def test_list_hides_secret_named_properties(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": [_VRO]})
        result = await run("vcfa.tenant.integration.list", {})
    row = result["result"]["rows"][0]
    assert "privateKey" not in row["integration_properties"]
    assert row["integration_properties"]["hostName"] == "https://<orchestrator-host>"


async def test_list_on_an_all_apps_organization_says_so(vcfa: VcfAutomationConnector) -> None:
    """9.x All Apps organizations serve no IaaS API; the op runs on a VM Apps organization."""
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(404, json={"message": "Not Found"})
        result = await run("vcfa.tenant.integration.list", {})
    assert result["status"] == "error"
    assert "VM Apps" in str(result["error"]) + str(result["extras"])


async def test_list_git_sources_with_their_last_sync(vcfa: VcfAutomationConnector) -> None:
    sources = {
        "content": [
            {
                "id": "src-1",
                "name": "<source>",
                "typeId": "com.github",
                "projectId": "p-1",
                "syncEnabled": True,
                "config": {
                    "integrationId": "int-git",
                    "repository": "<owner>/<repo>",
                    "branch": "main",
                    "path": "templates",
                    "contentType": "BLUEPRINT",
                },
            },
            {"id": "src-2", "name": "market", "typeId": "com.vmware.marketplace"},
        ]
    }
    history = {
        "content": [
            {"sourceId": "src-1", "requestId": "r-old", "status": "COMPLETED", "timestamp": "1"},
            {"sourceId": "src-1", "requestId": "r-new", "status": "FAILED", "timestamp": "2"},
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(SOURCES).respond(200, json=sources)
        mock.get(SYNC_HISTORY).respond(200, json=history)
        result = await run("vcfa.tenant.integration.list", {"view": "git_sources"})
    rows = result["result"]["rows"]
    assert [row["id"] for row in rows] == ["src-1"]
    assert rows[0]["last_sync"] == {"request_id": "r-new", "status": "FAILED", "at": "2"}


async def test_status_read_of_a_failed_sync(vcfa: VcfAutomationConnector) -> None:
    sync = {
        "requestId": "sync-1",
        "sourceId": "src-1",
        "status": "FAILED",
        "message": "Branch 'nope' not found",
    }
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{BASE_URL}/content/api/sourcecontrol/sync-requests/sync-1").respond(
            200, json=sync
        )
        result = await run("vcfa.tenant.integration.list", {"sync_request_id": "sync-1"})
    job = result["result"]["job"]
    assert job["state"] == "FAILED"
    assert job["done"] is True
    assert "not found" in job["message"]


async def test_status_read_logs_in_again_after_the_login_expired(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(f"{BASE_URL}/iaas/api/login").respond(200, json={"token": "t"})
        read = mock.get(f"{TRACKER}/t-1").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json=tracker("t-1", "INPROGRESS")),
            ]
        )
        result = await run("vcfa.tenant.integration.list", {"request_tracker_id": "t-1"})
    assert result["result"]["job"]["state"] == "INPROGRESS"
    assert read.call_count == 2
    assert login.call_count == 2


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("refused"), httpx.Response(503, text="upgrading")],
    ids=["no-answer", "503"],
)
async def test_status_read_of_a_system_that_does_not_answer(
    vcfa: VcfAutomationConnector, answer: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{BASE_URL}/content/api/sourcecontrol/sync-requests/sync-9").mock(
            side_effect=[answer]
        )
        result = await run("vcfa.tenant.integration.list", {"sync_request_id": "sync-9"})
    job = result["result"]["job"]
    assert job["state"] == "not_reachable"
    assert job["job_id"] == "sync-9"
    assert job["checked_at"]
    assert job["done"] is False


async def test_list_refuses_a_path_value(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        result = await run("vcfa.tenant.integration.list", {"id": ".."})
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# integration.set
# ---------------------------------------------------------------------------

_VRO_CALL = {
    "name": "<orchestrator>",
    "integration_type": "vro",
    "integration_properties": {"hostName": "https://<orchestrator-host>:443"},
    "user_name": "<user>",
    "password_secret_ref": SECRET_REF,
}

_IPAM_CALL = {
    "name": "<ipam>",
    "integration_type": "ipam",
    "user_name": "<ipam-user>",
    "password_secret_ref": SECRET_REF,
    "certificate": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",
    "integration_properties": {
        "providerId": "prov-1",
        "faasProviderEndpointId": "abx-endpoint-1",
        "hostName": "<ipam-host>",
    },
}


async def test_set_waits_for_approval(vcfa: VcfAutomationConnector, vault: Any) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        post = mock.post(INTEGRATIONS)
        result = await run("vcfa.tenant.integration.set", _VRO_CALL, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not post.called
    vault.assert_not_called()


async def test_set_tests_then_saves(vcfa: VcfAutomationConnector, vault: Any) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": []})
        post = mock.post(INTEGRATIONS).mock(
            side_effect=[
                httpx.Response(202, json=tracker("t-test", "INPROGRESS")),
                httpx.Response(202, json=tracker("t-save", "INPROGRESS")),
            ]
        )
        mock.get(f"{TRACKER}/t-test").respond(200, json=tracker("t-test", "FINISHED"))
        mock.get(f"{TRACKER}/t-save").respond(
            200,
            json=tracker("t-save", "FINISHED", resources=["/iaas/api/integrations/int-new"]),
        )
        mock.get(f"{INTEGRATIONS}/int-new").respond(200, json={**_VRO, "id": "int-new"})
        result = await run("vcfa.tenant.integration.set", _VRO_CALL)
    data = result["result"]
    assert data["status"] == "saved", data
    assert [s["result"] for s in data["steps"]] == ["done", "done", "done"]
    assert data["integration"]["id"] == "int-new"
    test_call, save_call = post.calls
    assert test_call.request.url.params["validateOnly"] == "true"
    assert "validateOnly" not in save_call.request.url.params
    sent = body(post, 0)
    assert sent["privateKey"] == VAULT_VALUE
    assert sent["privateKeyId"] == "<user>"
    assert no_secret(result)


async def test_validate_only_with_a_wrong_password_saves_nothing(
    vcfa: VcfAutomationConnector, vault: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": []})
        post = mock.post(INTEGRATIONS).respond(202, json=tracker("t-test", "INPROGRESS"))
        mock.get(f"{TRACKER}/t-test").respond(
            200,
            json=tracker("t-test", "FAILED", message=f"Login failed for user; got {VAULT_VALUE}"),
        )
        result = await run("vcfa.tenant.integration.set", _IPAM_CALL)
    data = result["result"]
    assert data["status"] == "test_failed"
    assert post.call_count == 1
    assert [s["result"] for s in data["steps"]] == ["failed", "not_run", "not_run"]
    assert no_secret(result)
    assert any("action run record" in note for note in data["notes"])


async def test_validate_only_with_a_wrong_address_reports_the_vendor_error(
    vcfa: VcfAutomationConnector, vault: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": []})
        post = mock.post(INTEGRATIONS).respond(
            400, json={"message": "Unable to connect to host <orchestrator-host>"}
        )
        result = await run("vcfa.tenant.integration.set", _VRO_CALL)
    data = result["result"]
    assert data["status"] == "test_failed"
    assert "Unable to connect" in data["steps"][0]["error"]
    assert post.call_count == 1


async def test_ipam_body_has_the_shape_the_provider_needs(
    vcfa: VcfAutomationConnector, vault: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": []})
        post = mock.post(INTEGRATIONS).respond(202, json=tracker("t-test", "FAILED"))
        await run("vcfa.tenant.integration.set", _IPAM_CALL)
    sent = body(post, 0)
    props = sent["integrationProperties"]
    assert props["faasProviderEndpointId"] == "abx-endpoint-1"
    assert props["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert "certificateInfo" not in sent
    assert sent["privateKeyId"] == "<ipam-user>"
    assert "privateKeyId" not in props
    assert props["dcId"] == "onprem"


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"integration_properties": {"providerId": "p", "endpointId": "abx"}}, "faasProvider"),
        (
            {
                "integration_properties": {
                    "providerId": "p",
                    "faasProviderEndpointId": "abx",
                    "privateKeyId": "user",
                }
            },
            "top level",
        ),
        ({"user_name": None}, "user_name"),
    ],
    ids=["endpointId", "login-inside", "no-user"],
)
async def test_ipam_shapes_that_fail_are_refused(
    vcfa: VcfAutomationConnector, vault: Any, change: dict[str, Any], text: str
) -> None:
    call = {**_IPAM_CALL, **change}
    call = {key: value for key, value in call.items() if value is not None}
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        post = mock.post(INTEGRATIONS)
        result = await run("vcfa.tenant.integration.set", call)
    assert result["result"]["status"] == "invalid_request"
    assert text in result["result"]["guidance"]
    assert not post.called


async def test_a_git_integration_with_a_wrong_token(
    vcfa: VcfAutomationConnector, vault: Any
) -> None:
    call = {
        "name": "<git>",
        "integration_type": "com.github.saas",
        "integration_properties": {"url": "https://<git-host>"},
        "password_secret_ref": SECRET_REF,
    }
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": []})
        mock.post(INTEGRATIONS).respond(202, json=tracker("t-test", "INPROGRESS"))
        mock.get(f"{TRACKER}/t-test").respond(
            200, json=tracker("t-test", "FAILED", message="401 Bad credentials")
        )
        result = await run("vcfa.tenant.integration.set", call)
    data = result["result"]
    assert data["status"] == "test_failed"
    assert data["steps"][0]["error"] == "401 Bad credentials"


async def test_set_with_a_name_that_exists_changes_nothing(
    vcfa: VcfAutomationConnector, vault: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": [_VRO]})
        post = mock.post(INTEGRATIONS)
        result = await run("vcfa.tenant.integration.set", _VRO_CALL)
    assert result["result"]["status"] == "exists"
    assert result["result"]["integration"]["id"] == "int-vro"
    assert not post.called


async def test_a_change_patches_after_the_test(vcfa: VcfAutomationConnector, vault: Any) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{INTEGRATIONS}/int-vro").respond(200, json=_VRO)
        mock.post(INTEGRATIONS).respond(202, json=tracker("t-test", "FINISHED"))
        patch = mock.patch(f"{INTEGRATIONS}/int-vro").respond(
            202, json=tracker("t-save", "FINISHED")
        )
        result = await run("vcfa.tenant.integration.set", {**_VRO_CALL, "id": "int-vro"})
    assert result["result"]["status"] == "saved", result
    sent = body(patch)
    assert "integrationType" not in sent
    assert sent["integrationProperties"]["hostName"] == "https://<orchestrator-host>:443"


async def test_a_save_that_fails_lists_the_steps(vcfa: VcfAutomationConnector, vault: Any) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(INTEGRATIONS).respond(200, json={"content": []})
        mock.post(INTEGRATIONS).mock(
            side_effect=[
                httpx.Response(202, json=tracker("t-test", "FINISHED")),
                httpx.Response(500, json={"message": "Internal error while saving"}),
            ]
        )
        result = await run("vcfa.tenant.integration.set", _VRO_CALL)
    data = result["result"]
    assert data["status"] == "failed"
    assert [s["result"] for s in data["steps"]] == ["done", "failed", "not_run"]
    assert "Internal error" in data["steps"][1]["error"]


def test_secret_is_only_a_vault_reference() -> None:
    schema = next(s for s in INTEGRATION_OPS if s.name == "integration.set").parameter_schema
    names = set(schema["properties"])
    assert "password_secret_ref" in names
    assert not names & {"password", "token", "private_key", "privateKey"}
    assert VAULT_VALUE not in str(schema)
