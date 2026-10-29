# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pinned-spec reconcile lane for ``host.maintenance.exit`` (#4967).

``vmware.composite.host.maintenance.exit`` sends ``ExitMaintenanceMode_Task``
with ``{"timeout": 0}`` and reads the host's ``runtime`` leaves before and
after. This module grounds those names against the vendor VI/JSON files on
the spec shelf, per the spec-reconcile standard
(``docs/decisions/spec-reconcile-guards-standard.md``):

* ``ExitMaintenanceModeRequestType`` declares ``timeout`` as an integer and
  requires it;
* ``HostRuntimeInfo`` declares ``connectionState``, ``inMaintenanceMode`` and
  ``dasHostState`` (a ``ClusterDasFdmHostState`` with a ``state``);
* the connection-state enum lists ``connected``, the value the handler
  checks before it sends anything.

Checked against the vCenter 9.0 file and the 8.0 U1, U2 and U3 files. Skips
uniformly when the shelf is unprovisioned (#2980 harness contract); CI
provisions it. The connector-side proof -- that the handler puts exactly
this body on the wire -- is ``tests/test_connectors_vmware_rest_composites_write.py``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _write
from tests._spec_shelf import require_shelf_spec
from tests.test_connectors_vmware_rest_host_evacuate_spec_reconcile import (
    _VI_JSON_FILES,
    _component_schema,
    _ref_name,
)


@pytest.fixture(params=sorted(_VI_JSON_FILES), ids=sorted(_VI_JSON_FILES))
def vi_json_text(request: pytest.FixtureRequest) -> str:
    product_dir, filename = _VI_JSON_FILES[request.param]
    return require_shelf_spec(product_dir, filename).read_text(encoding="utf-8")


def test_exit_body_matches_the_vendor_file(vi_json_text: str) -> None:
    """``timeout`` is the one required integer of the exit request type."""
    request_type = _component_schema(vi_json_text, "ExitMaintenanceModeRequestType")
    props = request_type.get("properties", {})
    assert props.get("timeout", {}).get("type") == "integer"
    assert request_type.get("required") == ["timeout"]


def test_host_runtime_reads_match_the_vendor_file(vi_json_text: str) -> None:
    """Every leaf the handler reads is declared on ``HostRuntimeInfo`` with the right type."""
    runtime = _component_schema(vi_json_text, "HostRuntimeInfo")
    props = runtime.get("properties", {})
    assert "connectionState" in props
    assert props.get("inMaintenanceMode", {}).get("type") == "boolean"
    assert _ref_name(props.get("dasHostState", {})) == "ClusterDasFdmHostState"

    das_state = _component_schema(vi_json_text, "ClusterDasFdmHostState")
    assert "state" in das_state.get("properties", {})

    connection_enum = _component_schema(vi_json_text, "HostSystemConnectionState_enum")
    assert _write._HOST_CONNECTION_STATE_CONNECTED in connection_enum.get("enum", [])


def test_names_emitted_by_the_handler_are_the_ones_grounded_above() -> None:
    """The handler's constants are the names the lanes above ground (always-on tie-in)."""
    assert _write._HOST_MAINTENANCE_STATE_PATH_SET == (
        "name",
        "runtime.connectionState",
        "runtime.inMaintenanceMode",
        "runtime.dasHostState",
    )
    assert _write._VIM_SUB_OPS_HOST_MAINTENANCE_EXIT == (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HostSystem/{moId}/ExitMaintenanceMode_Task",
    )
    assert _write._MAINTENANCE_VIM_TIMEOUT == 0
