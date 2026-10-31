# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the host power operations against the pinned VI-JSON files.

``host.maintenance.enter``, ``host.reboot``, ``host.shutdown``,
``host.standby.enter`` and ``host.standby.exit`` each send one
``HostSystem`` task method after a ``RetrievePropertiesEx`` read of the host.

* An always-on test pins the step lists and the host properties, so a change
  to them is seen.
* The shelf tests read the vCenter 9.0 file and the 8.0 U1, U2 and U3 files:
  every method is a POST path, every request field MEHO sends is declared with
  its type, every host property MEHO reads is declared, and the state values
  MEHO compares with are in the vendor enums. They skip when the spec shelf is
  not configured and run in CI.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import (
    _host_maintenance_enter,
    _host_power,
    _host_power_common,
)
from meho_backplane.connectors.vmware_rest.composites._host_power_ops import (
    HOST_POWER_GOVERNED_SUBOPS,
)
from tests._spec_shelf import require_shelf_spec
from tests.test_connectors_vmware_rest_host_evacuate_spec_reconcile import (
    _VI_JSON_FILES,
    _component_schema,
)

_READ = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

#: The methods, and the request fields MEHO sends with their vendor types.
_METHODS: dict[str, dict[str, str]] = {
    "EnterMaintenanceMode": {"timeout": "integer"},
    "RebootHost": {"force": "boolean"},
    "ShutdownHost": {"force": "boolean"},
    "PowerDownHostToStandBy": {"timeoutSec": "integer", "evacuatePoweredOffVms": "boolean"},
    "PowerUpHostFromStandBy": {"timeoutSec": "integer"},
}


@pytest.fixture(params=sorted(_VI_JSON_FILES), ids=sorted(_VI_JSON_FILES))
def vi_json_text(request: pytest.FixtureRequest) -> str:
    product_dir, filename = _VI_JSON_FILES[request.param]
    return require_shelf_spec(product_dir, filename).read_text(encoding="utf-8")


def test_step_lists_are_pinned() -> None:
    """Each operation reads with RetrievePropertiesEx and sends one HostSystem method."""
    assert {op: steps[1] for op, steps in HOST_POWER_GOVERNED_SUBOPS.items()} == {
        "vmware.composite.host.maintenance.enter": (
            "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task"
        ),
        "vmware.composite.host.reboot": "POST:/HostSystem/{moId}/RebootHost_Task",
        "vmware.composite.host.shutdown": "POST:/HostSystem/{moId}/ShutdownHost_Task",
        "vmware.composite.host.standby.enter": (
            "POST:/HostSystem/{moId}/PowerDownHostToStandBy_Task"
        ),
        "vmware.composite.host.standby.exit": (
            "POST:/HostSystem/{moId}/PowerUpHostFromStandBy_Task"
        ),
    }
    assert all(
        steps[0] == _READ and len(steps) == 2 for steps in HOST_POWER_GOVERNED_SUBOPS.values()
    )
    assert _host_power_common.HOST_STATE_PROPS == (
        "name",
        "runtime.connectionState",
        "runtime.powerState",
        "runtime.inMaintenanceMode",
        "runtime.standbyMode",
        "runtime.dasHostState",
    )
    assert _host_maintenance_enter._ENTER_MAINTENANCE_DESCRIPTION_ID == (
        "HostSystem.enterMaintenanceMode"
    )


def test_methods_are_post_paths(vi_json_text: str) -> None:
    lines = set(vi_json_text.splitlines())
    for steps in HOST_POWER_GOVERNED_SUBOPS.values():
        for op_id in steps:
            path = op_id.split(":", 1)[1]
            assert f"  {path}:" in lines, f"{path} is not a path in the pinned file"
    assert "  /HostSystem/{moId}/recentTask:" in lines


@pytest.mark.parametrize("method", sorted(_METHODS))
def test_request_fields_match_the_vendor_file(vi_json_text: str, method: str) -> None:
    request_type = _component_schema(vi_json_text, f"{method}RequestType")
    props = request_type.get("properties", {})
    for name, kind in _METHODS[method].items():
        assert props.get(name, {}).get("type") == kind, f"{method}.{name}"
    assert set(request_type.get("required", [])) <= set(_METHODS[method])


def test_host_reads_and_states_match_the_vendor_file(vi_json_text: str) -> None:
    runtime = _component_schema(vi_json_text, "HostRuntimeInfo").get("properties", {})
    for prop in _host_power_common.HOST_STATE_PROPS[1:]:
        assert prop.removeprefix("runtime.") in runtime, prop
    power = _component_schema(vi_json_text, "HostSystemPowerState_enum").get("enum", [])
    assert {_host_power._POWERED_ON, _host_power._STANDBY} <= set(power)
    connection = _component_schema(vi_json_text, "HostSystemConnectionState_enum").get("enum", [])
    assert _host_power_common.CONNECTED in connection
    task_state = _component_schema(vi_json_text, "TaskInfoState_enum").get("enum", [])
    assert set(task_state) >= _host_maintenance_enter._RUNNING_TASK_STATES
