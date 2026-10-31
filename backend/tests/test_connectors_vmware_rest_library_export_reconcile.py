# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the content-library lists, capture and export.

The two typed lists declare their vendor calls in
:data:`~meho_backplane.connectors.vmware_rest.typed_ops_content_library_list.VENDOR_OP_IDS`;
the four composites declare theirs in ``_SUB_OPS_*`` tuples in
``_library_capture``, ``_library_publish`` and ``_library_export``. Per
``docs/decisions/spec-reconcile-guards-standard.md``:

* **shape (always runs)** -- the declared ``METHOD:/path`` sets match the
  frozen lists below, and the introspection finds every ``_SUB_OPS_*`` tuple;
* **paths (skips without the shelf)** -- the pinned 9.0 and 8.0
  ``vcenter.yaml`` serve every path, so no operation needs a version check;
* **bodies and headers (skips without the shelf)** -- in the pinned 9.0
  ``vcenter.yaml`` the write bodies have the top-level fields the handlers
  send, and the OVF capture and the download-session create take the
  ``Client-Token`` header.

The two files name path placeholders differently (``{library_id}`` in 8.0,
``{libraryId}`` in 9.0), so the paths are compared with every placeholder
written ``{}``.
"""

from __future__ import annotations

import functools
import re
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites import (
    _library_capture,
    _library_export,
    _library_publish,
)
from meho_backplane.connectors.vmware_rest.typed_ops_content_library_list import VENDOR_OP_IDS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_FILE = "vcenter.yaml"
_PLACEHOLDER = re.compile(r"\{[^}]*\}")
_MODULES = (_library_capture, _library_publish, _library_export)

_CAPTURE = {
    "GET:/vcenter/vm/{}",
    "POST:/content/library?action=find",
    "GET:/content/library/{}",
    "POST:/content/library/item?action=find",
    "GET:/content/library/item/{}",
    "POST:/vcenter/ovf/library-item",
    "POST:/vcenter/vm-template/library-items",
}
_PUBLISH = {
    "POST:/content/library?action=find",
    "GET:/content/library/{}",
    "GET:/content/local-library/{}",
    "PATCH:/content/local-library/{}",
}
_ITEM_EXPORT = {
    "GET:/content/library/item/{}",
    "POST:/content/library/item/download-session",
    "GET:/content/library/item/download-session/{}/file",
    "POST:/content/library/item/download-session/{}/file?action=prepare",
    "GET:/content/library/item/download-session/{}/file?file_name",
    "POST:/content/library/item/download-session/{}?action=keep-alive",
    "DELETE:/content/library/item/download-session/{}",
}
_EXPORT_OVF = (
    _CAPTURE
    | _ITEM_EXPORT
    | {"GET:/content/library/item?library_id", "DELETE:/content/library/item/{}"}
)
_LISTS = {
    "POST:/content/library?action=find",
    "GET:/content/library",
    "GET:/content/library/{}",
    "GET:/content/library/item?library_id",
    "GET:/content/library/item/{}",
}


def _normal(op_id: str) -> str:
    return _PLACEHOLDER.sub("{}", op_id)


def _declared() -> set[str]:
    raw: set[str] = set(VENDOR_OP_IDS)
    for module in _MODULES:
        for name in dir(module):
            if name.startswith("_SUB_OPS_"):
                raw.update(getattr(module, name))
    return {_normal(op_id) for op_id in raw}


@functools.cache
def _served(product_dir: str) -> frozenset[str]:
    spec_path = require_shelf_spec(product_dir, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path, spec_source=f"spec:{_SPEC_FILE}")
    return frozenset(_normal(op_id) for op_id in served)


def test_the_sub_op_tuples_are_all_discovered() -> None:
    names = sorted(
        f"{module.__name__.rsplit('.', 1)[1]}.{name}"
        for module in _MODULES
        for name in dir(module)
        if name.startswith("_SUB_OPS_")
    )
    assert names == [
        "_library_capture._SUB_OPS_VM_CAPTURE_TO_LIBRARY",
        "_library_export._SUB_OPS_CONTENT_LIBRARY_ITEM_EXPORT",
        "_library_export._SUB_OPS_VM_EXPORT_OVF",
        "_library_publish._SUB_OPS_CONTENT_LIBRARY_PUBLISH_SET",
    ]


def test_declared_vendor_calls_are_frozen() -> None:
    def norm(ops: Any) -> set[str]:
        return {_normal(op) for op in ops}

    assert norm(VENDOR_OP_IDS) == _LISTS
    assert norm(_library_capture._SUB_OPS_VM_CAPTURE_TO_LIBRARY) == _CAPTURE
    assert norm(_library_publish._SUB_OPS_CONTENT_LIBRARY_PUBLISH_SET) == _PUBLISH
    assert norm(_library_export._SUB_OPS_CONTENT_LIBRARY_ITEM_EXPORT) == _ITEM_EXPORT
    assert norm(_library_export._SUB_OPS_VM_EXPORT_OVF) == _EXPORT_OVF


def test_paths_are_served_by_the_pinned_9_0_spec() -> None:
    assert_op_ids_served(_declared(), _served("vcenter-9.0"), spec_label="vcenter-9.0/vcenter.yaml")


def test_paths_are_served_by_the_pinned_8_0_spec() -> None:
    assert_op_ids_served(_declared(), _served("vcenter-8.0"), spec_label="vcenter-8.0/vcenter.yaml")


@pytest.mark.parametrize(
    ("op_id", "fields"),
    [
        ("POST:/vcenter/ovf/library-item", {"source", "target", "create_spec"}),
        (
            "POST:/vcenter/vm-template/library-items",
            {"source_vm", "name", "library", "placement", "vm_home_storage", "disk_storage"},
        ),
        ("PATCH:/content/local-library/{}", {"publish_info"}),
        ("POST:/content/library/item/download-session", {"library_item_id"}),
        ("POST:/content/library/item/download-session/{}/file?action=prepare", {"file_name"}),
    ],
)
def test_write_bodies_have_the_fields_the_handlers_send(op_id: str, fields: set[str]) -> None:
    spec_path = require_shelf_spec("vcenter-9.0", _SPEC_FILE)
    bodies = {
        _normal(key): props
        for key, props in openapi_request_body_props(
            spec_path, spec_source=f"spec:{_SPEC_FILE}"
        ).items()
    }
    assert op_id in bodies, f"vcenter-9.0/{_SPEC_FILE} has no body for {op_id}"
    assert fields <= bodies[op_id]


@pytest.mark.parametrize(
    "path", ["/vcenter/ovf/library-item", "/content/library/item/download-session"]
)
def test_the_two_creates_take_a_client_token(path: str) -> None:
    spec_path = require_shelf_spec("vcenter-9.0", _SPEC_FILE)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    params = spec["paths"][path]["post"].get("parameters", [])
    assert {"name": "Client-Token", "in": "header"} in [
        {"name": p.get("name"), "in": p.get("in")} for p in params
    ]
