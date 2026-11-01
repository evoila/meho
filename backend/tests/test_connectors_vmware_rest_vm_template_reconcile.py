# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the VM-template and customization spec operations.

The VM-template library changes (``library.vm_template.deploy`` /
``.check_out`` / ``.check_in``), the guest customization spec reads
(``vmware.guest.customization_spec.list`` / ``.get``) and the spec delete
call vSphere REST directly. Per ``docs/decisions/spec-reconcile-guards-standard.md``:

* **shape (always runs)** -- the calls each module declares match the frozen
  lists below;
* **paths (skips without the vendor-licensed shelf)** -- the pinned
  ``vcenter.yaml`` of vCenter 8.0 and 9.0 serves every call;
* **bodies (skips without the shelf)** -- every top-level field the three
  changes send is a top-level field of the request body in both files, so the
  bodies are the flat ``*Spec`` the ``/api`` surface wants, never ``{"spec": ...}``.

The two files name path placeholders differently (``{template_library_item}``
in 8.0, ``{templateLibraryItem}`` in 9.0), so paths are compared with every
placeholder written ``{}``.
"""

from __future__ import annotations

import functools
import re
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest import customization_specs
from meho_backplane.connectors.vmware_rest.composites import _customization_spec_delete as delete
from meho_backplane.connectors.vmware_rest.composites import _vm_template as tpl
from meho_backplane.connectors.vmware_rest.composites import _write
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_FILE = "vcenter.yaml"
_SHELVES = ["vcenter-8.0", "vcenter-9.0"]
_ITEM_PATH = "/vcenter/vm-template/library-items/{templateLibraryItem}"
_DEPLOY = f"POST:{_ITEM_PATH}?action=deploy"
_CHECK_OUT = f"POST:{_ITEM_PATH}/check-outs?action=check-out"
_CHECK_IN = f"POST:{_ITEM_PATH}/check-outs/{{vm}}?action=check-in"

_EXPECTED = {
    "deploy": {_DEPLOY, "GET:/vcenter/vm/{vm}"},
    "check_out": {_CHECK_OUT, f"GET:{_ITEM_PATH}/check-outs/{{vm}}", "GET:/vcenter/vm/{vm}"},
    "check_in": {_CHECK_IN, f"GET:{_ITEM_PATH}"},
    "spec_delete": {
        "GET:/vcenter/guest/customization-specs/{name}",
        "DELETE:/vcenter/guest/customization-specs/{name}",
    },
    "spec_reads": {
        "GET:/vcenter/guest/customization-specs",
        "GET:/vcenter/guest/customization-specs/{name}",
    },
}

_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _normal(op_id: str) -> str:
    return _PLACEHOLDER.sub("{}", op_id)


def _declared() -> dict[str, set[str]]:
    return {
        "deploy": set(tpl.SUB_OPS_DEPLOY),
        "check_out": set(tpl.SUB_OPS_CHECK_OUT),
        "check_in": set(tpl.SUB_OPS_CHECK_IN),
        "spec_delete": set(delete.SUB_OPS_CUSTOMIZATION_SPEC_DELETE),
        "spec_reads": {
            f"GET:{customization_specs.SPECS_PATH}",
            f"GET:{customization_specs.SPEC_PATH}",
        },
    }


def _all_declared() -> set[str]:
    return set().union(*_declared().values())


@functools.cache
def _served(shelf: str) -> frozenset[str]:
    spec_path = require_shelf_spec(shelf, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path, spec_source=f"spec:{_SPEC_FILE}")
    return frozenset(_normal(op_id) for op_id in served)


def _resolve(doc: dict[str, Any], node: Any) -> dict[str, Any]:
    """Follow ``$ref`` links (request bodies, then schemas) to the object they name."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        target: Any = doc
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node if isinstance(node, dict) else {}


