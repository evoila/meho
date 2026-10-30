# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Content-library ids are checked before they go into a URL path.

Covers the older library operations (subscribed sync / status / items list,
``vm.deploy_from_library``, ``vm.import_from_library``, ``vm.clone``) and the
shared path builder ``_split_sub_op``. The recording double cleans every URL
path the way httpx does before sending (``..`` segments collapse), so a test
can show that no input reaches another object.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.composites import _library, _write, schemas
from tests._vmware_teardown_fake import GateRecorder, operator

_LIB = "11111111-1111-1111-1111-111111111111"
_ITEM = "33333333-3333-3333-3333-333333333333"
_FIND_LIBRARY = "/api/content/library?action=find"
_FIND_ITEM = "/api/content/library/item?action=find"

_BAD_IDS = [
    "../../../vcenter/vm/vm-42",
    f"../local-library/{_LIB}",
    f"{_LIB}/../x",
    "%2e%2e",
    f"{_LIB}%2F..",
    "..",
    "lib-1",
    "",
]


class _RestFake:
    """Records every request; finds answer from canned id lists."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self, *, library_ids: list[str] | None = None, item_ids: list[str] | None = None):
        self.library_ids = library_ids or []
        self.item_ids = item_ids or []
        self.calls: list[tuple[str, str]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    @staticmethod
    def _wire(path: str) -> str:
        """The path httpx actually sends (dot segments removed)."""
        base, _, query = path.partition("?")
        real = httpx.URL("https://vc.example.test" + base).path
        return f"{real}?{query}" if query else real

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        real = self._wire(path)
        self.calls.append(("GET", real))
        return {"id": real.rsplit("/", 1)[-1], "name": "x"}

    async def _post_json(
        self, target: Any, path: str, *, operator: Operator, verb: str = "POST", **_: Any
    ) -> Any:
        real = self._wire(path)
        self.calls.append((verb, real))
        if real == _FIND_LIBRARY:
            return self.library_ids
        if real == _FIND_ITEM:
            return self.item_ids
        return None


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _run(handler: Any, conn: _RestFake, params: dict[str, Any]) -> Any:
    return await handler(operator=operator(), target=object(), params=params, connector=conn)


# ---------------------------------------------------------------------------
# _split_sub_op: every path value is one encoded segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "params", "path"),
    [
        ("GET:/vcenter/vm/{vm}", {"vm": "vm-42"}, "/vcenter/vm/vm-42"),
        (
            "POST:/content/subscribed-library/{libraryId}?action=sync",
            {"libraryId": _LIB},
            f"/content/subscribed-library/{_LIB}?action=sync",
        ),
        (
            "GET:/cis/tasks/{task}",
            {"task": "52a1b2c3-0000-0000-0000-000000000000:com.vmware.vcenter.vm"},
            "/cis/tasks/52a1b2c3-0000-0000-0000-000000000000:com.vmware.vcenter.vm",
        ),
        (
            "GET:/cis/tagging/tag/{tagId}",
            {"tagId": "urn:vmomi:InventoryServiceTag:abc:GLOBAL"},
            "/cis/tagging/tag/urn:vmomi:InventoryServiceTag:abc:GLOBAL",
        ),
        ("GET:/vcenter/vm/{vm}/hardware/disk/{disk}", {"vm": "vm-7", "disk": 2000}, None),
    ],
)
def test_split_sub_op_keeps_normal_ids_unchanged(
    op_id: str, params: dict[str, Any], path: str | None
) -> None:
    template = op_id.partition(":")[2]
    expected = path if path is not None else template.format(**params)
    assert _write._split_sub_op(op_id, params)[1] == expected


@pytest.mark.parametrize(
    ("value", "segment"),
    [
        ("../../../vcenter/vm/vm-42", "..%2F..%2F..%2Fvcenter%2Fvm%2Fvm-42"),
        ("..", "%2E%2E"),
        (".", "%2E"),
        ("%2e%2e", "%252e%252e"),
        ("a?b#c", "a%3Fb%23c"),
    ],
)
def test_split_sub_op_encodes_values_that_could_change_the_path(value: str, segment: str) -> None:
    _, path, rest = _write._split_sub_op(
        "GET:/content/library/item/{libraryItemId}",
        {
            "libraryItemId": value,
            "q": 1,
        },
    )
    assert path == f"/content/library/item/{segment}"
    assert rest == {"q": 1}
    # What httpx sends still names a library item, not another object.
    assert httpx.URL("https://vc.example.test/api" + path).path.startswith(
        "/api/content/library/item/"
    )


# ---------------------------------------------------------------------------
# Subscribed-library sync / status / items list
# ---------------------------------------------------------------------------

_SUBSCRIBED = [
    (
        _library.content_library_subscribed_sync_composite,
        schemas.CONTENT_LIBRARY_SUBSCRIBED_SYNC_PARAMETER_SCHEMA,
    ),
    (
        _library.content_library_subscribed_status_composite,
        schemas.CONTENT_LIBRARY_SUBSCRIBED_STATUS_PARAMETER_SCHEMA,
    ),
    (
        _library.content_library_subscribed_items_list_composite,
        schemas.CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_PARAMETER_SCHEMA,
    ),
]


@pytest.mark.parametrize("bad", _BAD_IDS)
@pytest.mark.parametrize(("handler", "schema"), _SUBSCRIBED)
async def test_subscribed_ops_refuse_a_non_uuid_library_id(
    gate: GateRecorder, handler: Any, schema: dict[str, Any], bad: str
) -> None:
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({"library_id": bad})
    conn = _RestFake()
    out = await _run(handler, conn, {"library_id": bad})
    assert out["status"] == "invalid_reference"
    assert conn.calls == []
    assert gate.calls == []


@pytest.mark.parametrize(("handler", "schema"), _SUBSCRIBED)
async def test_subscribed_ops_refuse_a_non_uuid_id_found_by_name(
    gate: GateRecorder, handler: Any, schema: dict[str, Any]
) -> None:
    conn = _RestFake(library_ids=["../../vcenter/vm/vm-42"])
    out = await _run(handler, conn, {"library_name": "demo-lib"})
    assert out["status"] == "invalid_reference"
    assert conn.calls == [("POST", _FIND_LIBRARY)]
    assert gate.calls == []


async def test_items_list_skips_a_non_uuid_item_id_from_vcenter(gate: GateRecorder) -> None:
    conn = _RestFake(item_ids=["../../../vcenter/vm/vm-42", _ITEM])
    out = await _run(
        _library.content_library_subscribed_items_list_composite, conn, {"library_id": _LIB}
    )
    assert [row["id"] for row in out["items"]] == [_ITEM]
    assert [i["severity"] for i in out["issues"]] == ["warning"]
    assert conn.calls == [("POST", _FIND_ITEM), ("GET", f"/api/content/library/item/{_ITEM}")]
    Draft202012Validator(schemas.CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST_RESPONSE_SCHEMA).validate(
        out
    )


async def test_sync_by_uuid_still_syncs(gate: GateRecorder) -> None:
    conn = _RestFake()
    out = await _run(_library.content_library_subscribed_sync_composite, conn, {"library_id": _LIB})
    assert out == {"status": "sync_triggered", "library_id": _LIB}
    assert conn.calls == [("POST", f"/api/content/subscribed-library/{_LIB}?action=sync")]


# ---------------------------------------------------------------------------
# vm.deploy_from_library / vm.import_from_library (shared item lookup)
# ---------------------------------------------------------------------------

_ITEM_OPS = [
    (_write.vm_deploy_from_library_composite, schemas.VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA),
    (_write.vm_import_from_library_composite, schemas.VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA),
]
_PLACEMENT = {"resource_pool": "resgroup-8", "datastore": "datastore-15"}


@pytest.mark.parametrize("bad", _BAD_IDS)
@pytest.mark.parametrize(("handler", "schema"), _ITEM_OPS)
async def test_library_item_ops_refuse_a_non_uuid_item_id(
    gate: GateRecorder, handler: Any, schema: dict[str, Any], bad: str
) -> None:
    params = {"library_item": bad, **_PLACEMENT}
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(params)
    conn = _RestFake()
    out = await _run(handler, conn, params)
    assert out["status"] == "invalid_reference"
    assert conn.calls == []
    assert gate.calls == []


@pytest.mark.parametrize(("handler", "schema"), _ITEM_OPS)
async def test_library_item_ops_refuse_a_non_uuid_id_found_by_name(
    gate: GateRecorder, handler: Any, schema: dict[str, Any]
) -> None:
    conn = _RestFake(item_ids=["../../../vcenter/vm/vm-42"])
    out = await _run(handler, conn, {"library_item_name": "appliance", **_PLACEMENT})
    assert out["status"] == "invalid_reference"
    assert conn.calls == [("POST", _FIND_ITEM)]
    assert gate.calls == []


# ---------------------------------------------------------------------------
# vm.clone (content-library template)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", _BAD_IDS)
async def test_vm_clone_refuses_a_non_uuid_library_item(gate: GateRecorder, bad: str) -> None:
    params = {"source_vm": "vm-42", "target_name": "copy", "library_item": bad}
    with pytest.raises(ValidationError):
        Draft202012Validator(schemas.VM_CLONE_PARAMETER_SCHEMA).validate(params)
    conn = _RestFake()
    with pytest.raises(ValueError, match="UUID"):
        await _run(_write.vm_clone_composite, conn, params)
    assert conn.calls == []
    assert gate.calls == []
