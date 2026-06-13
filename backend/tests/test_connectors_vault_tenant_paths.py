# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant Vault KV path convention + relocation helper (#1723).

Covers the two public helpers in
:mod:`meho_backplane.connectors.vault.tenant_paths`:

* :func:`tenant_secret_ref` — pure derivation of the canonical
  ``tenants/<tenant_id>/<target>`` logical ``secret_ref``.
* :func:`relocate_target_secret` — runbook-driven read→write(→delete)
  move of an existing per-``sub`` secret to its per-tenant home, run
  through the real ``vault_kv_*`` handlers against the shared in-process
  Vault fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vault.tenant_paths import (
    TENANT_SECRET_PREFIX,
    relocate_target_secret,
    tenant_secret_ref,
)
from meho_backplane.connectors.vault.tenant_scope import rendered_tenant_prefix
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_TENANT = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    # The relocation helper reads the *retired* per-`sub` path
    # (``targets/<sub>/...``) to move it to the per-tenant home. That source
    # path is outside the now-default-on tenant-scope guard
    # (``secret/tenants/{tenant_id}/``, #1725), so the migration runs with
    # the guard explicitly disabled — exactly as the operator runbook
    # (``vault-per-tenant-migration.md``) prescribes for the migration window.
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-1",
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


# ---------------------------------------------------------------------------
# tenant_secret_ref — pure derivation
# ---------------------------------------------------------------------------


def test_tenant_secret_ref_canonical_shape() -> None:
    """The ref is ``tenants/<dashed-uuid>/<target>`` — no mount, no /data/."""
    assert tenant_secret_ref(_TENANT, "rdc-vcenter") == (
        f"{TENANT_SECRET_PREFIX}/{_TENANT}/rdc-vcenter"
    )


def test_tenant_secret_ref_uses_canonical_dashed_uuid() -> None:
    """The tenant segment matches the guard's rendered prefix exactly."""
    op = _operator()
    ref = tenant_secret_ref(op.tenant_id, "x")
    # The mount-less ``secret_ref`` carries only the path portion; the
    # default-on guard pins the mount too (``secret/tenants/{tenant_id}/``),
    # so the path-relative prefix here is the path tail of that template.
    prefix = rendered_tenant_prefix(op, template="tenants/{tenant_id}/")
    # The derived ref sits inside the path tail of the prefix the guard renders.
    assert ref.startswith(prefix)


def test_tenant_secret_ref_strips_stray_separators() -> None:
    """Stray surrounding slashes/whitespace on the target collapse cleanly."""
    assert tenant_secret_ref(_TENANT, "  /vc/  ") == f"tenants/{_TENANT}/vc"


def test_tenant_secret_ref_rejects_empty_target() -> None:
    """An empty / slash-only target has no leaf to key the secret under."""
    with pytest.raises(ValueError, match="empty target identity"):
        tenant_secret_ref(_TENANT, "  //  ")


# ---------------------------------------------------------------------------
# relocate_target_secret — read → write (→ delete) through the real handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relocate_reads_old_writes_per_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads ``targets/<sub>/x`` and writes it to ``tenants/<id>/x``; returns the new ref."""
    fake = install_fake_client(monkeypatch, secret={"username": "demo", "password": "s3cr3t"})
    op = _operator()

    new_ref = await relocate_target_secret(op, old_ref="targets/op-1/vc", target="vc")

    assert new_ref == f"tenants/{_TENANT}/vc"
    # The read leg hit the old per-``sub`` path.
    assert fake.secrets.kv.v2.read_calls[0]["path"] == "targets/op-1/vc"
    # The write leg landed the same payload at the per-tenant path.
    put = fake.secrets.kv.v2.put_calls[0]
    assert put["path"] == new_ref
    assert put["secret"] == {"username": "demo", "password": "s3cr3t"}
    # Read-only by default — the source is left intact.
    assert fake.secrets.kv.v2.delete_calls == []


@pytest.mark.asyncio
async def test_relocate_new_ref_resolves_through_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """After relocation the rewritten secret_ref resolves through vault_kv_read."""
    from meho_backplane.connectors.vault.ops import vault_kv_read

    fake = install_fake_client(monkeypatch, secret={"kubeconfig": "yaml"})
    op = _operator()

    new_ref = await relocate_target_secret(op, old_ref="targets/op-1/k8s", target="k8s")

    read_back = await vault_kv_read(op, None, {"path": new_ref})
    assert read_back["data"] == {"kubeconfig": "yaml"}
    # The read-back addressed the new per-tenant path.
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == new_ref


@pytest.mark.asyncio
async def test_relocate_soft_deletes_source_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delete_old=True`` soft-deletes the read version at the old path."""
    fake = install_fake_client(monkeypatch, secret={"username": "demo"}, kv_version=4)
    op = _operator()

    await relocate_target_secret(op, old_ref="targets/op-1/vc", target="vc", delete_old=True)

    assert len(fake.secrets.kv.v2.delete_calls) == 1
    deleted = fake.secrets.kv.v2.delete_calls[0]
    assert deleted["path"] == "targets/op-1/vc"
    # The version named is the one the read returned (no destroy, reversible).
    assert deleted["versions"] == [4]
