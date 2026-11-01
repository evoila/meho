# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Catalog items and requests on VCF Automation 9 and Aria Automation 8.

The six ops are written once (``vmware_shared.vra_catalog``) and registered
on both connectors. Each test drives an op through the real dispatcher
against a respx-mocked appliance. The VCF Automation cases use the seeded
target of ``tests/_vcfa_provisioning_support.py``; the vRA 8 cases use an
in-memory target like ``test_connectors_vra8_typed_reads.py``. All names and
ids are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vcf_automation import (
    VCFA_CATALOG_OPS,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vmware_shared import vra_catalog
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_VERSION,
    Vra8Connector,
    register_vra8_typed_operations,
)
from meho_backplane.connectors.vra8.catalog_ops import VRA8_CATALOG_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    DispatchTrace,
    DispatchTraceSpan,
    EndpointDescriptor,
    Tenant,
)
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
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
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER
from tests._vcfa_provisioning_support import (
    BASE_URL,
    TENANT_TOKEN,
    mount_logins,
    run,
    seed_target,
    wire_connector,
)

_ITEMS = "/catalog/api/items"
_DEPLOYMENTS = "/deployment/api/deployments"
_REQUESTS = "/deployment/api/requests"

#: The table of the task: op name -> (safety level, requires approval).
_TIERS: dict[str, tuple[str, bool]] = {
    "catalog.item.list": ("safe", False),
    "catalog.item.get": ("safe", False),
    "catalog.item.request": ("dangerous", True),
    "request.get": ("safe", False),
    "request.events": ("safe", False),
    "request.logs": ("safe", False),
}

