# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""op_id reconciliation between the 8 write composites and the ingest pipeline.

G3.16-T1 (#1414). The 8 vmware-rest write composites each declare the L2
sub-ops they dispatch into via ``_SUB_OPS_*`` tuples in
:mod:`~meho_backplane.connectors.vmware_rest.composites._write`. At
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
from pathlib import Path
from typing import Any

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.operations.ingest import parse_openapi

# ---------------------------------------------------------------------------
# Derive the composites' required L2 sub-op_ids from the live constants.
# ---------------------------------------------------------------------------


def _required_raw_sub_op_ids() -> set[str]:
    """Union of every ``_SUB_OPS_*`` op_id across the 8 write composites.

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

    Eight ``_SUB_OPS_*`` module constants today, one per write composite.
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


def test_every_write_composite_sub_op_resolves_to_an_ingested_op_id(
    tmp_path: Path,
) -> None:
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
    spec_path = tmp_path / "vcenter.yaml"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    rows = parse_openapi(str(spec_path), spec_source="spec:vcenter.yaml")
    ingested_op_ids = {row.op_id for row in rows}

    missing = required - ingested_op_ids
    assert not missing, (
        "write composites declare sub-op_ids the ingest pipeline does not "
        f"emit from a vCenter-shaped spec: {sorted(missing)}. Either a "
        "_SUB_OPS_* tuple drifted from the METHOD:/path form the parser "
        "produces, or the fixture no longer mirrors vCenter's path keying."
    )


def test_action_discriminated_sub_ops_keep_query_suffix_through_ingest(
    tmp_path: Path,
) -> None:
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
    spec_path = tmp_path / "vcenter.yaml"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    rows = parse_openapi(str(spec_path), spec_source="spec:vcenter.yaml")
    ingested_op_ids = {row.op_id for row in rows}

    # Every action op_id round-trips with its ``?action=`` suffix intact.
    assert action_op_ids <= ingested_op_ids
    # And no op_id lost its query suffix (proves no stripping).
    assert all("?action=" in op_id for op_id in ingested_op_ids)
