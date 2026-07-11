# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Vault KV-v2 park-time ``proposed_effect`` preview builders.

G0.31 follow-up (#2332). The builders in
:mod:`meho_backplane.connectors.vault.kv_write_preview` give the approval
reviewer a redaction-safe view of the KV write a parked ``vault.kv.*``
would perform: the mount, the path, the KV version, the write semantics,
and the *names* of the keys being written — never their **values**.

Acceptance criteria (Issue #2332):

* Parking ``vault.kv.put`` surfaces the KV ``path`` and the set of
  ``key_names`` being written (names, not values), plus the mount, KV
  version, and put-vs-replace semantics.
* No secret **value** ever lands in the durable approval row.
* The parked-request envelope carries ``preview_populated`` (so a caller
  can refuse to auto-approve a blind, op-identity-only request) and, when
  a preview is intentionally sparse, a ``preview_reason``.

The builders are pure (they read only ``ctx.params`` — no connector I/O),
so these tests need no network mock. Importing the connector package wires
the builders via the ``register_preview_builder`` import side-effect, so
the tests exercise the real registration path through
:func:`build_proposed_effect`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

# Importing the package runs kv_write_preview's register_preview_builder
# calls (the import side-effect under test).
import meho_backplane.connectors.vault  # noqa: F401
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations._preview import (
    PREVIEW_REASON_CREDENTIAL_REDACTED,
    PREVIEW_REASON_NOT_POPULATED,
    PreviewContext,
    build_proposed_effect,
    describe_preview_provenance,
)


@dataclass
class _FakeDescriptor:
    """Minimal stand-in -- the hook only reads ``op_id``."""

    op_id: str


def _operator() -> Operator:
    return Operator(
        sub="op-vault-preview-test",
        name="Vault Preview Test Operator",
        email=None,
        raw_jwt="op.vault.preview.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000000cc"),
        tenant_role=TenantRole.OPERATOR,
    )


def _ctx(op_id: str, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=None,
        params=params,
        connector_id="vault-1.x",
    )


def _no_secret_anywhere(effect: dict[str, Any], *secrets: str) -> None:
    """Assert none of *secrets* appears anywhere in the serialised effect."""
    blob = json.dumps(effect)
    for secret in secrets:
        assert secret not in blob, f"secret value {secret!r} leaked into the durable row"


# ---------------------------------------------------------------------------
# vault.kv.put -- path + key names visible, values never
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_put_preview_shows_path_and_key_names_not_values() -> None:
    """The headline criterion: path + key names visible, secret values redacted-away."""
    ctx = _ctx(
        "vault.kv.put",
        {
            "mount": "secret",
            "path": "tenants/acme/db",
            "data": {
                "db_password": "super-secret-prod-pw",
                "db_user": "svc-acme",
            },
        },
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    # A bespoke builder runs even though vault.kv.put classifies as
    # credential_write -- the credential-class gate suppresses only the
    # generic echo, not a trusted bespoke builder (#2332).
    assert effect["op_class"] == "credential_write"
    preview = effect["preview"]
    assert preview["resource"] == "kv_secret"
    assert preview["mount"] == "secret"
    assert preview["path"] == "tenants/acme/db", "the KV path must be visible to the reviewer"
    assert preview["kv_version"] == 2
    assert preview["semantics"] == "replace", "put replaces the version wholesale"
    # Key NAMES are visible (sorted); the VALUES never are.
    assert preview["key_names"] == ["db_password", "db_user"]
    _no_secret_anywhere(effect, "super-secret-prod-pw", "svc-acme")


@pytest.mark.asyncio
async def test_kv_put_preview_defaults_mount_and_surfaces_cas() -> None:
    """Mount defaults to ``secret``; a CAS version guard is surfaced (not secret)."""
    ctx = _ctx(
        "vault.kv.put",
        {"path": "tenants/acme/api", "data": {"token": "t0p"}, "cas": 3},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["mount"] == "secret"
    assert preview["cas"] == 3
    assert preview["key_names"] == ["token"]
    _no_secret_anywhere(effect, "t0p")


# ---------------------------------------------------------------------------
# vault.kv.patch -- merge semantics, key names visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_patch_preview_shows_merge_semantics_and_key_names() -> None:
    ctx = _ctx(
        "vault.kv.patch",
        {"path": "tenants/acme/db", "data": {"db_password": "rotated-pw"}},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["op_class"] == "credential_write"
    preview = effect["preview"]
    assert preview["semantics"] == "merge", "patch merges onto the current version"
    assert preview["path"] == "tenants/acme/db"
    assert preview["key_names"] == ["db_password"]
    _no_secret_anywhere(effect, "rotated-pw")


# ---------------------------------------------------------------------------
# vault.kv.delete -- version soft-delete, no data param at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_delete_preview_shows_versions_and_soft_delete_semantics() -> None:
    ctx = _ctx(
        "vault.kv.delete",
        {"mount": "kv-prod", "path": "tenants/acme/db", "versions": [2, 3]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["resource"] == "kv_secret"
    assert preview["mount"] == "kv-prod"
    assert preview["path"] == "tenants/acme/db"
    assert preview["semantics"] == "soft_delete"
    assert preview["versions"] == [2, 3]


@pytest.mark.asyncio
async def test_kv_put_preview_tolerates_malformed_data_param() -> None:
    """A non-dict ``data`` previews no keys rather than raising (fail-soft)."""
    ctx = _ctx("vault.kv.put", {"path": "tenants/acme/db", "data": "not-a-dict"})
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview"]["key_names"] == []


# ---------------------------------------------------------------------------
# describe_preview_provenance -- the reviewer-facing provenance fields (#2332)
# ---------------------------------------------------------------------------


def test_provenance_populated_bespoke_preview() -> None:
    populated, reason = describe_preview_provenance(
        {"op_class": "credential_write", "preview": {"path": "x"}}, op_id="vault.kv.put"
    )
    assert populated is True
    assert reason is None


def test_provenance_populated_generic_echo() -> None:
    populated, reason = describe_preview_provenance(
        {"op_class": "write", "params_echo": {"name": "x"}}, op_id="vsphere.vm.create"
    )
    assert populated is True
    assert reason is None


def test_provenance_credential_class_none_is_redacted_reason() -> None:
    """A credential-class op that collapsed to identifier-only ⇒ redacted reason.

    ``vault.auth.userpass.write`` is a credential_write op with no bespoke
    builder, so its preview is intentionally suppressed. The provenance
    must name that as a deliberate redaction, not a missing preview.
    """
    populated, reason = describe_preview_provenance(None, op_id="vault.auth.userpass.write")
    assert populated is False
    assert reason == PREVIEW_REASON_CREDENTIAL_REDACTED


def test_provenance_plain_op_none_is_not_populated_reason() -> None:
    populated, reason = describe_preview_provenance(None, op_id="vsphere.vm.create")
    assert populated is False
    assert reason == PREVIEW_REASON_NOT_POPULATED


def test_provenance_builder_fault_is_unpopulated_without_reason() -> None:
    """A builder that faulted carries its own marker; not a sparse-preview reason."""
    populated, reason = describe_preview_provenance(
        {"op_class": "other", "preview_unavailable": True, "preview_error": "boom"},
        op_id="k8s.apply",
    )
    assert populated is False
    assert reason is None


# ---------------------------------------------------------------------------
# Dispatcher envelope stamping -- preview_populated / preview_reason land on
# the parked-request proposed_effect (#2332)
# ---------------------------------------------------------------------------


@dataclass
class _FakeStampDescriptor:
    """Descriptor stand-in for the dispatcher stamping path (op_id + severity)."""

    op_id: str
    safety_level: str


@pytest.mark.asyncio
async def test_dispatcher_stamps_preview_populated_for_vault_put(monkeypatch) -> None:
    """A parked ``vault.kv.put`` envelope carries the populated preview + flag."""
    from meho_backplane.operations import dispatcher

    async def _no_connector(descriptor: Any, target: Any) -> tuple[Any, Any, Any]:
        return None, None, None

    async def _no_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(dispatcher, "_resolve_connector_instance", _no_connector)
    monkeypatch.setattr(dispatcher, "build_permission_preflight", _no_preflight)

    effect = await dispatcher._build_proposed_effect(
        op_id="vault.kv.put",
        connector_id="vault-1.x",
        descriptor=_FakeStampDescriptor(op_id="vault.kv.put", safety_level="caution"),  # type: ignore[arg-type]
        operator=_operator(),
        target=None,
        params={"path": "tenants/acme/db", "data": {"db_password": "x"}},
    )
    assert effect is not None
    assert effect["preview_populated"] is True
    assert "preview_reason" not in effect
    assert effect["preview"]["path"] == "tenants/acme/db"
    assert effect["safety_level"] == "caution"
    _no_secret_anywhere(effect, "x")


@pytest.mark.asyncio
async def test_dispatcher_stamps_redacted_reason_for_builderless_credential_op(
    monkeypatch,
) -> None:
    """A credential op with no builder collapses to identifier-only + redacted reason."""
    from meho_backplane.operations import dispatcher

    async def _no_connector(descriptor: Any, target: Any) -> tuple[Any, Any, Any]:
        return None, None, None

    async def _no_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr(dispatcher, "_resolve_connector_instance", _no_connector)
    monkeypatch.setattr(dispatcher, "build_permission_preflight", _no_preflight)

    effect = await dispatcher._build_proposed_effect(
        op_id="vault.auth.userpass.write",
        connector_id="vault-1.x",
        descriptor=_FakeStampDescriptor(
            op_id="vault.auth.userpass.write", safety_level="dangerous"
        ),  # type: ignore[arg-type]
        operator=_operator(),
        target=None,
        params={"username": "svc", "password": "sekret"},
    )
    assert effect is not None
    assert effect["preview_populated"] is False
    assert effect["preview_reason"] == PREVIEW_REASON_CREDENTIAL_REDACTED
    # The identifier-only default still names the op for the reviewer.
    assert effect["op_id"] == "vault.auth.userpass.write"
    _no_secret_anywhere(effect, "sekret")