@functools.cache
def _bodies(shelf: str) -> dict[str, frozenset[str]]:
    """Top-level request body fields of each call, read from the pinned file itself.

    The 8.0 file puts its bodies under ``components/requestBodies``, which the
    ingest parser does not follow, so this reads the YAML directly for both files.
    """
    spec_path = require_shelf_spec(shelf, _SPEC_FILE)
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    doc = yaml.load(spec_path.read_text(encoding="utf-8"), Loader=loader)
    bodies: dict[str, frozenset[str]] = {}
    for path, item in doc["paths"].items():
        for method, op in item.items():
            if not isinstance(op, dict) or "requestBody" not in op:
                continue
            body = _resolve(doc, op["requestBody"])
            schema = _resolve(
                doc, body.get("content", {}).get("application/json", {}).get("schema")
            )
            fields = schema.get("properties")
            if isinstance(fields, dict):
                bodies[_normal(f"{method.upper()}:{path}")] = frozenset(fields)
    return bodies


def test_declared_calls_match_the_frozen_lists() -> None:
    """Shape gate (always runs)."""
    assert _declared() == _EXPECTED


@pytest.mark.parametrize("shelf", _SHELVES)
def test_every_call_is_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    """Path existence (skips without the shelf)."""
    assert_op_ids_served(
        {_normal(op_id) for op_id in _all_declared()},
        _served(shelf),
        spec_label=f"{shelf}/{_SPEC_FILE}",
    )


class _BodyRecorder:
    """Connector double that records the body of each write and answers ids."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {}

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Any) -> Any:
        return query

    async def _get_json(self, target: Any, path: str, **_: Any) -> Any:
        return {}

    async def _post_json(self, target: Any, path: str, *, json: Any = None, **_: Any) -> Any:
        self.bodies[path.removeprefix("/api")] = json
        return "3" if "check-in" in path else "vm-1"


_ITEM = "00000000-0000-4000-8000-000000000001"


async def _sent_bodies(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    """Run the three changes with every optional field set; return the body keys by call."""

    async def _allow(**_: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _allow)
    conn = _BodyRecorder()
    kwargs: dict[str, Any] = {"operator": None, "target": None, "connector": conn}
    placement = {"folder": "f", "resource_pool": "r", "host": "h", "cluster": "c"}
    await tpl.vm_template_deploy_composite(
        params={
            "template_library_item": _ITEM,
            "name": "n",
            "description": "d",
            "datastore": "ds",
            "powered_on": True,
            "customization_spec_name": "s",
            **placement,
        },
        **kwargs,
    )
    await tpl.vm_template_check_out_composite(
        params={"template_library_item": _ITEM, "name": "n", "powered_on": True, **placement},
        **kwargs,
    )
    await tpl.vm_template_check_in_composite(
        params={"template_library_item": _ITEM, "vm": "vm-1", "message": "m"}, **kwargs
    )
    by_call: dict[str, set[str]] = {}
    for path, body in conn.bodies.items():
        call = _normal(f"POST:{path.replace(_ITEM, '{}').replace('vm-1', '{}')}")
        by_call[call] = set(body)
    return by_call


async def test_the_changes_send_flat_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body gate (always runs): no ``spec`` wrapper, and the expected top-level fields."""
    sent = await _sent_bodies(monkeypatch)
    assert sent == {
        _normal(_DEPLOY): {
            "name",
            "description",
            "placement",
            "vm_home_storage",
            "disk_storage",
            "powered_on",
            "guest_customization",
        },
        _normal(_CHECK_OUT): {"name", "placement", "powered_on"},
        _normal(_CHECK_IN): {"message"},
    }


@pytest.mark.parametrize("shelf", _SHELVES)
async def test_every_body_field_is_a_spec_field(
    shelf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Body fields (skips without the shelf): each sent field is in the pinned request body."""
    bodies = _bodies(shelf)
    for call, fields in (await _sent_bodies(monkeypatch)).items():
        assert call in bodies, f"{shelf}/{_SPEC_FILE} has no request body for {call}"
        assert fields <= bodies[call], (
            f"{call}: fields {sorted(fields - bodies[call])} are not in {shelf}/{_SPEC_FILE}"
        )
