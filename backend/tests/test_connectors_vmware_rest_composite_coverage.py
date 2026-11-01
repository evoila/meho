# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Derived completeness guards for the vmware-rest composite registry (#3519)."""

from __future__ import annotations

import pytest

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.operations._preview import _PREVIEW_BUILDERS

# The preview builders the registration rows carry (``CompositeSpec.preview``).
_ROW_PREVIEW_BUILDERS = {
    spec.op_id: spec.preview for spec in _COMPOSITES if spec.preview is not None
}

# Guest channel children are governed inline and therefore have no discoverable
# standing-grant manifest.
_NO_GOVERNED_SUBOPS = frozenset(
    {
        "vmware.composite.vm.guest.file.write",
        "vmware.composite.vm.guest.program.run",
    }
)


def _assert_composite_coverage(
    specs: tuple[CompositeSpec, ...],
    typed_metadata: dict[str, object],
    preview_builders: dict[str, object],
    governed_subops: dict[str, object],
    classifier: dict[str, str],
) -> None:
    """Fail closed when a registry entry is absent from a required map."""
    registry_ids = {spec.op_id for spec in specs}
    approval_ids = {spec.op_id for spec in specs if spec.requires_approval}
    # An op that can wait for approval: every requires_approval op, plus a
    # caution or riskier op, which waits for an agent's call (#4850 — the
    # caution read vm.guest.file.read). Each needs a preview builder.
    parkable_ids = {
        spec.op_id for spec in specs if spec.requires_approval or spec.safety_level != "safe"
    }
    # The rows say which writes park no preview and which op ids the
    # classifier puts in ``other`` (``CompositeSpec.no_preview`` /
    # ``.classifier_other``), so a new composite changes no list here.
    no_preview = {spec.op_id for spec in specs if spec.no_preview}
    classifier_other = {spec.op_id for spec in specs if spec.classifier_other}
    assert set(typed_metadata) == registry_ids
    assert no_preview <= approval_ids <= registry_ids
    assert _NO_GOVERNED_SUBOPS <= approval_ids <= registry_ids
    assert set(preview_builders) | no_preview == parkable_ids
    assert set(governed_subops) | _NO_GOVERNED_SUBOPS == approval_ids
    assert set(preview_builders).isdisjoint(no_preview)
    assert set(governed_subops).isdisjoint(_NO_GOVERNED_SUBOPS)
    assert set(classifier) == registry_ids
    other_ids = {op_id for op_id, op_class in classifier.items() if op_class == "other"}
    assert other_ids == classifier_other


def _check(specs: tuple[CompositeSpec, ...]) -> None:
    """Run the coverage rules on *specs* with the real maps."""
    registry_ids = {spec.op_id for spec in specs}
    _assert_composite_coverage(
        specs,
        {spec.op_id: spec for spec in specs},
        _registered_preview_builders(registry_ids),
        dict(_GOVERNED_SUBOP_MANIFEST),
        {op_id: classify_op(op_id) for op_id in registry_ids},
    )


def test_registry_coverage_is_derived_from_composites() -> None:
    """Adding a composite changes no count guard; each map must still cover it."""
    _check(_COMPOSITES)


def _registered_preview_builders(registry_ids: set[str]) -> dict[str, object]:
    """Every preview builder of a composite: the one its row carries, registered at import."""
    found = {op_id: b for op_id, b in _PREVIEW_BUILDERS.items() if op_id in registry_ids}
    for op_id, builder in _ROW_PREVIEW_BUILDERS.items():
        assert found.get(op_id) is builder, op_id
    return found


def _with_row_changed(op_id: str, **changes: object) -> tuple[CompositeSpec, ...]:
    """The rows, with the row of *op_id* changed."""
    assert op_id in {spec.op_id for spec in _COMPOSITES}
    return tuple(spec._replace(**changes) if spec.op_id == op_id else spec for spec in _COMPOSITES)


def test_deliberately_unmapped_composite_fails_closed() -> None:
    """The guard is non-vacuous: one missing typed metadata entry fails."""
    registry_ids = {spec.op_id for spec in _COMPOSITES}
    metadata = {spec.op_id: spec for spec in _COMPOSITES}
    metadata.pop(next(iter(metadata)))
    with pytest.raises(AssertionError):
        _assert_composite_coverage(
            _COMPOSITES,
            metadata,
            dict(_ROW_PREVIEW_BUILDERS),
            dict(_GOVERNED_SUBOP_MANIFEST),
            {op_id: classify_op(op_id) for op_id in registry_ids},
        )


def test_deliberately_unhandled_classifier_op_fails_closed() -> None:
    """A new generic-classified composite needs an explicit classifier decision."""
    registry_ids = {spec.op_id for spec in _COMPOSITES}
    classifier = {op_id: classify_op(op_id) for op_id in registry_ids}
    classifier["vmware.composite.vm.create"] = "other"
    with pytest.raises(AssertionError):
        _assert_composite_coverage(
            _COMPOSITES,
            {spec.op_id: spec for spec in _COMPOSITES},
            dict(_ROW_PREVIEW_BUILDERS),
            dict(_GOVERNED_SUBOP_MANIFEST),
            classifier,
        )


@pytest.mark.parametrize(
    ("op_id", "changes"),
    [
        # A write that parks no preview but does not say so on its row.
        ("vmware.composite.network.portgroup.create", {"no_preview": False}),
        # A row that says "no preview" but has a preview builder.
        ("vmware.composite.vm.create", {"no_preview": True}),
        # "No preview" on a read that never waits for approval.
        ("vmware.composite.event.tail", {"no_preview": True}),
        # The classifier says ``other``, but the row does not.
        ("vmware.composite.vm.clone", {"classifier_other": False}),
        # The row says ``other``, but the classifier names a class.
        ("vmware.composite.vm.create", {"classifier_other": True}),
    ],
)
def test_each_row_flag_rule_fails_on_a_row_that_breaks_it(
    op_id: str, changes: dict[str, object]
) -> None:
    """Each rule on the row flags fails when one row breaks it."""
    with pytest.raises(AssertionError):
        _check(_with_row_changed(op_id, **changes))
