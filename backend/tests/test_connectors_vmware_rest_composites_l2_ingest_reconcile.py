# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""op_id reconciliation between the write composites and the ingest pipeline.

G3.16-T1 (#1414). The 14 REST-sub-op vmware-rest write composites each
declare the L2 sub-ops they dispatch into via ``_SUB_OPS_*`` tuples in
:mod:`~meho_backplane.connectors.vmware_rest.composites._write` (the
T6/#509 + vm.power writes, the #2891 hardware trio, the two GOSC
composites / #2892, and the OVF/OVA content-library deploy / #2909); the
four vi-json write composites (``vm.disk.grow`` /
#2893, ``vm.clone_from_template`` / #2894, and the vim cluster / inventory
writes ``cluster.drs_rule.create`` + ``folder.create`` / #2895) dispatch
into vi-json instead and declare their sub-ops in ``_VIM_SUB_OPS_*`` tuples
(reconciled in the dedicated vi-json section at the end of this module),
as do the composite steps #2970 switched to vim because the pinned
vcenter.yaml serves no REST path for them (snapshot list/revert, host
maintenance, DRS recommendations, DVS host removal). At
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
  composites' ``_power_vm_op_id`` helper builds the same string.

This module proves the match automatically, without a live backplane:

1. Derive the full set of raw L2 sub-op_ids the 14 composites need by
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

**Two tiers of proof -- shape vs. real-path existence.** The hand-built
fixture assertion
(:func:`test_every_write_composite_sub_op_resolves_to_an_ingested_op_id`)
proves op_id *shape*: that every ``_SUB_OPS_*`` string is a well-formed
``METHOD:/path`` the parser emits, keyed vCenter's action-verb-in-path
way. It synthesises the fixture *from* those same constants, so it
**cannot** prove a path actually exists in vCenter 9.0 -- a typo, a
renamed endpoint, or a wrong API-version path passes it. The env-gated
:func:`test_every_write_composite_sub_op_resolves_against_pinned_vcenter_spec`
closes that gap: it parses the canonical pinned ``vcenter.yaml`` and
asserts real *path existence*. It skips when the vendor-licensed
spec-shelf is unconfigured (the #1602 / G0.7-canary convention -- the
specs live in the operator's separate spec-shelf repo, not this chassis
repo), so the default local run and CI stay green on the shape assertion
with zero external dependency; wherever ``MEHO_VCENTER_OPENAPI_VCENTER``
/ ``MEHO_CONSUMER_DOCS_ROOT`` is wired it runs for real. The vi-json
write composites get the same real-path treatment in the
``*_paths_exist_in_the_pinned_spec`` checks lower down.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from meho_backplane.connectors.vmware_rest.composites import _host, _write
from meho_backplane.operations.ingest import parse_openapi
from tests.acceptance._vcenter_spec import (
    VCENTER_SPEC_REASON,
    resolve_vcenter_yaml,
    resolve_vi_json_yaml,
)

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
    """Union of every ``_SUB_OPS_*`` op_id across the 14 REST write composites.

    The vi-json sub-ops live in ``_VIM_SUB_OPS_*`` tuples (a distinct
    namespace this ``_SUB_OPS_*`` sweep deliberately skips), so a
    vcenter.yaml-shaped fixture never has to model a vi-json path; the
    vi-json sections at the end of this module reconcile them separately
    (the four vi-json write composites plus the #2970 vim-switched steps).

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

    Fifteen ``_SUB_OPS_*`` module constants today (the T6/#509 + vm.power
    writes, the #2891 hardware trio, the two GOSC composites / #2892, the
    OVF/OVA content-library deploy / #2909, plus the typed HttpNfcLease import
    / #3229 — whose REST sub-ops ride the content-library find + download-session
    actions, all served by the pinned vcenter.yaml).
    ``vm.snapshot.revert`` no longer appears: both of its sub-ops moved to
    the vim surface in #2970 (the pinned vcenter.yaml serves no snapshot
    REST resource), so its manifest lives in
    ``_VIM_SUB_OPS_VM_SNAPSHOT_REVERT`` and is reconciled against
    ``vi-json.yaml`` below. Pinning the exact set means a renamed or
    dropped constant can't silently shrink the reconciled set to a
    vacuous pass.
    """
    tuple_names = sorted(n for n in dir(_write) if n.startswith("_SUB_OPS_"))
    assert tuple_names == [
        "_SUB_OPS_CLUSTER_PATCH",
        "_SUB_OPS_GUEST_CUSTOMIZATION_SPEC_CREATE",
        "_SUB_OPS_HOST_DETACH_FROM_VDS",
        "_SUB_OPS_HOST_EVACUATE",
        "_SUB_OPS_VM_CLONE",
        "_SUB_OPS_VM_CREATE",
        "_SUB_OPS_VM_CUSTOMIZE",
        "_SUB_OPS_VM_DEPLOY_FROM_LIBRARY",
        "_SUB_OPS_VM_DEVICE_CDROM",
        "_SUB_OPS_VM_IMPORT_FROM_LIBRARY",
        "_SUB_OPS_VM_MIGRATE",
        "_SUB_OPS_VM_NIC_REPOINT",
        "_SUB_OPS_VM_POWER",
        "_SUB_OPS_VM_POWER_BULK",
        "_SUB_OPS_VM_RESIZE",
    ]


def test_vm_deploy_from_library_sub_op_manifest_is_expected() -> None:
    """Pin the deploy_from_library manifest so a drift can't shrink the reconcile.

    All four sub-ops are ``vcenter.yaml``-served REST paths (the OVF deploy,
    both content-library find actions used for name resolution, and the
    power-on), so — unlike the vim-shaped writes — they reconcile through the
    generic ``_SUB_OPS_*`` sweep above rather than a ``_VIM_SUB_OPS_*`` lane.
    """
    assert set(_write._SUB_OPS_VM_DEPLOY_FROM_LIBRARY) == {
        "POST:/content/library?action=find",
        "POST:/content/library/item?action=find",
        "POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy",
        "POST:/vcenter/vm/{vm}/power?action=start",
    }


def test_vm_import_from_library_sub_op_manifest_is_expected() -> None:
    """Pin the import_from_library REST manifest so a drift can't shrink the reconcile.

    All eight REST sub-ops are ``vcenter.yaml``-served paths (the two shared
    content-library find actions, the five download-session steps the typed
    HttpNfcLease source reads the OVF through, and the power-on), so they
    reconcile through the generic ``_SUB_OPS_*`` sweep. The vim control-plane
    sub-ops are the separate ``_VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY`` lane below.
    """
    assert set(_write._SUB_OPS_VM_IMPORT_FROM_LIBRARY) == {
        "POST:/content/library?action=find",
        "POST:/content/library/item?action=find",
        "POST:/content/library/item/download-session",
        "GET:/content/library/item/download-session/{downloadSessionId}/file",
        "POST:/content/library/item/download-session/{downloadSessionId}/file?action=prepare",
        "POST:/content/library/item/download-session/{downloadSessionId}?action=keep-alive",
        "POST:/content/library/item/download-session/{downloadSessionId}?action=cancel",
        "POST:/vcenter/vm/{vm}/power?action=start",
    }


def test_vm_deploy_from_library_sub_ops_resolve_against_pinned_vcenter_spec() -> None:
    """The #2909 OVF deploy + find + power paths are real ``vcenter.yaml`` paths.

    The definitive #2909 grounding (the issue's ingest-reconcile-against-the-
    pinned-spec-rows criterion, scoped to the ovf/library-item deploy paths):
    parse the canonical pinned ``vcenter.yaml`` through the real
    :func:`parse_openapi` and assert every ``_SUB_OPS_VM_DEPLOY_FROM_LIBRARY``
    op_id is in the emitted descriptor set — real path existence for the OVF
    library-item deploy, both content-library find actions, and the power-on.
    Skips when the spec-shelf is unconfigured (the canary's convention), so CI
    — where the env vars are wired — is the operator-visible signal.
    """
    spec_path = resolve_vcenter_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    required = set(_write._SUB_OPS_VM_DEPLOY_FROM_LIBRARY)
    spec_text = spec_path.read_text(encoding="utf-8")
    rows = parse_openapi(f"file://{spec_path}", spec_source="spec:vcenter.yaml", content=spec_text)
    ingested_op_ids = {row.op_id for row in rows}
    missing = required - ingested_op_ids
    assert not missing, (
        "vm.deploy_from_library declares REST sub-op_ids the real vcenter.yaml "
        f"ingest does not emit: {sorted(missing)} — re-check the OVF deploy / "
        "content-library find paths against the pinned 9.0 spec."
    )


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
    # (Host maintenance left this set in #2970 -- it is vim-only in the
    # pinned spec; the vLCM apply carries the compound
    # ``?action=apply&vmw-task=true`` suffix instead.)
    assert {
        "POST:/vcenter/vm/{vm}/power?action=start",
        "POST:/vcenter/vm/{vm}/power?action=stop",
        "POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true",
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


def test_every_write_composite_sub_op_resolves_against_pinned_vcenter_spec() -> None:
    """Every REST ``_SUB_OPS_*`` op_id is emitted by parsing the real vcenter.yaml.

    The env-gated real-spec analogue of
    :func:`test_every_write_composite_sub_op_resolves_to_an_ingested_op_id`.
    That test synthesises its OpenAPI fixture *from* the ``_SUB_OPS_*``
    constants, so it proves op_id **shape** (a well-formed ``METHOD:/path``
    that survives the parser, keyed vCenter's action-verb-in-path way) but
    cannot prove a path actually **exists** in vCenter 9.0 -- a typo, a
    renamed endpoint, or a wrong API-version path passes it. This parses the
    canonical pinned ``vcenter.yaml`` through the real :func:`parse_openapi`
    and asserts every REST ``_SUB_OPS_*`` op_id is in the emitted descriptor
    set, i.e. real path existence -- the vcenter.yaml/REST analogue of the
    vi-json ``*_paths_exist_in_the_pinned_spec`` checks below, mirroring
    #1602's ``test_portgroup_audit_op_id_reconcile.py``.

    Skips when the spec-shelf is unconfigured (the canary's convention), so
    the default local run and CI stay green on the hand-built-fixture
    assertion above with zero external dependency; wherever
    ``MEHO_VCENTER_OPENAPI_VCENTER`` / ``MEHO_CONSUMER_DOCS_ROOT`` is wired
    it runs for real. A shelf-backed red is the guard surfacing a real
    finding (a ``_SUB_OPS_*`` path the pinned spec does not serve), not a
    reason to withhold the guard.
    """
    spec_path = resolve_vcenter_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)

    required = _required_raw_sub_op_ids()
    assert required, "introspection found no raw sub-op_ids -- wiring broke"

    spec_text = spec_path.read_text(encoding="utf-8")
    # ``content=`` feeds the bytes verbatim (the https-only SSRF guard
    # applies only to the URL-fetch path); the URI arg is just the audit
    # label. Mirrors #1602's portgroup-audit real-spec reconcile.
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_text,
    )
    ingested_op_ids = {row.op_id for row in rows}

    missing = required - ingested_op_ids
    assert not missing, (
        "vmware write composites declare _SUB_OPS_* REST op_ids the real "
        f"vcenter.yaml ingest does not emit: {sorted(missing)}. Either a "
        "_SUB_OPS_* constant references a path vCenter 9.0 does not serve "
        "(a typo, a renamed endpoint, or a wrong API-version path -- the "
        "class of defect the hand-built fixture cannot catch), or the pinned "
        "spec revision moved the resource (re-check against the vSphere "
        "Automation REST API)."
    )


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
# vm.destroy VI-JSON sub-op reconciliation (#3198)
# ---------------------------------------------------------------------------
#
# vm.destroy's pre-9.0 delete arm is vi-json, not vcenter REST:
# ``POST:/VirtualMachine/{moId}/Destroy_Task`` (the core vim delete) plus the
# best-effort snapshot ``POST:/PropertyCollector/{moId}/RetrievePropertiesEx``
# the blast-radius preview reads. These are declared in
# ``_write._VIM_SUB_OPS_VM_DESTROY`` (deliberately NOT in the ``_SUB_OPS_*``
# namespace, so the vcenter.yaml sweep skips them). The 9.0+ arm is the
# synchronous REST ``DELETE:/vcenter/vm/{vm}`` — a plain vcenter.yaml path.


def test_vm_destroy_vi_json_sub_op_manifest_is_the_expected_pair() -> None:
    """Pin the destroy vi-json manifest so a drift can't shrink the reconcile."""
    assert set(_write._VIM_SUB_OPS_VM_DESTROY) == {
        "POST:/VirtualMachine/{moId}/Destroy_Task",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    }


def test_vm_destroy_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The destroy vi-json op_ids are byte-for-byte what the parser emits."""
    required = set(_write._VIM_SUB_OPS_VM_DESTROY)
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


def test_vm_destroy_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """Each destroy vi-json sub-op path is a real POST path in the pinned vi-json.yaml.

    ``VirtualMachine.Destroy_Task`` is a core vim25 method; the definitive
    #3198 grounding is that the connector's pre-9.0 delete arm targets a path
    the pinned spec serves. Skips when the spec-shelf is not configured (the
    canary's convention), so CI — where the env vars are wired — is the
    operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_VM_DESTROY:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "destroy vi-json sub-op targets a path the spec does not serve"
        )


# ---------------------------------------------------------------------------
# vm.import_from_library VI-JSON sub-op reconciliation (#3229)
# ---------------------------------------------------------------------------
#
# The typed HttpNfcLease import's control plane is vi-json, not vcenter REST:
# ``ServiceInstance.RetrieveServiceContent`` (resolve OvfManager/rootFolder),
# ``OvfManager.CreateImportSpec`` (validate descriptor), the governed
# ``ResourcePool.ImportVApp`` write, ``PropertyCollector.RetrievePropertiesEx``
# (lease-state poll), and the ``HttpNfcLease`` progress / complete / abort
# lifecycle. Declared in ``_write._VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY``
# (deliberately NOT in the ``_SUB_OPS_*`` namespace, so the vcenter.yaml sweep
# above skips them). Same ``METHOD:/path`` keying as the other vim writes — the
# moId rides the path as ``{moId}`` — so the same reconciliation proof applies,
# and it is additionally checked against the pinned ``vi-json.yaml`` when the
# spec-shelf is configured.


def test_vm_import_from_library_vi_json_sub_op_manifest_is_expected() -> None:
    """Pin the import vim manifest so a drift can't shrink the reconcile."""
    assert set(_write._VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY) == {
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/OvfManager/{moId}/CreateImportSpec",
        "POST:/ResourcePool/{moId}/ImportVApp",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HttpNfcLease/{moId}/HttpNfcLeaseProgress",
        "POST:/HttpNfcLease/{moId}/HttpNfcLeaseComplete",
        "POST:/HttpNfcLease/{moId}/HttpNfcLeaseAbort",
    }


def test_vm_import_from_library_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The import vim op_ids are byte-for-byte what the parser emits."""
    required = set(_write._VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY)
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


def test_vm_import_from_library_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """Each import vim sub-op path is a real POST path in the pinned vi-json.yaml.

    The #3229 grounding: the typed HttpNfcLease import's OvfManager /
    ResourcePool.ImportVApp / HttpNfcLease control-plane methods are core vim25
    (version-agnostic), so the pinned spec must serve every one. Skips when the
    spec-shelf is not configured, so CI — where the env vars are wired — is the
    operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "import vi-json sub-op targets a path the spec does not serve"
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


# ---------------------------------------------------------------------------
# #2895 vim cluster / inventory writes — the drs_rule + folder vi-json paths
# reconcile against the same pinned spec as the disk-grow substrate.
# ---------------------------------------------------------------------------


def test_cluster_drs_rule_create_vi_json_sub_op_manifest_is_the_expected_pair() -> None:
    """Pin the drs_rule vi-json manifest so a drift can't shrink the reconcile."""
    assert set(_write._VIM_SUB_OPS_CLUSTER_DRS_RULE_CREATE) == {
        "POST:/ClusterComputeResource/{moId}/ReconfigureComputeResource_Task",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    }


def test_folder_create_vi_json_sub_op_manifest_is_the_expected_single() -> None:
    """Pin the folder.create vi-json manifest — one synchronous CreateFolder, no poll."""
    assert set(_write._VIM_SUB_OPS_FOLDER_CREATE) == {
        "POST:/Folder/{moId}/CreateFolder",
    }


def test_cluster_drs_rule_create_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The drs_rule vi-json op_ids are byte-for-byte what ``parse_openapi`` emits."""
    required = set(_write._VIM_SUB_OPS_CLUSTER_DRS_RULE_CREATE)
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


def test_folder_create_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The folder.create vi-json op_id is byte-for-byte what ``parse_openapi`` emits."""
    required = set(_write._VIM_SUB_OPS_FOLDER_CREATE)
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


def test_cluster_drs_rule_create_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """Each drs_rule vi-json sub-op path is a real POST path in the pinned vi-json.yaml.

    The definitive #2895 grounding for the DRS-rule write: the
    ``ReconfigureComputeResource_Task`` + collision-read ``RetrievePropertiesEx``
    paths must exist in the pinned spec. Skips when the spec-shelf is not
    configured (the canary's convention), so CI is the operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_CLUSTER_DRS_RULE_CREATE:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "drs_rule vi-json sub-op targets a path the spec does not serve"
        )


def test_folder_create_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """The folder.create vi-json sub-op path is a real POST path in the pinned vi-json.yaml.

    The definitive #2895 grounding for the folder write: ``Folder.CreateFolder``
    must exist as a POST path in the pinned spec (``/vcenter/folder`` is GET-only,
    so vim is the sole write path). Skips when the spec-shelf is unconfigured.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_FOLDER_CREATE:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "folder.create vi-json sub-op targets a path the spec does not serve"
        )


# ---------------------------------------------------------------------------
# #2970 vim-switched composite steps — snapshot revert, DRS recommendations,
# host maintenance, DVS host removal. The pinned vcenter.yaml serves no REST
# path for any of these surfaces, so the affected steps ride vim and their
# manifests reconcile against the pinned vi-json.yaml like the #2893-#2895
# write composites above.
# ---------------------------------------------------------------------------

#: Expected #2970 vim manifests, pinned so a drift can't shrink a reconcile.
_EXPECTED_2970_VIM_MANIFESTS: dict[str, set[str]] = {
    "_VIM_SUB_OPS_VM_SNAPSHOT_REVERT": {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task",
    },
    "_VIM_SUB_OPS_VM_MIGRATE": {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
    },
    "_VIM_SUB_OPS_HOST_EVACUATE": {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task",
    },
    "_VIM_SUB_OPS_CLUSTER_PATCH": {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task",
        "POST:/HostSystem/{moId}/ExitMaintenanceMode_Task",
    },
    "_VIM_SUB_OPS_HOST_DETACH_FROM_VDS": {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/DistributedVirtualSwitch/{moId}/ReconfigureDvs_Task",
    },
}


@pytest.mark.parametrize("manifest_name", sorted(_EXPECTED_2970_VIM_MANIFESTS))
def test_2970_vim_sub_op_manifests_are_pinned(manifest_name: str) -> None:
    """Pin each #2970 vim manifest so a drift can't shrink the reconcile."""
    assert set(getattr(_write, manifest_name)) == _EXPECTED_2970_VIM_MANIFESTS[manifest_name]


@pytest.mark.parametrize("manifest_name", sorted(_EXPECTED_2970_VIM_MANIFESTS))
def test_2970_vim_sub_ops_round_trip_through_ingest(manifest_name: str) -> None:
    """The #2970 vim op_ids are byte-for-byte what ``parse_openapi`` emits.

    Same proof shape as the #2893-#2895 round-trips above: the ``{moId}``
    path template survives the parser, so governance op_ids / grants
    resolve against the ingested rows once ``vi-json.yaml`` is ingested.
    """
    required = set(getattr(_write, manifest_name))
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


@pytest.mark.parametrize("manifest_name", sorted(_EXPECTED_2970_VIM_MANIFESTS))
def test_2970_vim_paths_exist_in_the_pinned_spec(manifest_name: str) -> None:
    """Each #2970 vim sub-op path is a real POST path in the pinned vi-json.yaml.

    The definitive #2970 grounding: the vim-switched steps must target
    paths the pinned spec actually serves. Skips when the spec-shelf is
    not configured (the canary's convention), so CI is the
    operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in getattr(_write, manifest_name):
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            f"{manifest_name} vim sub-op targets a path the spec does not serve"
        )


# ---------------------------------------------------------------------------
# vm.create VI-JSON sub-op reconciliation (#3093 VHV leg + #3099 create arm)
# ---------------------------------------------------------------------------
#
# vm.create's optional ``nested_hv`` leg is vi-json, not vcenter REST:
# ``VirtualMachineConfigSpec.nestedHVEnabled`` has no REST expression (the
# #3087 recipe lanes pin that gap) and raw VI-JSON dispatch mounts on
# ``/api`` — a 9.x-fleet shape that 404s on vCenter 8.0.x (#2466) — so the
# leg rides the same governed vmomi substrate as disk-grow:
# ``POST:/VirtualMachine/{moId}/ReconfigVM_Task`` (the flag write) plus the
# shared Task-poll read ``RetrievePropertiesEx``. The #3099 pre-9.0 create
# arm adds ``POST:/Folder/{moId}/CreateVM_Task`` — on vCenter 8.0.x the
# bare REST ``POST /api/vcenter/vm`` is vendor-defective, so the whole
# create rides the vim path there (NICs + nested_hv folded into the one
# ConfigSpec). Declared in ``_write._VIM_SUB_OPS_VM_CREATE`` (deliberately
# NOT in the ``_SUB_OPS_*`` namespace, so the vcenter.yaml sweep above
# skips it).


def test_vm_create_vi_json_sub_op_manifest_is_the_expected_triple() -> None:
    """Pin the vm.create vi-json manifest so a drift can't shrink the reconcile."""
    assert set(_write._VIM_SUB_OPS_VM_CREATE) == {
        "POST:/VirtualMachine/{moId}/ReconfigVM_Task",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/Folder/{moId}/CreateVM_Task",
    }


def test_vm_create_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The vm.create vi-json op_ids are byte-for-byte what the parser emits.

    Same proof shape as the #2893-#2895 round-trips above: the ``{moId}``
    path template survives the parser, so ``enforce_subop_policy``'s op_id /
    a grant's op_pattern resolve against the ingested rows once the operator
    ingests ``vi-json.yaml``.
    """
    required = set(_write._VIM_SUB_OPS_VM_CREATE)
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


def test_vm_create_vim_paths_and_flag_exist_in_the_pinned_spec() -> None:
    """The vm.create vim paths are real POST paths and ``nestedHVEnabled`` is served.

    The definitive #3093 + #3099 grounding: the VHV leg and the pre-9.0
    create arm target paths the pinned ``vi-json.yaml`` actually serves
    (``ReconfigVM_Task``, ``CreateVM_Task``, the Task-poll read), and the
    one flag the bodies set — ``VirtualMachineConfigSpec.nestedHVEnabled``
    — exists in the pinned schema corpus (the schema-level pin lives in
    the #3087 recipe reconcile lane; this is the cheap text-level guard
    alongside the path check). Skips when the spec-shelf is not configured
    (the canary's convention), so CI is the operator-visible signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    for op_id in _write._VIM_SUB_OPS_VM_CREATE:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — the "
            "vm.create vi-json sub-op targets a path the spec does not serve"
        )
    assert "nestedHVEnabled:" in spec_text, (
        "``nestedHVEnabled`` is absent from the pinned vi-json.yaml — the "
        "vm.create VHV reconfigure body sets a field the spec does not serve"
    )


def test_vm_create_guest_id_map_is_grounded_in_both_pinned_specs() -> None:
    """Every #3099 guestId mapping row exists in both pinned spec enums.

    The pre-9.0 create arm maps the composite's REST-style ``guest_os``
    enum to the vim ``guestId`` the ``CreateVM_Task`` ConfigSpec takes.
    Both sides of every curated pair must be real enum values: the key in
    the pinned ``vcenter.yaml``'s ``Vcenter.Vm.GuestOS`` enum, the value
    in the pinned ``vi-json.yaml``'s
    ``VirtualMachineGuestOsIdentifier_enum``. Text-level pins (the
    ``nestedHVEnabled`` guard's convention): the YAML enum entries are
    unambiguous single-line items, so a substring with the trailing
    newline is an exact-value match. Skips when the spec-shelf is not
    configured, so CI is the operator-visible signal.
    """
    vcenter_path = resolve_vcenter_yaml()
    vi_json_path = resolve_vi_json_yaml()
    if vcenter_path is None or vi_json_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    vcenter_text = vcenter_path.read_text(encoding="utf-8")
    vi_json_text = vi_json_path.read_text(encoding="utf-8")
    assert _write._VIM_GUEST_ID_BY_REST_GUEST_OS, "guestId map is empty — wiring broke"
    for rest_enum, vim_guest_id in _write._VIM_GUEST_ID_BY_REST_GUEST_OS.items():
        assert f"- {rest_enum}\n" in vcenter_text, (
            f"{rest_enum!r} is not a Vm.GuestOS enum value in the pinned "
            "vcenter.yaml — the #3099 guestId map keys an identifier the REST "
            "surface does not serve"
        )
        assert f"- '{vim_guest_id}'\n" in vi_json_text, (
            f"{vim_guest_id!r} is not a VirtualMachineGuestOsIdentifier enum "
            "value in the pinned vi-json.yaml — the #3099 guestId map targets "
            "a vim identifier the spec does not serve"
        )


# ---------------------------------------------------------------------------
# host.datastore_mount_nfs / host.disk_mark_flash / host.service_control
# VI-JSON sub-op reconciliation (#3182)
# ---------------------------------------------------------------------------
#
# The three host-domain write composites' mutating paths are vi-json, not
# vcenter REST: HostDatastoreSystem.CreateNasDatastore,
# HostStorageSystem.MarkAsSsd_Task / MarkAsNonSsd_Task, and the four
# HostServiceSystem methods (StartService / StopService / RestartService /
# UpdateServicePolicy), each plus the shared configManager PropertyCollector
# read. These are declared in ``_host._VIM_SUB_OPS_HOST_*`` (deliberately NOT
# in any ``_SUB_OPS_*`` namespace, so the vcenter.yaml sweep skips them). Same
# ``METHOD:/path`` keying as disk-grow — the moId rides the path as ``{moId}``
# — so the same reconciliation proof applies, additionally checked against the
# pinned ``vi-json.yaml`` when the spec-shelf is configured.


def test_host_vi_json_sub_op_manifests_are_the_expected_sets() -> None:
    """Pin the host-domain vi-json manifests so a drift can't shrink the reconcile."""
    assert set(_host._VIM_SUB_OPS_HOST_DATASTORE_MOUNT_NFS) == {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HostDatastoreSystem/{moId}/CreateNasDatastore",
    }
    assert set(_host._VIM_SUB_OPS_HOST_DISK_MARK_FLASH) == {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HostStorageSystem/{moId}/MarkAsSsd_Task",
        "POST:/HostStorageSystem/{moId}/MarkAsNonSsd_Task",
    }
    assert set(_host._VIM_SUB_OPS_HOST_SERVICE_CONTROL) == {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/HostServiceSystem/{moId}/StartService",
        "POST:/HostServiceSystem/{moId}/StopService",
        "POST:/HostServiceSystem/{moId}/RestartService",
        "POST:/HostServiceSystem/{moId}/UpdateServicePolicy",
    }


def test_host_vi_json_sub_ops_round_trip_through_ingest() -> None:
    """The host-domain vi-json op_ids are byte-for-byte what the parser emits.

    Proves the ``METHOD:/path`` op_id strings the composites gate on match
    what ``parse_openapi`` produces from a vi-json-shaped spec (the ``{moId}``
    path template survives), so ``enforce_subop_policy``'s op_id / a grant's
    op_pattern resolve against the ingested rows once the operator ingests
    ``vi-json.yaml`` — the vi-json analogue of the vcenter reconcile above.
    """
    required = (
        set(_host._VIM_SUB_OPS_HOST_DATASTORE_MOUNT_NFS)
        | set(_host._VIM_SUB_OPS_HOST_DISK_MARK_FLASH)
        | set(_host._VIM_SUB_OPS_HOST_SERVICE_CONTROL)
    )
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


def test_host_vi_json_paths_exist_in_the_pinned_spec() -> None:
    """Each host-domain vi-json sub-op path is a real POST path in the pinned vi-json.yaml.

    The definitive #3182 grounding: the host-domain write composites must
    target paths that actually exist in the pinned spec (the issue's
    ``MarkAsHdd_Task`` mis-spelling is exactly the drift this catches — the
    real method is ``MarkAsNonSsd_Task``). Skips when the spec-shelf is not
    configured, so CI — where the env vars are wired — is the operator-visible
    signal.
    """
    spec_path = resolve_vi_json_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    spec_text = spec_path.read_text(encoding="utf-8")
    op_ids = (
        set(_host._VIM_SUB_OPS_HOST_DATASTORE_MOUNT_NFS)
        | set(_host._VIM_SUB_OPS_HOST_DISK_MARK_FLASH)
        | set(_host._VIM_SUB_OPS_HOST_SERVICE_CONTROL)
    )
    for op_id in op_ids:
        _, _, path = op_id.partition(":")
        assert _vi_json_path_item_has_post(spec_text, path), (
            f"{path!r} is not a POST path item in the pinned vi-json.yaml — a "
            "host-domain vi-json sub-op targets a path the spec does not serve"
        )
