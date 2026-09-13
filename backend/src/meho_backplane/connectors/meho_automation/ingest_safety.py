# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor for the meho-automation add-on generic connector.

The three ops ingested from the add-on's ``/openapi.json`` are all POSTs, so
the generic ingest heuristic (``_safety_level_for``: POST/PUT/PATCH ->
``caution``, #3563) classifies every one ``caution`` by default. This floor
**pins** the decided tiers so a spec re-ingest — or any future change to the
generic verb heuristic — cannot silently reset them:

* ``POST /api/v1/runs`` (**launch**) -> ``caution``. Launch executes a
  potentially destructive lifecycle run, but does **not** park for a
  backplane approval (``requires_approval=False``). Operator decision (T-dec-4
  of the governed-launch design): the add-on's own in-run **gate nodes** are
  the human control point, not a second backplane-side approval on the launch
  call. This is documented in docs/codebase/connectors-meho-automation.md.
* ``POST /api/v1/runs/{run_id}/gates/{node_id}/decision`` (**gate decision**)
  -> ``caution``, no approval park (same operator decision).
* ``POST /api/v1/blueprints/{blueprint_id}/validate`` (**validate**) ->
  ``safe``. Validate is a read-side dry-run preview (it dispatches nothing);
  the POST-by-default ``caution`` is downgraded to ``safe`` so agents can
  preview a blueprint freely.

The floor is idempotent: it sets each op to a fixed level via ``model_copy``,
so applying it to an already-floored proto yields the same result (and
``has_safety_floor`` still detects the floor). The interlock in
``_upsert._apply_safety_metadata`` never *weakens* a floored op below an
operator's manual edit; because ``apply_safety_floor`` bakes the decided
level into the proto before the merge, a re-ingest cannot drift launch/gate
below ``caution`` or raise validate above ``safe``.

Registered as an import side effect (see the package ``__init__``), keyed by
the dispatch-canonical ``(product="mehoauto", impl_id="mehoauto-rest")`` the
ingest pipeline derives from the connector_id via ``parse_connector_id``.
"""

from __future__ import annotations

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

#: The version label the floor governs (the catalog row / target fingerprint).
_MEHO_AUTOMATION_VERSION = "0.1.0"

#: Launch + gate-decision are pinned ``caution`` (no approval park); validate
#: is pinned ``safe``. Keyed by ``(METHOD, canonical path)``. Paths are the
#: add-on's spec paths verbatim (its OpenAPI declares no servers block, so no
#: mount prefix is stripped at ingest).
_CAUTION_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/runs"),
        ("POST", "/api/v1/runs/{run_id}/gates/{node_id}/decision"),
    }
)
_SAFE_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/blueprints/{blueprint_id}/validate"),
    }
)


def meho_automation_safety_floor(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Pin the meho-automation add-on op tiers (launch/gate ``caution``, validate ``safe``)."""
    if version != _MEHO_AUTOMATION_VERSION:
        return proto
    key = (proto.method.upper(), proto.path.split("?", 1)[0])
    if key in _CAUTION_OPS:
        return proto.model_copy(update={"safety_level": "caution", "requires_approval": False})
    if key in _SAFE_OPS:
        return proto.model_copy(update={"safety_level": "safe", "requires_approval": False})
    return proto


register_ingest_safety_floor(
    product="mehoauto",
    impl_id="mehoauto-rest",
    floor=meho_automation_safety_floor,
)
