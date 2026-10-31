# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Aria Automation 8 infrastructure ops (``vcfa.vra8.*``) on the shared handlers.

The handlers and the metadata are written once for VCF Automation 9 and Aria
Automation 8 (``tests/test_connectors_vcfa_infra.py`` covers them in depth).
These tests show that the vRA 8 connector registers all eleven ops with the
risk level of the Task table, that each one runs through the vRA 8 login on the
same vendor path, that a change waits for approval, that a vendor error reaches
the answer, that the project create is safe to run twice, and that the remove
has its preview. All names and ids are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_automation import _infra
from meho_backplane.connectors.vcf_automation import _lookups as lookups_module
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_IMPL_ID,
    VRA8_INFRA_OPS,
    VRA8_PRODUCT,
    VRA8_TYPED_WHEN_TO_USE_BY_GROUP,
    VRA8_VERSION,
    Vra8Connector,
    register_vra8_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.settings import get_settings

_HOST = "vra-infra.test.invalid"
_BASE_URL = f"https://{_HOST}"
_PASSWORD = "Pw-sentinel-4498"

_EXPECTED: dict[str, tuple[str, bool]] = {
    "vcfa.vra8.cloud_account.list": ("safe", False),
    "vcfa.vra8.cloud_account.create": ("dangerous", True),
    "vcfa.vra8.cloud_account.delete": ("destructive", True),
    "vcfa.vra8.zone.list": ("safe", False),
    "vcfa.vra8.zone.set": ("dangerous", True),
    "vcfa.vra8.flavor_profile.list": ("safe", False),
    "vcfa.vra8.flavor_profile.set": ("dangerous", True),
    "vcfa.vra8.network_profile.list": ("safe", False),
    "vcfa.vra8.storage_profile.list": ("safe", False),
    "vcfa.vra8.project.members.set": ("dangerous", True),
    "vcfa.vra8.project.create": ("caution", True),
}

_EMPTY_PAGE: dict[str, Any] = {"content": [], "totalElements": 0}
_PROJECT = {"id": "p-1", "name": "<project>", "members": [{"email": "<member>"}]}

# (op_id, params, method, vendor path, vendor answer)
_CASES: list[tuple[str, dict[str, Any], str, str, Any]] = [
    ("vcfa.vra8.cloud_account.list", {}, "GET", "/iaas/api/cloud-accounts", _EMPTY_PAGE),
    (
        "vcfa.vra8.cloud_account.delete",
        {"id": "ca-1"},
        "DELETE",
        "/iaas/api/cloud-accounts/ca-1",
        {"id": "t-1", "status": "INPROGRESS"},
    ),
    (
        "vcfa.vra8.zone.list",
        {},
        "GET",
        "/iaas/api/zones",
        {"content": [{"id": "z-1", "name": "<zone>"}], "totalElements": 1},
    ),
    ("vcfa.vra8.flavor_profile.list", {}, "GET", "/iaas/api/flavor-profiles", _EMPTY_PAGE),
    ("vcfa.vra8.storage_profile.list", {}, "GET", "/iaas/api/storage-profiles", _EMPTY_PAGE),
    (
        "vcfa.vra8.project.members.set",
        {"project_id": "p-1", "remove": [{"email": "<member>"}]},
        "PATCH",
        "/iaas/api/projects/p-1",
        _PROJECT,
    ),
]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )

    async def _load(_target: Any, _operator: Any, *, mount: str = "secret") -> dict[str, Any]:
        return {"password": _PASSWORD}

    monkeypatch.setattr(lookups_module, "load_vault_secret_data", _load)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=VRA8_PRODUCT, version=VRA8_VERSION, impl_id=VRA8_IMPL_ID, cls=Vra8Connector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


class _Target:
    """An Aria Automation 8.18 target (in the ``vcfa-vra8`` band ``>=8.0,<9.0``)."""

    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-000000004498")
        self.name = "vra-infra"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/vra-infra"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


_OPERATOR = Operator(
    sub="vra-infra-test",
    name="vRA Infra Test",
    email=None,
    raw_jwt="<vra-infra-jwt>",
    tenant_id=UUID("00000000-0000-0000-0000-000000004498"),
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<user>", "password": "<password>"}


async def _connector() -> Vra8Connector:
    await register_vra8_typed_operations()
    instance = get_or_create_connector_instance(Vra8Connector)
    assert isinstance(instance, Vra8Connector)
    instance._credentials_loader = _loader
    return instance


def _mount_login(mock: respx.MockRouter) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "<rt>"})
    mock.post("/iaas/api/login").respond(200, json={"token": "vra-bearer"})


async def _run(
    op_id: str, params: dict[str, Any], *, approved: bool = True, preview_hash: str | None = None
) -> Any:
    return await dispatch(
        operator=_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=params,
        _approved=approved,
        preview_hash=preview_hash,
    )


def test_each_op_has_the_risk_level_and_approval_of_the_table() -> None:
    ops = {op.op_id: (op.safety_level, op.requires_approval) for op in VRA8_INFRA_OPS}
    assert ops == _EXPECTED
    assert {op.group_key for op in VRA8_INFRA_OPS} == {"vcfa-vra8-infra"}


def test_group_text_names_every_op() -> None:
    text = VRA8_TYPED_WHEN_TO_USE_BY_GROUP["vcfa-vra8-infra"]
    for op_id in _EXPECTED:
        assert op_id in text, op_id
    assert "vcfa.tenant." not in text


