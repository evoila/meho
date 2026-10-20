# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vmware-rest-8.0 (vSphere 8.0 U3) shipped-surface reconcile lane (#3569) — #2980 harness.

Two guards for the second versioned ``vmware-rest`` catalog:

**Always-on surface pin (manifest-pin analog).** The 8.0 U3 catalog surfaces the
same governed inventory-read core as the 9.0 catalog, so the shipped 8.0 minimal
spec (``vmware_rest_8_0_minimal.yaml``) must parse cleanly and emit exactly the
same served ``METHOD:/path`` op_id set as the shipped 9.0 minimal
(``vmware_rest_minimal.yaml``). Editing one shipped spec without the other trips
this lane loudly. Runs unconditionally in the required unit sweep (no DB /
embeddings / containers), the same shape as the ``sddc-vcf5`` manifest-pin lane
for a hand-coded surface whose full vendor OpenAPI is not committed here.

**Shelf-gated strengthening (runs in CI).** When the pinned
8.0 U3 vendor ``vcenter.yaml`` is on the private spec shelf
(``vcenter-8.0/vcenter.yaml`` under ``MEHO_CONSUMER_DOCS_ROOT``, the same
convention as ``vcenter-9.0/``), the shipped minimal's inventory-read op_ids must
be a subset of the vendor-served set -- proving the ops MEHO surfaces really
exist in the vendor 8.0 U3 catalog. The ``/api/about`` fingerprint-probe path is
excluded: it is the ESXi/host REST surface MEHO probes, not part of the vCenter
Automation ``vcenter.yaml`` (its only about is ``/api/vcenter/phm/about``). Skips
with the uniform reason until the shelf is wired -- a red lane there is a real
drift finding, not harness noise.