#: A made-up secret, read from the stubbed Vault.
_SECRET = "Sv-sentinel-4c1e"


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
def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the Vault read of ``secret_inputs``."""
    stub = AsyncMock(return_value={"password": _SECRET})
    monkeypatch.setattr(vra_catalog, "load_vault_secret_data", stub)
    return stub


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


async def _approval_rows(op_id: str) -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(ApprovalRequest).where(ApprovalRequest.op_id == op_id))
        return list(rows.scalars().all())


def _read_the_form(mock: respx.MockRouter) -> bool:
    return any(call.request.url.path == f"{_ITEMS}/ci-1" for call in mock.calls)


# ---------------------------------------------------------------------------
# Metadata: both connectors, same table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ops", "prefix"),
    [(VCFA_CATALOG_OPS, "vcfa.tenant"), (VRA8_CATALOG_OPS, "vcfa.vra8")],
    ids=["vcfa-rest", "vcfa-vra8"],
)
def test_safety_and_approval_follow_the_table(ops: tuple[Any, ...], prefix: str) -> None:
    expected = {f"{prefix}.{name}": tier for name, tier in _TIERS.items()}
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in ops} == expected


def test_catalog_and_deployment_apis_use_the_tenant_login() -> None:
    assert {plane_for_path(op.path) for op in VCFA_CATALOG_OPS} == {"tenant"}


async def test_vcfa_ops_register_as_typed_rows(vcfa: VcfAutomationConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row for row in (await session.execute(select(EndpointDescriptor))).scalars()
        }
    for op in VCFA_CATALOG_OPS:
        row = rows[op.op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (
            op.safety_level,
            op.requires_approval,
        )


# ---------------------------------------------------------------------------
# VCF Automation: reads
# ---------------------------------------------------------------------------


async def test_item_list_returns_flat_rows(vcfa: VcfAutomationConnector) -> None:
    item = {
        "id": "ci-1",
        "name": "<item-name>",
        "type": {"id": "com.vmw.blueprint", "name": "VMware Cloud Templates"},
        "projectIds": ["p-1"],
        "sourceName": "<source-name>",
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_ITEMS).respond(200, json={"content": [item], "totalElements": 1})
        result = await run(
            "vcfa.tenant.catalog.item.list", {"project_id": "p-1", "search": "vm", "$top": 5}
        )
    out = result["result"]
    assert out["totalElements"] == 1
    assert out["items"][0]["typeId"] == "com.vmw.blueprint"
    assert out["items"][0]["projectIds"] == ["p-1"]
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"
    assert dict(request.url.params) == {"projects": "p-1", "search": "vm", "$top": "5"}


async def test_item_get_reads_the_version_form_and_hides_secret_defaults(
    vcfa: VcfAutomationConnector,
) -> None:
    schema = {
        "type": "object",
        "required": ["size"],
        "properties": {
            "size": {"type": "string", "enum": ["small", "large"], "default": "small"},
            "adminPassword": {"type": "string", "default": "<not-a-real-default>"},
            "token": {"type": "string", "encrypted": True, "default": "<not-a-real-token>"},
        },
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ITEMS}/ci-1").respond(
            200, json={"id": "ci-1", "name": "<item-name>", "schema": {"type": "object"}}
        )
        version = mock.get(f"{_ITEMS}/ci-1/versions/v%2F2").respond(
            200, json={"id": "v/2", "schema": schema}
        )
        result = await run("vcfa.tenant.catalog.item.get", {"id": "ci-1", "version_id": "v/2"})
    out = result["result"]
    assert version.called
    assert out["version"]["id"] == "v/2"
    rows = {row["name"]: row for row in out["inputs"]}
    assert rows["size"] == {
        "name": "size",
        "type": "string",
        "title": None,
        "description": None,
        "default": "small",
        "required": True,
        "enum": ["small", "large"],
        "secret": False,
    }
    assert rows["adminPassword"]["secret"] is True
    assert rows["adminPassword"]["default"] == SECRET_FIELD_MARKER
    assert rows["token"]["default"] == SECRET_FIELD_MARKER
    assert "<not-a-real" not in json.dumps(out)
    assert rows["adminPassword"]["type"] == "string"
    # The default view of every answer hides a secret-named field whole, so
    # the raw schema shows the marker there; the input rows above still name
    # the field with its type.
    assert out["schema"]["properties"]["adminPassword"] == SECRET_FIELD_MARKER
    assert out["schema"]["properties"]["size"]["type"] == "string"


async def test_item_get_hides_secrets_inside_object_and_list_defaults(
    vcfa: VcfAutomationConnector,
) -> None:
    """A secret inside an object or a list default is found by the nested field, or by name."""
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {
                    "size": {"type": "string"},
                    "pin": {"type": "string", "encrypted": True},
                },
                "default": {
                    "size": "small",
                    "pin": "<not-a-real-pin>",
                    "password": "<not-a-real-pw>",
                },
            },
            "nodes": {
                "type": "array",
                "items": {"type": "object", "properties": {"host": {"type": "string"}}},
                "default": [{"host": "n1", "apiKey": "<not-a-real-key>"}],
            },
            "handles": {
                "type": "array",
                "items": {"type": "string", "format": "password"},
                "default": ["<not-a-real-handle>"],
            },
        },
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ITEMS}/ci-1").respond(200, json={"id": "ci-1", "schema": schema})
        result = await run("vcfa.tenant.catalog.item.get", {"id": "ci-1"})
    out = result["result"]
    assert "<not-a-real" not in json.dumps(out)
    rows = {row["name"]: row for row in out["inputs"]}
    assert rows["config"]["default"] == {
        "size": "small",
        "pin": SECRET_FIELD_MARKER,
        "password": SECRET_FIELD_MARKER,
    }
    assert rows["nodes"]["default"] == [{"host": "n1", "apiKey": SECRET_FIELD_MARKER}]
    assert rows["handles"]["default"] == [SECRET_FIELD_MARKER]
    assert out["schema"]["properties"]["config"]["default"] == rows["config"]["default"]


async def test_request_get_hides_secret_inputs(vcfa: VcfAutomationConnector) -> None:
    request = {
        "id": "r-1",
        "status": "INPROGRESS",
        "inputs": {"size": "small", "password": "<not-a-real-value>"},
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_REQUESTS}/r-1").respond(200, json=request)
        result = await run("vcfa.tenant.request.get", {"request_id": "r-1"})
    out = result["result"]
    assert out["status"] == "INPROGRESS"
    assert out["inputs"] == {"size": "small", "password": SECRET_FIELD_MARKER}


async def test_request_events_are_in_order_and_name_the_last_step(
    vcfa: VcfAutomationConnector,
) -> None:
    events = [
        {"id": "e-2", "name": "Provisioning", "timestamp": "2026-01-01T10:02:00Z", "hasLogs": True},
        {"id": "e-1", "name": "Allocation", "timestamp": "2026-01-01T10:01:00Z"},
    ]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_REQUESTS}/r-1").respond(
            200, json={"id": "r-1", "status": "FAILED", "details": "no fitting zone"}
        )
        mock.get(f"{_REQUESTS}/r-1/events").respond(200, json={"content": events})
        result = await run("vcfa.tenant.request.events", {"request_id": "r-1"})
    out = result["result"]
    assert [event["id"] for event in out["events"]] == ["e-1", "e-2"]
    assert out["request_status"] == "FAILED"
    assert out["request_details"] == "no fitting zone"
    assert (out["last_event_id"], out["last_event_name"]) == ("e-2", "Provisioning")


async def test_request_logs_page_with_since_row(vcfa: VcfAutomationConnector) -> None:
    lines = [
        {"rownum": 7, "timestamp": "t", "message": "cloning"},
        {"rownum": 8, "timestamp": "t", "message": '{"password": "<not-a-real-value>"}'},
    ]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(f"{_REQUESTS}/r-1/events/e-2/logs").respond(200, json={"content": lines})
        result = await run(
            "vcfa.tenant.request.logs", {"request_id": "r-1", "event_id": "e-2", "since_row": 7}
        )
    out = result["result"]
    assert route.calls.last.request.url.params["sinceRow"] == "7"
    assert out["eof"] is False
    assert out["next_since_row"] == 9
    assert "<not-a-real-value>" not in json.dumps(out)


# ---------------------------------------------------------------------------
# VCF Automation: the request (a change)
# ---------------------------------------------------------------------------

_REQUEST_PARAMS: dict[str, Any] = {
    "id": "ci-1",
    "project_id": "p-1",
    "deployment_name": "<deployment-name>",
    "inputs": {"size": "small"},
    "secret_inputs": {"adminPassword": "<vault-path>#password"},
    "reason": "test",
}


#: The request form of item ci-1: ``bootstrap`` is a secret by the form's
#: flag only, ``config.pin`` is one inside an object, ``disks[].key`` inside
#: a list.
_FORM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "size": {"type": "string"},
        "adminPassword": {"type": "string"},
        "bootstrap": {"type": "string", "encrypted": True},
        "config": {
            "type": "object",
            "properties": {
                "zone": {"type": "string"},
                "pin": {"type": "string", "writeOnly": True},
            },
        },
        "disks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"size": {"type": "integer"}, "key": {"format": "password"}},
            },
        },
    },
}


def _mount_request(mock: respx.MockRouter, *, existing: list[dict[str, Any]] | None = None) -> Any:
    mock.get(f"{_ITEMS}/ci-1").respond(200, json={"id": "ci-1", "schema": _FORM})
    lookup = mock.get(_DEPLOYMENTS).respond(200, json={"content": existing or []})
    post = mock.post(f"{_ITEMS}/ci-1/request").respond(
        200, json=[{"deploymentId": "d-1", "deploymentName": "<deployment-name>"}]
    )
    read = mock.get(f"{_DEPLOYMENTS}/d-1").respond(
        200,
        json={"id": "d-1", "lastRequest": {"id": "r-1", "name": "Create", "status": "PENDING"}},
    )
    return lookup, post, read


async def test_request_waits_for_approval(vcfa: VcfAutomationConnector, vault: AsyncMock) -> None:
    """The form is read before the request is stored, and the stored inputs hold no secret."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, post, _ = _mount_request(mock)
        result = await run("vcfa.tenant.catalog.item.request", _REQUEST_PARAMS, approved=False)
        read_the_form = _read_the_form(mock)
    assert result["status"] == "awaiting_approval", result
    assert read_the_form
    assert not post.called
    vault.assert_not_awaited()
    (row,) = await _approval_rows("vcfa.tenant.catalog.item.request")
    assert row.params["inputs"] == {"size": "small"}
    assert row.params["secret_inputs"] == {"adminPassword": "<vault-path>#password"}


