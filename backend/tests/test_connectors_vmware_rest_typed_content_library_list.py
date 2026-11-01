# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The two typed content-library lists, over mocked vCenter HTTP.

* ``vmware.content_library.list`` (all libraries, the type filter, a library
  that is gone or cannot be read);
* ``vmware.content_library.item.list`` (by id and by name, a missing library,
  a long list behind a result handle).

Each list runs on the vCenter 9 connector and on the vCenter 8 connector:
both send the same ``/api`` calls. Saved answers use made-up names and ids.
The pinned-spec check of the paths is in
``test_connectors_vmware_rest_library_export_reconcile.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRest80Connector,
    VmwareRestConnector,
    VsphereTargetLike,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_content_library_list import (
    CONTENT_LIBRARY_LIST_OPS,
    VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP,
    VMWARE_CONTENT_LIBRARY_LIST_OP,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_BASE = "https://vc.example.test"
_LIB = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
_SUB_LIB = "7a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"
_GONE_LIB = "8b3c4d5e-6f70-4b2c-9d3e-4f5a6b7c8d9e"
_ITEM = "1e2d3c4b-5a69-4788-9a0b-1c2d3e4f5a6b"
_ITEM2 = "2e3d4c5b-0000-4000-8000-000000000003"


def _operator() -> Operator:
    return Operator(
        sub="op-content-library-list",
        name="Content Library List Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c1c2"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None
    version: str | None = None


@pytest.fixture(params=[VmwareRestConnector, VmwareRest80Connector], ids=["9.0", "8.0"])
async def connector(request: pytest.FixtureRequest) -> AsyncIterator[VmwareRestConnector]:
    conn = request.param(session_loader=_stub_loader)
    yield conn
    conn._session_tokens.clear()
    for client in conn._clients.values():
        await client.aclose()
    conn._clients.clear()


@pytest.fixture
def vc(connector: VmwareRestConnector) -> Iterator[respx.MockRouter]:
    version = "9.0.0" if connector.version == "9.0" else "8.0.3"
    with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(200, json="tok")
        mock.get("/api/about").respond(200, json={"version": version})
        yield mock


def _validate(op: Any, out: dict[str, Any]) -> None:
    Draft202012Validator(op.response_schema).validate(out)


_LOCAL: dict[str, Any] = {
    "id": _LIB,
    "name": "<local-library>",
    "type": "LOCAL",
    "last_modified_time": "2026-10-01T10:00:00.000Z",
    "storage_backings": [{"type": "DATASTORE", "datastore_id": "datastore-11"}],
    "publish_info": {
        "published": True,
        "authentication_method": "BASIC",
        "user_name": "vcsp",
        "password": "should-never-show",
    },
}
_SUBSCRIBED: dict[str, Any] = {
    "id": _SUB_LIB,
    "name": "<subscribed-library>",
    "type": "SUBSCRIBED",
    "last_sync_time": "2026-10-20T08:00:00.000Z",
    "storage_backings": [{"type": "DATASTORE", "datastore_id": "datastore-12"}],
    "subscription_info": {"password": "should-never-show"},
}


def _item(item_id: str, name: str, item_type: str = "ovf") -> dict[str, Any]:
    return {
        "id": item_id,
        "library_id": _LIB,
        "name": name,
        "type": item_type,
        "size": 2048,
        "content_version": "3",
        "cached": True,
        "last_modified_time": "2026-10-02T10:00:00.000Z",
    }


# ---------------------------------------------------------------------------
# vmware.content_library.list
# ---------------------------------------------------------------------------


async def test_list_returns_one_flat_row_per_library(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get("/api/content/library").respond(200, json=[_LIB, _SUB_LIB, _GONE_LIB])
    vc.get(f"/api/content/library/{_LIB}").respond(200, json=_LOCAL)
    vc.get(f"/api/content/library/{_SUB_LIB}").respond(200, json=_SUBSCRIBED)
    vc.get(f"/api/content/library/{_GONE_LIB}").respond(404, json={"error_type": "NOT_FOUND"})
    vc.get("/api/content/library/item", params={"library_id": _LIB}).respond(
        200, json=[_ITEM, _ITEM2]
    )
    vc.get("/api/content/library/item", params={"library_id": _SUB_LIB}).respond(200, json=[])

    out = await connector.content_library_list(_operator(), _StubTarget(), {})

    assert out["library_count"] == 2
    local, subscribed = out["libraries"]
    assert local == {
        "library_id": _LIB,
        "name": "<local-library>",
        "type": "LOCAL",
        "datastore_ids": ["datastore-11"],
        "published": True,
        "item_count": 2,
        "last_sync_time": None,
        "last_modified_time": "2026-10-01T10:00:00.000Z",
        "read_note": None,
    }
    assert subscribed["type"] == "SUBSCRIBED"
    assert subscribed["published"] is None
    assert subscribed["item_count"] == 0
    assert subscribed["last_sync_time"] == "2026-10-20T08:00:00.000Z"
    assert "should-never-show" not in json.dumps(out)
    _validate(VMWARE_CONTENT_LIBRARY_LIST_OP, out)


async def test_list_filters_by_type(connector: VmwareRestConnector, vc: respx.MockRouter) -> None:
    vc.get("/api/content/library").respond(200, json=[_LIB, _SUB_LIB])
    vc.get(f"/api/content/library/{_LIB}").respond(200, json=_LOCAL)
    vc.get(f"/api/content/library/{_SUB_LIB}").respond(200, json=_SUBSCRIBED)
    vc.get("/api/content/library/item").respond(200, json=[])

    out = await connector.content_library_list(
        _operator(), _StubTarget(), {"library_type": "SUBSCRIBED"}
    )

    assert [row["library_id"] for row in out["libraries"]] == [_SUB_LIB]


async def test_a_library_that_fails_to_read_gets_a_note(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get("/api/content/library").respond(200, json=[_LIB, "not-a-uuid"])
    vc.get(f"/api/content/library/{_LIB}").respond(500, json={"error_type": "ERROR"})

    out = await connector.content_library_list(_operator(), _StubTarget(), {})

    first, second = out["libraries"]
    assert first["read_note"] == "not read: HTTP 500"
    assert first["item_count"] is None
    assert "UUID" in second["read_note"]
    _validate(VMWARE_CONTENT_LIBRARY_LIST_OP, out)


# ---------------------------------------------------------------------------
# vmware.content_library.item.list
# ---------------------------------------------------------------------------


async def test_item_list_by_id(connector: VmwareRestConnector, vc: respx.MockRouter) -> None:
    vc.get("/api/content/library/item", params={"library_id": _LIB}).respond(
        200, json=[_ITEM, _ITEM2]
    )
    vc.get(f"/api/content/library/item/{_ITEM}").respond(200, json=_item(_ITEM, "<ovf-item>"))
    vc.get(f"/api/content/library/item/{_ITEM2}").respond(
        200, json=_item(_ITEM2, "<iso-item>", "iso")
    )

    out = await connector.content_library_item_list(
        _operator(), _StubTarget(), {"library_id": _LIB}
    )

    assert out["library_id"] == _LIB
    assert out["item_count"] == 2
    assert out["items"][0] == {
        "library_item_id": _ITEM,
        "name": "<ovf-item>",
        "type": "ovf",
        "size": 2048,
        "content_version": "3",
        "cached": True,
        "last_modified_time": "2026-10-02T10:00:00.000Z",
        "read_note": None,
    }
    assert out["items"][1]["type"] == "iso"
    _validate(VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP, out)


async def test_item_list_by_name(connector: VmwareRestConnector, vc: respx.MockRouter) -> None:
    find = vc.post("/api/content/library", params={"action": "find"}).respond(200, json=[_LIB])
    vc.get("/api/content/library/item", params={"library_id": _LIB}).respond(200, json=[])

    out = await connector.content_library_item_list(
        _operator(), _StubTarget(), {"library_name": "<local-library>"}
    )

    assert json.loads(find.calls.last.request.content) == {"name": "<local-library>"}
    assert out == {"library_id": _LIB, "item_count": 0, "items": []}


async def test_item_list_of_an_unknown_library_name_is_not_found(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.post("/api/content/library", params={"action": "find"}).respond(200, json=[])
    with pytest.raises(ConnectorResourceNotFoundError):
        await connector.content_library_item_list(
            _operator(), _StubTarget(), {"library_name": "nope"}
        )


async def test_item_list_of_a_missing_library_is_not_found(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get("/api/content/library/item", params={"library_id": _LIB}).respond(
        404, json={"error_type": "NOT_FOUND"}
    )
    with pytest.raises(ConnectorResourceNotFoundError):
        await connector.content_library_item_list(_operator(), _StubTarget(), {"library_id": _LIB})


async def test_item_list_refuses_a_bad_id_before_any_call(connector: VmwareRestConnector) -> None:
    with respx.mock(base_url=_BASE) as mock:
        with pytest.raises(ValueError, match="UUID"):
            await connector.content_library_item_list(
                _operator(), _StubTarget(), {"library_id": "../../vcenter/vm"}
            )
        assert mock.calls.call_count == 0


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_settings_env")
async def test_a_long_item_list_goes_behind_a_result_handle(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(60)]
    vc.get("/api/content/library/item", params={"library_id": _LIB}).respond(200, json=ids)
    for item_id in ids:
        vc.get(f"/api/content/library/item/{item_id}").respond(
            200, json=_item(item_id, f"<item-{item_id[-3:]}>")
        )

    out = await connector.content_library_item_list(
        _operator(), _StubTarget(), {"library_id": _LIB}
    )

    context = {"op_id": VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP.op_id}
    reduced, handle = await JsonFluxReducer(sample_size=5).reduce(out, None, context)
    assert handle is not None
    assert reduced["row_count"] == 60
    assert reduced["library_id"] == _LIB


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_the_two_lists_are_registered_typed_ops() -> None:
    assert {op.op_id for op in CONTENT_LIBRARY_LIST_OPS} == {
        "vmware.content_library.list",
        "vmware.content_library.item.list",
    }
    for op in CONTENT_LIBRARY_LIST_OPS:
        assert op in VMWARE_TYPED_OPS
        assert callable(getattr(VmwareRestConnector, op.handler_attr))
        assert op.group_key == "content_library"
        assert op.op_id in VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key]


@pytest.mark.parametrize("op", CONTENT_LIBRARY_LIST_OPS, ids=lambda op: op.op_id)
def test_each_list_is_safe_and_runs_without_approval(op: Any) -> None:
    assert op.safety_level == "safe"
    assert op.requires_approval is False


@pytest.mark.parametrize("op", CONTENT_LIBRARY_LIST_OPS, ids=lambda op: op.op_id)
def test_examples_validate(op: Any) -> None:
    for example in op.parameter_schema["examples"]:
        assert validate_params(op.parameter_schema, example) == []


def test_schemas_refuse_bad_input() -> None:
    items = VMWARE_CONTENT_LIBRARY_ITEM_LIST_OP.parameter_schema
    assert validate_params(items, {}) != []
    assert validate_params(items, {"library_id": "../x"}) != []
    libraries = VMWARE_CONTENT_LIBRARY_LIST_OP.parameter_schema
    assert validate_params(libraries, {"library_type": "OTHER"}) != []
