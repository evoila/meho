# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Application-layer tenant-scope guard for the agent-supplied ``vault.kv.*`` ops.

#1643 (cross-tenant isolation hardening, Goal #221). The KV-v2 handlers in
:mod:`meho_backplane.connectors.vault.ops` forward the agent-supplied
``mount`` / ``path`` to hvac **verbatim**. The intended access boundary is
the Vault ``meho-mcp`` role's ACL policy (per-operator templating,
``docs/cross-repo/connector-vault-policy.md`` §2). If that policy is
mis-provisioned *too broadly* — e.g. the templated identity segment is
dropped, or a wildcard leaks into the rendered output — a tenant-A caller
could read ``tenant-b/...`` secrets. This module adds a **defense-in-depth**
check *in front of* the hvac call so a broken Vault policy is not the only
thing standing between two tenants.

It is deliberately *additive*: the Vault policy stays the primary gate
(this guard never grants access the policy denies — it only ever denies
earlier). See ``docs/codebase/connectors-vault-tenant-scope.md`` for the
namespace convention, the rationale, and the failure mode.

Default-on as of #1725
----------------------
The canonical KV layout is now per-tenant
(``secret/data/tenants/<tenant_id>/<target>``, #1723), so the guard ships
**enforcing** by default. The guard is controlled by
:attr:`~meho_backplane.settings.Settings.vault_kv_tenant_scope_prefix`:

* **the mount-pinned default** ``"secret/tenants/{tenant_id}/"`` → the
  requested logical path must begin with the rendered prefix, or
  :class:`VaultTenantScopeError` is raised before any Vault round-trip. The
  mount segment is part of the default because the match candidate is the
  normalised ``<mount>/<path>`` (see :func:`_normalised_logical_path`) and
  the KV-v2 handlers default ``mount="secret"`` — a *path-only*
  ``"tenants/{tenant_id}/"`` would render to ``tenants/<id>/`` and never
  match the ``secret/tenants/<id>/...`` candidate, denying every legitimate
  per-tenant call.
* **empty** (``VAULT_KV_TENANT_SCOPE_PREFIX=""``) → :func:`enforce_tenant_scope`
  is a no-op; behaviour is byte-for-byte what it was before #1643. A deploy
  still mid-migration (secrets under the retired per-``sub`` layout) opts out
  this way until its secrets are relocated.

The system/shim operator (Nil-UUID tenant, empty ``raw_jwt``) is exempt:
its only callers exercise the unauthenticated ``vault.sys.health`` op and
forward no token to Vault, so there is no tenant identity to bind against.

A small set of **platform paths** is also exempt regardless of operator —
fixed, shared, non-tenant secrets the platform itself reads under any
authenticated operator's identity. The only such path today is the
federation-proof health-check secret (``secret/meho/test/federation``,
read by ``GET /api/v1/health`` to prove the JWT→OIDC→Vault chain). It is
provisioned shared per the Goal #11 cross-repo contract and carries no
tenant data, so the per-tenant guard must not deny it once enforced by
default (#1725). The exemption is deliberately a closed allow-list, not a
pattern, so it cannot be widened by a caller-supplied path.
"""

from __future__ import annotations

from uuid import UUID

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings

__all__ = [
    "PLATFORM_EXEMPT_PATHS",
    "VaultTenantScopeError",
    "enforce_tenant_scope",
    "rendered_tenant_prefix",
]

#: The Nil UUID the vault-connector shim synthesises as the system
#: operator's ``tenant_id`` (mirrors
#: :data:`meho_backplane.connectors.vault.connector._SYSTEM_TENANT_ID`).
#: A caller carrying this tenant has no real tenant identity, so the guard
#: is skipped for it. Duplicated here rather than imported to avoid a
#: connector ↔ ops import cycle; a regression test pins the two equal.
_SYSTEM_TENANT_ID: UUID = UUID(int=0)

#: Closed allow-list of normalised ``<mount>/<path>`` candidates the guard
#: never denies, regardless of operator tenant. These are fixed, shared,
#: non-tenant platform secrets the backplane reads on its own behalf under
#: the request operator's identity (the audit trail keeps the real
#: operator). Currently only the federation-proof health secret
#: ``GET /api/v1/health`` reads. Kept as an explicit set — never a prefix or
#: glob — so a caller cannot smuggle a tenant escape through it. A
#: regression test pins this equal to the health route's
#: ``_FEDERATION_PROOF_PATH`` rendered onto the default ``secret`` mount.
PLATFORM_EXEMPT_PATHS: frozenset[str] = frozenset({"secret/meho/test/federation"})


class VaultTenantScopeError(Exception):
    """A ``vault.kv.*`` call requested a path outside the operator's tenant namespace.

    Raised by :func:`enforce_tenant_scope` **before** the hvac call when
    :attr:`~meho_backplane.settings.Settings.vault_kv_tenant_scope_prefix`
    is configured and the requested logical path does not begin with the
    operator's rendered tenant prefix. The dispatcher's ``connector_error``
    branch wraps the raised exception into a structured
    :class:`~meho_backplane.connectors.schemas.OperationResult` with
    ``extras["exception_class"] == "VaultTenantScopeError"`` — distinct
    from the ``VaultClientError`` family (login-phase) and from generic
    read-phase errors, so a caller can tell a *local policy denial* apart
    from a Vault-side 403.

    The message names the rendered prefix but **never** echoes secret
    material — only the (non-secret) mount/path the caller already supplied.
    """


def rendered_tenant_prefix(operator: Operator, *, template: str) -> str:
    """Render the per-tenant logical-path prefix for *operator*.

    *template* is a ``str.format`` string carrying a single ``{tenant_id}``
    placeholder (validated non-empty by the caller). The operator's
    ``tenant_id`` UUID is rendered in its canonical dashed lowercase form
    (``str(UUID)``), matching how tenant UUIDs appear everywhere else in
    the codebase (audit rows, JWT claims).
    """
    return template.format(tenant_id=operator.tenant_id)


def _normalised_logical_path(mount: str, path: str) -> str:
    """Build the ``<mount>/<path>`` string the guard matches the prefix against.

    The KV handlers split the address into hvac's ``(mount_point, path)``
    pair; we recombine them into one logical string so a tenant prefix can
    constrain either the mount segment or the path segment (a deploy may
    partition tenants by mount *or* by a path prefix). Leading/trailing
    slashes are normalised away so ``"tenant-x/"`` matches ``"tenant-x/a"``
    regardless of stray slashes the caller typed.
    """
    clean_mount = mount.strip().strip("/")
    clean_path = path.strip().strip("/")
    return f"{clean_mount}/{clean_path}"


def enforce_tenant_scope(operator: Operator, *, mount: str, path: str) -> None:
    """Deny a ``vault.kv.*`` call whose path escapes the operator's tenant namespace.

    Enforced by default
    (``vault_kv_tenant_scope_prefix="secret/tenants/{tenant_id}/"``). No-op
    only when the prefix is explicitly emptied
    (``VAULT_KV_TENANT_SCOPE_PREFIX=""``), when *operator* is the Nil-tenant
    system shim, or when the normalised ``<mount>/<path>`` is one of the
    fixed :data:`PLATFORM_EXEMPT_PATHS` (shared non-tenant platform secrets,
    e.g. the federation-proof health read). Otherwise renders the operator's
    tenant prefix from the template and raises :class:`VaultTenantScopeError`
    unless the normalised ``<mount>/<path>`` begins with it.

    Parameters
    ----------
    operator
        The request-scoped operator. ``operator.tenant_id`` supplies the
        binding identity; it is already populated on every authenticated
        handler call (the dispatcher threads the real
        :class:`~meho_backplane.auth.operator.Operator`).
    mount, path
        The mount handle and logical path the handler is about to forward
        to hvac, already ``.strip()``-ed by the handler.

    Raises
    ------
    VaultTenantScopeError
        The requested path falls outside the operator's tenant prefix.
    """
    template = get_settings().vault_kv_tenant_scope_prefix.strip()
    if not template:
        # Guard disabled — preserve pre-#1643 behaviour exactly.
        return
    if operator.tenant_id == _SYSTEM_TENANT_ID:
        # System/shim operator: no real tenant to bind against; its only
        # callers run the unauthenticated sys.health op.
        return

    candidate = _normalised_logical_path(mount, path)
    if candidate in PLATFORM_EXEMPT_PATHS:
        # A fixed, shared, non-tenant platform secret (e.g. the
        # federation-proof health read) — exempt by closed allow-list so
        # default-on enforcement does not deny the platform's own probes.
        return

    prefix = rendered_tenant_prefix(operator, template=template)
    normalised_prefix = prefix.strip().strip("/")

    # Match on a path-segment boundary so a "tenant-1" prefix cannot be
    # satisfied by a "tenant-12/..." path. The candidate is normalised to
    # "<mount>/<path>"; an in-namespace request is the prefix itself or the
    # prefix followed by "/".
    if candidate == normalised_prefix or candidate.startswith(normalised_prefix + "/"):
        return

    raise VaultTenantScopeError(
        "vault.kv request outside tenant namespace: "
        f"requested {candidate!r} is not within tenant prefix "
        f"{normalised_prefix!r} (tenant_id={operator.tenant_id})"
    )
