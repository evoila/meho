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


def test_vmware_wiring_is_derived_from_manifests() -> None:
    """register_vmware_governed_subops populates the write composites from manifests.

    vm.create's rollback / delete leg (``DELETE:/vcenter/vm/{vm}``) rides its
    ``_SUB_OPS_VM_CREATE`` manifest unchanged and is flagged un-grantable.
    """
    from meho_backplane.connectors.vmware_rest.composites import _write
    from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
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
