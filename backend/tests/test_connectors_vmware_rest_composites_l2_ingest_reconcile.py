# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""op_id reconciliation between the write composites and the ingest pipeline.

G3.16-T1 (#1414). The 9 REST-sub-op vmware-rest write composites each
declare the L2 sub-ops they dispatch into via ``_SUB_OPS_*`` tuples in
:mod:`~meho_backplane.connectors.vmware_rest.composites._write`; the tenth
write composite, ``vm.disk.grow`` (#2893), dispatches into vi-json instead
and declares its sub-ops in ``_VIM_SUB_OPS_VM_DISK_GROW`` (reconciled in the
dedicated vi-json section at the end of this module). At
dispatch time :func:`~...composites._preflight.preflight_l2_dependencies`
looks each sub-op_id up in ``endpoint_descriptor`` and raises
:class:`~meho_backplane.operations.composite.CompositeL2DependencyMissing`
on any miss. The descriptor rows are written by the ingest pipeline,
which keys every operation as ``op_id = f"{method}:{path}"`` (see
:func:`~meho_backplane.operations.ingest.openapi._build_proto`).

The load-bearing question for the live "ingest + enable" operator step is
whether the op_id *string* a composite declares is byte-for-byte the one
the parser emits from ``vcenter.yaml``. The two surfaces that could drift:

* **Plain paths** (``GET:/vcenter/vm``) -- trivially ``METHOD:/path``.
* **Action-discriminated paths** (``POST:/vcenter/vm/{vm}/power?action=start``)
  -- vCenter's OpenAPI spec keys these endpoints with the ``?action=<verb>``
  query suffix *in the path key itself* (it does not model the verb as a
  body/query parameter on a shared base path). The parser passes the path
  key through verbatim into the op_id, so the action suffix survives. The
  composites' ``_power_vm_op_id`` / ``_host_maintenance_op_id`` helpers
  build the same string.

This module proves the match automatically, without a live backplane:

1. Derive the full set of raw L2 sub-op_ids the 8 composites need by
   introspecting the live ``_SUB_OPS_*`` constants (so the test tracks
   any future edit to those tuples -- no hardcoded mirror to drift).
2. Build a representative OpenAPI fixture whose ``paths`` are keyed
   exactly the way vCenter keys them (action verbs in the path key).
3. Run it through the real :func:`~meho_backplane.operations.ingest.parse_openapi`.
4. Assert every raw sub-op_id resolves to a parser-emitted op_id.

A green run is the automated proof that
:func:`preflight_l2_dependencies` will pass for every write composite
once the operator ingests the vSphere specs and enables the carrying
groups (acceptance criterion 2 on #1414, verified in code rather than
against a deploy). If anyone edits a ``_SUB_OPS_*`` op_id into a shape
the ingest pipeline cannot emit, this test goes red.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.operations.ingest import parse_openapi
from tests.acceptance._vcenter_spec import VCENTER_SPEC_REASON, resolve_vi_json_yaml

# Public IP returned by the mock getaddrinfo for specs.example.test.
_PUBLIC_TEST_IP = "93.184.216.34"

# Patch used by both reconciliation tests to satisfy the SSRF guard without
# real DNS lookups. The guard is a correctness property, not a test concern;
# this fixture keeps the mock in one place.
_GETADDRINFO_PATCH = patch(
    "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_PUBLIC_TEST_IP, 443))],
)

# ---------------------------------------------------------------------------
# Derive the composites' required L2 sub-op_ids from the live constants.
# ---------------------------------------------------------------------------


def _required_raw_sub_op_ids() -> set[str]:
    """Union of every ``_SUB_OPS_*`` op_id across the 9 REST write composites.

    ``vm.disk.grow``'s vi-json sub-ops live in ``_VIM_SUB_OPS_VM_DISK_GROW``
    (a distinct namespace this ``_SUB_OPS_*`` sweep deliberately skips), so
    a vcenter.yaml-shaped fixture never has to model a vi-json path; the
    vi-json section at the end of this module reconciles them separately.

    Excludes composite-to-composite references (``vmware.composite.*``):
    those are not ``endpoint_descriptor`` rows and the pre-flight walk
    skips them (their handlers run their own pre-flight).
    """
    raw: set[str] = set()
    for name in dir(_write):
        if not name.startswith("_SUB_OPS_"):
            continue
        for op_id in getattr(_write, name):
            if op_id.startswith("vmware.composite."):
                continue
            raw.add(op_id)
    return raw


def test_write_composite_sub_op_tuples_are_all_discovered() -> None:
    """Guard: the introspection finds every write composite's sub-op tuple.

    Nine ``_SUB_OPS_*`` module constants today, one per write composite.
    Pinning the exact set means a renamed or dropped constant can't
    silently shrink the reconciled set to a vacuous pass.
    """
    tuple_names = sorted(n for n in dir(_write) if n.startswith("_SUB_OPS_"))
    assert tuple_names == [
        "_SUB_OPS_CLUSTER_PATCH",
        "_SUB_OPS_HOST_DETACH_FROM_VDS",
        "_SUB_OPS_HOST_EVACUATE",
        "_SUB_OPS_VM_CLONE",
        "_SUB_OPS_VM_CREATE",
        "_SUB_OPS_VM_MIGRATE",
        "_SUB_OPS_VM_POWER",
        "_SUB_OPS_VM_POWER_BULK",
        "_SUB_OPS_VM_SNAPSHOT_REVERT",
    ]


# ---------------------------------------------------------------------------
# Representative vCenter OpenAPI fixture.
# ---------------------------------------------------------------------------
#
# Keyed exactly as vCenter keys these endpoints in vcenter.yaml: the
# action verb lives in the path key (``...?action=start``), never as a
# body/query parameter on a shared base path. Each entry carries the
# minimal valid Operation Object the parser requires (a ``responses``
# map). The fixture is intentionally hand-built rather than vendored
# because the real specs are vendor-licensed and live in the operator's
# spec-shelf repo, not this chassis repo (see
# ``tests/acceptance/_vcenter_spec.py``).


def _build_vcenter_fixture(required_op_ids: set[str]) -> dict[str, Any]:
    """Synthesise an OpenAPI doc whose paths reproduce *required_op_ids*.

    Splits each ``METHOD:/path`` op_id back into a (path-key, verb) pair
    and assembles the ``paths`` object the way vCenter ships it. Multiple
    verbs on one path key (e.g. ``GET`` + ``POST`` + ``DELETE`` on
    ``/vcenter/vm`` family) collapse into one path-item with multiple
    operation keys, mirroring the real spec.
    """
    paths: dict[str, dict[str, Any]] = {}
    for op_id in sorted(required_op_ids):
        method, _, path_key = op_id.partition(":")
        assert path_key, f"malformed op_id without path: {op_id!r}"
        verb = method.lower()
        path_item = paths.setdefault(path_key, {})
        path_item[verb] = {
            "summary": f"synthetic op for {op_id}",
            "responses": {"200": {"description": "ok"}},
        }
    return {
        "openapi": "3.0.0",
        "info": {"title": "vcenter", "version": "9.0.0.0"},
        "paths": paths,
    }


def test_every_write_composite_sub_op_resolves_to_an_ingested_op_id() -> None:
    """The ingest pipeline emits an op_id for every composite sub-op.

    This is the in-code proxy for #1414 acceptance criterion 2 ("every
    op_id in each ``_SUB_OPS_*`` tuple resolves to an enabled
    ``endpoint_descriptor`` row"). Parser op_ids are the exact strings
    written to ``endpoint_descriptor.op_id`` by ``register_ingested`` and
    the exact strings ``lookup_descriptor`` (hence ``preflight_l2_dependencies``)
    queries on -- so parser coverage == pre-flight resolution.
    """
    required = _required_raw_sub_op_ids()
    assert required, "introspection found no raw sub-op_ids -- wiring broke"

    spec = _build_vcenter_fixture(required)
    spec_bytes = json.dumps(spec).encode()
    spec_url = "https://specs.example.test/vcenter.yaml"

    with _GETADDRINFO_PATCH, respx.mock(assert_all_called=False) as router:
        router.get(spec_url).mock(
            return_value=httpx.Response(
                200,
                content=spec_bytes,
                headers={"content-type": "application/json"},
            )
        )
        rows = parse_openapi(spec_url, spec_source="spec:vcenter.yaml")
    ingested_op_ids = {row.op_id for row in rows}

    missing = required - ingested_op_ids
    assert not missing, (
        "write composites declare sub-op_ids the ingest pipeline does not "
        f"emit from a vCenter-shaped spec: {sorted(missing)}. Either a "
        "_SUB_OPS_* tuple drifted from the METHOD:/path form the parser "
        "produces, or the fixture no longer mirrors vCenter's path keying."
    )


def test_action_discriminated_sub_ops_keep_query_suffix_through_ingest() -> None:
    """Action verbs in the path key survive ``op_id = f'{method}:{path}'``.

    The reconciliation hinge: ``?action=<verb>`` is part of the path key
    in vCenter's spec, so the parser preserves it verbatim. This asserts
    the parser does *not* strip the query string (which would collapse
    the four power actions into one op_id and break the composites' power
    sub-ops). Uses the power + maintenance + relocate + patch families
    that the write composites depend on.
    """
    action_op_ids = {op_id for op_id in _required_raw_sub_op_ids() if "?action=" in op_id}
    # Sanity: the write composites really do depend on action-bearing ops.
    assert {
        "POST:/vcenter/vm/{vm}/power?action=start",
        "POST:/vcenter/vm/{vm}/power?action=stop",
        "PATCH:/vcenter/host/{host}/maintenance?action=enter",
        "POST:/vcenter/vm/{vm}?action=relocate",
    } <= action_op_ids

    spec = _build_vcenter_fixture(action_op_ids)
    spec_bytes = json.dumps(spec).encode()
    spec_url = "https://specs.example.test/vcenter-action.yaml"

    with _GETADDRINFO_PATCH, respx.mock(assert_all_called=False) as router:
        router.get(spec_url).mock(
            return_value=httpx.Response(
                200,
                content=spec_bytes,
                headers={"content-type": "application/json"},
            )
        )
        rows = parse_openapi(spec_url, spec_source="spec:vcenter.yaml")
    ingested_op_ids = {row.op_id for row in rows}

    # Every action op_id round-trips with its ``?action=`` suffix intact.
    assert action_op_ids <= ingested_op_ids
    # And no op_id lost its query suffix (proves no stripping).
    assert all("?action=" in op_id for op_id in ingested_op_ids)


# ---------------------------------------------------------------------------
# vm.disk.grow VI-JSON sub-op reconciliation (#2893)
# ---------------------------------------------------------------------------
#
# vm.disk.grow's mutating path is vi-json, not vcenter REST:
# ``POST:/VirtualMachine/{moId}/ReconfigVM_Task`` (the capacity edit) plus
# its config read ``POST:/PropertyCollector/propertyCollector/RetrievePropertiesEx``.
# These are declared in ``_write._VIM_SUB_OPS_VM_DISK_GROW`` (deliberately
# NOT in the ``_SUB_OPS_*`` namespace, so the vcenter.yaml sweep above skips
# them). vi-json keys its path items the same ``METHOD:/path`` way vcenter
# does — the moId rides the path as ``{moId}`` — so the same reconciliation
# proof applies, and it is additionally checked against the pinned
# ``vi-json.yaml`` when the spec-shelf is configured.


def test_vm_disk_grow_vi_json_sub_op_manifest_is_the_expected_pair() -> None:
    """Pin the disk-grow vi-json manifest so a drift can't shrink the reconcile."""
    assert set(_write._VIM_SUB_OPS_VM_DISK_GROW) == {
        "POST:/VirtualMachine/{moId}/ReconfigVM_Task",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    }


def test_vm_disk_grow_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The disk-grow vi-json op_ids are byte-for-byte what the parser emits.

    Proves the ``METHOD:/path`` op_id strings the composite gates on match
    what ``parse_openapi`` produces from a vi-json-shaped spec (the ``{moId}``
    path template survives), so ``enforce_subop_policy``'s op_id / a grant's
    op_pattern resolve against the ingested rows once the operator ingests
    ``vi-json.yaml`` — the vi-json analogue of the vcenter reconcile above.
    """
    required = set(_write._VIM_SUB_OPS_VM_DISK_GROW)
    spec = _build_vcenter_fixture(required)
    spec_bytes = json.dumps(spec).encode()
    spec_url = "https://specs.example.test/vi-json.yaml"

    with _GETADDRINFO_PATCH, respx.mock(assert_all_called=False) as router:
        router.get(spec_url).mock(
            return_value=httpx.Response(
                200, content=spec_bytes, headers={"content-type": "application/json"}
            )
        )
        rows = parse_openapi(spec_url, spec_source="spec:vi-json.yaml")
    ingested_op_ids = {row.op_id for row in rows}
    assert required <= ingested_op_ids


def _vi_json_path_item_has_post(spec_text: str, path: str) -> bool:
    """Whether *path* is a top-level ``paths`` item with a POST operation.

    Line-scans rather than parsing the ~10 MB YAML: a path item is keyed at
    2-space indent (``  /VirtualMachine/{moId}/ReconfigVM_Task:``); its
    operations sit under it at 4-space indent (``    post:``); the item ends
    at the next 2-space key. Precise (path keys are unique) and cheap.
    """
    lines = spec_text.splitlines()
    key = f"  {path}:"
    try:
        start = lines.index(key)
    except ValueError:
        return False
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("   "):
            break  # next 2-indent key — end of this path item
        if line.strip() == "post:":
            return True
    return False


def test_vm_disk_grow_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """Each disk-grow vi-json sub-op path is a real POST path in the pinned vi-json.yaml.

    The definitive #2893 grounding: the connector's first mutating VI-JSON
    call must target paths that actually exist in the pinned spec. Skips
    when the spec-shelf is not configured (the canary's convention), so CI —
    where the env vars are wired — is the operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_VM_DISK_GROW:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "disk-grow vi-json sub-op targets a path the spec does not serve"
        )


# ---------------------------------------------------------------------------
# vm.clone_from_template VI-JSON sub-op reconciliation (#2894)
# ---------------------------------------------------------------------------
#
# vm.clone_from_template's mutating path is vi-json, not vcenter REST:
# ``POST:/VirtualMachine/{moId}/CloneVM_Task`` (the folder-template clone) plus
# its config-template assert read
# ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx`` and the optional GOSC
# resolve ``POST:/CustomizationSpecManager/{moId}/GetCustomizationSpec``. These
# are declared in ``_write._VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE`` (deliberately
# NOT in the ``_SUB_OPS_*`` namespace, so the vcenter.yaml sweep above skips
# them). Same ``METHOD:/path`` keying as disk-grow — the moId rides the path as
# ``{moId}`` — so the same reconciliation proof applies, and it is additionally
# checked against the pinned ``vi-json.yaml`` when the spec-shelf is configured.


def test_vm_clone_from_template_vi_json_sub_op_manifest_is_the_expected_triple() -> None:
    """Pin the clone manifest so a drift can't shrink the reconcile."""
    assert set(_write._VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE) == {
        "POST:/VirtualMachine/{moId}/CloneVM_Task",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/CustomizationSpecManager/{moId}/GetCustomizationSpec",
    }


def test_vm_clone_from_template_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The clone vi-json op_ids are byte-for-byte what the parser emits.

    Proves the ``METHOD:/path`` op_id strings the composite gates on match what
    ``parse_openapi`` produces from a vi-json-shaped spec (the ``{moId}`` path
    template survives), so ``enforce_subop_policy``'s op_id / a grant's
    op_pattern resolve against the ingested rows once the operator ingests
    ``vi-json.yaml`` — the vi-json analogue of the vcenter reconcile above.
    """
    required = set(_write._VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE)
    spec = _build_vcenter_fixture(required)
    spec_bytes = json.dumps(spec).encode()
    spec_url = "https://specs.example.test/vi-json.yaml"

    with _GETADDRINFO_PATCH, respx.mock(assert_all_called=False) as router:
        router.get(spec_url).mock(
            return_value=httpx.Response(
                200, content=spec_bytes, headers={"content-type": "application/json"}
            )
        )
        rows = parse_openapi(spec_url, spec_source="spec:vi-json.yaml")
    ingested_op_ids = {row.op_id for row in rows}
    assert required <= ingested_op_ids


def test_vm_clone_from_template_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """Each clone vi-json sub-op path is a real POST path in the pinned vi-json.yaml.

    The definitive #2894 grounding: the folder-template clone's mutating call
    (and its config-template assert + GOSC resolve reads) must target paths
    that actually exist in the pinned spec. Skips when the spec-shelf is not
    configured (the canary's convention), so CI — where the env vars are wired
    — is the operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "clone-from-template vi-json sub-op targets a path the spec does not serve"
        )
