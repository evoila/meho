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

**VI-JSON methods against the 8.0 U1, U2 and U3 files (#4333).** The vCenter
connector calls VI-JSON methods (``POST /sdk/vim25/{release}/<Type>/<moId>/<Method>``)
in its typed ops and composites. The always-on half reads the connector's source
and collects every method it calls. The shelf-gated half checks each one against
the vendor VI/JSON file of each 8.0 update (``vcenter-8.0/vi-json-8.0u1.yaml``,
``-8.0u2.yaml``, ``-8.0u3.yaml``) and prints the methods a file lacks. Each
missing method must have an entry in :data:`_NOT_IN_8_0_VI_JSON` with the reason;
an entry that is no longer missing fails the lane too, so the list stays true.

**vSAN methods against the 8.0 vSAN SDK (#4441).** The 8.0 VI/JSON files have
no vSAN paths, so on vCenter 8.0 the connector sends vSAN methods as SOAP to
``/vsanHealth``. The shelf-gated half checks every vSAN method the connector
calls (every ``VsanMethod`` constant) against the method lists of the vendor's
vSAN Management SDK for Python 8.0 U1 and U2
(``vcenter-8.0/vsan/8.0u1/vsan-8.0u1-methods.txt``, ``.../8.0u2/...``).

The 8.0 U3 ``vcenter.yaml`` and the three 8.0 VI/JSON files are pinned on the
shelf (provenance: the ``vsphere-8.x`` and ``vsphere-vi-json-8.0u*`` artifacts in
``docs/compatibility/vcf-api-contract-manifest.yaml``), and CI checks out
``vcenter-8.0/`` and requires all four files, so the shelf-gated halves run in
CI (``docs/decisions/vendor-spec-ci-provisioning.md``).
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

import pytest

import meho_backplane.connectors.vmware_rest as vmware_rest_package
from meho_backplane.connectors.vmware_rest.ingest_corrections import TASKS_LIST_OP_PATH
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.catalog import load_spec_resource
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.ingest.spec_corrections import apply_spec_correction
from tests._spec_shelf import openapi_served_op_ids, require_shelf_spec
from tests._vmware_vsan_methods import vsan_methods

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


# ---------------------------------------------------------------------------
# VI-JSON methods against the vendor 8.0 U1 / U2 / U3 VI/JSON files (#4333)
# ---------------------------------------------------------------------------

#: The vendor VI/JSON file of each 8.0 update, as pinned on the shelf.
_VI_JSON_8_0_FILES: dict[str, str] = {
    "8.0u1": "vi-json-8.0u1.yaml",
    "8.0u2": "vi-json-8.0u2.yaml",
    "8.0u3": "vi-json-8.0u3.yaml",
}

#: VI-JSON methods MEHO calls that an 8.0 VI/JSON file does not have. Each
#: entry names the updates that lack it and why the call is still safe there.
#: The lane fails when a file lacks a method that is not listed here, and when
#: a listed method is in fact in the file. The vSAN methods, which no 8.0
#: VI/JSON file has, go as SOAP on 8.0 (#4441, checked below).
_NOT_IN_8_0_VI_JSON: dict[str, tuple[frozenset[str], str]] = {
    "POST:/HostCertificateManager/{moId}/NotifyAffectedServices": (
        frozenset({"8.0u1", "8.0u2"}),
        "In the API from 8.0 U3 on. host.certificate.install checks the vCenter "
        "version and does not send it on an older vCenter.",
    ),
}

#: ``POST:/<Type>/{moId}/<Method>`` -- the governed sub-op form.
_SUB_OP_RE = re.compile(r"^POST:/([A-Z][A-Za-z0-9]*)/\{moId\}/([A-Za-z_][A-Za-z0-9_]*)$")
#: ``/<Type>/<moId>/<Method>`` -- the raw path given to ``_post_vmomi_json``.
#: ``{}`` stands for an f-string placeholder.
_RAW_PATH_RE = re.compile(r"^/([A-Z][A-Za-z0-9]*|\{\})/[^/]+/([A-Za-z_][A-Za-z0-9_]*|\{\})$")

#: Call sites whose method name is only known at run time. Their methods are
#: declared as ``POST:`` sub-op constants next to them, so the collection still
#: sees them. A new site like this must declare its methods the same way and
#: be added here.
_RUNTIME_METHOD_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("composites/_datastore_browse.py", "/{}/{}/{}"),
        ("composites/_host.py", "/HostServiceSystem/{}/{}"),
        ("composites/_host.py", "/HostStorageSystem/{}/{}"),
        ("composites/_write.py", "/{}/{}/{}"),
    }
)


