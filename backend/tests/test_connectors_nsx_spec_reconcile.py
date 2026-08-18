# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""nsx real-spec reconcile lane — hand-coded paths vs the pinned nsx-9.0 specs.

#2981 (initiative #2979): every hand-coded ``METHOD:/path`` literal in the
nsx connector asserts against the pinned ``nsx-9.0`` specs through the
#2980 harness (:mod:`tests._spec_shelf`). The declared set is introspected
from the connector's live module constants — the ten ``*_PATH`` typed-read
paths in :mod:`meho_backplane.connectors.nsx.typed_reads` (all dispatched
via ``HttpConnector._get_json``, hence ``GET``) plus the session-establish
``POST`` path in :mod:`meho_backplane.connectors.nsx.connector`. The
fingerprint probe's inline ``/api/v1/node`` literal collapses into the
``GET:/api/v1/node`` typed-read op_id, so the sweep covers it too.

**This lane is armed** (PR #3007 review blocker B1 resolution,
2026-08-18). NSX serves its own specs from every live manager at the
vendor-documented ``GET /api/v1/spec/openapi/nsx_api.{json,yaml}`` and
``nsx_policy_api.{json,yaml}`` endpoints — the shelf's earlier
"no pinnable spec" negative (2026-04-29, 30 probed paths) never covered
that family. The shelf's ``nsx-9.0/`` directory pins the fetched JSON
artifacts plus deterministic OpenAPI 3.0 conversions (``parse_openapi``
accepts 3.0/3.1 only; the conversion recipe and provenance live in the
shelf's ``nsx-9.0/MANIFEST.md``). NSX splits its surface across two
specs — the Manager API (``servers[0].url = /api/v1``) and the Policy
API (``/policy/api/v1``) — so the lane resolves both files and unions
their served sets per the standard's multi-spec guidance.

One vendor-spec quirk is mapped rather than repointed: the Manager API
spec models the session-establish endpoint as path key
``/api/session/create`` **under** ``basePath: /api/v1``, so its ingested
op_id is ``POST:/api/v1/api/session/create``. The live manager serves
both concatenations (probed 2026-08-18: the canonical documented
``/api/session/create`` is the working session-auth flow; the
base-folded path answers HTTP 400 on an empty body where garbage-path
controls answer 404). The connector keeps the canonical endpoint;
:data:`_SPEC_MODELED_OP_IDS` translates it to the spec's modeling for
the reconcile only.

Evidence + activation record: the ``nsx-9.0`` entry of
``docs/decisions/spec-reconcile-guards-standard.md``.

The manifest-pin test below runs unconditionally, so the enumeration
wiring stays proven even where the shelf is not provisioned: a renamed
constant, a dropped path, or a new hand-coded path shows up as a loud
diff there, never as a silently shrunk (or vacuously green) reconcile.
"""

from __future__ import annotations

from meho_backplane.connectors.nsx import connector as _connector_module
from meho_backplane.connectors.nsx import typed_reads as _typed_reads_module
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "nsx-9.0"
#: Both manager-served specs, converted to OpenAPI 3.0 on the shelf
#: (see the module docstring); the lane unions their served sets.
_SPEC_FILES = ("nsx_api.openapi3.json", "nsx_policy_api.openapi3.json")

#: Live-truth op_ids translated to the pinned spec's modeling for the
#: reconcile assert only. Every entry is a documented vendor-spec quirk,
#: never a repoint of working connector code — see the module docstring
#: for the single current case (session-create under ``basePath``).
_SPEC_MODELED_OP_IDS = {
    "POST:/api/session/create": "POST:/api/v1/api/session/create",
}


def _declared_op_ids() -> set[str]:
    """Introspect the nsx connector's hand-coded ``METHOD:/path`` op_ids.

    Sweeps every ``*_PATH`` string constant in the typed-reads module
    (each is issued through ``HttpConnector._get_json``, so the method is
    ``GET``) and adds the connector module's session-establish ``POST``.
    Live constants, never a hardcoded mirror — a path edit in the
    connector flows into the reconcile automatically (the #2944 pattern).
    """
    declared = {
        f"GET:{value}"
        for name, value in vars(_typed_reads_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }
    declared.add(f"POST:{_connector_module._SESSION_CREATE_PATH}")
    return declared


def test_nsx_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift can't silently shrink the reconcile.

    Runs unconditionally (no shelf needed), so the enumeration is proven
    on every sweep. A new hand-coded path, a renamed ``*_PATH``
    constant, or a typed op that stops using ``GET`` must update this
    manifest consciously — and the lane's declared set moves with it.
    Template segments carry the vendor's own parameter names (reconciled
    against the pinned specs in the lane below).
    """
    assert _declared_op_ids() == {
        "GET:/api/v1/alarms",
        "GET:/api/v1/cluster/backups/config",
        "GET:/api/v1/cluster/backups/status",
        "GET:/api/v1/cluster/status",
        "GET:/api/v1/node",
        "GET:/api/v1/transport-nodes",
        "GET:/api/v1/transport-nodes/{transport-node-id}/state",
        "GET:/policy/api/v1/infra/segments",
        "GET:/policy/api/v1/infra/sites/{site-id}/enforcement-points"
        "/{enforcementpoint-id}/transport-zones",
        "GET:/policy/api/v1/infra/tier-1s",
        "POST:/api/session/create",
    }


def test_nsx_hand_coded_paths_are_served_by_the_pinned_specs() -> None:
    """Every hand-coded nsx op_id is served by the pinned nsx-9.0 specs.

    Resolves both shelf specs (skipping with the harness's uniform
    reason wherever either is unprovisioned), unions their served sets,
    and asserts the declared set — with :data:`_SPEC_MODELED_OP_IDS`
    applied — against the union. A red run here is the guard surfacing
    a finding; triage per docs/decisions/spec-reconcile-guards-standard.md.
    """
    served: set[str] = set()
    for spec_file in _SPEC_FILES:
        spec_path = require_shelf_spec(_SPEC_DIR, spec_file)
        served |= openapi_served_op_ids(spec_path)
    declared = {_SPEC_MODELED_OP_IDS.get(op_id, op_id) for op_id in _declared_op_ids()}
    assert_op_ids_served(
        declared,
        served,
        spec_label=f"{_SPEC_DIR}/{{{' + '.join(_SPEC_FILES)}}}",
    )
