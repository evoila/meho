# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Autonomous-agent credential resolution for the G11.3 scheduler (#823).

The scheduler fires agent runs via
:meth:`~meho_backplane.agent.invocation.AgentInvoker.run_scheduled`
(G11.2-T2 #1096) which expects ``(agent_client_id, agent_client_secret)``
for the Keycloak ``client_credentials`` grant. The scheduler is
operator-less (no JWT to hand to
:func:`~meho_backplane.auth.vault.vault_client_for_operator`), so it
sources the secret under its own **static service token** rather than a
per-operator Keycloak JWT.

Resolution order (G0.19-T2 #1478)
---------------------------------

:func:`resolve_agent_credentials` is **Vault-first**:

1. **Vault** — read the agent's secret from
   :attr:`Settings.scheduler_agent_vault_path_pattern` under
   :attr:`Settings.vault_scheduler_token`
   (:func:`meho_backplane.scheduler.vault_credentials.read_agent_secret`).
   This is the path registration writes to
   (:meth:`~meho_backplane.auth.agent_principals.AgentPrincipalService.register`),
   so an agent registered + defined purely over the API is schedulable
   with **no pod env var and no redeploy**.
2. **Env var (fallback / break-glass)** — when Vault yields nothing (not
   configured, secret absent), read the secret from the env var derived
   from :attr:`Settings.scheduler_agent_secret_env_pattern`. Operators
   wire agent secrets into the pod the same way ``ANTHROPIC_API_KEY`` is
   wired when Vault is unavailable.

When **neither** source yields a secret, the resolver raises
:class:`AgentCredentialsUnresolvedError` (loud, trigger-preserving — the
loop logs ``scheduler_credentials_unresolved`` and leaves the trigger
``active`` for the next tick).

Per-principal, per-tenant key derivation (security S10, #298)
-------------------------------------------------------------

Both the Vault path and the env-var name are derived from **two** inputs
— the agent's ``identity_ref`` *and* the owning ``tenant_id`` — so that
two distinct principals can never resolve to the same storage key:

* ``AgentDefinition.identity_ref`` is the Keycloak client-id reference
  set at definition-create time, in the form ``agent:<name>`` /
  ``runner:<name>``. Its ``{client_id}`` segment is derived by
  :func:`agent_client_id_from_identity_ref`, a **reversible, injective**
  hex encoding of the UTF-8 bytes — *not* a lossy sanitiser. The old
  ``[^A-Za-z0-9_]`` -> ``_`` collapse mapped ``agent:a-b``, ``agent:a_b``
  and ``agent:a.b`` onto one key (``AGENT_A_B``); the hex encoding keeps
  them distinct (``6167656e743a612d62`` etc.), and because hex is a
  byte-level encoding it survives the trailing ``upper()`` without
  re-colliding case variants (``agent:a-b`` vs ``agent:A-B``).
* ``{tenant_id}`` is the owning tenant's UUID rendered as ``.hex``. The
  scheduler / event-matcher / checks-investigator read under the
  **trigger's** tenant, which is the tenant the principal was registered
  under, so read and write derive the same key. A principal registered
  in a different tenant resolves to a different key — the tenant-isolation
  invariant this filing exists to enforce.

The env-var name applies a final ``upper()`` across the whole substituted
pattern; the hex ``{client_id}`` and hex ``{tenant_id}`` are both
``upper()``-stable, so the uppercase env name and the (mixed-case) Vault
path address the same logical principal.

:func:`agent_client_id_from_identity_ref` is deterministic + pure (no
side effects, no I/O) so the key derivation can be unit-tested without
fixtures. :func:`resolve_agent_credentials` performs a Vault read (I/O)
before falling back to :func:`os.environ`.
"""

from __future__ import annotations

import os
import uuid

import structlog

from meho_backplane.settings import get_settings

_log = structlog.get_logger(__name__)

__all__ = [
    "AgentCredentialsUnresolvedError",
    "agent_client_id_from_identity_ref",
    "resolve_agent_credentials",
    "tenant_key_segment",
]


class AgentCredentialsUnresolvedError(RuntimeError):
    """The scheduler could not source credentials for a scheduled fire.

    Raised when neither the Vault path nor the env-var fallback the
    pattern resolves to yields a secret. The scheduler loop catches this,
    logs + audits the skip, and leaves the trigger ``active`` so an
    operator who wires the secret unblocks the schedule on the next tick.
    """


def agent_client_id_from_identity_ref(identity_ref: str) -> str:
    """Return the storage-key ``{client_id}`` segment for *identity_ref*.

    A **reversible, injective** hex encoding of the ``identity_ref``'s
    UTF-8 bytes (``bytes.fromhex(result).decode("utf-8")`` recovers the
    original). Two distinct identity refs always produce two distinct
    segments — the property the per-principal Vault path and env-var name
    depend on (security S10, #298).

    This replaces the pre-#298 lossy sanitiser that collapsed every
    non-``[A-Za-z0-9_]`` character to ``_``: that mapped ``agent:a-b``,
    ``agent:a_b`` and ``agent:a.b`` onto the same key, so a second
    registration silently clobbered the first's stored secret. Hex is a
    byte-level encoding, so it also survives the callers' trailing
    ``upper()`` without re-colliding case variants (``agent:a-b`` vs
    ``agent:A-B``) — every byte difference is preserved as a distinct
    hex digit pair, and ``upper()`` only lifts ``a``-``f`` to ``A``-``F``
    uniformly.

    Pure / deterministic — no I/O, no settings read.
    """
    return identity_ref.encode("utf-8").hex()


def tenant_key_segment(tenant_id: uuid.UUID) -> str:
    """Return the storage-key ``{tenant_id}`` segment for *tenant_id*.

    The UUID's 32-hex-digit ``.hex`` form: env-var-legal (no hyphens) and
    Vault-path-legal, and — being fixed-width with no ``_`` — unambiguous
    when concatenated with the ``{client_id}`` segment under a ``_``
    separator. Pure / deterministic.
    """
    return tenant_id.hex


def _env_var_name_for(identity_ref: str, tenant_id: uuid.UUID) -> str:
    """Return the env-var name the secret pattern resolves to.

    Substitutes the per-tenant ``{tenant_id}`` segment and the reversible
    ``{client_id}`` segment into
    :attr:`Settings.scheduler_agent_secret_env_pattern`, then upper-cases
    the whole result. Operators who set a non-upper-cased pattern (e.g.
    ``meho_agent_secret_{tenant_id}_{client_id}``) otherwise resolve to a
    mixed-case env-var name that Linux's case-sensitive lookup would miss
    — the precondition gate would then skip every fire with a
    ``credentials_unresolved`` warning pointing at the secret rather than
    the case-mismatch. The contract is "the whole substituted name is
    upper-cased"; this makes the code match it. Both hex segments are
    ``upper()``-stable, so the uppercase env name still names a distinct
    key per principal.
    """
    settings = get_settings()
    client_seg = agent_client_id_from_identity_ref(identity_ref)
    return settings.scheduler_agent_secret_env_pattern.format(
        tenant_id=tenant_key_segment(tenant_id),
        client_id=client_seg,
    ).upper()


def _secret_from_env(identity_ref: str, tenant_id: uuid.UUID) -> str:
    """Return the agent secret from the env var, or ``""`` when unset/empty."""
    return os.environ.get(_env_var_name_for(identity_ref, tenant_id), "").strip()


async def resolve_agent_credentials(identity_ref: str, *, tenant_id: uuid.UUID) -> tuple[str, str]:
    """Resolve ``(client_id, client_secret)`` for a scheduled-fire agent.

    *identity_ref* is :attr:`AgentDefinition.identity_ref` (the Keycloak
    client-id reference set at definition-create time). *tenant_id* is the
    owning tenant of the firing trigger / dashboard — the same tenant the
    principal was registered under, so the read derives the key the write
    used. A principal registered in a different tenant resolves to a
    different key and does not leak across the tenant boundary (S10 #298).

    **Vault-first** (G0.19-T2 #1478): the secret is read from Vault under
    the scheduler's static service token, falling back to the env-var
    path only when Vault yields nothing. See the module docstring for the
    full resolution order.

    Returns the tuple :meth:`AgentInvoker.run_scheduled` expects:

    * ``client_id`` -- the identity_ref verbatim (Keycloak's
      client-id namespace tolerates the ``:`` separators MEHO uses,
      so no transformation is needed for the grant request).
    * ``client_secret`` -- the resolved secret (Vault, else env var).

    Raises:
        AgentCredentialsUnresolvedError: neither Vault nor the env-var
            fallback yielded a secret. The loop logs
            ``scheduler_credentials_unresolved`` and leaves the trigger
            active for the next tick.
    """
    # Local import avoids a module-load cycle (vault_credentials imports
    # this module's key-segment helpers for its Vault-path derivation).
    from meho_backplane.scheduler.vault_credentials import (
        SchedulerVaultBrokerError,
        SchedulerVaultNotConfiguredError,
        read_agent_secret,
    )

    # 1. Vault-first.
    try:
        vault_secret = await read_agent_secret(identity_ref, tenant_id=tenant_id)
    except SchedulerVaultNotConfiguredError:
        # No scheduler Vault identity wired — fall through to the env-var
        # path silently (the documented fallback configuration).
        vault_secret = None
    except SchedulerVaultBrokerError as exc:
        # Vault is configured but the read failed (unreachable, denied,
        # malformed). Log at WARN and try the env-var fallback rather than
        # failing the fire outright — a transient Vault blip shouldn't
        # block an agent whose secret is also wired into the pod env.
        _log.warning(
            "scheduler_vault_read_failed",
            identity_ref=identity_ref,
            reason=str(exc),
        )
        vault_secret = None
    if vault_secret:
        return identity_ref, vault_secret

    # 2. Env-var fallback / break-glass.
    env_secret = _secret_from_env(identity_ref, tenant_id)
    if env_secret:
        return identity_ref, env_secret

    env_name = _env_var_name_for(identity_ref, tenant_id)
    raise AgentCredentialsUnresolvedError(
        f"no client_credentials secret resolved for identity_ref={identity_ref!r} "
        f"(tenant={tenant_id}); neither the Vault path "
        f"(scheduler_agent_vault_path_pattern, read under VAULT_SCHEDULER_TOKEN) "
        f"nor the fallback env var {env_name!r} yielded a secret. Register the "
        "agent over the API (persists the secret to Vault) or wire the agent's "
        "Keycloak client secret into the backplane pod env and retry."
    )
