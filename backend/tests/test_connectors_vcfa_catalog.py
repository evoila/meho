# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Catalog content source and sharing ops, and the same ops on Aria Automation 8.

The first part drives the VCF Automation 9 ids (``vcfa.tenant.catalog.*``)
through the real dispatcher against a respx-mocked appliance. The second part
drives the Aria Automation 8 ids (``vcfa.vra8.*``) to show that the ops written
once run on the 8.x connector with its own login. All names are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    Vra8Connector,
    register_vra8_typed_operations,
)
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from tests._vcfa_provisioning_support import (
    BASE_URL,
    TENANT_TOKEN,
    mount_logins,
    run,
    seed_target,
    wire_connector,
)

_SOURCES = "/catalog/api/admin/sources"
_SHARES = "/catalog/api/admin/entitlements"

_SOURCE = {
    "id": "src-1",
    "name": "<source-name>",
    "typeId": "com.vmw.blueprint",
    "projectId": "p-1",
    "config": {"sourceProjectId": "p-1"},
    "itemsFound": 2,
    "itemsImported": 2,
}
_SHARE = {
    "id": "ent-1",
    "projectId": "p-2",
    "definition": {"id": "src-1", "type": "CatalogSourceIdentifier", "name": "<source-name>"},
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


def test_catalog_api_uses_the_tenant_login() -> None:
    assert plane_for_path(_SOURCES) == "tenant"
    assert plane_for_path(f"{_SHARES}/{{id}}") == "tenant"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_source_list_is_flat_rows(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_SOURCES).respond(200, json={"content": [_SOURCE], "totalElements": 1})
        result = await run("vcfa.tenant.catalog.source.list", {"project_id": "p-2"})
    row = result["result"]["content"][0]
    assert row["sourceProjectId"] == "p-1"
    assert "config" not in row
    assert route.calls.last.request.url.params["projectId"] == "p-2"
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"


async def test_share_list_is_flat_rows(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SHARES).respond(200, json=[_SHARE])
        result = await run("vcfa.tenant.catalog.share.list", {})
    assert result["result"] == {
        "content": [
            {
                "id": "ent-1",
                "projectId": "p-2",
                "contentId": "src-1",
                "contentType": "CatalogSourceIdentifier",
                "contentName": "<source-name>",
                "sourceName": None,
                "sourceType": None,
            }
        ],
        "totalElements": 1,
    }


# ---------------------------------------------------------------------------
# catalog.source.create
# ---------------------------------------------------------------------------

_CREATE = {"name": "<source-name>", "source_project_id": "p-1"}


async def test_source_create_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_SOURCES).respond(201, json=_SOURCE)
        result = await run("vcfa.tenant.catalog.source.create", _CREATE, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not post.called


async def test_source_create_posts_a_template_source(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SOURCES).respond(200, json={"content": []})
        post = mock.post(_SOURCES).respond(201, json=_SOURCE)
        result = await run("vcfa.tenant.catalog.source.create", _CREATE)
    assert result["result"]["status"] == "created", result
    assert _body(post) == {
        "name": "<source-name>",
        "typeId": "com.vmw.blueprint",
        "config": {"sourceProjectId": "p-1"},
    }


async def test_source_create_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SOURCES).respond(200, json={"content": [_SOURCE]})
        post = mock.post(_SOURCES).respond(201, json=_SOURCE)
        result = await run("vcfa.tenant.catalog.source.create", _CREATE)
    assert result["result"]["status"] == "unchanged"
    assert not post.called


async def test_source_create_vendor_error_reaches_the_answer(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SOURCES).respond(200, json={"content": []})
        mock.post(_SOURCES).respond(400, json={"message": "Project has no templates"})
        result = await run("vcfa.tenant.catalog.source.create", _CREATE)
    assert result["status"] == "error"
    assert "Project has no templates" in (result["extras"].get("upstream_message") or "")


# ---------------------------------------------------------------------------
# catalog.share.set
# ---------------------------------------------------------------------------

_SHARE_SET = {"project_id": "p-2", "content_id": "src-1", "content_type": "source"}


async def test_share_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_SHARES).respond(201, json=_SHARE)
        result = await run(
            "vcfa.tenant.catalog.share.set", {**_SHARE_SET, "action": "share"}, approved=False
        )
    assert result["status"] == "awaiting_approval", result
    assert not post.called


async def test_share_posts_the_share(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SHARES).respond(200, json=[])
        post = mock.post(_SHARES).respond(201, json=_SHARE)
        result = await run(
            "vcfa.tenant.catalog.share.set",
            {**_SHARE_SET, "content_type": "item", "action": "share"},
        )
    assert result["result"]["status"] == "shared", result
    assert _body(post) == {
        "projectId": "p-2",
        "definition": {"id": "src-1", "type": "CatalogItemIdentifier"},
    }