@pytest.mark.parametrize(
    ("inputs", "named", "reads_the_form"),
    [
        ({"password": "<not-a-real-value>"}, "['password']", False),
        ({"config": {"apiKey": "<not-a-real-value>"}}, "['config.apiKey']", False),
        ({"bootstrap": "<not-a-real-value>"}, "['bootstrap']", True),
        ({"disks": [{"size": 1, "key": "<not-a-real-value>"}]}, "['disks[0].key']", True),
    ],
    ids=["secret-name", "secret-name-in-object", "form-flag", "form-flag-in-list"],
)
async def test_request_with_a_plain_secret_is_refused_before_approval(
    vcfa: VcfAutomationConnector,
    vault: AsyncMock,
    inputs: dict[str, Any],
    named: str,
    reads_the_form: bool,
) -> None:
    """A plain secret, by name or by the form's flag, is never stored in the approval queue."""
    params = {**_REQUEST_PARAMS, "inputs": inputs}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup, post, _ = _mount_request(mock)
        result = await run("vcfa.tenant.catalog.item.request", params, approved=False)
        read_the_form = _read_the_form(mock)
    assert result["status"] == "denied", result
    assert result["extras"]["error_code"] == "refused_before_approval"
    assert named in result["error"]
    assert "<not-a-real-value>" not in json.dumps(result)
    assert read_the_form is reads_the_form
    assert not lookup.called and not post.called
    assert await _approval_rows("vcfa.tenant.catalog.item.request") == []
    vault.assert_not_awaited()


