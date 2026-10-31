# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud template ops on VCF Automation 9 and Aria Automation 8.

The ops are written once (``connectors/vmware_shared``) and registered on
both connectors. These tests drive the VCF Automation 9 ids through the real
dispatcher against a respx-mocked appliance: the wire calls, the "safe to run
twice" answers, the approval wait, the vendor error text, and the preview of
the destructive delete. ``test_connectors_vcfa_catalog.py`` covers the
catalog ops and the Aria Automation 8 connector. All names are made up.
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

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vra8 import register_vra8_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TARGET_NAME,
    TENANT_TOKEN,
    mount_logins,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_BP = "/blueprint/api/blueprints"
_BP_1 = f"{_BP}/bp-1"

#: The table of the Task: op name -> (safety level, requires approval).
TIERS: dict[str, tuple[str, bool]] = {
    "blueprint.get": ("safe", False),
    "blueprint.create": ("caution", True),
    "blueprint.update": ("dangerous", True),
    "blueprint.delete": ("destructive", True),
    "blueprint.version.list": ("safe", False),
    "blueprint.version.release": ("dangerous", True),
    "catalog.source.list": ("safe", False),
    "catalog.source.create": ("caution", True),
    "catalog.share.list": ("safe", False),
    "catalog.share.set": ("dangerous", True),
    "blueprint.validate": ("safe", False),
}

