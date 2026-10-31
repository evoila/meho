# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Automation integration remove and the Git repository add or remove.

Drives the ``vcfa.tenant`` ids through the real dispatcher against a
respx-mocked appliance: the "what uses it" stop, the preview of the
destructive remove, and the Git sync job (accepted, already running, a
failure part-way). All names and ids are made up.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from tests import _vcfa_integration_support as _support
from tests._vcfa_integration_support import (
    INTEGRATIONS,
    PROFILES,
    RANGES,
    SOURCES,
    SYNC_HISTORY,
    SYNC_REQUESTS,
    TRACKER,
    body,
    mount_logins_full,
    tracker,
)
from tests._vcfa_provisioning_support import OPERATOR, TARGET_NAME, resolved_target, run

# Fixtures shared through the support module.
_env_and_caches = _support._env_and_caches
vcfa = _support.vcfa


_IPAM = {"id": "int-ipam", "name": "<ipam>", "integrationType": "ipam"}
_RANGE = {
    "id": "range-1",
    "name": "<range>",
    "integrationId": "int-ipam",
    "_links": {"fabric-networks": {"hrefs": ["/iaas/api/fabric-networks/net-1"]}},
}
_PROFILE = {
    "id": "np-1",
    "name": "<network-profile>",
    "_links": {"fabric-networks": {"hrefs": ["/iaas/api/fabric-networks/net-1"]}},
}
_OTHER_PROFILE = {
    "id": "np-2",
    "name": "<other-profile>",
    "_links": {"fabric-networks": {"hrefs": ["/iaas/api/fabric-networks/net-9"]}},
}


def _mount_users(mock: respx.MockRouter) -> None:
    mock.get(f"{INTEGRATIONS}/int-ipam").respond(200, json=_IPAM)
    mock.get(RANGES).respond(200, json={"content": [_RANGE]})
    mock.get(PROFILES).respond(200, json={"content": [_PROFILE, _OTHER_PROFILE]})
    mock.get(SOURCES).respond(200, json={"content": []})


# ---------------------------------------------------------------------------
# integration.delete
# ---------------------------------------------------------------------------


async def test_remove_of_an_integration_in_use_stops(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_users(mock)
        delete = mock.delete(f"{INTEGRATIONS}/int-ipam").respond(204)
        result = await run("vcfa.tenant.integration.delete", {"id": "int-ipam"})
    data = result["result"]
    assert data["status"] == "in_use", data
    assert {(u["kind"], u["id"]) for u in data["users"]} == {
        ("ip_range", "range-1"),
        ("network_profile", "np-1"),
    }
    assert not delete.called


async def test_remove_after_the_person_confirmed(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_users(mock)
        delete = mock.delete(f"{INTEGRATIONS}/int-ipam").respond(
            202, json=tracker("t-del", "INPROGRESS")
        )
        mock.get(f"{TRACKER}/t-del").respond(200, json=tracker("t-del", "FINISHED"))
        result = await run(
            "vcfa.tenant.integration.delete", {"id": "int-ipam", "confirm_in_use": True}
        )
    data = result["result"]
    assert data["status"] == "removed", data
    assert data["tracker"]["status"] == "FINISHED"
    assert delete.calls.last.request.url.params["apiVersion"] == "2021-07-15"


async def test_remove_whose_tracker_stops_answering_lists_the_step(
    vcfa: VcfAutomationConnector,
) -> None:
    """The remove was sent; the system then stops answering at the tracker read."""
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_users(mock)
        mock.delete(f"{INTEGRATIONS}/int-ipam").respond(202, json=tracker("t-del", "INPROGRESS"))
        mock.get(f"{TRACKER}/t-del").mock(side_effect=httpx.ConnectError("no route"))
        result = await run(
            "vcfa.tenant.integration.delete", {"id": "int-ipam", "confirm_in_use": True}
        )
    data = result["result"]
    assert data["status"] == "unfinished", result
    assert data["steps"][0]["result"] == "unknown"
    assert "did not answer" in data["steps"][0]["error"]
    assert data["tracker"]["id"] == "t-del"
    assert data["tracker"]["status"] == "not_reachable"


async def test_remove_error_text_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{INTEGRATIONS}/int-ipam").respond(200, json=_IPAM)
        mock.get(RANGES).respond(200, json={"content": []})
        mock.get(SOURCES).respond(200, json={"content": []})
        mock.delete(f"{INTEGRATIONS}/int-ipam").respond(
            409, json={"message": "Integration is referenced by a cloud zone"}
        )
        result = await run("vcfa.tenant.integration.delete", {"id": "int-ipam"})
    data = result["result"]
    assert data["status"] == "failed"
    assert "referenced by a cloud zone" in data["steps"][0]["error"]


def _ctx(vcfa: VcfAutomationConnector, target: Any, params: dict[str, Any]) -> Any:
    return SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=target,
        params=params,
        descriptor=SimpleNamespace(op_id="vcfa.tenant.integration.delete"),
    )


async def test_remove_preview_fills_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.integration.delete"]
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_users(mock)
        preview = await builder(_ctx(vcfa, await resolved_target(), {"id": "int-ipam"}))
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "integration",
        "id": "int-ipam",
        "name": "<ipam>",
        "integration_type": "ipam",
    }
    assert {child["id"] for child in blast["children"]} == {"range-1", "np-1"}
    assert blast["irreversibility"] == "irreversible"


