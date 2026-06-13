# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant Vault KV path convention + a runbook-driven relocation helper (#1723).

Goal #221 / Initiative #1685. The shipped deploy convention scoped each
target's secret per operator ``sub`` (``secret/data/targets/<sub>/*``,
``docs/cross-repo/connector-vault-policy.md`` §2). That duplicated a
target's credential per operator and blocked two operators in one tenant
from sharing it; it also meant the #1643 tenant-scope guard
(:mod:`meho_backplane.connectors.vault.tenant_scope`) had no universal
``{tenant_id}`` partition to enforce against, so it had to ship default-off.

This module moves the canonical layout to a **per-tenant shared** path:

.. code-block:: text

    secret/data/tenants/<tenant_id>/<target>

so a target's credential lives once per tenant and every operator in that
tenant reads the same path. ``<tenant_id>`` is the canonical dashed
lowercase UUID (``str(UUID)``) — the exact rendering
:func:`~meho_backplane.connectors.vault.tenant_scope.rendered_tenant_prefix`
produces. The #1643 guard enforces this layout by default (#1725) with the
mount-pinned ``secret/tenants/{tenant_id}/`` prefix (the guard matches a
``<mount>/<path>`` candidate and these secrets sit on the default ``secret``
mount); a deploy mid-migration disables it with
``VAULT_KV_TENANT_SCOPE_PREFIX=""`` until its secrets are relocated.

Two public helpers:

* :func:`tenant_secret_ref` — a pure function deriving the logical
  ``secret_ref`` (the path relative to the KV-v2 mount, the shape
  ``targets.secret_ref`` carries) for a target from its tenant + name.
  Called at target create/update so freshly-created targets default onto
  the per-tenant path instead of a per-``sub`` path the operator would
  otherwise have to type by hand.
* :func:`relocate_target_secret` — an operator-driven coroutine that moves
  one existing ``targets/<sub>/<x>`` secret to ``tenants/<id>/<x>`` via the
  existing ``vault_kv_*`` handlers (read → write → optional delete) and
  returns the rewritten ``secret_ref``. It is **not** wired into any app
  request path: the migration is operator-run from the runbook
  (``docs/cross-repo/vault-per-tenant-migration.md``); RDC owns the Vault
  deployment, so the backplane never auto-relocates secrets.

