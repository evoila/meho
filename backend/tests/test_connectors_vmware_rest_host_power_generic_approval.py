# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The generic host power-state calls wait for approval like the typed ones (#4350).

The typed ``host.maintenance.enter``, ``host.reboot``, ``host.shutdown`` and
``host.standby.enter`` are ``dangerous`` with approval; ``host.standby.exit``
is ``caution`` with approval. The VMware floor (``ingest_safety.py``) gives
the matching generic older-API methods on ``/HostSystem/{moId}/`` the same
level and approval on the 8.0 and 9.0 catalogs.

The pinned-spec lane checks that each rule matches an operation in the vendor
``vi-json`` files on the spec shelf; it skips when the shelf is not configured
(see ``tests/_spec_shelf.py``) and runs in CI.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.connectors.vmware_rest.ingest_safety import (
    is_vmware_vim_host_power_write,
    is_vmware_vim_host_wake,
    vmware_rest_safety_floor,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

#: generic op_id -> the typed op that does the same change.
_TYPED_MIRROR: dict[str, str] = {
    "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task": "vmware.composite.host.maintenance.enter",
    "POST:/HostSystem/{moId}/RebootHost_Task": "vmware.composite.host.reboot",
    "POST:/HostSystem/{moId}/ShutdownHost_Task": "vmware.composite.host.shutdown",
    "POST:/HostSystem/{moId}/PowerDownHostToStandBy_Task": "vmware.composite.host.standby.enter",
    "POST:/HostSystem/{moId}/PowerUpHostFromStandBy_Task": "vmware.composite.host.standby.exit",
}
_WAKE = "POST:/HostSystem/{moId}/PowerUpHostFromStandBy_Task"
_VIM_READ = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

#: ``(shelf product dir, file, catalog version)`` of every pinned VI/JSON file.
_VI_JSON_FILES: list[tuple[str, str, str]] = [
    ("vcenter-9.0", "vi-json.yaml", "9.0"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml", "8.0"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml", "8.0"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml", "8.0"),
]

_HAND_MADE_SPEC = "\n".join(
    [
        "openapi: 3.0.3",
        "info: {title: host power fixture, version: '1.0'}",
        "paths:",
        *(
            f"  {op_id.removeprefix('POST:')}:\n"
            "    post:\n"
            "      responses: {'200': {description: ok}}"
            for op_id in [*_TYPED_MIRROR, _VIM_READ]
        ),
    ]
)


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


def _typed_level(op_id: str) -> str:
    spec = next(s for s in _COMPOSITES if s.op_id == _TYPED_MIRROR[op_id])
    assert spec.requires_approval is True
    return spec.safety_level


@pytest.mark.parametrize("version", ["8.0", "9.0"])
@pytest.mark.parametrize("op_id", sorted(_TYPED_MIRROR))
def test_ingested_generic_call_waits_like_its_typed_mirror(op_id: str, version: str) -> None:
    """Each generic row lands with approval and the level of its typed op."""
    rows = parse_openapi("file:///host-power.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, version)

    assert ops[op_id].requires_approval is True
    assert ops[op_id].safety_level == _typed_level(op_id)


def test_the_wake_from_standby_stays_caution() -> None:
    rows = parse_openapi("file:///host-power.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, "9.0")

    assert ops[_WAKE].safety_level == "caution"
    assert ops[_WAKE].requires_approval is True


def test_the_property_read_keeps_its_level() -> None:
    rows = parse_openapi("file:///host-power.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, "9.0")

    assert ops[_VIM_READ].safety_level == "caution"
    assert ops[_VIM_READ].requires_approval is False


def test_floor_does_not_apply_to_an_unlisted_catalog_version() -> None:
    rows = parse_openapi("file:///host-power.yaml", content=_HAND_MADE_SPEC)
    ops = _floored(rows, "7.0")

    for op_id in _TYPED_MIRROR:
        assert ops[op_id].requires_approval is False


#: ``(method, path, the dangerous rule accepts it, the wake rule accepts it)``.
_RULE_CASES: list[tuple[str, str, bool, bool]] = [
    ("POST", "/HostSystem/{moId}/EnterMaintenanceMode_Task", True, False),
    ("POST", "/api/HostSystem/{moId}/RebootHost_Task", True, False),
    ("post", "/HostSystem/{mo_id}/ShutdownHost_Task", True, False),
    ("POST", "/api/HostSystem/{host}/PowerDownHostToStandBy_Task", True, False),
    ("POST", "/HostSystem/{moId}/PowerUpHostFromStandBy_Task", False, True),
    ("POST", "/api/HostSystem/{mo_id}/PowerUpHostFromStandBy_Task", False, True),
    ("GET", "/HostSystem/{moId}/RebootHost_Task", False, False),
    ("POST", "/HostSystem/{moId}/RebootHost_Task?x=1", False, False),
    ("POST", "/HostSystem/{moId}/ExitMaintenanceMode_Task", False, False),
    ("POST", "/ClusterComputeResource/{moId}/EnterMaintenanceMode_Task", False, False),
    ("POST", "/HostSystem/RebootHost_Task", False, False),
]


@pytest.mark.parametrize(("method", "path", "power", "wake"), _RULE_CASES)
def test_host_power_rules(method: str, path: str, power: bool, wake: bool) -> None:
    assert is_vmware_vim_host_power_write(method, path) is power
    assert is_vmware_vim_host_wake(method, path) is wake


def test_floor_never_lowers_a_destructive_row() -> None:
    proto = EndpointDescriptorProto(
        op_id="POST:/HostSystem/{moId}/ShutdownHost_Task",
        method="POST",
        path="/HostSystem/{moId}/ShutdownHost_Task",
        parameter_schema={"type": "object", "properties": {}},
        safety_level="destructive",
        requires_approval=True,
    )

    floored = apply_safety_floor(
        product="vmware", version="8.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "destructive"
    assert floored.requires_approval is True


@pytest.mark.parametrize(
    ("product_dir", "filename", "version"),
    _VI_JSON_FILES,
    ids=[f"{d}/{f}" for d, f, _ in _VI_JSON_FILES],
)
def test_pinned_vendor_spec_serves_the_host_power_rules(
    product_dir: str, filename: str, version: str
) -> None:
    """Each rule matches an operation in the pinned file, and lands with approval."""
    spec_path = require_shelf_spec(product_dir, filename)
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, version)

    assert_op_ids_served(set(_TYPED_MIRROR), set(ops), spec_label=f"{product_dir}/{filename}")
    for op_id in _TYPED_MIRROR:
        assert ops[op_id].requires_approval is True
        assert ops[op_id].safety_level == _typed_level(op_id)
