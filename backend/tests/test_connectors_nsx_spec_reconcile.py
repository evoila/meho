# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""nsx real-spec reconcile lane — hand-coded paths vs the pinned nsx-9.0 spec.

#2981 (initiative #2979): every hand-coded ``METHOD:/path`` literal in the
nsx connector asserts against the pinned ``nsx-9.0`` spec through the
#2980 harness (:mod:`tests._spec_shelf`). The declared set is introspected
from the connector's live module constants — the ten ``*_PATH`` typed-read
paths in :mod:`meho_backplane.connectors.nsx.typed_reads` (all dispatched
via ``HttpConnector._get_json``, hence ``GET``) plus the session-establish
``POST`` path in :mod:`meho_backplane.connectors.nsx.connector`. The
fingerprint probe's inline ``/api/v1/node`` literal collapses into the
``GET:/api/v1/node`` typed-read op_id, so the sweep covers it too.

**This lane ships dormant.** No public NSX 9 OpenAPI spec exists to pin as
of 2026-04-29: the shelf's ``nsx-9.0/`` directory carries only a
``MANIFEST.md`` recording the negative result (five public locations
checked; 30 candidate spec endpoints probed on a live NSX 9.0.2 manager
under both HTTP Basic and session auth — all 404 or 302 to the UI). The
harness's uniform skip covers exactly this shape, so the lane skips with
a reason naming the missing file and arms itself the day a spec lands at
``nsx-9.0/nsx-openapi.json`` on the shelf — no code change needed here.
Evidence + activation steps: the "Evidenced exclusions" section of
``docs/decisions/spec-reconcile-guards-standard.md``.

The manifest-pin test below runs unconditionally, so the enumeration
wiring stays proven while the lane itself is dormant: a renamed constant,
a dropped path, or a new hand-coded path shows up as a loud diff there,
never as a silently shrunk (or vacuously green) reconcile.
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
_SPEC_FILE = "nsx-openapi.json"


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
    on every sweep even while the lane below is dormant. A new hand-coded
    path, a renamed ``*_PATH`` constant, or a typed op that stops using
    ``GET`` must update this manifest consciously — and the lane's
    declared set moves with it.
    """
    assert _declared_op_ids() == {
        "GET:/api/v1/alarms",
        "GET:/api/v1/cluster/backups/config",
        "GET:/api/v1/cluster/backups/status",
        "GET:/api/v1/cluster/status",
        "GET:/api/v1/node",
        "GET:/api/v1/transport-nodes",
        "GET:/api/v1/transport-nodes/{id}/state",
        "GET:/policy/api/v1/infra/segments",
        "GET:/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones",
        "GET:/policy/api/v1/infra/tier-1s",
        "POST:/api/session/create",
    }


def test_nsx_hand_coded_paths_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded nsx op_id is served by the pinned nsx-9.0 spec.

    Dormant until a spec lands on the shelf (see the module docstring);
    skips with the harness's uniform reason today. Wherever
    ``MEHO_CONSUMER_DOCS_ROOT`` resolves ``nsx-9.0/nsx-openapi.json`` the
    lane runs for real, and a red first run is the guard surfacing a
    finding — triage per docs/decisions/spec-reconcile-guards-standard.md
    (expect at least the ``{id}`` template segment of the transport-node
    state path to need reconciling against the vendor's parameter name).
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(
        _declared_op_ids(),
        served,
        spec_label=f"{_SPEC_DIR}/{_SPEC_FILE}",
    )