async def test_request_waits_for_approval_after_the_tenant_login_expired(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    """A 401 on the form read logs in again inside the connector; the request still waits."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, post, _ = _mount_request(mock)
        form = mock.get(f"{_ITEMS}/ci-1").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"id": "ci-1", "schema": _FORM}),
            ]
        )
        result = await run("vcfa.tenant.catalog.item.request", _REQUEST_PARAMS, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert form.call_count == 2
    assert not post.called
    (row,) = await _approval_rows("vcfa.tenant.catalog.item.request")
    assert row.params["inputs"] == {"size": "small"}
    vault.assert_not_awaited()


async def test_request_is_refused_before_approval_when_the_form_cannot_be_read(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    """Without the form the inputs cannot be checked, so nothing is stored (fail-closed)."""
    params = {**_REQUEST_PARAMS, "inputs": {"bootstrap": "<not-a-real-value>"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, post, _ = _mount_request(mock)
        mock.get(f"{_ITEMS}/ci-1").mock(return_value=httpx.Response(404))
        result = await run("vcfa.tenant.catalog.item.request", params, approved=False)
    assert result["status"] == "denied", result
    assert result["extras"]["error_code"] == "refused_before_approval"
    assert "call the operation again" in result["error"]
    assert "<not-a-real-value>" not in json.dumps(result)
    assert not post.called
    assert await _approval_rows("vcfa.tenant.catalog.item.request") == []
    vault.assert_not_awaited()


async def test_request_submitted_reads_the_secret_from_vault(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup, post, read = _mount_request(mock)
        result = await run("vcfa.tenant.catalog.item.request", _REQUEST_PARAMS)
    out = result["result"]
    assert out["status"] == "submitted", out
    assert out["deployment"] == {"id": "d-1", "name": "<deployment-name>", "projectId": "p-1"}
    assert out["request"] == {"id": "r-1", "name": "Create", "status": "PENDING"}
    assert dict(lookup.calls.last.request.url.params) == {
        "name": "<deployment-name>",
        "projects": "p-1",
    }
    assert _body(post) == {
        "projectId": "p-1",
        "deploymentName": "<deployment-name>",
        "inputs": {"size": "small", "adminPassword": _SECRET},
        "reason": "test",
    }
    assert post.calls.last.request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"
    assert read.calls.last.request.url.params["expand"] == "lastRequest"
    assert vault.await_args.args[0].secret_ref == "<vault-path>"
    assert _SECRET not in json.dumps(result)


async def test_request_same_name_in_the_project_is_unchanged(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    existing = [{"id": "d-9", "name": "<deployment-name>", "projectId": "p-1"}]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, post, _ = _mount_request(mock, existing=existing)
        result = await run("vcfa.tenant.catalog.item.request", _REQUEST_PARAMS)
    out = result["result"]
    assert out["status"] == "unchanged", out
    assert out["deployment"]["id"] == "d-9"
    assert not post.called
    vault.assert_not_awaited()


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"inputs": {"password": "<not-a-real-value>"}}, "['password']"),
        ({"inputs": {"adminPassword": "x"}}, "['adminPassword']"),
        ({"inputs": {"config": {"password": "<not-a-real-value>"}}}, "['config.password']"),
        (
            {"inputs": {"disks": [{"size": 1}, {"size": 2, "apiKey": "<not-a-real-value>"}]}},
            "['disks[1].apiKey']",
        ),
    ],
    ids=["secret-in-inputs", "in-both", "nested-object", "nested-list"],
)
async def test_request_refuses_a_secret_named_value_at_any_depth(
    vcfa: VcfAutomationConnector, vault: AsyncMock, change: dict[str, Any], named: str
) -> None:
    """The name check needs no vendor call: nothing is read and nothing is sent."""
    params = {**_REQUEST_PARAMS, **change}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup, post, _ = _mount_request(mock)
        result = await run("vcfa.tenant.catalog.item.request", params)
    assert result["result"]["status"] == "invalid_request", result
    assert named in result["result"]["guidance"]
    assert not lookup.called and not post.called
    assert not mock.calls.called or mock.calls.last.request.method == "POST"  # logins only
    assert "<not-a-real-value>" not in json.dumps(result)
    vault.assert_not_awaited()


@pytest.mark.parametrize(
    ("inputs", "named"),
    [
        ({"bootstrap": "<not-a-real-value>"}, "['bootstrap']"),
        ({"config": {"zone": "a", "pin": "<not-a-real-value>"}}, "['config.pin']"),
        ({"disks": [{"size": 1, "key": "<not-a-real-value>"}]}, "['disks[0].key']"),
    ],
    ids=["flagged-top-level", "flagged-in-object", "flagged-in-list"],
)
async def test_request_refuses_a_plain_value_for_a_form_secret_field(
    vcfa: VcfAutomationConnector, vault: AsyncMock, inputs: dict[str, Any], named: str
) -> None:
    """A field the form marks as a secret, with an ordinary name, needs secret_inputs too."""
    params = {**_REQUEST_PARAMS, "inputs": inputs}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup, post, _ = _mount_request(mock)
        result = await run("vcfa.tenant.catalog.item.request", params)
    assert result["result"]["status"] == "invalid_request", result
    assert named in result["result"]["guidance"]
    assert not lookup.called and not post.called
    assert "<not-a-real-value>" not in json.dumps(result)
    vault.assert_not_awaited()


async def test_request_reads_the_form_of_the_requested_version(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    """With ``version``, that version's form decides which fields are secrets."""
    params = {**_REQUEST_PARAMS, "version": "v2", "inputs": {"size": "small", "bootstrap": "x"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _, post, _ = _mount_request(mock)
        version = mock.get(f"{_ITEMS}/ci-1/versions/v2").respond(
            200, json={"id": "v2", "schema": {"type": "object", "properties": {"size": {}}}}
        )
        result = await run("vcfa.tenant.catalog.item.request", params)
    assert version.called
    assert result["result"]["status"] == "submitted", result
    assert _body(post)["version"] == "v2"
    assert _body(post)["inputs"]["bootstrap"] == "x"


async def test_request_refuses_a_reference_without_a_field(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    """The parameter schema stops it before the handler runs."""
    params = {**_REQUEST_PARAMS, "secret_inputs": {"adminPassword": "<vault-path>"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        lookup, post, _ = _mount_request(mock)
        result = await run("vcfa.tenant.catalog.item.request", params)
    assert result["status"] != "ok"
    assert not lookup.called and not post.called


@pytest.mark.parametrize("raw", ["<vault-path>", "a#b#c", "#field", "path#", "pa\nth#field", 7])
def test_parse_secret_ref_refuses_a_bad_reference(raw: Any) -> None:
    with pytest.raises(ValueError, match="reference"):
        vra_catalog.parse_secret_ref(raw)


def test_parse_secret_ref_splits_path_and_field() -> None:
    assert vra_catalog.parse_secret_ref(" <vault-path> # password ") == ("<vault-path>", "password")


async def test_request_vendor_error_reaches_the_answer_without_the_secret(
    vcfa: VcfAutomationConnector, vault: AsyncMock
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_request(mock)
        mock.post(f"{_ITEMS}/ci-1/request").respond(
            400, json={"message": f"Input adminPassword={_SECRET} is too short for size small"}
        )
        result = await run("vcfa.tenant.catalog.item.request", _REQUEST_PARAMS)
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 400
    assert "is too short for size small" in result["extras"]["upstream_message"]
    assert _SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "secret",
    ['Sv"quote-4c1e', "Sv\\back-4c1e", "Sv-tab\t4c1e"],
    ids=["quote", "backslash", "tab"],
)
async def test_request_vendor_error_hides_a_secret_the_json_text_escapes(
    vcfa: VcfAutomationConnector, vault: AsyncMock, secret: str
) -> None:
    """A JSON error body escapes the value; it is still found and replaced."""
    vault.return_value = {"password": secret}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_request(mock)
        mock.post(f"{_ITEMS}/ci-1/request").respond(
            400, json={"message": f"Input adminPassword={secret} is too short", "code": 400}
        )
        result = await run("vcfa.tenant.catalog.item.request", _REQUEST_PARAMS)
    assert result["status"] == "error"
    assert "is too short" in result["extras"]["upstream_message"]
    assert SECRET_FIELD_MARKER in result["extras"]["upstream_message"]
    text = json.dumps(result)
    assert secret not in text and json.dumps(secret)[1:-1] not in text


@pytest.mark.parametrize(
    ("text", "secrets", "expected"),
    [
        ('{"message": "bad: Sv\\"q"}', ['Sv"q'], '{"message": "bad: [REDACTED:param_name]"}'),
        (
            "plain: Sv\\\\b and Sv\\\\b",
            ["Sv\\b"],
            "plain: [REDACTED:param_name] and [REDACTED:param_name]",
        ),
        ("not json: Sv-x", ["Sv-x", ""], "not json: [REDACTED:param_name]"),
    ],
    ids=["json-escaped-quote", "text-escaped-backslash", "plain-text-empty-secret-ignored"],
)
def test_without_secrets_handles_json_and_plain_text(
    text: str, secrets: list[str], expected: str
) -> None:
    request = httpx.Request("POST", "https://vra.test/x")
    response = httpx.Response(400, text=text, request=request)
    exc = httpx.HTTPStatusError("bad", request=request, response=response)
    assert vra_catalog._without_secrets(exc, secrets).response.text == expected


# ---------------------------------------------------------------------------
# Aria Automation 8: the same ops on the vRA 8 connector
# ---------------------------------------------------------------------------

_VRA_HOST = "vra-catalog.test.invalid"
_VRA_BASE_URL = f"https://{_VRA_HOST}"


class _Vra8Target:
    """In-memory vRA 8 target; version 8.18 resolves to the vRA 8 connector."""

    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = "vra-catalog"
        self.host = _VRA_HOST
        self.port = 443
        self.secret_ref = "targets/vra-catalog"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


_VRA_OPERATOR = Operator(
    sub="vra-catalog-test",
    name="vRA Catalog Test",
    email=None,
    raw_jwt="vra.catalog.jwt",
    tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
    tenant_role=TenantRole.OPERATOR,
)


async def _vra_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "vra-svc", "password": "vra-pw"}


@pytest.fixture
async def vra8() -> AsyncIterator[Vra8Connector]:
    from meho_backplane.connectors.registry import all_connectors_v2

    set_default_reducer(PassThroughReducer())
    await register_vra8_typed_operations()
    cls = all_connectors_v2()[(VRA8_PRODUCT, VRA8_VERSION, VRA8_IMPL_ID)]
    instance = get_or_create_connector_instance(cls)
    assert isinstance(instance, Vra8Connector)
    instance._credentials_loader = _vra_loader
    yield instance
    await instance.aclose()


def _mount_vra_logins(mock: respx.MockRouter) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "rt"})
    mock.post("/iaas/api/login").respond(200, json={"token": "vra-bt"})


async def _vra_run(op_id: str, params: dict[str, Any], *, approved: bool = True) -> Any:
    return await dispatch(
        operator=_VRA_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=_Vra8Target(),
        params=params,
        _approved=approved,
    )


@pytest.mark.parametrize(
    ("op_id", "path", "params", "payload"),
    [
        ("vcfa.vra8.catalog.item.list", _ITEMS, {}, {"content": [{"id": "ci-1"}]}),
        ("vcfa.vra8.catalog.item.get", f"{_ITEMS}/ci-1", {"id": "ci-1"}, {"id": "ci-1"}),
        ("vcfa.vra8.request.get", f"{_REQUESTS}/r-1", {"request_id": "r-1"}, {"id": "r-1"}),
        (
            "vcfa.vra8.request.logs",
            f"{_REQUESTS}/r-1/events/e-1/logs",
            {"request_id": "r-1", "event_id": "e-1"},
            {"content": [{"rownum": 1, "message": "done", "eof": True}]},
        ),
    ],
    ids=["item-list", "item-get", "request-get", "request-logs"],
)
async def test_vra8_reads_use_the_vra8_bearer(
    vra8: Vra8Connector, op_id: str, path: str, params: dict[str, Any], payload: Any
) -> None:
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra_logins(mock)
        route = mock.get(path).respond(200, json=payload)
        result = await _vra_run(op_id, params)
    assert result.status == "ok", result.error
    assert route.calls.last.request.headers["Authorization"] == "Bearer vra-bt"


async def test_vra8_request_submitted(vra8: Vra8Connector, vault: AsyncMock) -> None:
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra_logins(mock)
        _, post, _ = _mount_request(mock)
        result = await _vra_run("vcfa.vra8.catalog.item.request", _REQUEST_PARAMS)
    assert result.status == "ok", result.error
    assert result.result["status"] == "submitted"
    assert result.result["request"]["id"] == "r-1"
    assert "vcfa.vra8.request.get" in result.result["guidance"]
    assert post.calls.last.request.headers["Authorization"] == "Bearer vra-bt"
    assert _body(post)["inputs"]["adminPassword"] == _SECRET


async def test_vra8_request_with_a_plain_secret_is_refused_before_approval(
    vra8: Vra8Connector, vault: AsyncMock
) -> None:
    """The vRA 8 op registers the same check before the wait for approval."""
    params = {**_REQUEST_PARAMS, "inputs": {"bootstrap": "<not-a-real-value>"}}
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra_logins(mock)
        _, post, _ = _mount_request(mock)
        result = await _vra_run("vcfa.vra8.catalog.item.request", params, approved=False)
        read_the_form = _read_the_form(mock)
    assert result.status == "denied", result.error
    assert result.extras["error_code"] == "refused_before_approval"
    assert "['bootstrap']" in result.error
    assert read_the_form
    assert not post.called
    assert await _approval_rows("vcfa.vra8.catalog.item.request") == []
    assert "<not-a-real-value>" not in json.dumps(result.model_dump(mode="json"))
    vault.assert_not_awaited()


async def test_vra8_request_waits_for_approval_after_the_cached_token_expired(
    vra8: Vra8Connector, vault: AsyncMock
) -> None:
    """An expired cached bearer does not refuse the park: the check logs in again.

    The first form read carries the stale bearer and answers 401. The check
    before the wait for approval drops the token through the connector's
    ``invalidate_session`` hook and reads the form once more with a fresh
    login. The request then waits for approval; nothing is sent.
    """
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post("/csp/gateway/am/api/login").mock(
            side_effect=[
                httpx.Response(200, json={"refresh_token": "rt-1"}),
                httpx.Response(200, json={"refresh_token": "rt-2"}),
            ]
        )
        iaas = mock.post("/iaas/api/login").mock(
            side_effect=[
                httpx.Response(200, json={"token": "bt-stale"}),
                httpx.Response(200, json={"token": "bt-fresh"}),
            ]
        )
        _, post, _ = _mount_request(mock)
        form = mock.get(f"{_ITEMS}/ci-1").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"id": "ci-1", "schema": _FORM}),
            ]
        )
        result = await _vra_run("vcfa.vra8.catalog.item.request", _REQUEST_PARAMS, approved=False)
    assert result.status == "awaiting_approval", result.error
    assert csp.call_count == 2 and iaas.call_count == 2
    assert form.call_count == 2
    assert form.calls[0].request.headers["Authorization"] == "Bearer bt-stale"
    assert form.calls[1].request.headers["Authorization"] == "Bearer bt-fresh"
    assert not post.called
    (row,) = await _approval_rows("vcfa.vra8.catalog.item.request")
    assert row.params["inputs"] == {"size": "small"}
    vault.assert_not_awaited()


