# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-logs (vRLI) real-spec reconcile lane (#2993) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the vcf-logs (VCF Operations for
Logs / vRealize Log Insight) connector dispatches is served by the pinned
``vcf-operations-9.0`` shelf's ``vcf-operations-for-logs-openapi.json`` — the
Logs (vRLI) OpenAPI document vendored in the **public, Apache-2.0**
`vmware/vcf-api-specs <https://github.com/vmware/vcf-api-specs>`_ repo (pinned
at ``85151f6b``; provenance in the shelf's ``vcf-operations-9.0/MANIFEST.md``).
Parse-only set compare — no DB, no embeddings, no containers — so it lives in
the required unit sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when the
shelf is unconfigured (``tests/_spec_shelf.py`` contract).

Outcome (a) — an **already-pinned** shelf spec serves these paths (#2993).
The Logs spec shares the ``vcf-operations-9.0`` directory the vcf-operations
lane (#2984) already provisions in CI, so no new shelf dir and no
sparse-checkout widening is needed; the file is added to the fail-loud verify
step so a shelf-layout move can't silently regress this lane to skip.

Own served-set extraction (the standard's non-OpenAPI-artifact clause).
The vendor document is OpenAPI 3.0.1, but ``parse_openapi`` refuses it at the
metaschema gate: ``components.securitySchemes.Bearer`` is a ``type: http``
scheme carrying the apiKey-only ``name`` / ``in`` keys, illegal for an
``http`` scheme under the OpenAPI 3.0 metaschema — so an operator ingest of
the raw document fails identically. Rather than pin a repaired
``.ingestable.json`` derivative (the vcf-operations precedent), this lane
extracts the served set directly: the vcf-logs reconciled paths are all
**typed** — the fingerprint version read, the events query, and the
session-login POST — never operator-ingested, so a repaired derivative would
be a test-only artifact with no dispatch-fidelity value (the vcf-automation
``vra-iaas.json`` reasoning). The extractor folds the relative
``servers[0].url`` (``/api/v2``) base onto each path key exactly as
``parse_openapi`` (#1796) does — verified byte-for-byte against the three
declared op_ids.

The declared set is introspected from the connector's live constants (the
#2944 pattern — never a hardcoded mirror), across the three modules that
hand-code a vRLI path:

* :mod:`~meho_backplane.connectors.vcf_logs.profile` —
  ``VRLI_EXECUTION_PROFILE.fingerprint.path`` (``GET /api/v2/version``, the
  unauthenticated version probe).
* :mod:`~meho_backplane.connectors.vcf_logs.typed_ops` —
  ``_EVENTS_PATH_PREFIX`` (``GET /api/v2/events/``). The ``vrli.event.query``
  op appends an RFC6570 ``{+constraints}`` reserved-expansion sub-path onto
  this prefix; the vendor spec models that exact endpoint as the reserved
  template ``/events/{+path}``, so the reconcile declares
  ``GET:/api/v2/events/{+path}`` — the op_id ``parse_openapi`` emits for that
  path key.
* :mod:`~meho_backplane.connectors._shared.profile_auth` —
  ``_VRLI_SESSION_PATH`` (``POST /api/v2/sessions``), the ``session_login``
  scheme's vRLI login endpoint the profiled connector POSTs. Hand-coded as a
  vRLI-specific literal even though it lives in the shared scheme module.

A red lane here is the guard surfacing a real finding, not harness noise —
triage per docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from meho_backplane.connectors._shared.profile_auth import _VRLI_SESSION_PATH
from meho_backplane.connectors.vcf_logs.profile import VRLI_EXECUTION_PROFILE
from meho_backplane.connectors.vcf_logs.typed_ops import _EVENTS_PATH_PREFIX
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "vcf-operations-9.0"
_SPEC_FILE = "vcf-operations-for-logs-openapi.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: OpenAPI 3.x path-item keys that are HTTP operations (its fixed verb
#: vocabulary); everything else on a path item (``parameters``, ``summary``,
#: ``$ref``, vendor extensions) is not a served operation.
_OPENAPI3_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

#: The reserved-expansion template segment the vendor spec models the events
#: query with. ``vrli.event.query`` builds its request path by appending an
#: RFC6570 ``{+constraints}`` chain onto ``_EVENTS_PATH_PREFIX``
#: (:func:`~meho_backplane.connectors.vcf_logs.typed_ops.build_event_query_path`);
#: the spec's ``/events/{+path}`` key is that same reserved-expansion endpoint,
#: so the declared op_id folds this segment onto the live prefix constant.
_EVENTS_RESERVED_TEMPLATE = "{+path}"


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcf-logs connector dispatches.

    Built from the live constants (never a hardcoded mirror): the profile's
    fingerprint path (GET), the events-query prefix folded with the spec's
    reserved-expansion template segment (GET), and the shared ``session_login``
    scheme's vRLI login path (POST).
    """
    return {
        f"GET:{VRLI_EXECUTION_PROFILE.fingerprint.path}",
        f"GET:{_EVENTS_PATH_PREFIX}{_EVENTS_RESERVED_TEMPLATE}",
        f"POST:{_VRLI_SESSION_PATH}",
    }


def test_hand_coded_path_surface_is_pinned() -> None:
    """Guard: the introspected constants and declared set can't silently drift.

    Pinning the exact live-constant values plus the assembled op_id set (the
    #2944 guard shape) means a renamed/moved constant — or a change to the
    events reserved-expansion shape — fails here until the reconcile is updated
    consciously, so the declared set can never shrink to a vacuous pass.
    """
    assert VRLI_EXECUTION_PROFILE.fingerprint.path == "/api/v2/version"
    assert _EVENTS_PATH_PREFIX == "/api/v2/events/"
    assert _VRLI_SESSION_PATH == "/api/v2/sessions"
    assert _declared_op_ids() == {
        "GET:/api/v2/version",
        "GET:/api/v2/events/{+path}",
        "POST:/api/v2/sessions",
    }


def _openapi3_served_op_ids(spec_path: Path) -> set[str]:
    """Extract the served ``METHOD:/path`` set from an OpenAPI 3.x document.

    Used instead of :func:`tests._spec_shelf.openapi_served_op_ids` because the
    pinned Logs document fails ``parse_openapi``'s metaschema gate on a
    malformed ``securitySchemes.Bearer`` (see the module docstring) — the
    standard's non-OpenAPI-artifact clause. Served op_ids are the path keys
    crossed with their operation-verb keys, with the relative ``servers[0].url``
    base folded on (``/api/v2`` → every path folds under it), byte-identical to
    ``parse_openapi``'s #1796 relative-server fold for the declared paths.
    Format drift on the shelf (no longer OpenAPI 3, no server to fold, or an
    empty path map) fails loudly rather than regressing the lane to a vacuous
    compare.
    """
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if not str(document.get("openapi", "")).startswith("3."):
        pytest.fail(
            f"{_SPEC_LABEL}: expected an OpenAPI 3.x document (got "
            f"openapi={document.get('openapi')!r} / swagger={document.get('swagger')!r}) "
            "— the shelf artifact format moved; update this lane's extraction."
        )
    servers = document.get("servers")
    if not isinstance(servers, list) or not servers:
        pytest.fail(
            f"{_SPEC_LABEL}: no servers[] to fold the relative base from — the "
            "shelf artifact layout moved; update this lane's extraction."
        )
    base = str((servers[0] or {}).get("url") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in (document.get("paths") or {}).items()
        if isinstance(item, dict)
        for method in item
        if method.lower() in _OPENAPI3_METHODS
    }
    if not served:
        pytest.fail(
            f"{_SPEC_LABEL}: extracted zero served operations — the shelf "
            "artifact layout moved; update this lane's extraction."
        )
    return served


def test_declared_op_ids_are_served_by_the_pinned_logs_spec() -> None:
    """Every hand-coded op_id is served by the pinned vcf-operations-for-logs spec."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = _openapi3_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
