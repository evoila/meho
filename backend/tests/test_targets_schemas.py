# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the targets Pydantic schemas (G0.3-T2 acceptance criteria).

Coverage matrix:

* All four schemas import from the package root.
* :class:`Target` and :class:`TargetSummary` are frozen.
* :class:`TargetCreate` and :class:`TargetUpdate` are not frozen (mutable
  input models).
* Round-trip: ``model_dump()`` → ``model_validate()`` is lossless for
  :class:`Target` and :class:`TargetSummary`.
* :class:`TargetCreate` validation:
  - Rejects empty ``name`` (min_length=1).
  - Rejects port=0 and port=70000 (ge=1, le=65535).
  - Accepts valid port values.
  - Defaults: ``auth_model=shared_service_account``, ``vpn_required=False``,
    ``extras={}``, ``aliases=[]``.
* :class:`TargetUpdate` accepts all-None (no fields provided).
* :class:`AuthModel` re-exported correctly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.targets import Target, TargetCreate, TargetSummary, TargetUpdate
from meho_backplane.targets.schemas import AuthModel as _AuthModelReexport

# ---------------------------------------------------------------------------
# Package-level import checks
# ---------------------------------------------------------------------------


def test_all_schemas_importable_from_package_root() -> None:
    """All four schemas and AuthModel re-export from ``meho_backplane.targets``."""
    from meho_backplane.targets import (  # noqa: F401
        Target,
        TargetCreate,
        TargetSummary,
        TargetUpdate,
    )


def test_auth_model_re_exported_from_schemas() -> None:
    """``AuthModel`` is importable from ``meho_backplane.targets.schemas``."""
    assert _AuthModelReexport is AuthModel


# ---------------------------------------------------------------------------
# Frozen / mutability
# ---------------------------------------------------------------------------


def test_target_is_frozen() -> None:
    t = Target(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="prod-k8s",
        aliases=[],
        product="kubernetes",
        host="10.0.0.1",
        port=None,
        fqdn=None,
        secret_ref=None,
        auth_model=AuthModel.SHARED_SERVICE_ACCOUNT,
        vpn_required=False,
        extras={},
        notes=None,
        fingerprint=None,
        preferred_impl_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        t.name = "mutated"  # type: ignore[misc]


def test_target_summary_is_frozen() -> None:
    s = _make_summary()
    with pytest.raises(ValidationError):
        s.name = "mutated"  # type: ignore[misc]


def _make_summary(**overrides: Any) -> TargetSummary:
    """Helper: construct a :class:`TargetSummary` with sensible defaults.

    G0.16-T6 Finding D (#1312) widened :class:`TargetSummary` to
    mirror the detail-endpoint shape per
    ``docs/codebase/api-shape-conventions.md`` §5; the helper carries
    a fully-populated default so each test names only the fields it
    asserts on.
    """
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "prod-k8s",
        "aliases": ["k8s"],
        "product": "kubernetes",
        "version": None,
        "host": "10.0.0.1",
        "port": None,
        "fqdn": None,
        "secret_ref": None,
        "auth_model": AuthModel.SHARED_SERVICE_ACCOUNT,
        "vpn_required": False,
        "verify_tls": True,
        "fingerprint": None,
        "preferred_impl_id": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "deleted_at": None,
    }
    defaults.update(overrides)
    return TargetSummary(**defaults)


# ---------------------------------------------------------------------------
# Round-trip: model_dump → model_validate
# ---------------------------------------------------------------------------


def _make_target(**overrides: Any) -> Target:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "rdc-vcenter",
        "aliases": ["vcenter", "vc.corp.internal"],
        "product": "vsphere",
        "host": "vcenter.corp.internal",
        "port": 443,
        "fqdn": "vcenter.corp.internal",
        "secret_ref": "secret/meho/vcenter",
        "auth_model": AuthModel.SHARED_SERVICE_ACCOUNT,
        "vpn_required": True,
        "verify_tls": False,
        "extras": {"datacenter": "fra1"},
        "notes": "Production vCenter",
        "fingerprint": None,
        "preferred_impl_id": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Target(**defaults)


