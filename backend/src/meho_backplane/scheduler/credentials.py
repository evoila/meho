# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Autonomous-agent credential resolution for the G11.3 scheduler (#823).

The scheduler fires agent runs via
:meth:`~meho_backplane.agent.invocation.AgentInvoker.run_scheduled`
(G11.2-T2 #1096) which expects ``(agent_client_id, agent_client_secret)``
for the Keycloak ``client_credentials`` grant. The scheduler is
operator-less (no JWT to hand to
:func:`~meho_backplane.auth.vault.vault_client_for_operator`), so the
v0.2 credential source is the backplane pod's environment variable
matrix. Operators wire agent secrets into the pod the same way
``ANTHROPIC_API_KEY`` is wired today (Helm chart secret /
external-secrets / sealed-secret); the env-var name is derived from the
agent's ``identity_ref`` via the
:attr:`Settings.scheduler_agent_secret_env_pattern` pattern.

The forward-compat Vault path
(:attr:`Settings.scheduler_agent_vault_path_pattern`) is shipped but
unused -- a future G11.2 follow-up will swap this module's
:func:`resolve_agent_credentials` over to a scheduler-service-token
Vault read without changing the call site.

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

The function is deterministic + pure (no side effects, no I/O beyond
:func:`os.environ`'s lookup) so unit tests can exercise it without
fixtures by monkey-patching the env directly.
"""

from __future__ import annotations

import os
import re

from meho_backplane.settings import get_settings

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


def resolve_agent_credentials(identity_ref: str) -> tuple[str, str]:
    """Resolve ``(client_id, client_secret)`` for a scheduled-fire agent.

    *identity_ref* is :attr:`AgentDefinition.identity_ref` (the
    Keycloak client-id reference set at definition-create time).

    Returns the tuple :meth:`AgentInvoker.run_scheduled` expects:

    * ``client_id`` -- the identity_ref verbatim (Keycloak's
      client-id namespace tolerates the ``:`` separators MEHO uses,
      so no transformation is needed for the grant request).
    * ``client_secret`` -- read from the env-var whose name the
      :attr:`Settings.scheduler_agent_secret_env_pattern` resolves to
      against the sanitised + upper-cased *identity_ref*.

    Raises:
        AgentCredentialsUnresolvedError: the resolved env-var is not
            set or empty. Operator must wire the secret before the
            trigger can fire.
    """
    settings = get_settings()
    sanitised = agent_client_id_from_identity_ref(identity_ref).upper()
    env_name = settings.scheduler_agent_secret_env_pattern.format(client_id=sanitised)
    secret = os.environ.get(env_name, "").strip()
    if not secret:
        raise AgentCredentialsUnresolvedError(
            f"no client_credentials secret resolved for identity_ref={identity_ref!r}; "
            f"expected env var {env_name!r} to be set. Wire the agent's Keycloak "
            "client secret into the backplane pod (Helm chart secret / "
            "external-secrets / sealed-secret) and retry."
        )
    return identity_ref, secret
