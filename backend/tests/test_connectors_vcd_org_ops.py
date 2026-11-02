# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vCD organisation ops: read one, create, change and delete.

Drives ``vcd.org.get`` / ``.create`` / ``.update`` / ``.delete`` through the
real dispatch stack against a respx-mocked Cloud Director. No live system and
no Vault: the credential loader is a stub and the connector instance is put
into the dispatcher's cache. All names and ids are made up.

What is proven:

* each op has the risk level of the task table, and every change needs
  approval;
* the read returns the Org object from ``GET /cloudapi/1.0.0/orgs/{orgUrn}``,
  and a bad id is refused before any call;
* each change waits for approval when a person calls it, runs with the right
  request after approval, and passes a vendor error text on to the answer;
* the delete approval card (preview builder): it reads the org and fills the
  blast-radius block, and refuses a missing, enabled or non-empty org;
* the delete is refused without a matching ``preview_hash``, and the full
  flow works: preview, wait, a second person approves, then the DELETE runs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcd import (
    VCD_CONNECTOR_ID,
    VCD_IMPL_ID,
    VCD_PRODUCT,
    VCD_VERSION,
    VcloudDirectorConnector,
    register_vcd_typed_operations,
)
from meho_backplane.connectors.vcd.org_ops import VCD_ORG_GROUP_KEY, VCD_ORG_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    reset_handler_cache,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    blast_radius_missing_reason,
    build_proposed_effect,
)
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000045d9")
_TARGET_NAME = "vcd-org-ops"
_VCD_HOST = "vcd-org-ops.test.invalid"
_VCD_BASE_URL = f"https://{_VCD_HOST}"
_LOGIN_PATH = "/cloudapi/1.0.0/sessions/provider"
_TOKEN_HEADER = "X-VMWARE-VCLOUD-ACCESS-TOKEN"
_ORGS_PATH = "/cloudapi/1.0.0/orgs"
_ORG_ID = "urn:vcloud:org:11111111-2222-3333-4444-555555555555"
_ORG_PATH = f"{_ORGS_PATH}/{_ORG_ID}"
_TASK_HREF = f"{_VCD_BASE_URL}/api/task/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

#: The task table: op id -> (risk level, needs approval).
_RISK_TABLE: dict[str, tuple[str, bool]] = {
    "vcd.org.get": ("safe", False),
    "vcd.org.create": ("caution", True),
    "vcd.org.update": ("dangerous", True),
    "vcd.org.delete": ("destructive", True),
}


def _operator(sub: str = "vcd-org-requester") -> Operator:
    return Operator(
        sub=sub,
        name="vCD Org Test Operator",
        email=None,
        raw_jwt="<vcd-org-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "<provider-user>", "password": "<provider-password>"}  # NOSONAR


def _org(**overrides: Any) -> dict[str, Any]:
    org: dict[str, Any] = {
        "id": _ORG_ID,
        "name": "tenant-a",
        "displayName": "Tenant A",
        "description": "made up for tests",
        "isEnabled": False,
        "orgVdcCount": 0,
        "catalogCount": 0,
        "vappCount": 0,
        "runningVMCount": 0,
        "userCount": 3,
        "diskCount": 0,
        "canPublish": False,
    }
    return {**org, **overrides}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=VCD_PRODUCT, version=VCD_VERSION, impl_id=VCD_IMPL_ID, cls=VcloudDirectorConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
async def vcd() -> AsyncIterator[VcloudDirectorConnector]:
    set_default_reducer(PassThroughReducer())
    await register_vcd_typed_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product=VCD_PRODUCT,
                host=_VCD_HOST,
                port=443,
                fqdn=None,
                secret_ref="vcd/provider",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "10.6.2"},
                notes="seeded by test_connectors_vcd_org_ops",
            )
        )
        await session.commit()
    connector = VcloudDirectorConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[VcloudDirectorConnector] = connector
    yield connector
    await connector.aclose()


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


async def _run(op_id: str, params: dict[str, Any], *, approved: bool = True) -> dict[str, Any]:
    """Dispatch an op; ``approved`` runs it as an already-approved call."""
    result = await dispatch(
        operator=_operator(),
        connector_id=VCD_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(),
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": VCD_CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }


def _mount_login(mock: respx.MockRouter) -> None:
    mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "<provider-jwt>"})


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


@dataclass
class _FakeDescriptor:
    op_id: str


async def _card(params: dict[str, Any], connector: VcloudDirectorConnector | None) -> Any:
    """The approval card (proposed effect) the delete would wait with."""
    return await build_proposed_effect(
        PreviewContext(
            descriptor=_FakeDescriptor(op_id="vcd.org.delete"),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=_operator(),
            target=await _target(),
            params=params,
            connector_id=VCD_CONNECTOR_ID,
        )
    )


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------


def test_risk_table_matches_the_task() -> None:
    """Each op has the risk level of the task table; every change needs approval."""
    table = {op.op_id: (op.safety_level, op.requires_approval) for op in VCD_ORG_OPS}
    assert table == _RISK_TABLE
    assert {op.group_key for op in VCD_ORG_OPS} == {VCD_ORG_GROUP_KEY}


