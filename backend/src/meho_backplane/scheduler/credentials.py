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

Identity-ref -> client-id derivation
------------------------------------

``AgentDefinition.identity_ref`` is the Keycloak client-id reference
set at definition-create time, in the form ``agent:<name>`` (see
:mod:`~meho_backplane.agents.service` -- the create path normalises
the reference). The client-id portion is what the
``client_credentials`` grant authenticates as, so the env-var key the
scheduler derives is rooted at the bare ``identity_ref`` (sanitised
for env-var conventions).

The pattern's ``{client_id}`` placeholder is substituted with the
sanitised identity_ref:

* Non-alphanumeric characters (``:``, ``/``, ``-``, ``.``, ...) collapse
  to ``_`` so an ``identity_ref`` like ``agent:incident-triage`` is
  reachable as ``AGENT_INCIDENT_TRIAGE`` (the only env-var-legal form
  of that string).
* The whole substituted name is upper-cased.
* Default pattern ``MEHO_AGENT_SECRET_{client_id}`` therefore yields
  ``MEHO_AGENT_SECRET_AGENT_INCIDENT_TRIAGE``.

:func:`agent_client_id_from_identity_ref` is deterministic + pure (no
side effects, no I/O) so the env-var-name derivation can be unit-tested
without fixtures. :func:`resolve_agent_credentials` performs a Vault read
(I/O) before falling back to :func:`os.environ`.
"""

from __future__ import annotations

import os
import re

import structlog

from meho_backplane.settings import get_settings

_log = structlog.get_logger(__name__)

__all__ = [
    "AgentCredentialsUnresolvedError",
    "agent_client_id_from_identity_ref",
    "resolve_agent_credentials",
]

#: Pattern matching any character that is not legal in an env-var name.
#: POSIX env names are ``[A-Z_][A-Z0-9_]*``; the sanitisation here uses
#: the same alphabet plus a lowercase tolerance (the post-substitution
#: ``upper()`` lifts it). Anchored as a class so :func:`re.sub` walks
#: the string once with no backtracking.
_ENV_NAME_FORBIDDEN: re.Pattern[str] = re.compile(r"[^A-Za-z0-9_]")


class AgentCredentialsUnresolvedError(RuntimeError):
    """The scheduler could not source credentials for a scheduled fire.

    Raised when the env-var (or future Vault path) the pattern resolves
    to is not present / empty. The scheduler loop catches this, logs +
    audits the skip, and leaves the trigger ``active`` so an operator
    who wires the secret unblocks the schedule on the next tick.
    """


def agent_client_id_from_identity_ref(identity_ref: str) -> str:
    """Return the env-var-safe client-id derived from *identity_ref*.

    Sanitises by replacing every non-``[A-Za-z0-9_]`` character with
    ``_``. Preserves case for the caller's later
    upper-/lower-casing; the secret-key pattern in
    :attr:`Settings.scheduler_agent_secret_env_pattern` applies the
    final ``upper()`` after the substitution.

    Pure / deterministic. Callers should not rely on the result being
    reversible -- ``agent:x:y`` and ``agent_x_y`` map to the same
    sanitised form on purpose (env-vars are a flat namespace).
    """
    return _ENV_NAME_FORBIDDEN.sub("_", identity_ref)


def _env_var_name_for(identity_ref: str) -> str:
    """Return the env-var name the secret pattern resolves to for *identity_ref*.

    Substitutes the sanitised + upper-cased identity_ref into
    :attr:`Settings.scheduler_agent_secret_env_pattern`, then upper-cases
    the whole result. Operators who set a non-upper-cased pattern (e.g.
    ``meho_agent_secret_{client_id}``) otherwise resolve to a mixed-case
    env-var name that Linux's case-sensitive lookup would miss — the
    precondition gate would skip every fire with a
    ``credentials_unresolved`` warning pointing at the secret rather than
    the case-mismatch. The contract is "the whole substituted name is
    upper-cased"; this makes the code match it.
    """
    settings = get_settings()
    sanitised = agent_client_id_from_identity_ref(identity_ref).upper()
    return settings.scheduler_agent_secret_env_pattern.format(client_id=sanitised).upper()


def _secret_from_env(identity_ref: str) -> str:
    """Return the agent secret from the env var, or ``""`` when unset/empty."""
    return os.environ.get(_env_var_name_for(identity_ref), "").strip()


async def resolve_agent_credentials(identity_ref: str) -> tuple[str, str]:
    """Resolve ``(client_id, client_secret)`` for a scheduled-fire agent.

    *identity_ref* is :attr:`AgentDefinition.identity_ref` (the
    Keycloak client-id reference set at definition-create time).

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
    # this module's sanitiser for its Vault-path derivation).
    from meho_backplane.scheduler.vault_credentials import (
        SchedulerVaultBrokerError,
        SchedulerVaultNotConfiguredError,
        read_agent_secret,
    )

    # 1. Vault-first.
    try:
        vault_secret = await read_agent_secret(identity_ref)
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
    env_secret = _secret_from_env(identity_ref)
    if env_secret:
        return identity_ref, env_secret

    env_name = _env_var_name_for(identity_ref)
    raise AgentCredentialsUnresolvedError(
        f"no client_credentials secret resolved for identity_ref={identity_ref!r}; "
        f"neither the Vault path (scheduler_agent_vault_path_pattern, read under "
        f"VAULT_SCHEDULER_TOKEN) nor the fallback env var {env_name!r} yielded a "
        "secret. Register the agent over the API (persists the secret to Vault) or "
        "wire the agent's Keycloak client secret into the backplane pod env and retry."
    )
