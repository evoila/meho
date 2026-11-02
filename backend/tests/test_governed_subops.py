# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the governed-subop discovery registry + classifier (#3349).

Covers the connector-agnostic core
(:mod:`meho_backplane.operations.governed_subops`) and the vmware-rest
wiring that populates it from the ``_SUB_OPS_*`` / ``_VIM_SUB_OPS_*``
manifests — asserting the child sets are derived (not re-typed) and that a
delete / rollback leg is flagged un-grantable.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meho_backplane.operations.governed_subops import (
    classify_subop_grantability,
    governed_subops_for,
    register_governed_subops,
    registered_governed_subop_surfaces,
    reset_governed_subop_registry,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_governed_subop_registry()
    yield
    reset_governed_subop_registry()


def test_register_dedups_and_lookup() -> None:
    """Registration de-dups children (a child can appear in both manifest halves)."""
    register_governed_subops(
        composite_op_id="x.composite.foo",
        connector_id="x-rest-1.0",
        sub_op_ids=("POST:/a", "POST:/b", "POST:/a"),
    )
    surface = governed_subops_for("x.composite.foo")
    assert surface is not None
    assert surface.connector_id == "x-rest-1.0"
    assert surface.sub_op_ids == ("POST:/a", "POST:/b")


def test_register_empty_is_noop() -> None:
    """A composite with no governed children registers nothing (no grant set)."""
    register_governed_subops(
        composite_op_id="x.composite.bare", connector_id="x-1.0", sub_op_ids=()
    )
    assert governed_subops_for("x.composite.bare") is None


def test_classify_flags_delete_shaped() -> None:
    """The classifier flags delete-shaped ops un-grantable, others grantable."""
    assert classify_subop_grantability("POST:/vcenter/vm").grantable is True
    delete_leg = classify_subop_grantability("DELETE:/vcenter/vm/{vm}")
    assert delete_leg.grantable is False
    assert "delete-shaped" in (delete_leg.ungrantable_reason or "")


def test_classify_flags_vendor_delete_action_names() -> None:
    """A VI-JSON delete is a POST; its action name makes it un-grantable (#4632)."""
    destroy = classify_subop_grantability("POST:/VirtualMachine/{moId}/Destroy_Task")
    assert destroy.grantable is False
    assert "vendor action name 'Destroy_Task'" in (destroy.ungrantable_reason or "")
    assert classify_subop_grantability("POST:/VirtualMachine/{moId}/Reset_Task").grantable is True


def test_classify_vendor_delete_action_name_ignores_pattern_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The action-name sign holds when SERVICE_GRANT_DELETE_SHAPED_PATTERNS is changed."""
    monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", "acme.custom.nuke")
    get_settings.cache_clear()
    assert (
        classify_subop_grantability("POST:/VirtualMachine/{moId}/Destroy_Task").grantable is False
    )


def test_vm_destroy_children_flag_the_vim_destroy_step() -> None:
    """vm.destroy's governed steps: the VIM destroy is un-grantable, the read stays grantable."""
    from meho_backplane.connectors.vmware_rest.composites._register import (
        register_vmware_governed_subops,
    )

    register_vmware_governed_subops()
    vm_destroy = governed_subops_for("vmware.composite.vm.destroy")
    assert vm_destroy is not None
    by_op = {c: classify_subop_grantability(c) for c in vm_destroy.sub_op_ids}

    destroy = by_op["POST:/VirtualMachine/{moId}/Destroy_Task"]
    assert destroy.grantable is False
    assert destroy.ungrantable_reason is not None
    assert "Destroy_Task" in destroy.ungrantable_reason

    read = by_op["POST:/PropertyCollector/{moId}/RetrievePropertiesEx"]
    assert read.grantable is True
    assert read.ungrantable_reason is None


def test_vmware_wiring_is_derived_from_manifests() -> None:
    """register_vmware_governed_subops populates the write composites from manifests.

    vm.create's rollback / delete leg (``DELETE:/vcenter/vm/{vm}``) rides its
    ``_SUB_OPS_VM_CREATE`` manifest unchanged and is flagged un-grantable.
    """
    from meho_backplane.connectors.vmware_rest.composites import _write
    from meho_backplane.connectors.vmware_rest.composites._register import (
        register_vmware_governed_subops,
    )

    register_vmware_governed_subops()
    surfaces = registered_governed_subop_surfaces()
    assert "vmware.composite.vm.create" in surfaces
    assert "vmware.composite.vm.destroy" in surfaces

    vm_create = governed_subops_for("vmware.composite.vm.create")
    assert vm_create is not None
    # Derived, not re-typed: every REST-manifest child id is present verbatim.
    assert set(_write._SUB_OPS_VM_CREATE) <= set(vm_create.sub_op_ids)

    # The delete rollback leg is present and flagged un-grantable.
    delete_children = [
        c for c in vm_create.sub_op_ids if not classify_subop_grantability(c).grantable
    ]
    assert any(c.startswith("DELETE:") for c in delete_children)


def test_vm_destroy_lists_the_delete_step_for_both_vcenter_apis() -> None:
    """vm.destroy's manifest names the delete step of both arms.

    Before vCenter 9.0 the composite deletes through the vim ``Destroy_Task``;
    on 9.0+ it issues the REST ``DELETE:/vcenter/vm/{vm}``. Both steps run
    through the sub-op gate, so both must be discoverable, and the REST delete
    is flagged un-grantable like vm.create's rollback leg.
    """
    from meho_backplane.connectors.vmware_rest.composites import _write
    from meho_backplane.connectors.vmware_rest.composites._register import (
        register_vmware_governed_subops,
    )

    register_vmware_governed_subops()
    surface = governed_subops_for("vmware.composite.vm.destroy")
    assert surface is not None
    assert _write._OP_DESTROY_VM_TASK in surface.sub_op_ids  # before vCenter 9.0
    assert _write._OP_DELETE_VM in surface.sub_op_ids  # vCenter 9.0 and later
    assert classify_subop_grantability(_write._OP_DELETE_VM).grantable is False