_TEMPLATE = {
    "id": "bp-1",
    "name": "<template-name>",
    "projectId": "p-1",
    "status": "DRAFT",
    "content": "formatVersion: 1\nresources: {}\n",
    "description": "old",
    "requestScopeOrg": False,
    "valid": True,
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
# Registration on both connectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("impl_id", "prefix", "groups"),
    [
        ("vcfa-rest", "vcfa.tenant", {"vcfa-tenant-templates", "vcfa-tenant-catalog"}),
        ("vcfa-vra8", "vcfa.vra8", {"vcfa-vra8-templates", "vcfa-vra8-catalog"}),
    ],
)
async def test_each_op_has_its_risk_level_on_both_connectors(
    impl_id: str, prefix: str, groups: set[str]
) -> None:
    await VcfAutomationConnector.register_typed_operations()
    await register_vra8_typed_operations()
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.impl_id == impl_id)
                )
            ).scalars()
        }
        texts = {
            g.group_key: g.when_to_use
            for g in (
                await session.execute(
                    select(OperationGroup).where(OperationGroup.group_key.in_(groups))
                )
            ).scalars()
        }
    for name, (safety, approval) in TIERS.items():
        row = rows[f"{prefix}.{name}"]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (safety, approval), name
        assert f"{prefix}.{name}" in "".join(texts.values()), name
        assert (f"{prefix}.{name}" in _PREVIEW_BUILDERS) == (safety == "destructive")
    assert set(texts) == groups
    assert "{ns}" not in "".join(texts.values())


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_blueprint_get_returns_yaml_and_inputs(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        read = mock.get(_BP_1).respond(200, json=_TEMPLATE)
        mock.get(f"{_BP_1}/inputs-schema").respond(
            200, json={"properties": {"size": {"type": "string"}}, "required": ["size"]}
        )
        result = await run("vcfa.tenant.blueprint.get", {"id": "bp-1"})
    out = result["result"]
    assert out["blueprint"]["content"] == _TEMPLATE["content"]
    assert out["blueprint"]["projectId"] == "p-1"
    assert out["inputs_schema"]["required"] == ["size"]
    assert read.calls.last.request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"


async def test_version_list_is_flat_rows_without_yaml(vcfa: VcfAutomationConnector) -> None:
    page = {
        "content": [
            {"id": "v-1", "version": "1", "status": "RELEASED", "content": "yaml"},
            {"id": "v-2", "version": "2", "status": "VERSIONED", "content": "yaml"},
        ],
        "totalElements": 2,
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(f"{_BP_1}/versions").respond(200, json=page)
        result = await run(
            "vcfa.tenant.blueprint.version.list", {"id": "bp-1", "status": "RELEASED"}
        )
    out = result["result"]
    assert out["totalElements"] == 2
    assert [row["version"] for row in out["content"]] == ["1", "2"]
    assert all("content" not in row for row in out["content"])
    assert route.calls.last.request.url.params["status"] == "RELEASED"


async def test_validate_sends_yaml_and_changes_nothing(vcfa: VcfAutomationConnector) -> None:
    answer = {"valid": False, "validationMessages": [{"type": "ERROR", "message": "bad"}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.post("/blueprint/api/blueprint-validation").respond(200, json=answer)
        result = await run(
            "vcfa.tenant.blueprint.validate", {"project_id": "p-1", "content": "x: 1"}
        )
    assert result["result"] == answer
    assert _body(route) == {"projectId": "p-1", "content": "x: 1"}


async def test_validate_needs_exactly_one_of_content_and_id(
    vcfa: VcfAutomationConnector,
) -> None:
    result = await run(
        "vcfa.tenant.blueprint.validate",
        {"project_id": "p-1", "content": "x: 1", "blueprint_id": "bp-1"},
    )
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# blueprint.create
# ---------------------------------------------------------------------------

_CREATE = {"project_id": "p-1", "name": "<template-name>", "content": "formatVersion: 1"}


async def test_create_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_BP).respond(201, json=_TEMPLATE)
        result = await run("vcfa.tenant.blueprint.create", _CREATE, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not post.called


async def test_create_posts_the_template(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup = mock.get(_BP).respond(200, json={"content": []})
        post = mock.post(_BP).respond(201, json=_TEMPLATE)
        result = await run("vcfa.tenant.blueprint.create", _CREATE)
    assert result["result"]["status"] == "created", result
    assert result["result"]["blueprint"]["id"] == "bp-1"
    assert _body(post) == {
        "name": "<template-name>",
        "projectId": "p-1",
        "content": "formatVersion: 1",
        "requestScopeOrg": False,
    }
    assert lookup.calls.last.request.url.params["projects"] == "p-1"


async def test_create_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP).respond(200, json={"content": [_TEMPLATE]})
        post = mock.post(_BP).respond(201, json=_TEMPLATE)
        result = await run("vcfa.tenant.blueprint.create", _CREATE)
    assert result["result"]["status"] == "unchanged"
    assert not post.called


async def test_create_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP).respond(200, json={"content": []})
        mock.post(_BP).respond(400, json={"message": "Project not found"})
        result = await run("vcfa.tenant.blueprint.create", _CREATE)
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 400
    assert "Project not found" in (result["extras"].get("upstream_message") or "")


# ---------------------------------------------------------------------------
# blueprint.update
# ---------------------------------------------------------------------------


async def test_update_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        put = mock.put(_BP_1).respond(200, json=_TEMPLATE)
        result = await run(
            "vcfa.tenant.blueprint.update", {"id": "bp-1", "content": "new"}, approved=False
        )
    assert result["status"] == "awaiting_approval", result
    assert not put.called


async def test_update_sends_the_whole_template_with_the_change(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=_TEMPLATE)
        put = mock.put(_BP_1).respond(200, json={**_TEMPLATE, "content": "new"})
        result = await run("vcfa.tenant.blueprint.update", {"id": "bp-1", "content": "new"})
    out = result["result"]
    assert out["status"] == "updated", result
    assert out["changed_fields"] == ["content"]
    assert _body(put) == {
        "name": "<template-name>",
        "description": "old",
        "projectId": "p-1",
        "content": "new",
        "requestScopeOrg": False,
    }


async def test_update_with_the_same_values_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=_TEMPLATE)
        put = mock.put(_BP_1).respond(200, json=_TEMPLATE)
        result = await run("vcfa.tenant.blueprint.update", {"id": "bp-1", "description": "old"})
    assert result["result"]["status"] == "unchanged"
    assert not put.called


async def test_update_needs_a_change_field(vcfa: VcfAutomationConnector) -> None:
    result = await run("vcfa.tenant.blueprint.update", {"id": "bp-1"})
    assert result["status"] == "error"


async def test_update_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=_TEMPLATE)
        mock.put(_BP_1).respond(400, json={"message": "Invalid YAML at line 3"})
        result = await run("vcfa.tenant.blueprint.update", {"id": "bp-1", "content": "x"})
    assert result["status"] == "error"
    assert "Invalid YAML at line 3" in (result["extras"].get("upstream_message") or "")


# ---------------------------------------------------------------------------
# blueprint.version.release
# ---------------------------------------------------------------------------

_RELEASE = {"id": "bp-1", "version": "2", "action": "release"}


async def test_release_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(f"{_BP_1}/versions").respond(201, json={})
        result = await run("vcfa.tenant.blueprint.version.release", _RELEASE, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not post.called


async def test_release_of_a_new_version_makes_and_releases_it(
    vcfa: VcfAutomationConnector,
) -> None:
    made = {"id": "v-2", "version": "2", "status": "RELEASED"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_BP_1}/versions").respond(200, json={"content": []})
        post = mock.post(f"{_BP_1}/versions").respond(201, json=made)
        result = await run(
            "vcfa.tenant.blueprint.version.release", {**_RELEASE, "change_log": "<text>"}
        )
    assert result["result"]["status"] == "created_and_released", result
    assert _body(post) == {"version": "2", "release": True, "changeLog": "<text>"}


async def test_release_of_an_existing_version(vcfa: VcfAutomationConnector) -> None:
    row = {"id": "v-2", "version": "2", "status": "VERSIONED"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_BP_1}/versions").respond(200, json={"content": [row]})
        release = mock.post(f"{_BP_1}/versions/2/actions/release").respond(
            200, json={**row, "status": "RELEASED"}
        )
        result = await run("vcfa.tenant.blueprint.version.release", _RELEASE)
    assert result["result"]["status"] == "released"
    assert release.called


async def test_release_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    row = {"id": "v-2", "version": "2", "status": "RELEASED"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_BP_1}/versions").respond(200, json={"content": [row]})
        release = mock.post(f"{_BP_1}/versions/2/actions/release").respond(200, json=row)
        result = await run("vcfa.tenant.blueprint.version.release", _RELEASE)
    assert result["result"]["status"] == "unchanged"
    assert not release.called


async def test_unrelease(vcfa: VcfAutomationConnector) -> None:
    row = {"id": "v-2", "version": "2", "status": "RELEASED"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_BP_1}/versions").respond(200, json={"content": [row]})
        unrelease = mock.post(f"{_BP_1}/versions/2/actions/unrelease").respond(
            200, json={**row, "status": "VERSIONED"}
        )
        result = await run(
            "vcfa.tenant.blueprint.version.release", {**_RELEASE, "action": "unrelease"}
        )
    assert result["result"]["status"] == "unreleased"
    assert result["result"]["version"]["status"] == "VERSIONED"
    assert unrelease.called


async def test_release_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_BP_1}/versions").respond(200, json={"content": []})
        mock.post(f"{_BP_1}/versions").respond(400, json={"message": "Template is invalid"})
        result = await run("vcfa.tenant.blueprint.version.release", _RELEASE)
    assert result["status"] == "error"
    assert "Template is invalid" in (result["extras"].get("upstream_message") or "")


# ---------------------------------------------------------------------------
# blueprint.delete (destructive)
# ---------------------------------------------------------------------------


async def test_delete_runs_after_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        delete = mock.delete(_BP_1).respond(204)
        result = await run("vcfa.tenant.blueprint.delete", {"id": "bp-1"})
    assert result["result"]["status"] == "deleted", result
    assert delete.called


async def test_delete_of_an_unknown_id_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.delete(_BP_1).respond(404, json={"message": "Not found"})
        result = await run("vcfa.tenant.blueprint.delete", {"id": "bp-1"})
    assert result["result"]["status"] == "unchanged"


async def test_delete_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.delete(_BP_1).respond(409, json={"message": "Template has deployments"})
        result = await run("vcfa.tenant.blueprint.delete", {"id": "bp-1"})
    assert result["status"] == "error"
    assert "Template has deployments" in (result["extras"].get("upstream_message") or "")


async def test_delete_is_refused_without_a_preview_hash(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        delete = mock.delete(_BP_1).respond(204)
        result = await run("vcfa.tenant.blueprint.delete", {"id": "bp-1"}, approved=False)
    assert result["status"] == "denied", result
    assert result["extras"]["error_code"] == "preview_binding_required"
    assert not delete.called


def _versions_page() -> dict[str, Any]:
    return {
        "content": [
            {"id": "v-1", "version": "1", "status": "RELEASED"},
            {"id": "v-2", "version": "2", "status": "VERSIONED"},
        ],
        "totalElements": 2,
    }


async def test_delete_preview_fills_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.blueprint.delete"]
    ctx: Any = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=await resolved_target(),
        params={"id": "bp-1"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=_TEMPLATE)
        mock.get(f"{_BP_1}/versions").respond(200, json=_versions_page())
        preview = await builder(ctx)
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "cloud_template",
        "id": "bp-1",
        "name": "<template-name>",
        "project_id": "p-1",
        "status": "DRAFT",
    }
    assert blast["children"] == [
        {"kind": "cloud_template_version", "version": "1", "status": "RELEASED"},
        {"kind": "cloud_template_version", "version": "2", "status": "VERSIONED"},
    ]
    assert blast["irreversibility"] == "irreversible"
    assert "1 released version(s)" in blast["note"]


async def test_delete_preview_refuses_an_unknown_template(
    vcfa: VcfAutomationConnector,
) -> None:
    builder = _PREVIEW_BUILDERS["vcfa.tenant.blueprint.delete"]
    ctx: Any = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=await resolved_target(),
        params={"id": "bp-1"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(404, json={"message": "Not found"})
        with pytest.raises(LookupError):
            await builder(ctx)


def _call_args() -> dict[str, Any]:
    return {
        "connector_id": "vcfa-rest-9.0",
        "op_id": "vcfa.tenant.blueprint.delete",
        "target": TARGET_NAME,
        "params": {"id": "bp-1"},
    }


async def test_delete_parks_with_the_blast_radius(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(200, json=_TEMPLATE)
        mock.get(f"{_BP_1}/versions").respond(200, json=_versions_page())
        delete = mock.delete(_BP_1).respond(204)
        preview = await preview_operation(OPERATOR, _call_args())
        assert preview["status"] == "ok", preview
        call = await call_operation(
            OPERATOR, {**_call_args(), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "awaiting_approval", call
    assert not delete.called
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(ApprovalRequest))).scalar_one()
    assert row.proposed_effect["blast_radius"]["object"]["id"] == "bp-1"


async def test_delete_of_an_unknown_template_is_refused(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_BP_1).respond(404, json={"message": "Not found"})
        delete = mock.delete(_BP_1).respond(204)
        preview = await preview_operation(OPERATOR, _call_args())
        call = await call_operation(
            OPERATOR, {**_call_args(), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert not delete.called
