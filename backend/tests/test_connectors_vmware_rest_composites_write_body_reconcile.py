# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec body-shape reconcile lane for the write-composite request bodies (#2973).

The path-only reconcile lanes (``test_connectors_vmware_rest_composites_read_reconcile.py``,
``test_connectors_vmware_rest_composites_l2_ingest_reconcile.py``) assert every
hand-coded sub-op **path** is served by the pinned spec, but nothing asserted the
request-**body** shape. That gap is how the legacy ``/rest``-style
``{"spec": {...}}`` envelope survived on the modern ``/api`` write bodies:
``vmware.composite.vm.migrate``'s relocate 400ed ``INVALID_ARGUMENT`` because the
``/api`` surface takes the ``RelocateSpec`` at the **top level** of the body, not
under a ``spec`` wrapper (#2973). The vm.create / NIC / CPU / memory / CD-ROM
PATCH and guest-customization bodies shared the divergent envelope.

This lane closes the gap per the spec-reconcile standard
(``docs/decisions/spec-reconcile-guards-standard.md``): for every ``/api`` write
sub-op the connector POSTs / PATCHes / PUTs a JSON body to, the pinned
``vcenter.yaml`` must declare that ``requestBody`` as a **flat ``*Spec``** (its
top-level properties are the Spec's own fields), never a single-``spec`` wrapper.
The ingest parser folds the resolved body schema onto
``parameter_schema["properties"]["body"]["properties"]``, so a ``/rest``-style
``{"spec": ...}`` body would parse to the single top-level key ``spec`` -- the
exact signal this lane bans.

Division of labour (mirrors the read lane): this lane grounds the flat body
**contract** against the pinned vendor spec and skips uniformly when the spec
shelf is unprovisioned (the #2980 harness contract). The **connector-side** proof
-- that the composites now emit those flat bodies with no ``{"spec": ...}``
envelope -- is the byte-for-byte body assertion set in
``test_connectors_vmware_rest_composites_write.py``, which runs everywhere
(including public CI, where the shelf is absent) and fails the moment a body is
re-wrapped.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites import _write
from tests._spec_shelf import openapi_request_body_props, require_shelf_spec

# The full REST-Automation write surface: every ``/api`` op the connector sends a
# POST / PATCH / PUT to (introspected live in :func:`_rest_write_op_ids`). Frozen
# here as the always-on deliberate-change gate -- a new REST write op that lands
# without being added forces a body-shape review (the shelf-backed lanes below
# skip where the shelf is unprovisioned, so this pin is the sandbox backstop).
_EXPECTED_REST_WRITE_OP_IDS = {
    "POST:/vcenter/vm",
    "POST:/vcenter/vm/{vm}/hardware/ethernet",
    "POST:/vcenter/vm/{vm}?action=relocate",
    "POST:/vcenter/vm-template/library-items/{templateLibraryItem}?action=deploy",
    "POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy",
    "POST:/content/library?action=find",
    "POST:/content/library/item?action=find",
    "POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true",
    "POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=disconnect",
    "PATCH:/vcenter/vm/{vm}/hardware/cpu",
    "PATCH:/vcenter/vm/{vm}/hardware/memory",
    "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
    "PATCH:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
    "POST:/vcenter/guest/customization-specs",
    "PUT:/vcenter/vm/{vm}/guest/customization",
}

# The bodies #2973 flattened from the ``/rest`` ``{"spec": {...}}`` envelope to
# the flat ``/api`` ``*Spec``. Each must be served by the pinned spec with a flat
# request body -- this set is the non-vacuous floor for the core lane (a parser
# regression that dropped one would make the sweep silently green). The two NIC
# PATCH call sites (repoint + detach fallback) share one op_id.
_FLATTENED_WRITE_OP_IDS = {
    "POST:/vcenter/vm",
    "POST:/vcenter/vm/{vm}/hardware/ethernet",
    "POST:/vcenter/vm/{vm}?action=relocate",
    "PATCH:/vcenter/vm/{vm}/hardware/cpu",
    "PATCH:/vcenter/vm/{vm}/hardware/memory",
    "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
    "PATCH:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}",
    "POST:/vcenter/guest/customization-specs",
    "PUT:/vcenter/vm/{vm}/guest/customization",
}


def _rest_write_op_ids() -> set[str]:
    """Every REST-Automation write op_id in ``_write``, introspected live.

    A REST write op is a ``POST`` / ``PATCH`` / ``PUT`` ``_OP_*`` constant whose
    path root is a lowercase Automation family (``/vcenter``, ``/esx``,
    ``/content``, ...). The vmomi VI-JSON methods (``ReconfigVM_Task``,
    ``CloneVM_Task``, ...) dispatch through ``_post_vmomi_json`` onto the
    ``/sdk/vim25`` mount and are keyed by an **uppercase** managed-object type
    (``/VirtualMachine/{moId}/...``); their request types genuinely carry a
    ``spec`` field, so they are correctly excluded from the flat-body contract.
    """
    ops: set[str] = set()
    for name in dir(_write):
        if not name.startswith("_OP_"):
            continue
        value = getattr(_write, name)
        if not isinstance(value, str) or ":" not in value:
            continue
        method, _, path = value.partition(":")
        if method not in {"POST", "PATCH", "PUT"}:
            continue
        first = path.lstrip("/").split("/", 1)[0].split("?", 1)[0]
        if not first or first[0].isupper():  # vmomi VI-JSON method, not the /api surface
            continue
        ops.add(value)
    return ops


def test_rest_write_op_ids_match_the_frozen_set() -> None:
    """Introspection agrees with the frozen surface (always runs, no shelf).

    A REST-Automation write op that lands without being added to
    ``_EXPECTED_REST_WRITE_OP_IDS`` fails here, forcing a body-shape review of
    the new op -- the deliberate-change gate the shelf-backed lanes need (they
    skip where the shelf is unprovisioned).
    """
    live = _rest_write_op_ids()
    assert live, "introspection found no REST write _OP_* constants -- wiring broke"
    assert live == _EXPECTED_REST_WRITE_OP_IDS, (
        f"unexpected REST write op_ids: {sorted(live - _EXPECTED_REST_WRITE_OP_IDS)}; "
        f"missing from the frozen surface: {sorted(_EXPECTED_REST_WRITE_OP_IDS - live)}"
    )
    assert _FLATTENED_WRITE_OP_IDS <= _EXPECTED_REST_WRITE_OP_IDS


def test_api_write_bodies_are_flat_specs_not_rest_spec_envelopes() -> None:
    """No ``/api`` write op declares a single-``spec`` request-body wrapper.

    The core body-shape reconcile: for every REST write sub-op the pinned
    ``vcenter.yaml`` serves with a JSON body, the ``requestBody`` must be a flat
    ``*Spec`` (top-level Spec fields), never the ``/rest``-style
    ``{"spec": {...}}`` envelope -- whose parsed body has the single top-level key
    ``spec``. Ops served without a body (the bodyless ``?action=disconnect``
    verb) are not the body lane's concern; ops not served at all are the path
    lane's. Skips uniformly without the spec shelf.
    """
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    body_props = openapi_request_body_props(spec_path)
    checked = 0
    for op_id in sorted(_rest_write_op_ids()):
        props = body_props.get(op_id)
        if props is None:
            continue  # bodyless action verb, or not served -- out of the body lane's scope
        assert props != {"spec"}, (
            f"{op_id}: the pinned vcenter.yaml declares a single-`spec` request-body "
            f"wrapper ({sorted(props)}). The /api surface takes the *Spec at the top "
            "level, so the connector must send the Spec's fields directly, never a "
            "/rest-style {'spec': ...} envelope (#2973). "
            "Protocol: docs/decisions/spec-reconcile-guards-standard.md."
        )
        checked += 1
    assert checked, "no write-op request bodies were checked -- shelf or parser wiring broke"


def test_flattened_2973_bodies_are_served_flat_by_the_pinned_spec() -> None:
    """Every body #2973 flattened is served by the pinned spec as a flat ``*Spec``.

    The non-vacuous floor: each op the fix unwrapped must appear in the pinned
    spec's request-body map with top-level Spec fields (not a ``spec`` wrapper),
    so a parser/introspection regression that silently dropped the ops cannot
    leave the core lane vacuously green. Skips without the shelf.
    """
    spec_path = require_shelf_spec("vcenter-9.0", "vcenter.yaml")
    body_props = openapi_request_body_props(spec_path)
    for op_id in sorted(_FLATTENED_WRITE_OP_IDS):
        assert op_id in body_props, (
            f"{op_id}: not served with a request body by the pinned vcenter.yaml -- the "
            "flatten target moved (the path reconcile lane localises a moved path)."
        )
        assert body_props[op_id] != {"spec"}, (
            f"{op_id}: still a single-`spec` wrapper in the pinned spec -- the flat-body "
            "contract this task relies on does not hold for it."
        )