async def test_vra8_request_without_a_readable_request_still_answers(
    vra8: Vra8Connector,
) -> None:
    params = {key: value for key, value in _REQUEST_PARAMS.items() if key != "secret_inputs"}
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra_logins(mock)
        _mount_request(mock)
        mock.get(f"{_DEPLOYMENTS}/d-1").mock(return_value=httpx.Response(404))
        result = await _vra_run("vcfa.vra8.catalog.item.request", params)
    assert result.status == "ok", result.error
    assert result.result["status"] == "submitted"
    assert result.result["request"] is None
    assert result.result["deployment"]["id"] == "d-1"


async def _turn_on_the_flight_recorder() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_VRA_OPERATOR.tenant_id,
                slug="vra-catalog",
                name="vra-catalog",
                flight_recorder_enabled=True,
            )
        )
        await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _spans(audit_id: UUID) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as session:
        trace = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        rows = await session.execute(
            select(DispatchTraceSpan)
            .where(DispatchTraceSpan.trace_id == trace.id)
            .order_by(DispatchTraceSpan.seq.asc())
        )
        return list(rows.scalars().all())


@pytest.mark.parametrize(
    ("status", "answer"),
    [
        (200, [{"deploymentId": "d-1", "deploymentName": "<deployment-name>"}]),
        (400, {"message": f"Input bootstrap={_SECRET} is too short"}),
    ],
    ids=["submitted", "vendor-error-echoes-the-secret"],
)
async def test_vra8_trace_holds_no_secret_input_with_an_ordinary_name(
    vra8: Vra8Connector, vault: AsyncMock, status: int, answer: Any
) -> None:
    """``bootstrap`` is a secret only by the form; the trace redaction cannot know it."""
    await _turn_on_the_flight_recorder()
    params = {**_REQUEST_PARAMS, "secret_inputs": {"bootstrap": "<vault-path>#password"}}
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra_logins(mock)
        _, post, _ = _mount_request(mock)
        post.respond(status, json=answer)
        result = await _vra_run("vcfa.vra8.catalog.item.request", params)
    assert result.status == ("ok" if status == 200 else "error"), result.error
    assert _body(post)["inputs"]["bootstrap"] == _SECRET
    assert result.audit_id is not None
    spans = await _spans(result.audit_id)
    (span,) = [s for s in spans if s.span_kind == "vendor_call" and s.name.endswith("/request")]
    assert span.attributes["body_recorded"] is True
    assert span.attributes["request_body"]["inputs"] == {
        "size": "small",
        "bootstrap": SECRET_FIELD_MARKER,
    }
    assert _SECRET not in json.dumps([[s.name, s.attributes] for s in spans], default=str)
    assert _SECRET not in json.dumps(result.result or result.extras)