async def test_registered_rows_carry_the_risk_levels(vcd: VcloudDirectorConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_(list(_RISK_TABLE))
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id: (row.safety_level, row.requires_approval) for row in rows} == _RISK_TABLE
    assert all(row.source_kind == "typed" for row in rows)


# ---------------------------------------------------------------------------
# vcd.org.get
# ---------------------------------------------------------------------------


async def test_get_reads_one_org(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        route = mock.get(_ORG_PATH).respond(200, json=_org())
        result = await _run("vcd.org.get", {"org_id": _ORG_ID}, approved=False)
    assert result["status"] == "ok", result
    assert result["result"]["name"] == "tenant-a"
    assert result["result"]["userCount"] == 3
    request = route.calls.last.request
    assert request.headers["Accept"] == "application/json;version=38.0"
    assert request.headers["Authorization"] == "Bearer <provider-jwt>"


async def test_get_refuses_an_id_that_is_not_an_org_urn(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        route = mock.get(url__regex=r".*/orgs/.*").respond(200, json=_org())
        result = await _run("vcd.org.get", {"org_id": "../sessions"}, approved=False)
    assert result["status"] == "error", result
    assert "urn:vcloud:org:" in json.dumps(result)
    assert not route.called


# ---------------------------------------------------------------------------
# vcd.org.create
# ---------------------------------------------------------------------------


async def test_create_waits_for_approval(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        post = mock.post(_ORGS_PATH).respond(201, json=_org())
        call = await call_operation(
            _operator(),
            _args("vcd.org.create", {"name": "tenant-a", "display_name": "Tenant A"}),
        )
    assert call["status"] == "awaiting_approval", call
    assert not post.called
    assert await _approval_rows() == 1


async def test_create_posts_the_org_and_returns_it(vcd: VcloudDirectorConnector) -> None:
    created = _org(isEnabled=True)
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        post = mock.post(_ORGS_PATH).respond(201, json=created)
        result = await _run(
            "vcd.org.create",
            {"name": "tenant-a", "display_name": "Tenant A", "description": "made up"},
        )
    assert result["status"] == "ok", result
    assert result["result"] == created
    assert json.loads(post.calls.last.request.content) == {
        "name": "tenant-a",
        "displayName": "Tenant A",
        "description": "made up",
        "isEnabled": True,
    }


async def test_create_vendor_error_reaches_the_answer(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post(_ORGS_PATH).respond(
            400,
            json={
                "minorErrorCode": "DUPLICATE_NAME",
                "message": "An organization with the name tenant-a already exists.",
            },
        )
        result = await _run("vcd.org.create", {"name": "tenant-a", "display_name": "Tenant A"})
    assert result["status"] == "error", result
    assert "already exists" in json.dumps(result)


# ---------------------------------------------------------------------------
# vcd.org.update
# ---------------------------------------------------------------------------


async def test_update_waits_for_approval(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org(isEnabled=True))
        put = mock.put(_ORG_PATH).respond(200, json=_org())
        call = await call_operation(
            _operator(), _args("vcd.org.update", {"org_id": _ORG_ID, "is_enabled": False})
        )
    assert call["status"] == "awaiting_approval", call
    assert not put.called


async def test_update_needs_a_field_to_change(vcd: VcloudDirectorConnector) -> None:
    result = await _run("vcd.org.update", {"org_id": _ORG_ID})
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"


async def test_update_sends_the_whole_org_with_the_change(vcd: VcloudDirectorConnector) -> None:
    current = _org(isEnabled=True)
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=current)
        put = mock.put(_ORG_PATH).respond(200, json=_org(displayName="Tenant A2"))
        result = await _run(
            "vcd.org.update",
            {"org_id": _ORG_ID, "display_name": "Tenant A2", "is_enabled": False},
        )
    assert result["status"] == "ok", result
    assert result["result"]["displayName"] == "Tenant A2"
    sent = json.loads(put.calls.last.request.content)
    assert sent == {**current, "displayName": "Tenant A2", "isEnabled": False}


async def test_update_passes_on_the_task_link(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org())
        mock.put(_ORG_PATH).respond(202, headers={"Location": _TASK_HREF})
        result = await _run("vcd.org.update", {"org_id": _ORG_ID, "description": "new text"})
    assert result["status"] == "ok", result
    assert result["result"]["task_href"] == _TASK_HREF
    assert result["result"]["description"] == "new text"


async def test_update_vendor_error_reaches_the_answer(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org())
        mock.put(_ORG_PATH).respond(
            400, json={"minorErrorCode": "BAD_REQUEST", "message": "displayName is too long."}
        )
        result = await _run("vcd.org.update", {"org_id": _ORG_ID, "display_name": "x"})
    assert result["status"] == "error", result
    assert "displayName is too long." in json.dumps(result)


# ---------------------------------------------------------------------------
# vcd.org.delete — the approval card
# ---------------------------------------------------------------------------


async def test_delete_card_names_the_org_and_what_goes_with_it(
    vcd: VcloudDirectorConnector,
) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org())
        effect = await _card({"org_id": _ORG_ID}, vcd)
    assert effect is not None
    preview = effect["preview"]
    assert blast_radius_missing_reason(preview) is None
    block = preview["blast_radius"]
    assert block["object"] == {
        "kind": "organization",
        "id": _ORG_ID,
        "name": "tenant-a",
        "display_name": "Tenant A",
        "is_enabled": False,
    }
    assert block["children"] == [{"kind": "user", "count": 3}]
    assert block["irreversibility"] == "permanent"


async def test_delete_card_refuses_a_missing_org(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(404, json={"message": "not found"})
        effect = await _card({"org_id": _ORG_ID}, vcd)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert "found no organization" in reason


@pytest.mark.parametrize(
    ("org", "expected"),
    [
        (_org(isEnabled=True), "is enabled"),
        (_org(orgVdcCount=2, catalogCount=1), "2 VDCs, 1 catalogs"),
    ],
    ids=["enabled", "not-empty"],
)
async def test_delete_card_refuses_an_enabled_or_full_org(
    vcd: VcloudDirectorConnector, org: dict[str, Any], expected: str
) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=org)
        effect = await _card({"org_id": _ORG_ID}, vcd)
    assert effect is not None
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert expected in reason


async def test_delete_card_without_a_live_connector_declines(
    vcd: VcloudDirectorConnector,
) -> None:
    assert await _card({"org_id": _ORG_ID}, None) is None


# ---------------------------------------------------------------------------
# vcd.org.delete — the approval gate and the run
# ---------------------------------------------------------------------------


async def test_delete_without_preview_hash_is_refused(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org())
        delete = mock.delete(_ORG_PATH).respond(202, headers={"Location": _TASK_HREF})
        call = await call_operation(_operator(), _args("vcd.org.delete", {"org_id": _ORG_ID}))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not delete.called
    assert await _approval_rows() == 0


async def test_delete_of_an_enabled_org_is_refused_before_it_waits(
    vcd: VcloudDirectorConnector,
) -> None:
    args = _args("vcd.org.delete", {"org_id": _ORG_ID})
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org(isEnabled=True))
        delete = mock.delete(_ORG_PATH).respond(202)
        preview = await preview_operation(_operator(), args)
        assert preview["status"] == "ok", preview
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "is enabled" in call["extras"]["reason"]
    assert not delete.called
    assert await _approval_rows() == 0


async def test_delete_waits_for_a_second_person_then_runs(vcd: VcloudDirectorConnector) -> None:
    """Preview, wait for approval, a second person approves, then the DELETE runs once."""
    args = _args("vcd.org.delete", {"org_id": _ORG_ID})
    requester = _operator("vcd-org-requester")
    approver = _operator("vcd-org-approver")
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org())
        delete = mock.delete(_ORG_PATH).respond(202, headers={"Location": _TASK_HREF})

        preview = await preview_operation(requester, args)
        assert preview["status"] == "ok", preview
        call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        assert not delete.called
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            effect = dict(row.proposed_effect)
        assert effect["safety_level"] == "destructive"
        assert effect["blast_radius"]["object"]["name"] == "tenant-a"

        async with get_sessionmaker()() as session:
            with pytest.raises(SelfApprovalForbiddenError):
                await approve_request(session, request_id, operator=requester, params=None)

        async with get_sessionmaker()() as session:
            approved = await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        assert approved.status == "approved"

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed
    assert resumed.result == {
        "org_id": _ORG_ID,
        "name": "tenant-a",
        "delete_started": True,
        "already_absent": False,
        "task_href": _TASK_HREF,
    }
    assert delete.call_count == 1
    assert "force" not in str(delete.calls.last.request.url)


async def test_delete_run_checks_the_rules_again(vcd: VcloudDirectorConnector) -> None:
    """Enabled again after the approval: nothing is deleted."""
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org(isEnabled=True))
        delete = mock.delete(_ORG_PATH).respond(202)
        result = await _run("vcd.org.delete", {"org_id": _ORG_ID})
    assert result["status"] == "error", result
    assert "Nothing was deleted" in json.dumps(result)
    assert not delete.called


async def test_delete_of_an_org_already_gone(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(404)
        delete = mock.delete(_ORG_PATH).respond(202)
        result = await _run("vcd.org.delete", {"org_id": _ORG_ID})
    assert result["status"] == "ok", result
    assert result["result"]["already_absent"] is True
    assert result["result"]["delete_started"] is False
    assert not delete.called


async def test_delete_vendor_error_reaches_the_answer(vcd: VcloudDirectorConnector) -> None:
    with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_ORG_PATH).respond(200, json=_org())
        mock.delete(_ORG_PATH).respond(
            400,
            json={"minorErrorCode": "BAD_REQUEST", "message": "Organization has resources."},
        )
        result = await _run("vcd.org.delete", {"org_id": _ORG_ID})
    assert result["status"] == "error", result
    assert "Organization has resources." in json.dumps(result)
