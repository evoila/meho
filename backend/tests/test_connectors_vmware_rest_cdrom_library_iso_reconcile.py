# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the content-library ISO mount and unmount.

``vmware.composite.vm.cdrom.mount_library_iso`` and
``vmware.composite.vm.cdrom.unmount_library_iso`` call vSphere REST directly.
This file checks those calls in three steps, like
``test_connectors_vmware_rest_vm_hardware_reconcile.py``:

* **shape (always runs)** -- the calls the module declares match the frozen
  lists below;
* **body (always runs)** -- the mount and unmount bodies the composites send
  have the fields the ``Iso.Image`` rows of ``_governed_iso_recipe.py`` pin,
  so the composite and the raw recipe cannot drift apart;
* **path existence (skips without the vendor-licensed shelf)** -- every call
  is served by the pinned ``vcenter.yaml`` of vCenter 8.0 and 9.0.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.vmware_rest.composites import _vm_cdrom_library_iso as iso
from meho_backplane.connectors.vmware_rest.composites import _write
from tests._governed_iso_recipe import RECIPE_OPS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_EXPECTED_MOUNT = {
    "POST:/content/library?action=find",
    "POST:/content/library/item?action=find",
    "POST:/vcenter/iso/image?action=mount",
    "GET:/vcenter/vm/{vm}/hardware/cdrom",
    "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
}
_EXPECTED_UNMOUNT = {"POST:/vcenter/iso/image?action=unmount"}

_SHELVES = ["vcenter-8.0", "vcenter-9.0"]


def test_declared_calls_match_the_frozen_lists() -> None:
    """Shape gate (always runs)."""
    assert set(iso.SUB_OPS_MOUNT_LIBRARY_ISO) == _EXPECTED_MOUNT
    assert set(iso.SUB_OPS_UNMOUNT_LIBRARY_ISO) == _EXPECTED_UNMOUNT


class _BodyRecorder:
    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {}

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return f"/api{path}"

    async def _get_json(self, target: Any, path: str, **_: Any) -> Any:
        return []

    async def _post_json(self, target: Any, path: str, *, json: Any = None, **_: Any) -> Any:
        self.bodies[path.removeprefix("/api")] = json
        return "16002" if path.endswith("action=mount") else {}


async def test_bodies_match_the_iso_image_recipe_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body gate (always runs): the composite sends the recipe's exact body fields."""

    async def _allow(**_: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _allow)
    conn = _BodyRecorder()
    kwargs: dict[str, Any] = {"operator": None, "target": None, "connector": conn}
    await iso.vm_cdrom_mount_library_iso_composite(
        params={"vm": "vm-42", "library_item": "item-1"}, **kwargs
    )
    await iso.vm_cdrom_unmount_library_iso_composite(
        params={"vm": "vm-42", "cdrom": "16002"}, **kwargs
    )
    for op_id in (*iso.SUB_OPS_UNMOUNT_LIBRARY_ISO, "POST:/vcenter/iso/image?action=mount"):
        path = op_id.partition(":")[2]
        assert RECIPE_OPS[op_id].body_props_exact
        assert set(conn.bodies[path]) == RECIPE_OPS[op_id].body_props


@pytest.mark.parametrize("shelf", _SHELVES)
def test_every_call_is_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    """Path existence (skips without the shelf)."""
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    calls = set(iso.SUB_OPS_MOUNT_LIBRARY_ISO) | set(iso.SUB_OPS_UNMOUNT_LIBRARY_ISO)
    assert_op_ids_served(calls, served, spec_label=f"{shelf}/vcenter.yaml")