**Tasks-list body-shape guard (#3700).** ``POST /cis/tasks?action=list`` is
not in the shipped minimal; it arrives with a full-surface re-ingest. The 8.0
spec gives it a flat body (``exclude_result`` / ``return_all`` at the top),
which a vCenter 8.0.3 server rejects. The connector's ingest-time correction
(``connectors/vmware_rest/ingest_corrections.py``) replaces it with the
``{filter_spec, result_spec}`` body. The always-on half runs a MEHO-authored
fixture of the flat shape through the real parser plus the correction and pins
the result; the shelf-gated half does the same against the pinned vendor
8.0 U3 ``vcenter.yaml``.

The 8.0 U3 ``vcenter.yaml`` is pinned on the shelf (provenance: the
``vsphere-8.x`` artifact in ``docs/compatibility/vcf-api-contract-manifest.yaml``),
and CI checks out ``vcenter-8.0/`` and requires the file, so the shelf-gated half
runs in CI (``docs/decisions/vendor-spec-ci-provisioning.md``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meho_backplane.connectors.vmware_rest.ingest_corrections import TASKS_LIST_OP_PATH
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.catalog import load_spec_resource
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.ingest.spec_corrections import apply_spec_correction
from tests._spec_shelf import openapi_served_op_ids, require_shelf_spec

_SHIPPED_8_0_SPEC = "vmware_rest_8_0_minimal.yaml"
_SHIPPED_9_0_SPEC = "vmware_rest_minimal.yaml"

#: The vCenter Automation ``vcenter.yaml`` has no ``/api/about`` -- that path is
#: the ESXi/host REST surface MEHO probes for fingerprinting. Excluded from the
#: vendor-served subset check (the only shipped op not sourced from the
#: Automation spec).
_FINGERPRINT_PROBE_OP_ID = "GET:/api/about"

#: Private-shelf location for the pinned 8.0 U3 vendor catalog, mirroring the
#: ``vcenter-9.0/`` convention (see tests/acceptance/_vcenter_spec.py).
_SHELF_DIR = "vcenter-8.0"
_SHELF_FILE = "vcenter.yaml"


def _shipped_served_op_ids(spec_resource: str) -> set[str]:
    """Served ``METHOD:/path`` op_ids of a shipped, package-data minimal spec."""
    content = load_spec_resource(spec_resource)
    rows = parse_openapi(
        f"spec:{spec_resource}",
        spec_source=f"spec:{spec_resource}",
        content=content,
    )
    return {row.op_id for row in rows}


def test_shipped_8_0_minimal_matches_9_0_governed_read_core() -> None:
    """The 8.0 U3 shipped minimal surfaces exactly the 9.0 governed read core."""
    served_8_0 = _shipped_served_op_ids(_SHIPPED_8_0_SPEC)
    assert served_8_0, "the 8.0 U3 shipped minimal parsed to zero served ops"
    served_9_0 = _shipped_served_op_ids(_SHIPPED_9_0_SPEC)
    assert served_8_0 == served_9_0, (
        "the 8.0 U3 and 9.0 shipped minimal specs must surface the same governed "
        "inventory-read core; update both in lock-step"
    )


def test_shipped_8_0_minimal_is_subset_of_pinned_vendor_catalog() -> None:
    """Shipped 8.0 read ops exist in the pinned vendor 8.0 U3 vcenter.yaml (shelf-gated)."""
    spec_path = require_shelf_spec(_SHELF_DIR, _SHELF_FILE)
    vendor_served = openapi_served_op_ids(spec_path, spec_source="spec:vcenter.yaml")
    shipped = _shipped_served_op_ids(_SHIPPED_8_0_SPEC) - {_FINGERPRINT_PROBE_OP_ID}
    missing = shipped - vendor_served
    assert not missing, (
        f"shipped 8.0 minimal declares op_ids absent from the pinned vendor 8.0 U3 "
        f"catalog: {sorted(missing)}"
    )


_TASKS_LIST_OP_ID = f"POST:{TASKS_LIST_OP_PATH}"

#: MEHO-authored fixture of the flat body the 8.0 spec gives the tasks-list
#: op (the ``GetSpec`` fields at the top of the body). Only the shape matters.
_FLAT_TASKS_LIST_SPEC = """
openapi: 3.0.3
info:
  title: tasks-list body fixture
  version: 8.0.3
servers:
  - url: https://{host}/api
    variables:
      host:
        default: localhost
paths:
  /cis/tasks?action=list:
    post:
      operationId: list
      requestBody:
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/GetSpec"
      responses:
        "200":
          description: Map of task id to task info.
          content:
            application/json:
              schema:
                type: object
components:
  schemas:
    GetSpec:
      type: object
      properties:
        return_all:
          type: boolean
        exclude_result:
          type: boolean
"""


def _corrected_tasks_list(rows: list[EndpointDescriptorProto], version: str) -> dict[str, object]:
    """Run the tasks-list row through the connector correction; return its schema."""
    (row,) = [r for r in rows if r.op_id == _TASKS_LIST_OP_ID]
    corrected = apply_spec_correction(
        product="vmware", version=version, impl_id="vmware-rest", proto=row
    )
    return corrected.parameter_schema


def _body_props(parameter_schema: dict[str, object]) -> set[str]:
    properties = parameter_schema["properties"]
    assert isinstance(properties, dict)
    return set(properties["body"]["properties"])


def test_8_0_tasks_list_body_is_filter_spec_and_result_spec() -> None:
    """The flat 8.0 body is corrected to ``{filter_spec, result_spec}`` (#3700)."""
    rows = parse_openapi(
        "spec:tasks-list-fixture", spec_source="spec:vcenter.yaml", content=_FLAT_TASKS_LIST_SPEC
    )
    assert _body_props(rows[0].parameter_schema) == {"return_all", "exclude_result"}

    schema = _corrected_tasks_list(rows, "8.0")
    assert _body_props(schema) == {"filter_spec", "result_spec"}

    good = {"body": {"filter_spec": {"services": ["com.vmware.vcenter.vm"]}}}
    assert validate_params(schema, good) == []
    flat = {"body": {"return_all": True, "exclude_result": True}}
    assert validate_params(schema, flat), "the flat body must be refused before vCenter"
    empty = {"body": {"filter_spec": {}}}
    assert validate_params(schema, empty), "an empty filter must be refused before vCenter"


def test_9_0_tasks_list_body_is_left_as_ingested() -> None:
    """The 9.0 spec models the body correctly, so the correction leaves it alone."""
    rows = parse_openapi(
        "spec:tasks-list-fixture", spec_source="spec:vcenter.yaml", content=_FLAT_TASKS_LIST_SPEC
    )
    (row,) = rows
    assert (
        apply_spec_correction(product="vmware", version="9.0", impl_id="vmware-rest", proto=row)
        is row
    )


def test_8_0_tasks_list_body_shape_in_pinned_vendor_catalog() -> None:
    """Against the pinned vendor 8.0 U3 vcenter.yaml, the corrected body is two-level."""
    spec_path: Path = require_shelf_spec(_SHELF_DIR, _SHELF_FILE)
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_path.read_text(encoding="utf-8"),
    )
    if not any(r.op_id == _TASKS_LIST_OP_ID for r in rows):
        pytest.fail(f"pinned vendor 8.0 U3 catalog does not serve {_TASKS_LIST_OP_ID}")
    assert _body_props(_corrected_tasks_list(rows, "8.0")) == {"filter_spec", "result_spec"}
