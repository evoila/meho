# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-operations real-spec reconcile lane (#2984) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the vcf-operations connector
dispatches is served by the pinned ``vcf-operations-9.0`` core (vROps
Suite API) spec on the shelf (``vmware/vcf-api-specs``' OpenAPI 3.0.1
document — Apache-2.0, provenance in the shelf's ``MANIFEST.md``).
Parse-only set compare — no DB, no embeddings, no containers — so it
lives in the required unit sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when
the shelf is unconfigured (``tests/_spec_shelf.py`` contract).

The declared set is introspected from the connector's live constants
(the #2944 pattern — never a hardcoded mirror): the five ``_*_PATH``
module constants of
:mod:`~meho_backplane.connectors.vcf_operations.connector` — the
session mint (``POST /suite-api/api/auth/token/acquire``, issued by the
shared ``vcf_session_login`` helper's ``client.post``), the
version/liveness surface ``fingerprint`` / ``probe`` / ``vrops.liveness``
all read, and the three remaining typed reads (#2303 / #2838). The HTTP
method cannot be introspected from a bare path string, so
:data:`_METHOD_BY_CONSTANT` declares it per constant and a guard test
pins the constant-name set — a new or renamed ``_*_PATH`` constant
fails the guard until its method mapping is added, so the reconciled
set can never silently shrink.

Sweep note (f-strings / concatenations): the only composed request path
in the package is ``resource_query``'s query-string append onto
``_RESOURCES_QUERY_PATH`` (``f"{path}?{urlencode(...)}"``) — no second
path literal. The ``probe_method="GET /suite-api/api/versions/current"``
strings in ``fingerprint`` are display metadata on the result object,
never dispatched; the dispatched path is ``_VERSIONS_CURRENT_PATH``
itself.

Spec-shape note: the pinned spec's path keys are ``/api/...`` under the
server template ``/suite-api``; the ingest parser folds a relative
``servers[0].url`` base onto every op path (#1796), so the served
op_ids carry the full ``/suite-api/api/...`` shape the connector's
constants declare — the comparison is byte-for-byte against what an
operator ingest of the same spec would dispatch.

A red lane here is the guard surfacing a real finding, not harness
noise — first run against the real shelf caught exactly one, of a
different class than an unserved path: the vendor-published
``vcf-operations-openapi.json`` itself fails ``parse_openapi``'s
OpenAPI 3.0 metaschema gate (three ``components.schemas`` entries carry
``"required": []``, illegal per JSON Schema draft-04), so an operator
ingest fails identically. The shelf therefore pins
``vcf-operations-openapi.ingestable.json`` — the vendor document with
the three empty arrays dropped (a semantic no-op), deterministic recipe
+ sha256 in the shelf's ``MANIFEST.md`` — and this lane parses that
file, byte-for-byte what an operator ingest consumes (the nsx-9.0
derived-artifact precedent). All five op_ids are served; no path
repoints were needed. Protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_operations import connector as _connector
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "vcf-operations-9.0"
_SPEC_FILE = "vcf-operations-openapi.ingestable.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: HTTP method per hand-coded path constant. Declared (not introspected)
#: because a module-level path string carries no method; the guard test
#: below pins this mapping's key set against the live constants so the
#: two can never drift apart silently.
_METHOD_BY_CONSTANT: dict[str, str] = {
    "_SESSION_ACQUIRE_PATH": "POST",
    "_VERSIONS_CURRENT_PATH": "GET",
    "_ALERTS_PATH": "GET",
    "_RESOURCES_QUERY_PATH": "POST",
    "_RESOURCES_STATS_LATEST_PATH": "GET",
}


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` constants of ``connector``, by constant name."""
    return {
        name: value
        for name, value in vars(_connector).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcf-operations connector dispatches."""
    return {f"{_METHOD_BY_CONSTANT[name]}:{path}" for name, path in _path_constants().items()}


def test_path_constants_are_all_discovered_and_method_mapped() -> None:
    """Guard: the introspection finds every path constant, each with a method.

    Pinning the exact constant-name set (the #2944 guard shape) means a
    renamed or dropped ``_*_PATH`` constant — or a new one that skips
    the ``_PATH`` suffix convention or lacks a ``_METHOD_BY_CONSTANT``
    entry — can't silently shrink the reconciled set to a vacuous pass.
    """
    assert (
        sorted(_path_constants())
        == sorted(_METHOD_BY_CONSTANT)
        == [
            "_ALERTS_PATH",
            "_RESOURCES_QUERY_PATH",
            "_RESOURCES_STATS_LATEST_PATH",
            "_SESSION_ACQUIRE_PATH",
            "_VERSIONS_CURRENT_PATH",
        ]
    )


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded op_id is served by the pinned vcf-operations-9.0 spec."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