Both stay clear of the system/shim operator: the Nil-UUID tenant exemption
that :mod:`meho_backplane.connectors.vault.tenant_scope` preserves applies
to the *guard*; these helpers only ever run for a real authenticated tenant
admin (target CRUD) or under an operator the runbook supplies explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from meho_backplane.connectors.vault.ops import (
    vault_kv_delete,
    vault_kv_put,
    vault_kv_read,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = ["TENANT_SECRET_PREFIX", "relocate_target_secret", "tenant_secret_ref"]

#: The logical-path prefix (relative to the KV-v2 mount) under which a
#: tenant's target secrets live. Combined with the dashed-UUID tenant id
#: and the target identity this yields ``tenants/<tenant_id>/<target>``;
#: hvac inserts the mount and the ``/data/`` API segment itself, so the
#: full KV-v2 wire path becomes ``secret/data/tenants/<tenant_id>/<target>``.
#: The #1643 guard enforces this layout **by default** (#1725) via the
#: mount-pinned ``VAULT_KV_TENANT_SCOPE_PREFIX="secret/tenants/{tenant_id}/"``
#: — the mount segment is required because the guard matches a
#: ``<mount>/<path>`` candidate and these secrets sit on the default
#: ``secret`` mount.
TENANT_SECRET_PREFIX = "tenants"


def tenant_secret_ref(tenant_id: UUID, target: str) -> str:
    """Derive the per-tenant logical ``secret_ref`` for *target*.

    Returns ``"tenants/<tenant_id>/<target>"`` — the logical KV-v2 path
    relative to the mount, which is the shape ``targets.secret_ref``
    stores (no mount segment, no ``/data/`` API segment; hvac inserts
    both at read time, see
    :func:`meho_backplane.connectors._shared.vault_creds._is_api_path_shaped`).

    ``tenant_id`` is rendered in its canonical dashed lowercase form
    (``str(UUID)``), matching how tenant UUIDs appear everywhere else —
    audit rows, JWT claims, and
    :func:`~meho_backplane.connectors.vault.tenant_scope.rendered_tenant_prefix`'s
    output — so the default-on guard
    (``secret/tenants/{tenant_id}/``) finds the derived ref already inside
    the rendered prefix.

    Parameters
    ----------
    tenant_id
        The owning tenant's UUID (``operator.tenant_id`` at the CRUD
        boundary). Always a real tenant here; the Nil-UUID system shim
        never reaches target CRUD.
    target
        The target's stable identity — its ``name``. Stripped of stray
        surrounding slashes/whitespace so the joined path has exactly one
        separator between segments. Must be non-empty after stripping.

    Raises
    ------
    ValueError
        *target* is empty or whitespace/slash-only after stripping —
        there is no meaningful leaf to key the secret under.
    """
    leaf = target.strip().strip("/").strip()
    if not leaf:
        raise ValueError("cannot derive a per-tenant secret_ref for an empty target identity")
    return f"{TENANT_SECRET_PREFIX}/{tenant_id}/{leaf}"


async def relocate_target_secret(
    operator: Operator,
    *,
    old_ref: str,
    target: str,
    mount: str = "secret",
    delete_old: bool = False,
) -> str:
    """Move one ``targets/<sub>/<x>`` secret to its per-tenant home and return the new ref.

    Runbook-driven (``docs/cross-repo/vault-per-tenant-migration.md``), not
    an app request path. Performs the KV-v2 read → write sequence via the
    existing typed-op handlers so the move runs under *operator*'s OIDC
    identity exactly as a real ``vault.kv.*`` call would, and the Vault
    audit log attributes both legs to that operator:

    1. **read** the secret at *old_ref* (``vault.kv.read``) — the full
       key/value payload of the latest version.
    2. **write** that payload to the derived per-tenant path
       (:func:`tenant_secret_ref`) as a new version (``vault.kv.put``).
    3. optionally **soft-delete** the latest version at *old_ref*
       (``vault.kv.delete``) when *delete_old* is set — off by default so a
       dry run leaves the source untouched and the operator deletes only
       after verifying the new ref resolves.

    Returns the rewritten logical ``secret_ref`` the operator then writes
    back onto the target row (``PATCH /api/v1/targets/{name}`` with
    ``{"secret_ref": <returned value>}``).

    Parameters
    ----------
    operator
        The authenticated operator the migration runs as. Its
        ``tenant_id`` keys the destination path; its ``raw_jwt`` is
        forwarded to Vault by the underlying handlers.
    old_ref
        The current logical KV-v2 path of the secret (e.g.
        ``targets/<sub>/<target>``). No mount, no ``/data/`` prefix —
        the same shape ``secret_ref`` carries.
    target
        The target's identity (``name``) used to derive the per-tenant
        leaf via :func:`tenant_secret_ref`.
    mount
        The KV-v2 mount both legs address. Defaults to ``"secret"``,
        matching the handlers' default mount.
    delete_old
        When ``True``, soft-delete the latest version at *old_ref* after
        the write succeeds. Default ``False`` (read+write only) so the
        operator verifies the relocation before retiring the source.

    Returns
    -------
    str
        The new per-tenant ``secret_ref`` (``tenants/<tenant_id>/<target>``).
    """
    new_ref = tenant_secret_ref(operator.tenant_id, target)

    read_result = await vault_kv_read(operator, None, {"mount": mount, "path": old_ref})
    secret_data: dict[str, Any] = read_result["data"]

    await vault_kv_put(
        operator,
        None,
        {"mount": mount, "path": new_ref, "data": secret_data},
    )

    if delete_old:
        # Soft-delete the latest version only — reversible via Vault's
        # undelete path, so a misverified relocation can be rolled back.
        await vault_kv_delete(
            operator,
            None,
            {"mount": mount, "path": old_ref, "versions": [_latest_version(read_result)]},
        )

    return new_ref


def _latest_version(read_result: dict[str, Any]) -> int:
    """Return the version number from a ``vault.kv.read`` result, defaulting to 1.

    ``vault_kv_read`` returns ``{"data": ..., "version": <int|None>}``.
    A ``None`` version (metadata lacked the key) falls back to ``1`` —
    the only version a freshly-written per-``sub`` secret can have — so
    the soft-delete still names a concrete version rather than failing.
    """
    version = read_result.get("version")
    return int(version) if isinstance(version, int) else 1