async def test_remove_preview_refuses_an_unknown_integration(
    vcfa: VcfAutomationConnector,
) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.integration.delete"]
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{INTEGRATIONS}/int-ipam").respond(404, json={"message": "Not found"})
        with pytest.raises(LookupError):
            await builder(_ctx(vcfa, await resolved_target(), {"id": "int-ipam"}))


def _call_args() -> dict[str, Any]:
    return {
        "connector_id": "vcfa-rest-9.0",
        "op_id": "vcfa.tenant.integration.delete",
        "target": TARGET_NAME,
        "params": {"id": "int-ipam"},
    }


async def test_remove_is_refused_without_a_preview_hash(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        delete = mock.delete(f"{INTEGRATIONS}/int-ipam").respond(204)
        result = await run("vcfa.tenant.integration.delete", {"id": "int-ipam"}, approved=False)
    assert result["status"] == "denied", result
    assert result["extras"]["error_code"] == "preview_binding_required"
    assert not delete.called


async def test_remove_waits_with_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_users(mock)
        delete = mock.delete(f"{INTEGRATIONS}/int-ipam").respond(204)
        preview = await preview_operation(OPERATOR, _call_args())
        assert preview["status"] == "ok", preview
        call = await call_operation(
            OPERATOR, {**_call_args(), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "awaiting_approval", call
    assert not delete.called
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(ApprovalRequest))).scalar_one()
    assert row.proposed_effect["blast_radius"]["object"]["id"] == "int-ipam"


# ---------------------------------------------------------------------------
# git_repo.set
# ---------------------------------------------------------------------------

_GIT_INTEGRATION = {"id": "int-git", "name": "<git>", "integrationType": "com.github.saas"}
_ADD = {
    "action": "add",
    "project_id": "p-1",
    "name": "<source>",
    "integration_id": "int-git",
    "repository": "example-owner/example-repo",
    "branch": "main",
    "path": "templates",
    "content_type": "BLUEPRINT",
}


def _mount_git(mock: respx.MockRouter, sources: list[dict[str, Any]] | None = None) -> None:
    mock.get(f"{INTEGRATIONS}/int-git").respond(200, json=_GIT_INTEGRATION)
    mock.get(SOURCES).respond(200, json={"content": sources or []})


async def test_git_add_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        create = mock.post(SOURCES)
        result = await run("vcfa.tenant.git_repo.set", _ADD, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not create.called


async def test_git_add_is_accepted_with_the_sync_id(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_git(mock)
        create = mock.post(SOURCES).respond(201, json={"id": "src-new"})
        mock.get(SYNC_HISTORY).respond(200, json={"content": []})
        sync = mock.post(SYNC_REQUESTS).respond(
            202, json={"requestId": "sync-1", "sourceId": "src-new", "status": "REQUESTED"}
        )
        result = await run("vcfa.tenant.git_repo.set", _ADD)
    data = result["result"]
    assert data["status"] == "accepted", data
    assert data["job"]["job_id"] == "sync-1"
    assert data["job"]["already_running"] is False
    assert data["job"]["status_read"] == {
        "op_id": "vcfa.tenant.integration.list",
        "params": {"sync_request_id": "sync-1"},
    }
    sent = body(create)
    assert sent["typeId"] == "com.github"
    assert sent["config"] == {
        "integrationId": "int-git",
        "repository": "example-owner/example-repo",
        "branch": "main",
        "path": "templates",
        "contentType": "BLUEPRINT",
    }
    assert body(sync) == {"sourceId": "src-new"}


async def test_git_add_while_a_sync_runs_starts_nothing(vcfa: VcfAutomationConnector) -> None:
    existing = {
        "id": "src-1",
        "name": "<source>",
        "typeId": "com.github",
        "projectId": "p-1",
        "config": {
            "integrationId": "int-git",
            "repository": "example-owner/example-repo",
            "branch": "main",
            "path": "templates",
            "contentType": "BLUEPRINT",
        },
    }
    history = {"content": [{"sourceId": "src-1", "requestId": "sync-run", "status": "PROCESSING"}]}
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_git(mock, [existing])
        create = mock.post(SOURCES)
        mock.get(SYNC_HISTORY).respond(200, json=history)
        sync = mock.post(SYNC_REQUESTS)
        result = await run("vcfa.tenant.git_repo.set", _ADD)
    data = result["result"]
    assert data["status"] == "accepted"
    assert data["job"] == {
        "job_id": "sync-run",
        "state": "PROCESSING",
        "already_running": True,
        "status_read": {
            "op_id": "vcfa.tenant.integration.list",
            "params": {"sync_request_id": "sync-run"},
        },
    }
    assert not create.called
    assert not sync.called


async def test_git_add_with_a_branch_that_does_not_exist(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_git(mock)
        mock.post(SOURCES).respond(400, json={"message": "Branch 'main' does not exist"})
        sync = mock.post(SYNC_REQUESTS)
        result = await run("vcfa.tenant.git_repo.set", _ADD)
    data = result["result"]
    assert data["status"] == "failed"
    assert "does not exist" in data["steps"][1]["error"]
    assert not sync.called


async def test_git_add_that_fails_at_the_sync_lists_the_steps(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_git(mock)
        mock.post(SOURCES).respond(201, json={"id": "src-new"})
        mock.get(SYNC_HISTORY).respond(200, json={"content": []})
        mock.post(SYNC_REQUESTS).respond(404, json={"message": "source not found"})
        result = await run("vcfa.tenant.git_repo.set", _ADD)
    data = result["result"]
    assert data["status"] == "failed"
    assert [s["result"] for s in data["steps"]] == ["done", "done", "done", "failed"]
    assert data["source"]["id"] == "src-new"
    assert "source not found" in data["steps"][3]["error"]


async def test_git_add_refuses_a_non_git_integration(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{INTEGRATIONS}/int-git").respond(200, json={"integrationType": "vro"})
        create = mock.post(SOURCES)
        result = await run("vcfa.tenant.git_repo.set", _ADD)
    assert result["status"] == "error"
    assert not create.called


@pytest.mark.parametrize(
    "change",
    [{"repository": "../etc"}, {"branch": "main/../x"}, {"repository": "no-owner"}],
)
async def test_git_add_refuses_bad_names(
    vcfa: VcfAutomationConnector, change: dict[str, str]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        _mount_git(mock)
        create = mock.post(SOURCES)
        result = await run("vcfa.tenant.git_repo.set", {**_ADD, **change})
    assert result["status"] == "error"
    assert not create.called


async def test_git_remove(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{SOURCES}/src-1").respond(200, json={"id": "src-1"})
        delete = mock.delete(f"{SOURCES}/src-1").respond(204)
        result = await run("vcfa.tenant.git_repo.set", {"action": "remove", "id": "src-1"})
    assert result["result"]["status"] == "removed"
    assert delete.called


async def test_git_remove_of_a_missing_source(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mount_logins_full(mock)
        mock.get(f"{SOURCES}/src-1").mock(return_value=httpx.Response(404))
        delete = mock.delete(f"{SOURCES}/src-1")
        result = await run("vcfa.tenant.git_repo.set", {"action": "remove", "id": "src-1"})
    assert result["result"]["status"] == "unchanged"
    assert not delete.called