def test_target_round_trip_lossless() -> None:
    """``model_dump()`` → ``model_validate()`` preserves every field."""
    t = _make_target()
    dumped = t.model_dump()
    restored = Target.model_validate(dumped)
    assert restored == t


def test_target_summary_round_trip_lossless() -> None:
    s = _make_summary(
        name="rdc-vcenter",
        aliases=["vcenter"],
        product="vsphere",
        host="vcenter.corp.internal",
    )
    assert TargetSummary.model_validate(s.model_dump()) == s


def test_target_summary_field_set_superset_of_target() -> None:
    """List-row field set ⊇ detail field set (G0.16-T6 Finding D #1312).

    Per ``docs/codebase/api-shape-conventions.md`` §5 the list
    endpoint must not silently mask fields the detail endpoint
    exposes. The two deliberately-omitted fields are ``notes`` and
    ``extras`` (operator-authored blobs that can carry meaningful
    payload but inflate the list page for the common
    "names + routing" question). Every other detail field must
    appear on :class:`TargetSummary`.
    """
    target_fields = set(Target.model_fields)
    summary_fields = set(TargetSummary.model_fields)
    deliberately_omitted = {"notes", "extras"}
    silently_masked = target_fields - summary_fields - deliberately_omitted
    assert silently_masked == set(), (
        f"TargetSummary silently masks {silently_masked!r} relative to "
        "Target; widen the projection or document the omission as a "
        "summary-shape decision per api-shape-conventions.md §5."
    )


def test_target_round_trip_with_empty_aliases() -> None:
    t = _make_target(aliases=[])
    assert Target.model_validate(t.model_dump()).aliases == ()


def test_target_round_trip_with_null_optional_fields() -> None:
    t = _make_target(port=None, fqdn=None, secret_ref=None, notes=None)
    restored = Target.model_validate(t.model_dump())
    assert restored.port is None
    assert restored.fqdn is None
    assert restored.notes is None


# ---------------------------------------------------------------------------
# TargetCreate validation
# ---------------------------------------------------------------------------


def test_target_create_rejects_empty_name() -> None:
    with pytest.raises(ValidationError) as exc_info:
        TargetCreate(name="", product="ssh", host="10.0.0.1")
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("name",) for e in errors)


def test_target_create_rejects_port_zero() -> None:
    with pytest.raises(ValidationError) as exc_info:
        TargetCreate(name="t", product="ssh", host="10.0.0.1", port=0)
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("port",) for e in errors)


def test_target_create_rejects_port_too_large() -> None:
    with pytest.raises(ValidationError) as exc_info:
        TargetCreate(name="t", product="ssh", host="10.0.0.1", port=70000)
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("port",) for e in errors)


def test_target_create_accepts_valid_port() -> None:
    t = TargetCreate(name="t", product="ssh", host="10.0.0.1", port=22)
    assert t.port == 22


def test_target_create_accepts_max_port() -> None:
    t = TargetCreate(name="t", product="ssh", host="10.0.0.1", port=65535)
    assert t.port == 65535


def test_target_create_defaults() -> None:
    t = TargetCreate(name="minimal", product="ssh", host="10.0.0.1")
    assert t.auth_model == AuthModel.SHARED_SERVICE_ACCOUNT
    assert t.vpn_required is False
    # T1 (#1780): default-secure -- an omitted ``verify_tls`` lands True.
    assert t.verify_tls is True
    assert t.extras == {}
    assert t.aliases == []
    assert t.port is None
    assert t.fqdn is None
    assert t.secret_ref is None
    assert t.notes is None


def test_target_create_accepts_verify_tls_false() -> None:
    """T1 (#1780): an operator can opt a fresh target out of TLS verify."""
    t = TargetCreate(name="lab", product="ssh", host="10.0.0.1", verify_tls=False)
    assert t.verify_tls is False


def test_target_create_all_fields() -> None:
    t = TargetCreate(
        name="rdc-vcenter",
        aliases=["vcenter", "vc"],
        product="vsphere",
        host="vcenter.corp.internal",
        port=443,
        fqdn="vcenter.corp.internal",
        secret_ref="secret/meho/vcenter",
        auth_model=AuthModel.IMPERSONATION,
        vpn_required=True,
        verify_tls=False,
        extras={"dc": "fra1"},
        notes="Production vCenter",
    )
    assert t.name == "rdc-vcenter"
    assert t.auth_model == AuthModel.IMPERSONATION
    assert t.vpn_required is True
    assert t.verify_tls is False


