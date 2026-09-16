# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor for the meho-automation add-on generic connector.

The connector's op allowlist admits six ops (see the catalog row): four
**write** POSTs and two run-read GETs (#3699). This floor governs the
four writes. They are all POSTs, so the generic ingest heuristic
(``_safety_level_for``: POST/PUT/PATCH -> ``caution``, #3563) classifies every
one ``caution`` by default. This floor **pins** those decided tiers so a spec
re-ingest — or any future change to the generic verb heuristic — cannot
silently reset them:

* ``POST /api/v1/runs`` (**launch**) -> ``caution``. Launch executes a
  potentially destructive lifecycle run, but does **not** park for a
  backplane approval (``requires_approval=False``). Operator decision (T-dec-4
  of the governed-launch design): the add-on's own in-run **gate nodes** are
  the human control point, not a second backplane-side approval on the launch
  call. This is documented in docs/codebase/connectors-meho-automation.md.
* ``POST /api/v1/runs/{run_id}/gates/{node_id}/decision`` (**gate decision**)
  -> ``caution``, no approval park (same operator decision).
* ``POST /api/v1/blueprints/{blueprint_id}/validate`` (**validate**) ->
  ``caution``. Validate is a read-side dry-run preview (it dispatches
  nothing), but an ingested POST never sits below the ``caution`` floor, so it
  rides ``caution`` too. ``caution`` executes immediately with no approval
  park, so the tier is operationally identical to a lower one here — pinning
  it ``caution`` keeps the decided tier deterministic regardless of whether
  this connector's floor registration is in force at ingest time.
* ``POST /api/v1/runs/{run_id}/nodes/{node_id}/resume`` (**run-node resume**)
  -> ``caution``, no approval park. Re-checks or re-runs a failed run node
  (``action`` = ``recheck``|``rerun``) — an agent nudge on the add-on's own
  run, audited synchronously. The sibling ``.../skip`` route is deliberately
  **not** ingested (human-only in the add-on): the catalog allowlist keys on
  ``(method, path)``, so the skip pair never matches and is dropped before
  persistence, so it never reaches this floor or the allowlist.

The floor is idempotent: it sets each op to a fixed level via ``model_copy``,
so applying it to an already-floored proto yields the same result (and
``has_safety_floor`` still detects the floor). The interlock in
``_upsert._apply_safety_metadata`` never *weakens* a floored op below an
operator's manual edit; because ``apply_safety_floor`` bakes the decided
level into the proto before the merge, a re-ingest cannot drift any of the
four below ``caution``.

Registered as an import side effect (see the package ``__init__``), keyed by
the dispatch-canonical ``(product="mehoauto", impl_id="mehoauto-rest")`` the
ingest pipeline derives from the connector_id via ``parse_connector_id``.
"""

from __future__ import annotations

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

#: The version label the floor governs (the catalog row / target fingerprint).
_MEHO_AUTOMATION_VERSION = "0.1.0"

#: The four add-on WRITE ops are pinned ``caution`` (no approval park). Keyed
#: by ``(METHOD, canonical path)``. Paths are the add-on's spec paths verbatim
#: (its OpenAPI declares no servers block, so no mount prefix is stripped at
#: ingest). The two allowlisted run-read GETs (#3699) are deliberately absent:
#: reads land ``safe`` under the generic verb heuristic, below the caution
#: write floor, so they need no pin. A drift-guard test
#: (``test_operations_ingest_catalog.py``) asserts this set is a subset of the
#: catalog allowlist, whose only extra entries are those two GET reads.
_CAUTION_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/runs"),
        ("POST", "/api/v1/runs/{run_id}/gates/{node_id}/decision"),
        ("POST", "/api/v1/blueprints/{blueprint_id}/validate"),
        ("POST", "/api/v1/runs/{run_id}/nodes/{node_id}/resume"),
    }
)


def meho_automation_safety_floor(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Pin the meho-automation add-on op tiers (launch/gate/validate/resume ``caution``)."""
    if version != _MEHO_AUTOMATION_VERSION:
        return proto
    key = (proto.method.upper(), proto.path.split("?", 1)[0])
    if key in _CAUTION_OPS:
        return proto.model_copy(update={"safety_level": "caution", "requires_approval": False})
    return proto


#: Dispatch-canonical product / impl the floor is keyed by (see module docstring).
_PRODUCT = "mehoauto"
_IMPL_ID = "mehoauto-rest"


def register_safety_floor() -> None:
    """Register this connector's ingest safety floor in the process-global registry.

    Idempotent: :func:`register_ingest_safety_floor` assigns
    ``_FLOORS[(product, impl_id)] = floor``, so calling this more than once
    (e.g. re-invoked by a test after another test cleared ``_FLOORS``) leaves
    exactly one registration and is safe. Called once as an import side effect
    below (via the package ``__init__``); a test may call it directly to make
    a ``has_safety_floor`` assertion order-robust without relying on import
    order (``_FLOORS`` isolation is tracked in #3605).
    """
    register_ingest_safety_floor(
        product=_PRODUCT,
        impl_id=_IMPL_ID,
        floor=meho_automation_safety_floor,
    )


register_safety_floor()