def _string_literals(tree: ast.AST) -> list[str]:
    """Every string constant and f-string in *tree*; placeholders become ``{}``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            found.append(
                "".join(
                    part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
                )
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


@functools.cache
def _scan_vi_json_calls() -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """Read the vCenter connector source; return its VI-JSON methods.

    Returns the ``POST:/<Type>/{moId}/<Method>`` set, plus the raw-path
    templates whose type or method is a placeholder (``(file, template)``).
    """
    package_dir = Path(vmware_rest_package.__file__).parent
    methods: set[str] = set()
    templates: set[tuple[str, str]] = set()
    for source in sorted(package_dir.rglob("*.py")):
        relative = source.relative_to(package_dir).as_posix()
        for text in _string_literals(ast.parse(source.read_text(encoding="utf-8"))):
            match = _SUB_OP_RE.match(text) or _RAW_PATH_RE.match(text)
            if match is None:
                continue
            mo_type, method = match.groups()
            if "{}" in (mo_type, method):
                templates.add((relative, text))
            else:
                methods.add(f"POST:/{mo_type}/{{moId}}/{method}")
    return frozenset(methods), frozenset(templates)


def _vi_json_post_op_ids(spec_path: Path) -> set[str]:
    """``POST:<path>`` for every path item with a POST operation.

    Line-scans the ``paths`` block instead of parsing the 7-8 MB YAML: a path
    key sits at 2-space indent, its operations at 4-space indent. The same
    shape as the 9.0 VI-JSON lanes' check.
    """
    served: set[str] = set()
    in_paths = False
    current: str | None = None
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_paths = line.rstrip() == "paths:"
            current = None
        elif in_paths and line.startswith("  ") and not line.startswith("   "):
            key = line.strip()
            current = key[:-1] if key.startswith("/") and key.endswith(":") else None
        elif current is not None and line.strip() == "post:":
            served.add(f"POST:{current}")
    return served


def test_vi_json_method_collection_is_wired() -> None:
    """The source scan finds the connector's VI-JSON calls and misses none.

    A scan that finds nothing guards nothing, so anchor methods must be
    present. A call whose type is a placeholder must use a method the scan
    already has, and a call whose method is a placeholder must be a known
    site whose methods are declared as ``POST:`` constants.
    """
    methods, templates = _scan_vi_json_calls()
    for anchor in (
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/VirtualMachine/{moId}/ReconfigVM_Task",
        "POST:/HostServiceSystem/{moId}/StartService",
        "POST:/HostDatastoreBrowser/{moId}/SearchDatastore_Task",
    ):
        assert anchor in methods, f"the VI-JSON source scan lost {anchor}"

    method_names = {op_id.rsplit("/", 1)[-1] for op_id in methods}
    runtime_sites = set()
    for relative, template in templates:
        method = template.rsplit("/", 1)[-1]
        if method == "{}":
            runtime_sites.add((relative, template))
        else:
            assert method in method_names, (
                f"{relative} calls VI-JSON method {method!r} on a run-time type, but no "
                f"'POST:/<Type>/{{moId}}/{method}' constant declares it"
            )
    assert runtime_sites == _RUNTIME_METHOD_SITES, (
        "VI-JSON call sites with a run-time method name changed. Declare the "
        "methods of a new site as POST: sub-op constants, then update "
        f"_RUNTIME_METHOD_SITES. Found: {sorted(runtime_sites)}"
    )


def test_8_0_exclusions_name_known_updates_and_called_methods() -> None:
    """Every exclusion names real 8.0 updates, a method MEHO calls, and a reason."""
    methods, _ = _scan_vi_json_calls()
    for op_id, (updates, reason) in _NOT_IN_8_0_VI_JSON.items():
        assert op_id in methods, f"{op_id} is excluded but MEHO no longer calls it"
        assert updates and updates <= set(_VI_JSON_8_0_FILES), op_id
        assert reason.strip(), op_id


@pytest.mark.parametrize("update", sorted(_VI_JSON_8_0_FILES))
def test_vi_json_methods_exist_in_pinned_8_0_file(update: str) -> None:
    """Each VI-JSON method MEHO calls is in the vendor 8.0 file, or is excluded."""
    spec_path = require_shelf_spec(_SHELF_DIR, _VI_JSON_8_0_FILES[update])
    served = _vi_json_post_op_ids(spec_path)
    assert served, f"{spec_path.name} parsed to zero POST paths"
    methods, _ = _scan_vi_json_calls()

    missing = methods - served
    print(f"{spec_path.name}: {len(methods)} VI-JSON methods MEHO calls, {len(missing)} missing")
    for op_id in sorted(missing):
        print(f"  missing: {op_id}")

    expected = {
        op_id for op_id, (updates, _reason) in _NOT_IN_8_0_VI_JSON.items() if update in updates
    }
    unexplained = sorted(missing - expected)
    stale = sorted(expected - missing)
    assert not unexplained, (
        f"{spec_path.name} lacks VI-JSON methods MEHO calls: {unexplained}. Add a "
        "version gate to the operation, or an entry with the reason to "
        "_NOT_IN_8_0_VI_JSON."
    )
    assert not stale, (
        f"{spec_path.name} has these excluded methods, so remove {update!r} from "
        f"their _NOT_IN_8_0_VI_JSON entry: {stale}"
    )


# ---------------------------------------------------------------------------
# vSAN methods against the vendor vSAN Management SDK 8.0 U1 / U2 (#4441)
# ---------------------------------------------------------------------------

#: The method list of each vSAN SDK on the shelf: one ``Type.Method`` per line.
_VSAN_8_0_METHOD_LISTS: dict[str, str] = {
    "8.0u1": "vsan/8.0u1/vsan-8.0u1-methods.txt",
    "8.0u2": "vsan/8.0u2/vsan-8.0u2-methods.txt",
}


def test_vsan_methods_are_not_vi_json_calls_on_8_0() -> None:
    """The VI-JSON scan of the 8.0 lane does not see the vSAN methods.

    On 8.0 they go as SOAP, so they must not show up as VI-JSON methods the
    8.0 VI/JSON files would have to serve.
    """
    methods, _ = _scan_vi_json_calls()
    for vsan_method in vsan_methods():
        assert not any(op_id.endswith(f"/{vsan_method.method}") for op_id in methods)


@pytest.mark.parametrize("update", sorted(_VSAN_8_0_METHOD_LISTS))
def test_vsan_methods_exist_in_pinned_8_0_vsan_sdk(update: str) -> None:
    """Each vSAN method the connector calls is in the vendor's vSAN SDK of that 8.0 update."""
    list_path = require_shelf_spec(_SHELF_DIR, _VSAN_8_0_METHOD_LISTS[update])
    served = {line.strip() for line in list_path.read_text(encoding="utf-8").splitlines()}
    called = {f"{method.mo_type}.{method.method}" for method in vsan_methods()}
    assert called, "the vSAN method collection found nothing"
    missing = sorted(called - served)
    assert not missing, f"{list_path.name} lacks vSAN methods MEHO calls: {missing}"