async def test_share_twice_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SHARES).respond(200, json=[_SHARE])
        post = mock.post(_SHARES).respond(201, json=_SHARE)
        result = await run("vcfa.tenant.catalog.share.set", {**_SHARE_SET, "action": "share"})
    assert result["result"]["status"] == "unchanged"
    assert not post.called


async def test_unshare_deletes_the_share(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SHARES).respond(200, json=[_SHARE])
        delete = mock.delete(f"{_SHARES}/ent-1").respond(204)
        result = await run("vcfa.tenant.catalog.share.set", {**_SHARE_SET, "action": "unshare"})
    assert result["result"]["status"] == "unshared", result
    assert delete.called


async def test_unshare_without_a_share_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SHARES).respond(200, json=[])
        delete = mock.delete(f"{_SHARES}/ent-1").respond(204)
        result = await run("vcfa.tenant.catalog.share.set", {**_SHARE_SET, "action": "unshare"})
    assert result["result"]["status"] == "unchanged"
    assert not delete.called


async def test_share_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_SHARES).respond(200, json=[])
        mock.post(_SHARES).respond(
            400, json={"message": "Catalog item or source cannot be entitled"}
        )
        result = await run("vcfa.tenant.catalog.share.set", {**_SHARE_SET, "action": "share"})
    assert result["status"] == "error"
    assert "cannot be entitled" in (result["extras"].get("upstream_message") or "")


# ---------------------------------------------------------------------------
# The same ops on Aria Automation 8 (connector vcfa-vra8-8.0)
# ---------------------------------------------------------------------------

_VRA_HOST = "vra-content.test.invalid"
_VRA_BASE_URL = f"https://{_VRA_HOST}"


class _Vra8Target:
    """An Aria Automation 8.18 target; the 8.x version resolves to ``vcfa-vra8``."""

    def __init__(self) -> None:
        self.product = "vcfa"
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000044a7")
        self.name = "vra-content"
        self.host = _VRA_HOST
        self.port = 443
        self.secret_ref = "targets/vra-content"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


_VRA_OPERATOR = Operator(
    sub="vra-content-test",
    name="Aria Automation 8 content test",
    email=None,
    raw_jwt="<vra-content-raw-jwt>",
    tenant_id=UUID("00000000-0000-0000-0000-0000000044a7"),
    tenant_role=TenantRole.OPERATOR,
)


async def _vra8_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<password>"}


@pytest.fixture
async def vra8() -> AsyncIterator[Vra8Connector]:
    set_default_reducer(PassThroughReducer())
    await register_vra8_typed_operations()
    instance = get_or_create_connector_instance(Vra8Connector)
    assert isinstance(instance, Vra8Connector)
    instance._credentials_loader = _vra8_loader
    yield instance
    await instance.aclose()


async def _vra8_run(op_id: str, params: dict[str, Any], *, approved: bool = True) -> Any:
    return await dispatch(
        operator=_VRA_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=_Vra8Target(),
        params=params,
        _approved=approved,
    )


def _mount_vra8_login(mock: Any) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "<rt>"})
    mock.post("/iaas/api/login").respond(200, json={"token": "vra-bearer"})


async def test_vra8_blueprint_get_uses_the_vra8_login(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra8_login(mock)
        read = mock.get("/blueprint/api/blueprints/bp-1").respond(
            200, json={"id": "bp-1", "name": "<template-name>", "content": "yaml"}
        )
        mock.get("/blueprint/api/blueprints/bp-1/inputs-schema").respond(200, json={})
        result = await _vra8_run("vcfa.vra8.blueprint.get", {"id": "bp-1"})
    assert result.status == "ok", result.error
    assert result.result["blueprint"]["content"] == "yaml"
    request = read.calls.last.request
    assert request.headers["Authorization"] == "Bearer vra-bearer"
    assert request.headers["Accept"] == "application/json"


async def test_vra8_share_set_waits_for_approval(vra8: Vra8Connector) -> None:
    params = {**_SHARE_SET, "action": "share"}
    with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra8_login(mock)
        post = mock.post(_SHARES).respond(201, json=_SHARE)
        result = await _vra8_run("vcfa.vra8.catalog.share.set", params, approved=False)
    assert result.status == "awaiting_approval", result
    assert not post.called


async def test_vra8_release_runs_after_approval(vra8: Vra8Connector) -> None:
    params = {"id": "bp-1", "version": "1", "action": "release"}
    with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra8_login(mock)
        mock.get("/blueprint/api/blueprints/bp-1/versions").respond(200, json={"content": []})
        post = mock.post("/blueprint/api/blueprints/bp-1/versions").respond(
            201, json={"id": "v-1", "version": "1", "status": "RELEASED"}
        )
        result = await _vra8_run("vcfa.vra8.blueprint.version.release", params)
    assert result.status == "ok", result.error
    assert result.result["status"] == "created_and_released"
    assert _body(post) == {"version": "1", "release": True}
