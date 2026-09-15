# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec-reconcile lane for the SUBSCRIBED content-library composites (#3495).

The four ``content_library.subscribed.*`` composites declare the raw-REST
sub-op paths they dispatch into via ``_SUB_OPS_*`` tuples in
:mod:`meho_backplane.connectors.vmware_rest.composites._library`. This lane
pins those tuples against the real vCenter 9.0 OpenAPI:

* :func:`test_library_sub_op_tuples_are_all_discovered` guards the
  introspection (every ``_SUB_OPS_*`` constant is found) so a rename can't
  silently drop a manifest from the sweep;
* :func:`test_library_sub_ops_resolve_to_an_ingested_op_id_from_fixture`
  parses a vCenter-shaped fixture synthesised from the declared op_ids
  through the real :func:`parse_openapi` and asserts every op_id round-trips
  — the always-runs proof (no spec-shelf needed) that each is a well-formed
  ``METHOD:/path`` the ingest pipeline emits;
* :func:`test_library_sub_ops_resolve_against_pinned_vcenter_spec` parses the
  operator's pinned ``vcenter.yaml`` (skipped when the spec-shelf is
  unconfigured — CI wires it) and asserts every op_id is a **real** path the
  9.0 spec serves — the "no fictional-path drift" acceptance criterion.

The reads' GET paths sit in the manifests too (not just the writes), so the
whole subscribed-library surface — create / sync / status / items.list — is
reconciled here.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from meho_backplane.connectors.vmware_rest.composites import _library
from meho_backplane.operations.ingest import parse_openapi
from tests.acceptance._vcenter_spec import VCENTER_SPEC_REASON, resolve_vcenter_yaml

# Public IP the mock getaddrinfo returns so the ingest SSRF guard passes
# without real DNS (the guard is a correctness property, not a test concern).
_PUBLIC_TEST_IP = "93.184.216.34"
_GETADDRINFO_PATCH = patch(
    "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_PUBLIC_TEST_IP, 443))],
)


def _required_sub_op_ids() -> set[str]:
    """Union of every ``_SUB_OPS_*`` op_id across the subscribed-library composites."""
    raw: set[str] = set()
    for name in dir(_library):
        if not name.startswith("_SUB_OPS_"):
            continue
        raw.update(getattr(_library, name))
    return raw


def _build_vcenter_fixture(required_op_ids: set[str]) -> dict[str, Any]:
    """Synthesise an OpenAPI doc whose paths reproduce *required_op_ids*.

    Splits each ``METHOD:/path`` op_id into a (path-key, verb) pair and
    assembles the ``paths`` object the way vCenter keys these endpoints — the
    ``?action=<verb>`` verb rides in the path key, never as a parameter.
    """
    paths: dict[str, dict[str, Any]] = {}
    for op_id in sorted(required_op_ids):
        method, _, path_key = op_id.partition(":")
        assert path_key, f"malformed op_id without path: {op_id!r}"
        path_item = paths.setdefault(path_key, {})
        path_item[method.lower()] = {
            "summary": f"synthetic op for {op_id}",
            "responses": {"200": {"description": "ok"}},
        }
    return {
        "openapi": "3.0.0",
        "info": {"title": "vcenter", "version": "9.0.0.0"},
        "paths": paths,
    }


def test_library_sub_op_tuples_are_all_discovered() -> None:
    """Guard: the introspection finds every subscribed-library sub-op tuple."""
    tuple_names = sorted(n for n in dir(_library) if n.startswith("_SUB_OPS_"))
    assert tuple_names == [
        "_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_CREATE",
        "_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST",
        "_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_STATUS",
        "_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_SYNC",
    ]


def test_subscribed_library_manifests_are_expected() -> None:
    """The declared sub-op paths are exactly the create/sync/status/items set."""
    assert set(_library._SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_CREATE) == {
        "GET:/vcenter/datastore",
        "POST:/content/subscribed-library",
    }
    assert set(_library._SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_SYNC) == {
        "POST:/content/library?action=find",
        "POST:/content/subscribed-library/{libraryId}?action=sync",
    }
    assert set(_library._SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_STATUS) == {
        "POST:/content/library?action=find",
        "GET:/content/subscribed-library/{libraryId}",
    }
    assert set(_library._SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST) == {
        "POST:/content/library?action=find",
        "POST:/content/library/item?action=find",
        "GET:/content/library/item/{libraryItemId}",
    }


def test_library_sub_ops_resolve_to_an_ingested_op_id_from_fixture() -> None:
    """Every op_id round-trips through the real parser from a vCenter-shaped spec."""
    required = _required_sub_op_ids()
    assert required, "introspection found no sub-op_ids -- wiring broke"

    spec = _build_vcenter_fixture(required)
    spec_bytes = json.dumps(spec).encode()
    spec_url = "https://specs.example.test/vcenter.yaml"

    with _GETADDRINFO_PATCH, respx.mock(assert_all_called=False) as router:
        router.get(spec_url).mock(
            return_value=httpx.Response(
                200, content=spec_bytes, headers={"content-type": "application/json"}
            )
        )
        rows = parse_openapi(spec_url, spec_source="spec:vcenter.yaml")
    ingested_op_ids = {row.op_id for row in rows}

    missing = required - ingested_op_ids
    assert not missing, (
        "subscribed-library composites declare sub-op_ids the ingest pipeline "
        f"does not emit: {sorted(missing)} — a _SUB_OPS_* tuple drifted from the "
        "METHOD:/path form the parser produces."
    )


def test_library_sub_ops_resolve_against_pinned_vcenter_spec() -> None:
    """Every subscribed-library sub-op path is a real ``vcenter.yaml`` 9.0 path.

    The definitive #3495 grounding: parse the canonical pinned ``vcenter.yaml``
    through the real :func:`parse_openapi` and assert every declared op_id is
    in the emitted descriptor set — real path existence for the
    ``/content/subscribed-library`` create + sync, the subscribed-library GET,
    the content-library / item find actions, the per-item GET, and the
    datastore-resolve read. Skips when the spec-shelf is unconfigured (the
    canary's convention), so CI — where the env vars are wired — is the
    operator-visible signal.
    """
    spec_path = resolve_vcenter_yaml()
    if spec_path is None:
        pytest.skip(VCENTER_SPEC_REASON)
    required = _required_sub_op_ids()
    spec_text = spec_path.read_text(encoding="utf-8")
    rows = parse_openapi(f"file://{spec_path}", spec_source="spec:vcenter.yaml", content=spec_text)
    ingested_op_ids = {row.op_id for row in rows}
    missing = required - ingested_op_ids
    assert not missing, (
        "subscribed-library composites declare REST sub-op_ids the real "
        f"vcenter.yaml ingest does not emit: {sorted(missing)} — re-check the "
        "/content/subscribed-library + content-library find/item paths against "
        "the pinned 9.0 spec."
    )