#: A form whose secret fields carry defaults: by flag, by name, and inside an object.
_FORM_WITH_SECRET_DEFAULTS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "size": {"type": "string", "default": "small"},
        "bootstrap": {"type": "string", "encrypted": True, "default": _SECRET},
        "adminPassword": {"type": "string", "default": _SECRET},
        "config": {
            "type": "object",
            "properties": {"pin": {"type": "string", "writeOnly": True}},
            "default": {"zone": "a", "pin": _SECRET},
        },
    },
}


async def test_vra8_trace_holds_no_secret_default_of_the_form(vra8: Vra8Connector) -> None:
    """The form read's trace span records the form with each secret default replaced."""
    await _turn_on_the_flight_recorder()
    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        _mount_vra_logins(mock)
        mock.get(f"{_ITEMS}/ci-1").respond(
            200, json={"id": "ci-1", "schema": _FORM_WITH_SECRET_DEFAULTS}
        )
        result = await _vra_run("vcfa.vra8.catalog.item.get", {"id": "ci-1"})
    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _spans(result.audit_id)
    (span,) = [s for s in spans if s.span_kind == "vendor_call" and s.name.endswith("/ci-1")]
    assert span.attributes["body_recorded"] is True
    recorded = span.attributes["response_body"]["schema"]["properties"]
    assert recorded["size"]["default"] == "small"
    assert recorded["bootstrap"]["default"] == SECRET_FIELD_MARKER
    assert recorded["config"]["default"] == {"zone": "a", "pin": SECRET_FIELD_MARKER}
    # ``adminPassword`` is a secret by name: the shared trace redaction masks the field.
    assert _SECRET not in json.dumps(recorded["adminPassword"])
    assert _SECRET not in json.dumps([[s.name, s.attributes] for s in spans], default=str)


async def test_trace_response_body_cleans_the_form_on_both_connectors(
    vcfa: VcfAutomationConnector, vra8: Vra8Connector
) -> None:
    """Both connectors hand the trace a cleaned form; a non-JSON answer stays as it is."""
    form = httpx.Response(200, json={"id": "ci-1", "schema": _FORM_WITH_SECRET_DEFAULTS})
    text = httpx.Response(200, content=b"<html>not json</html>")
    for connector in (vcfa, vra8):
        cleaned = connector.trace_response_body(form)
        assert cleaned["schema"]["properties"]["bootstrap"]["default"] == SECRET_FIELD_MARKER
        assert _SECRET not in json.dumps(cleaned)
        assert connector.trace_response_body(text) is None