def test_texts_name_only_vra8_ops() -> None:
    for op in VRA8_INFRA_OPS:
        assert "vcfa.tenant." not in json.dumps([op.llm_instructions, op.parameter_schema])


async def test_registered_rows_carry_the_risk_level_and_approval() -> None:
    await _connector()
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.impl_id == VRA8_IMPL_ID)
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    for op_id, (safety, approval) in _EXPECTED.items():
        assert by_op[op_id].source_kind == "typed", op_id
        assert (by_op[op_id].safety_level, by_op[op_id].requires_approval) == (safety, approval)


@pytest.mark.parametrize(("op_id", "params", "method", "path", "answer"), _CASES)
async def test_each_op_runs_on_the_vra8_login(
    op_id: str, params: dict[str, Any], method: str, path: str, answer: Any
) -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get("/iaas/api/projects/p-1").respond(200, json=_PROJECT)
        route = mock.request(method, path).respond(200, json=answer)
        result = await _run(op_id, params)
    assert result.status == "ok", result.error
    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer vra-bearer"
    assert request.url.params["apiVersion"] == "2021-07-15"
    assert "vcfa.tenant." not in json.dumps(result.result)


async def test_cloud_account_create_runs_with_the_vault_password() -> None:
    await _connector()
    params = {
        "type": "vsphere",
        "name": "<account-name>",
        "host_name": "<vcenter-host>",
        "username": "<username>",
        "password_secret_ref": "example/vcenter-account",
        "regions": [{"external_region_id": "Datacenter:dc-1", "name": "<datacenter>"}],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get("/iaas/api/cloud-accounts").respond(200, json=_EMPTY_PAGE)
        post = mock.post("/iaas/api/cloud-accounts-vsphere").respond(202, json={"id": "t-1"})
        result = await _run("vcfa.vra8.cloud_account.create", params)
    assert result.status == "ok", result.error
    assert result.result["status"] == "submitted"
    assert "vcfa.vra8.cloud_account.list" in result.result["guidance"]
    assert json.loads(post.calls.last.request.content)["password"] == _PASSWORD
    assert _PASSWORD not in json.dumps(result.model_dump(mode="json"))


async def test_project_create_is_safe_to_run_twice() -> None:
    await _connector()
    params = {
        "name": "<project>",
        "members": [{"email": "<member>", "type": "user"}],
        "zones": [{"zone_id": "z-1", "max_instances": 5}],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        lookup = mock.get("/iaas/api/projects").respond(200, json=_EMPTY_PAGE)
        post = mock.post("/iaas/api/projects").respond(
            201, json={"id": "p-2", "name": "<project>", "orgId": "o-1"}
        )
        created = await _run("vcfa.vra8.project.create", params)
        lookup.respond(200, json={"content": [{"id": "p-2", "name": "<project>"}]})
        again = await _run("vcfa.vra8.project.create", params)
    assert created.result["status"] == "created", created
    assert created.result["project"] == {"id": "p-2", "name": "<project>", "orgId": "o-1"}
    body = json.loads(post.calls.last.request.content)
    assert body["zoneAssignmentConfigurations"] == [{"zoneId": "z-1", "maxNumberInstances": 5}]
    assert again.result["status"] == "unchanged"
    assert "vcfa.vra8.project.members.set" in again.result["guidance"]
    assert post.call_count == 1


async def test_a_change_waits_for_approval() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        write = mock.post("/iaas/api/projects").respond(201, json={})
        result = await _run("vcfa.vra8.project.create", {"name": "<project>"}, approved=False)
    assert result.status == "awaiting_approval", result
    assert not write.called


async def test_a_vendor_error_reaches_the_answer() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get("/iaas/api/zones").respond(200, json=_EMPTY_PAGE)
        mock.post("/iaas/api/zones").respond(400, json={"message": "Region is not enabled"})
        result = await _run("vcfa.vra8.zone.set", {"name": "<zone>", "region_id": "r-1"})
    assert result.status == "error"
    assert "Region is not enabled" in json.dumps(result.model_dump(mode="json"))


async def test_the_delete_has_a_blast_radius_preview() -> None:
    assert _PREVIEW_BUILDERS["vcfa.vra8.cloud_account.delete"] is (
        _infra.cloud_account_delete_preview
    )
    connector = await _connector()
    ctx: Any = SimpleNamespace(
        connector_instance=connector, operator=_OPERATOR, target=_Target(), params={"id": "ca-1"}
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get("/iaas/api/cloud-accounts/ca-1").respond(200, json={"id": "ca-1", "name": "<a>"})
        mock.get("/iaas/api/zones").respond(
            200, json={"content": [{"id": "z-1", "name": "<zone>", "cloudAccountId": "ca-1"}]}
        )
        for path in ("flavor-profiles", "network-profiles", "storage-profiles"):
            mock.get(f"/iaas/api/{path}").respond(200, json=_EMPTY_PAGE)
        preview = await _infra.cloud_account_delete_preview(ctx)
    assert preview is not None
    assert preview["blast_radius"]["children"] == [
        {"kind": "cloud_zone", "id": "z-1", "name": "<zone>"}
    ]


async def test_the_delete_is_refused_without_a_preview_hash() -> None:
    await _connector()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        delete = mock.delete("/iaas/api/cloud-accounts/ca-1").respond(202, json={})
        result = await _run("vcfa.vra8.cloud_account.delete", {"id": "ca-1"}, approved=False)
    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    assert not delete.called
