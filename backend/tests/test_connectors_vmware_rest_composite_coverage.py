# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Derived completeness guards for the vmware-rest composite registry (#3519)."""

from __future__ import annotations

import pytest

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.vmware_rest.composites import _write_preview
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES

# These writes deliberately do not park a proposed-effect preview. The exception
# set names policy, while the registry remains the source for every op-id set.
_NO_PREVIEW = frozenset(
    {
        "vmware.composite.vm.import_from_library",
        "vmware.composite.network.portgroup.create",
        "vmware.composite.network.portgroup.security.set",
        "vmware.composite.network.portgroup.vlan.set",
    }
)
# Guest channel children are governed inline and therefore have no discoverable
# standing-grant manifest.
_NO_GOVERNED_SUBOPS = frozenset(
    {
        "vmware.composite.vm.guest.file.write",
        "vmware.composite.vm.guest.program.run",
    }
)

# These registry entries intentionally use the classifier's generic ``other``
# branch. Every remaining composite must resolve to a named classifier class.
_CLASSIFIER_OTHER = frozenset(
    {
        "vmware.composite.cluster.drs_recommendations",
        "vmware.composite.event.tail",
        "vmware.composite.datastore.usage",
        "vmware.composite.datastore.refresh",
        "vmware.composite.network.portgroup.audit",
        "vmware.composite.vm.clone",
        "vmware.composite.vm.deploy_from_library",
        "vmware.composite.vm.import_from_library",
        "vmware.composite.vm.snapshot.revert",
        "vmware.composite.vm.destroy",
        "vmware.composite.vm.migrate",
        "vmware.composite.vm.power.bulk",
        "vmware.composite.vm.power",
        "vmware.composite.vm.disk.grow",
        "vmware.composite.vm.disk.attach",
        "vmware.composite.vm.clone_from_template",
        "vmware.composite.host.evacuate",
        "vmware.composite.host.detach_from_vds",
        "vmware.composite.network.portgroup.security.set",
        # vlan.set ends in ``.set`` (not a write suffix) -> the classifier's
        # generic ``other`` branch, like security.set.
        "vmware.composite.network.portgroup.vlan.set",
        "vmware.composite.vm.resize",
        "vmware.composite.vm.nic.repoint",
        "vmware.composite.vm.device.cdrom",
        "vmware.composite.vm.customize",
        "vmware.composite.host.datastore_mount_nfs",
        "vmware.composite.host.disk_mark_flash",
        "vmware.composite.host.service_control",
        # #3717 — guest.env.read / guest.file.read moved OUT of ``other`` into
        # ``credential_write``: they log into the guest and their vim
        # ``NamePasswordAuthentication`` request body must never be recorded by
        # the flight recorder (which delegates body exclusion to classify_op).
        # ``net.show`` stays ``other`` — it sends no in-guest login.
        "vmware.composite.vm.guest.net.show",
        "vmware.composite.supervisor.enable",
        "vmware.composite.supervisor.disable",
        "vmware.composite.supervisor.status",
        "vmware.composite.namespace.status",
        "vmware.composite.content_library.subscribed.sync",
        "vmware.composite.content_library.subscribed.status",
    }
)


def _assert_composite_coverage(
    registry_ids: set[str],
    typed_metadata: dict[str, object],
    preview_builders: dict[str, object],
    governed_subops: dict[str, object],
    classifier: dict[str, str],
) -> None:
    """Fail closed when a registry entry is absent from a required map."""
    approval_ids = {spec.op_id for spec in _COMPOSITES if spec.requires_approval}
    assert set(typed_metadata) == registry_ids
    assert _NO_PREVIEW <= approval_ids <= registry_ids
    assert _NO_GOVERNED_SUBOPS <= approval_ids <= registry_ids
    assert set(preview_builders) | _NO_PREVIEW == approval_ids
    assert set(governed_subops) | _NO_GOVERNED_SUBOPS == approval_ids
    assert set(preview_builders).isdisjoint(_NO_PREVIEW)
    assert set(governed_subops).isdisjoint(_NO_GOVERNED_SUBOPS)
    assert set(classifier) == registry_ids
    other_ids = {op_id for op_id, op_class in classifier.items() if op_class == "other"}
    assert other_ids == _CLASSIFIER_OTHER


def test_registry_coverage_is_derived_from_composites() -> None:
    """Adding a composite changes no count guard; each map must still cover it."""
    registry_ids = {spec.op_id for spec in _COMPOSITES}
    _assert_composite_coverage(
        registry_ids,
        {spec.op_id: spec for spec in _COMPOSITES},
        dict(_write_preview._WRITE_PREVIEW_BUILDERS),
        dict(_GOVERNED_SUBOP_MANIFEST),
        {op_id: classify_op(op_id) for op_id in registry_ids},
    )


def test_deliberately_unmapped_composite_fails_closed() -> None:
    """The guard is non-vacuous: one missing typed metadata entry fails."""
    registry_ids = {spec.op_id for spec in _COMPOSITES}
    metadata = {spec.op_id: spec for spec in _COMPOSITES}
    metadata.pop(next(iter(metadata)))
    with pytest.raises(AssertionError):
        _assert_composite_coverage(
            registry_ids,
            metadata,
            dict(_write_preview._WRITE_PREVIEW_BUILDERS),
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
            registry_ids,
            {spec.op_id: spec for spec in _COMPOSITES},
            dict(_write_preview._WRITE_PREVIEW_BUILDERS),
            dict(_GOVERNED_SUBOP_MANIFEST),
            classifier,
        )