def test_target_create_rejects_invalid_auth_model() -> None:
    with pytest.raises(ValidationError):
        TargetCreate(
            name="t",
            product="ssh",
            host="10.0.0.1",
            auth_model="not_a_real_model",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# TargetUpdate validation
# ---------------------------------------------------------------------------


def test_target_update_all_none_is_valid() -> None:
    """Empty PATCH body (all None) is valid — no fields to update."""
    u = TargetUpdate()
    assert u.aliases is None
    assert u.host is None
    assert u.auth_model is None


def test_target_update_partial_fields() -> None:
    u = TargetUpdate(host="new-host.corp.internal", vpn_required=True)
    assert u.host == "new-host.corp.internal"
    assert u.vpn_required is True
    assert u.port is None  # untouched
    # T1 (#1780): ``verify_tls`` is None (absent-marker) when not sent.
    assert u.verify_tls is None


def test_target_update_verify_tls_patchable() -> None:
    """T1 (#1780): ``verify_tls`` is sendable via PATCH; absent stays None.

    The ``None`` default is the absent-marker the route handler uses to
    gate the audit fold-in (a PATCH that does not touch ``verify_tls``
    binds no TLS audit keys); an explicit ``False`` / ``True`` flips the
    column.
    """
    assert "verify_tls" in TargetUpdate.model_fields
    assert TargetUpdate(verify_tls=False).verify_tls is False
    assert TargetUpdate(verify_tls=True).verify_tls is True
    # exclude_unset distinguishes "sent" from "defaulted to None".
    assert "verify_tls" in TargetUpdate(verify_tls=False).model_dump(exclude_unset=True)
    assert "verify_tls" not in TargetUpdate(host="h").model_dump(exclude_unset=True)


def test_target_update_rejects_port_out_of_range() -> None:
    with pytest.raises(ValidationError):
        TargetUpdate(port=0)


def test_target_update_name_absent() -> None:
    """``name`` is not patchable — rename = delete + re-create."""
    assert "name" not in TargetUpdate.model_fields


def test_target_update_product_is_patchable() -> None:
    """``product`` is patchable as of G0.14-T4 #1145.

    The original G0.3 contract treated ``product`` as immutable; the
    v0.6.0 dogfood (signal 6) showed the combination of "no DELETE"
    + "no PATCH on product" left a misregistered target permanently
    broken. T4 #1145 adds ``product`` to ``TargetUpdate`` with route-
    handler validation against the registered connectors.
    """
    assert "product" in TargetUpdate.model_fields
    # Accepts a non-empty string.
    u = TargetUpdate(product="k8s")
    assert u.product == "k8s"


def test_target_update_rejects_empty_product() -> None:
    """``product`` must be at least one character (min_length=1)."""
    with pytest.raises(ValidationError):
        TargetUpdate(product="")


def test_target_full_schema_includes_deleted_at() -> None:
    """``Target.deleted_at`` is part of the read shape (G0.14-T4 #1145)."""
    assert "deleted_at" in Target.model_fields
    # Live targets have ``None``.
    now = datetime.now(UTC)
    t = Target(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="live",
        aliases=(),
        product="ssh",
        host="h",
        port=None,
        fqdn=None,
        secret_ref=None,
        auth_model=AuthModel.SHARED_SERVICE_ACCOUNT,
        vpn_required=False,
        extras={},
        notes=None,
        fingerprint=None,
        preferred_impl_id=None,
        created_at=now,
        updated_at=now,
    )
    assert t.deleted_at is None
    # The field round-trips with a real timestamp.
    deleted = Target(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="retired",
        aliases=(),
        product="ssh",
        host="h",
        port=None,
        fqdn=None,
        secret_ref=None,
        auth_model=AuthModel.SHARED_SERVICE_ACCOUNT,
        vpn_required=False,
        extras={},
        notes=None,
        fingerprint=None,
        preferred_impl_id=None,
        created_at=now,
        updated_at=now,
        deleted_at=now,
    )
    assert deleted.deleted_at == now
