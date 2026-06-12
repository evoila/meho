# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduler-service-token Vault broker for agent client_credentials secrets.

G0.19-T2 (#1478). Two operations, both authenticated with the scheduler's
**static service token** (:attr:`Settings.vault_scheduler_token`) rather
than a per-operator Keycloak JWT:

* :func:`write_agent_secret` — called from
  :meth:`~meho_backplane.auth.agent_principals.AgentPrincipalService.register`
  to persist a freshly-registered agent's Keycloak ``client_credentials``
  secret into Vault, so the operator-less scheduler can read it back
  without a pod env-var wire-up + redeploy.
* :func:`read_agent_secret` — called from
  :func:`~meho_backplane.scheduler.credentials.resolve_agent_credentials`
  (Vault-first) to source the secret for a scheduled fire.

Why a static service token, not AppRole
=======================================

The scheduler is operator-less: :func:`vault_client_for_operator` is
JWT/OIDC-bound and there is no operator JWT on the tick loop. The
architecture doc (``docs/architecture/connector-auth.md`` §Option B)
records AppRole as the alternative service identity but flags its
``secret_id`` bootstrap (secret-zero) cost. A static token bound to a
narrow read/write policy on the agent-credentials path is the
lowest-friction shippable identity — it reuses hvac's
``Client(token=…)`` primitive (the same one the live-Vault test harness
uses) with no ``secret_id`` exchange. An operator who prefers AppRole
runs a Vault Agent sidecar that renews a token into
``VAULT_SCHEDULER_TOKEN``: additive, no code change here.

Path convention
===============

:attr:`Settings.scheduler_agent_vault_path_pattern` is the **raw Vault
HTTP API path** (default ``secret/data/agents/{client_id}/credentials``)
— it embeds the mount (``secret``) and the KV-v2 ``data/`` infix. hvac's
``secrets.kv.v2`` helpers want the **logical** path relative to the
mount (they insert ``data/`` themselves — verified against hvac 2.4.0).
:func:`split_kv_v2_api_path` reconciles the two so the shipped default
setting keeps working unchanged (#823 promised "a code swap, not an
env-var rename").

The secret payload shape is ``{"client_secret": "<value>"}``; the read
returns the ``client_secret`` field. No secret value ever enters a log
event or an error message — only the path and the field *name* do.
"""

from __future__ import annotations

import asyncio

import hvac
import hvac.exceptions
import requests.exceptions
import structlog

from meho_backplane.auth.vault import _build_client
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "SECRET_FIELD",
    "SchedulerVaultBrokerError",
    "SchedulerVaultNotConfiguredError",
    "read_agent_secret",
    "split_kv_v2_api_path",
    "vault_path_for_client_id",
    "write_agent_secret",
]

_log = structlog.get_logger(__name__)

#: The single field name the agent-credentials secret payload carries.
#: Both the write (register) and the read (scheduler) agree on this key.
SECRET_FIELD: str = "client_secret"


class SchedulerVaultBrokerError(Exception):
    """Base class for scheduler-service-token Vault broker failures.

    Raised on read/write failures that are *not* the
    not-configured case (which has its own subclass so callers can choose
    to fall back to the env-var path rather than fail).
    """


class SchedulerVaultNotConfiguredError(SchedulerVaultBrokerError):
    """The scheduler Vault service token is unset.

    Distinct from :class:`SchedulerVaultBrokerError` so
    :func:`~meho_backplane.scheduler.credentials.resolve_agent_credentials`
    can treat "no Vault identity configured" as "fall back to the env-var
    path" rather than a hard failure, while a genuine Vault read/write
    error (network, permission, malformed payload) surfaces as the base
    class.
    """


def split_kv_v2_api_path(api_path: str) -> tuple[str, str]:
    """Split a raw KV-v2 *API* path into ``(mount_point, logical_path)``.

    hvac's ``secrets.kv.v2`` helpers take the mount separately and a
    *logical* path relative to it (they insert the ``data/`` segment on
    the wire themselves). The configured
    :attr:`Settings.scheduler_agent_vault_path_pattern` is the raw API
    path that embeds both the mount and the ``data/`` infix, e.g.
    ``secret/data/agents/agent_reporter/credentials``. This helper splits
    that into ``("secret", "agents/agent_reporter/credentials")``.

    Rules:

    * A ``<mount>/data/<rest>`` shape splits on the **first** ``data``
      segment: mount is everything before it, logical path everything
      after. This handles non-default mounts (``kv/data/…`` →
      ``("kv", "…")``).
    * A path with no ``data/`` segment is treated as already-logical on
      the default ``secret`` mount — defensive, so a hand-edited pattern
      that drops the ``data/`` infix still resolves rather than 404s.

    Raises
    ------
    ValueError
        The path is empty, or splits to an empty logical path (a pattern
        like ``secret/data/`` with nothing after the infix is a
        misconfiguration that would read/write the mount root).
    """
    stripped = api_path.strip().strip("/")
    if not stripped:
        raise ValueError(f"empty Vault KV-v2 path: {api_path!r}")
    segments = stripped.split("/")
    try:
        data_idx = segments.index("data")
    except ValueError:
        # No ``data/`` infix — treat the whole thing as a logical path on
        # the default ``secret`` mount.
        return "secret", stripped
    mount = "/".join(segments[:data_idx]) or "secret"
    logical = "/".join(segments[data_idx + 1 :])
    if not logical:
        raise ValueError(
            f"Vault KV-v2 path {api_path!r} has no logical path after the "
            "'data/' segment; it would address the mount root"
        )
    return mount, logical


def vault_path_for_client_id(client_id: str, *, settings: Settings | None = None) -> str:
    """Render the configured Vault path pattern for *client_id*.

    Substitutes the sanitised-and-upper-cased ``{client_id}`` token into
    :attr:`Settings.scheduler_agent_vault_path_pattern`. The sanitisation
    mirrors the env-var derivation in
    :func:`~meho_backplane.scheduler.credentials.agent_client_id_from_identity_ref`
    so the Vault key and the env-var key are derived from one identity in
    a consistent shape (``agent:reporter`` → ``AGENT_REPORTER``).
    """
    # Local import avoids a module-load cycle: credentials imports this
    # module's read path, so importing credentials at top level here would
    # be circular.
    from meho_backplane.scheduler.credentials import agent_client_id_from_identity_ref

    if settings is None:
        settings = get_settings()
    sanitised = agent_client_id_from_identity_ref(client_id).upper()
    return settings.scheduler_agent_vault_path_pattern.format(client_id=sanitised)


def _scheduler_client(settings: Settings) -> hvac.Client:
    """Build a token-authenticated hvac client for the scheduler identity.

    Raises :class:`SchedulerVaultNotConfiguredError` when
    :attr:`Settings.vault_scheduler_token` is unset — the caller decides
    whether that is a hard failure (register) or a fall-back trigger
    (scheduler read).
    """
    token = settings.vault_scheduler_token.strip()
    if not token:
        raise SchedulerVaultNotConfiguredError(
            "VAULT_SCHEDULER_TOKEN is not set; the scheduler has no Vault "
            "read/write identity for agent client_credentials secrets"
        )
    # Reuse the auth.vault client builder so vault_addr / namespace /
    # timeout mapping stays single-sourced; bind the static token instead
    # of a JWT-login-issued one.
    return _build_client(settings, token=token)


async def write_agent_secret(identity_ref: str, client_secret: str) -> str:
    """Persist *client_secret* for *identity_ref* to Vault; return the path.

    Called from the agent-principal register path after the Keycloak
    client is created and its secret fetched. Writes
    ``{SECRET_FIELD: client_secret}`` as a KV-v2 secret at the configured
    path under the scheduler service token.

    Returns the rendered Vault API path (for logging / audit), never the
    secret value.

    Raises
    ------
    SchedulerVaultNotConfiguredError
        ``VAULT_SCHEDULER_TOKEN`` is unset. The register caller treats this
        as "the deployment opted out of the Vault path" and skips the write
        with a warning (env-var fallback remains); the read caller treats
        it as "fall back to the env var".
    SchedulerVaultBrokerError
        Vault is unreachable or rejected the write.
    """
    settings = get_settings()
    api_path = vault_path_for_client_id(identity_ref, settings=settings)
    mount, logical = split_kv_v2_api_path(api_path)
    client = _scheduler_client(settings)
    try:
        await asyncio.to_thread(
            client.secrets.kv.v2.create_or_update_secret,
            path=logical,
            secret={SECRET_FIELD: client_secret},
            mount_point=mount,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise SchedulerVaultBrokerError(
            f"vault unreachable writing agent secret at {api_path!r}: {type(exc).__name__}"
        ) from exc
    except hvac.exceptions.VaultError as exc:
        raise SchedulerVaultBrokerError(
            f"vault rejected agent-secret write at {api_path!r}: {type(exc).__name__}"
        ) from exc
    _log.info(
        "scheduler_agent_secret_written",
        identity_ref=identity_ref,
        vault_path=api_path,
    )
    return api_path


async def read_agent_secret(identity_ref: str) -> str | None:
    """Read the agent ``client_secret`` for *identity_ref* from Vault.

    Returns the secret string, or ``None`` when the secret does not exist
    (Vault 404 / missing path) so the caller can fall back to the env-var
    path. A ``None`` return is the "not in Vault" signal — distinct from a
    raised error, which signals an *infrastructure* failure the caller
    must not silently swallow.

    Raises
    ------
    SchedulerVaultNotConfiguredError
        ``VAULT_SCHEDULER_TOKEN`` is unset — the caller treats this as
        "Vault not available, try the env-var fallback".
    SchedulerVaultBrokerError
        Vault is unreachable or rejected the read for a reason other than
        a missing path.
    """
    settings = get_settings()
    api_path = vault_path_for_client_id(identity_ref, settings=settings)
    mount, logical = split_kv_v2_api_path(api_path)
    client = _scheduler_client(settings)
    try:
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version,
            path=logical,
            mount_point=mount,
            raise_on_deleted_version=False,
        )
    except hvac.exceptions.InvalidPath:
        # KV-v2 read of a non-existent path raises InvalidPath (404). This
        # is the "agent secret not in Vault yet" case — return None so the
        # resolver falls back to the env var rather than failing the fire.
        return None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise SchedulerVaultBrokerError(
            f"vault unreachable reading agent secret at {api_path!r}: {type(exc).__name__}"
        ) from exc
    except hvac.exceptions.VaultError as exc:
        raise SchedulerVaultBrokerError(
            f"vault rejected agent-secret read at {api_path!r}: {type(exc).__name__}"
        ) from exc
    secret = _unwrap_secret(payload)
    if not secret:
        return None
    return secret


def _unwrap_secret(payload: object) -> str:
    """Pull ``SECRET_FIELD`` out of an hvac KV-v2 read payload as a string.

    KV-v2's read returns ``{"data": {"data": {<kv>}, "metadata": {...}}}``;
    the secret content is the nested ``data["data"]``. Returns the
    stripped ``SECRET_FIELD`` value, or ``""`` when the payload is
    malformed or the field is absent (caller treats ``""`` as "not in
    Vault"). Mirrors the structural-unwrap discipline in
    :mod:`meho_backplane.connectors._shared.vault_creds`.
    """
    outer = payload.get("data") if isinstance(payload, dict) else None
    secret_data = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(secret_data, dict):
        return ""
    value = secret_data.get(SECRET_FIELD)
    if value is None:
        return ""
    return str(value).strip()
