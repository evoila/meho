# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile lane for the two content-library find reads (#3550).

``vmware.content_library.find`` and ``vmware.content_library.item.find`` call
vSphere REST directly. This file checks those calls in two steps:

* **shape (always runs)** -- the module declares exactly the two find calls,
  the same calls the composites use;
* **against the pinned spec (skips without the vendor-licensed shelf)** --
  both calls are served by the ``vcenter.yaml`` of vCenter 8.0 and 9.0, and
  every parameter of the two ops is a top-level property of the request body
  the 9.0 spec declares (the ``FindSpec`` itself, not a ``{"spec": {...}}``
  wrapper).
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.typed_ops_content_library_find import (
    CONTENT_LIBRARY_FIND_REST_CALLS,
    FIND_LIBRARY_CALL,
    FIND_LIBRARY_ITEM_CALL,
    VMWARE_CONTENT_LIBRARY_FIND_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
)
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SHELVES = ["vcenter-8.0", "vcenter-9.0"]

_PARAMS_BY_CALL = {
    FIND_LIBRARY_CALL: VMWARE_CONTENT_LIBRARY_FIND_OP,
    FIND_LIBRARY_ITEM_CALL: VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
}


def test_declared_calls_match_the_frozen_list() -> None:
    """Shape gate (always runs)."""
    assert set(CONTENT_LIBRARY_FIND_REST_CALLS) == {
        "POST:/content/library?action=find",
        "POST:/content/library/item?action=find",
    }
    # The composites send the same two calls through the same helper.
    assert (_write._OP_FIND_LIBRARY, _write._OP_FIND_LIBRARY_ITEM) == (
        FIND_LIBRARY_CALL,
        FIND_LIBRARY_ITEM_CALL,
    )


@pytest.mark.parametrize("shelf", _SHELVES)
def test_both_calls_are_served_by_the_pinned_vcenter_spec(shelf: str) -> None:
    """Path existence (skips without the shelf)."""
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    assert_op_ids_served(
        CONTENT_LIBRARY_FIND_REST_CALLS, served, spec_label=f"{shelf}/vcenter.yaml"
    )


def test_every_parameter_is_a_top_level_find_spec_field() -> None:
    """Body shape (skips without the shelf): the ``FindSpec`` fields sit at the top level.

    Only the 9.0 file: the 8.0 file names the body through a shared
    ``components/requestBodies`` entry, and the ingest parser reads no body
    fields from that form.
    """
    shelf = "vcenter-9.0"
    spec_path = require_shelf_spec(shelf, "vcenter.yaml")
    body_props = openapi_request_body_props(spec_path, spec_source="spec:vcenter.yaml")
    for call, op in _PARAMS_BY_CALL.items():
        served = body_props.get(call, set())
        assert "spec" not in served, f"{shelf}: {call} body is wrapped in 'spec'"
        missing = set(op.parameter_schema["properties"]) - served
        assert not missing, f"{shelf}: {op.op_id} params not in the {call} body: {sorted(missing)}"
