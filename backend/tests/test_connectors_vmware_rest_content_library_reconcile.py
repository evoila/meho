# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-shelf reconcile test for the typed content-library reads.

The six ``vmware.content_library.*`` / ``vmware.supervisor.content_library.list``
reads call the vendor REST paths in
:data:`~meho_backplane.connectors.vmware_rest.content_library_reads.VENDOR_OP_IDS`.
Per ``docs/decisions/spec-reconcile-guards-standard.md``:

* **shape (always runs)** -- the declared ``METHOD:/path`` set matches the
  frozen list below;
* **paths (skips without the shelf)** -- the pinned 9.0 ``vcenter.yaml`` serves
  every path, and the pinned 8.0 ``vcenter.yaml`` serves every path but the
  Supervisor library list, which vCenter has only since 9.0 (the operation
  stops early on vCenter 8);
* **bodies (skips without the shelf)** -- in the pinned 9.0 ``vcenter.yaml``
  the OVF ``filter`` body has a top-level ``target`` and the ``probe`` body a
  top-level ``subscription_info``, the shapes the reads send.

The two files name path placeholders differently (``{library_id}`` in 8.0,
``{libraryId}`` in 9.0), so the paths are compared with every placeholder
written ``{}``.
"""

from __future__ import annotations

import functools
import re

import pytest

from meho_backplane.connectors.vmware_rest.content_library_reads import VENDOR_OP_IDS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_request_body_props,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_FILE = "vcenter.yaml"
_SUPERVISOR_LIST = "GET:/vcenter/namespace-management/lifecycle/content/libraries"
_OVF_FILTER = "POST:/vcenter/ovf/library-item/{}?action=filter"
_PROBE = "POST:/content/subscribed-library?action=probe"

_EXPECTED_OP_IDS = {
    "POST:/content/library?action=find",
    "GET:/content/library/{}",
    "GET:/content/local-library/{}",
    "GET:/content/subscribed-library/{}",
    "GET:/content/library/item?library_id",
    "GET:/content/library/item/{}",
    "GET:/content/library/item/{}/file",
    "GET:/content/library/item/{}/storage",
    _OVF_FILTER,
    _PROBE,
    _SUPERVISOR_LIST,
}

_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _normal(op_id: str) -> str:
    return _PLACEHOLDER.sub("{}", op_id)


@functools.cache
def _served(product_dir: str) -> frozenset[str]:
    spec_path = require_shelf_spec(product_dir, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path, spec_source=f"spec:{_SPEC_FILE}")
    return frozenset(_normal(op_id) for op_id in served)


def test_declared_vendor_calls_are_frozen() -> None:
    assert set(VENDOR_OP_IDS) == _EXPECTED_OP_IDS


def test_paths_are_served_by_the_pinned_9_0_spec() -> None:
    assert_op_ids_served(
        VENDOR_OP_IDS, _served("vcenter-9.0"), spec_label="vcenter-9.0/vcenter.yaml"
    )


def test_paths_are_served_by_the_pinned_8_0_spec_except_the_supervisor_list() -> None:
    served = _served("vcenter-8.0")
    assert_op_ids_served(
        VENDOR_OP_IDS - {_SUPERVISOR_LIST}, served, spec_label="vcenter-8.0/vcenter.yaml"
    )
    assert _SUPERVISOR_LIST not in served, (
        "vCenter 8.0 now serves the Supervisor library list; drop the version check "
        "of vmware.supervisor.content_library.list"
    )


@pytest.mark.parametrize(
    ("op_id", "field"), [(_OVF_FILTER, "target"), (_PROBE, "subscription_info")]
)
def test_post_bodies_have_the_wrapper_the_reads_send(op_id: str, field: str) -> None:
    spec_path = require_shelf_spec("vcenter-9.0", _SPEC_FILE)
    bodies = {
        _normal(key): props
        for key, props in openapi_request_body_props(
            spec_path, spec_source=f"spec:{_SPEC_FILE}"
        ).items()
    }
    assert op_id in bodies, f"vcenter-9.0/{_SPEC_FILE} has no body for {op_id}"
    assert bodies[op_id] == {field}
