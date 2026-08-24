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


# ---------------------------------------------------------------------------
# vim (VI-JSON) ``_typeName`` annotation reconcile (#3103)
# ---------------------------------------------------------------------------
#
# VI-JSON request bodies must tag every DataObject with its ``_typeName``
# discriminator: the pinned ``vi-json.yaml`` derives all data objects from
# ``Any`` (whose ``required`` list names ``_typeName``), and a live vCenter
# 8.0.3 rejects un-annotated bodies (``500 InvalidArgument`` — the #3103
# controlled differential; the annotated body returns ``200``). The
# byte-for-byte body pins live in the always-on unit lanes
# (``test_connectors_vmware_rest_composites_write.py`` and the typed-op
# tests); this lane grounds the annotation *vocabulary*: every ``_typeName``
# literal the substrate can emit must name a real component schema in the
# pinned ``vi-json.yaml``.

from meho_backplane.connectors.vmware_rest.vim_body import (  # noqa: E402
    MOREF_TYPE_NAME,
    retrieve_properties_body,
)

#: Every ``_typeName`` value the vim substrate emits, sourced from the
#: emitting constants themselves (a rename/typo there fails this lane's
#: spec grounding immediately). New hand-assembled vim bodies add their
#: type names here; the unit-test byte pins force that addition.
_EMITTED_VIM_TYPE_NAMES: set[str] = {
    MOREF_TYPE_NAME,
    _write._VIRTUAL_DISK_TYPE,
    _write._VIRTUAL_VMXNET3_TYPE,
    _write._DV_PORT_BACKING_TYPE,
    _write._STANDARD_NETWORK_BACKING_TYPE,
    _write._DV_PORT_CONNECTION_TYPE,
    _write._VM_CONFIG_SPEC_TYPE,
    _write._VM_FILE_INFO_TYPE,
    _write._VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
    _write._VM_CLONE_SPEC_TYPE,
    _write._VM_RELOCATE_SPEC_TYPE,
    _write._DVS_CONFIG_SPEC_TYPE,
    _write._DVS_HOST_MEMBER_CONFIG_SPEC_TYPE,
    _write._CLUSTER_CONFIG_SPEC_EX_TYPE,
    _write._CLUSTER_RULE_SPEC_TYPE,
    _write._CLUSTER_AFFINITY_RULE_TYPE,
    _write._CLUSTER_ANTI_AFFINITY_RULE_TYPE,
}


def _collect_type_names(node: object) -> set[str]:
    """Collect every ``_typeName`` value in a *built* body (test-side walk)."""
    found: set[str] = set()
    if isinstance(node, dict):
        tag = node.get("_typeName")
        if isinstance(tag, str):
            found.add(tag)
        for value in node.values():
            found |= _collect_type_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_type_names(item)
    return found


def test_single_prop_retrieve_body_is_the_live_verified_annotated_shape() -> None:
    """Pin the exact annotated single-prop retrieve body (always runs, no shelf).

    Byte-for-byte (modulo values) the body the #3103 live differential
    proved: un-annotated → ``500 InvalidArgument`` (``Invalid MoRef field:
    pathSet``), annotated → ``200 RetrieveResult`` on vCenter 8.0.3.
    """
    body = _write._build_single_prop_retrieve_params("VirtualMachine", "vm-42", "snapshot")
    assert body == {
        "specSet": [
            {
                "_typeName": "PropertyFilterSpec",
                "propSet": [
                    {
                        "_typeName": "PropertySpec",
                        "type": "VirtualMachine",
                        "pathSet": ["snapshot"],
                    }
                ],
                "objectSet": [
                    {
                        "_typeName": "ObjectSpec",
                        "obj": {
                            "_typeName": "ManagedObjectReference",
                            "type": "VirtualMachine",
                            "value": "vm-42",
                        },
                    }
                ],
            }
        ],
        "options": {"_typeName": "RetrieveOptions"},
    }


def test_trio_helper_annotates_every_data_object_and_nothing_else() -> None:
    """The shared retrieve trio emits exactly the five annotation names."""
    body = retrieve_properties_body("HostSystem", ["host-1", "host-2"], ["summary.quickStats"])
    assert _collect_type_names(body) == {
        "PropertyFilterSpec",
        "PropertySpec",
        "ObjectSpec",
        "ManagedObjectReference",
        "RetrieveOptions",
    }
    # One annotated ObjectSpec per moid — multi-object reads (the task
    # poll shape) stay fully annotated too.
    object_set = body["specSet"][0]["objectSet"]
    assert [o["obj"]["value"] for o in object_set] == ["host-1", "host-2"]


def _vi_json_component_schema_exists(spec_text: str, name: str) -> bool:
    """Whether *name* is a component schema key in the pinned ``vi-json.yaml``.

    Line-scans rather than parsing the ~10 MB YAML (the
    ``_vi_json_path_item_has_post`` precedent in the l2 ingest reconcile):
    component schemas are keyed at 4-space indent (``    VirtualDisk:``),
    and the CamelCase vim type names collide with no other 4-indent key
    family in the document.
    """
    return f"\n    {name}:\n" in spec_text


def test_every_emitted_vim_type_name_is_a_pinned_vi_json_schema() -> None:
    """Each ``_typeName`` the substrate emits names a schema in the pinned spec.

    The #3103 grounding lane: the annotation vocabulary — the retrieve trio
    + options, the MoRef tag, and every hand-assembled write-spec tag —
    must exist as component schemas in the pinned ``vi-json.yaml``. A
    fictional or misspelled discriminator fails here before it ever
    reaches a live vCenter. Skips uniformly without the spec shelf.
    """
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    spec_text = spec_path.read_text(encoding="utf-8")
    trio_names = _collect_type_names(
        retrieve_properties_body("VirtualMachine", ["vm-1"], ["snapshot"])
    )
    missing = [
        name
        for name in sorted(_EMITTED_VIM_TYPE_NAMES | trio_names)
        if not _vi_json_component_schema_exists(spec_text, name)
    ]
    assert not missing, (
        f"_typeName value(s) {missing} are not component schemas in the pinned "
        "vi-json.yaml — a fictional/misspelled vim discriminator would be "
        "rejected (or mis-deserialised) by a live vCenter. "
        "Protocol: docs/decisions/spec-reconcile-guards-standard.md."
    )
