# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ending maintenance mode on a host waits for approval on every path (#4967).

The typed ``vmware.composite.host.maintenance.exit`` is ``dangerous`` with
``requires_approval=True`` on the 8.0 and 9.0 catalogs, like its mirror
``host.evacuate``. The VMware floor (``ingest_safety.py``) raises the generic
older-API method ``POST:/HostSystem/{moId}/ExitMaintenanceMode_Task`` to the
same level on both catalogs, so no MEHO path ends maintenance mode on a
written go in chat. The generic start, reboot, shut down and standby calls of
the host power-state work (#4350) have their own tests in
``test_connectors_vmware_rest_host_power_generic_approval.py``.

The pinned-spec lane checks that the floor rule matches an operation in the
vendor ``vi-json`` files on the spec shelf; it skips when the shelf is not
configured (see ``tests/_spec_shelf.py``) and runs in CI.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
)
from meho_backplane.connectors.vmware_rest.ingest_safety import (
    is_vmware_host_maintenance_exit,
    vmware_rest_safety_floor,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_TYPED_OP_ID = "vmware.composite.host.maintenance.exit"
_VIM_EXIT = "POST:/HostSystem/{moId}/ExitMaintenanceMode_Task"
_VIM_ENTER = "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task"
_VIM_READ = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

#: ``(shelf product dir, file)`` of every pinned VI/JSON file: vCenter 9.0
#: plus the three vCenter 8.0 updates (the same files the 8.0 VI-JSON lanes use).
_VI_JSON_FILES: list[tuple[str, str, str]] = [
    ("vcenter-9.0", "vi-json.yaml", "9.0"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml", "8.0"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml", "8.0"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml", "8.0"),
]


@pytest.fixture(autouse=True)
def _vmware_floor_registered() -> None:
    """Make sure the VMware floor is registered, whatever ran before this test."""
    register_ingest_safety_floor(
        product="vmware", impl_id="vmware-rest", floor=vmware_rest_safety_floor
    )


def _floored(
    protos: list[EndpointDescriptorProto], version: str
) -> dict[str, EndpointDescriptorProto]:
    return {
        p.op_id: apply_safety_floor(
            product="vmware", version=version, impl_id="vmware-rest", proto=p
        )
        for p in protos
    }


def _waits_for_approval(proto: EndpointDescriptorProto) -> bool:
    return proto.safety_level == "dangerous" and proto.requires_approval is True


# ---------------------------------------------------------------------------
# The typed operation
# ---------------------------------------------------------------------------


def test_typed_maintenance_exit_is_dangerous_with_approval_on_both_catalogs() -> None:
    """The typed op is registered like host.evacuate, and is not left off the 8.0 catalog."""
    spec = next(s for s in _COMPOSITES if s.op_id == _TYPED_OP_ID)
    mirror = next(s for s in _COMPOSITES if s.op_id == "vmware.composite.host.evacuate")

    assert spec.safety_level == "dangerous"
    assert spec.requires_approval is True
    assert (spec.safety_level, spec.requires_approval) == (
        mirror.safety_level,
        mirror.requires_approval,
    )
    assert spec.group_key == "host"
    assert _TYPED_OP_ID not in COMPOSITES_NOT_ON_8_0


# ---------------------------------------------------------------------------
# The floor rule on the generic call
# ---------------------------------------------------------------------------

_HAND_MADE_SPEC = """
openapi: 3.0.3
info: {title: host maintenance exit fixture, version: '1.0'}
paths:
  /HostSystem/{moId}/ExitMaintenanceMode_Task:
    post:
      responses: {'200': {description: ok}}
  /HostSystem/{moId}/EnterMaintenanceMode_Task:
    post:
      responses: {'200': {description: ok}}
  /PropertyCollector/{moId}/RetrievePropertiesEx:
    post:
      responses: {'200': {description: ok}}
"""


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_ingested_generic_exit_waits_for_approval(version: str) -> None:
    """After an ingest, the generic exit lands ``dangerous`` with approval on both catalogs."""
    rows = parse_openapi("file:///host-maintenance-exit.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, version)

    assert set(ops) == {_VIM_EXIT, _VIM_ENTER, _VIM_READ}
    assert _waits_for_approval(ops[_VIM_EXIT])


def test_the_property_read_keeps_its_level() -> None:
    """The floor raises the host writes only: the property read stays ``caution``."""
    rows = parse_openapi("file:///host-maintenance-exit.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, "9.0")

    assert ops[_VIM_READ].safety_level == "caution"
    assert ops[_VIM_READ].requires_approval is False


#: ``(method, path, the rule accepts it)``.
_SAFETY_RULE_CASES: list[tuple[str, str, bool]] = [
    ("POST", "/HostSystem/{moId}/ExitMaintenanceMode_Task", True),
    ("POST", "/api/HostSystem/{moId}/ExitMaintenanceMode_Task", True),
    ("POST", "/HostSystem/{mo_id}/ExitMaintenanceMode_Task", True),
    ("post", "/api/HostSystem/{mo_id}/ExitMaintenanceMode_Task", True),
    ("GET", "/HostSystem/{moId}/ExitMaintenanceMode_Task", False),
    ("POST", "/HostSystem/{moId}/ExitMaintenanceMode_Task?x=1", False),
    ("POST", "/HostSystem/{moId}/EnterMaintenanceMode_Task", False),
    ("POST", "/ClusterComputeResource/{moId}/ExitMaintenanceMode_Task", False),
    ("POST", "/HostSystem/ExitMaintenanceMode_Task", False),
]


@pytest.mark.parametrize(("method", "path", "expected"), _SAFETY_RULE_CASES)
def test_is_vmware_host_maintenance_exit(method: str, path: str, expected: bool) -> None:
    assert is_vmware_host_maintenance_exit(method, path) is expected


def test_floor_never_lowers_a_destructive_exit() -> None:
    proto = EndpointDescriptorProto(
        op_id=_VIM_EXIT,
        method="POST",
        path="/HostSystem/{moId}/ExitMaintenanceMode_Task",
        parameter_schema={"type": "object", "properties": {}},
        safety_level="destructive",
        requires_approval=True,
    )

    floored = apply_safety_floor(
        product="vmware", version="9.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "destructive"
    assert floored.requires_approval is True


def test_floor_does_not_apply_to_an_unlisted_catalog_version() -> None:
    """The floor is scoped to the 8.0 and 9.0 catalogs."""
    proto = EndpointDescriptorProto(
        op_id=_VIM_EXIT,
        method="POST",
        path="/HostSystem/{moId}/ExitMaintenanceMode_Task",
        parameter_schema={"type": "object", "properties": {}},
        safety_level="caution",
        requires_approval=False,
    )

    floored = apply_safety_floor(
        product="vmware", version="7.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "caution"
    assert floored.requires_approval is False


# ---------------------------------------------------------------------------
# The pinned vendor files serve the rule (shelf-gated, runs in CI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product_dir", "filename", "version"),
    _VI_JSON_FILES,
    ids=[f"{d}/{f}" for d, f, _ in _VI_JSON_FILES],
)
def test_pinned_vendor_spec_serves_the_exit_rule(
    product_dir: str, filename: str, version: str
) -> None:
    """The floor rule matches an operation in the pinned file, and lands gated."""
    spec_path = require_shelf_spec(product_dir, filename)
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, version)

    assert_op_ids_served({_VIM_EXIT}, set(ops), spec_label=f"{product_dir}/{filename}")
    assert _waits_for_approval(ops[_VIM_EXIT])
