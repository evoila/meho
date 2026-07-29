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
``Client(token=…)`` primitive with no ``secret_id`` exchange. An
operator who prefers AppRole runs a Vault Agent sidecar that renews a
token into ``VAULT_SCHEDULER_TOKEN``: additive, no code change here.

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

The secret payload shape is ``{"client_secret": "<value>"}``. No secret
value ever enters a log event or an error message — only the path and
the field *name* do.

Token lifetime — renew-on-use + self-lookup (#2328)
===================================================

The documented scheduler token is a Vault **periodic** token
(``-period=768h`` in the onboarding guidance). A periodic token expires
``period`` after its *last renewal*, so a long-running scheduler that
never renews carries a built-in ~32-day fuse: once it blows, every
Vault-first read returns 403 and the scheduler silently skips. Two
mechanisms defuse it here:

* :func:`_maybe_renew_scheduler_token` fires a best-effort
  ``auth/token/renew-self`` after every successful read/write, at
  scheduler-tick frequency, so a periodic token with any sane
  ``period`` never expires while the process runs. A failed renewal is
  logged and swallowed — the read/write it follows already succeeded.
* :func:`verify_scheduler_token` runs ``auth/token/lookup-self`` at
  scheduler startup and on a slow cadence from the tick loop. It does
  not fix the fuse — it shortens time-to-notice from weeks to minutes
  by logging a dead/unreachable token as a loud ``ERROR``. Sibling
  #2327 consumes the same signal for its ``/ready features.scheduler``
  skip-state surface.

The same ``lookup-self`` primitive also disambiguates the *write*
failure path (#2652): a dead token and a live token on an under-scoped
policy both draw a **403**, but the fixes are opposites (re-mint vs.
widen the policy). :func:`write_agent_secret` probes
:func:`_scheduler_token_rejected` on that 403 — and only that 403 — and
stamps ``token_invalid`` on the raised
:class:`SchedulerVaultBrokerError`. Diagnosis only: no retry.

The token is resolved from its live source on **every** use
(:func:`_current_scheduler_token`) rather than frozen at process start,
so a Vault-Agent sidecar (or an operator) that re-mints the token into
``VAULT_SCHEDULER_TOKEN_FILE`` is picked up without a pod restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import hvac
import hvac.exceptions
import requests.exceptions
import structlog

from meho_backplane.auth.vault import _build_client
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "SCHEDULER_VAULT_TOKEN_INVALID_DETAIL",
    "SCHEDULER_VAULT_WRITE_DENIED_DETAIL",
    "SECRET_FIELD",
    "SchedulerTokenStatus",
    "SchedulerVaultBrokerError",
    "SchedulerVaultNotConfiguredError",
    "read_agent_secret",
    "split_kv_v2_api_path",
    "vault_path_for_client_id",
    "verify_scheduler_token",
    "write_agent_secret",
]

_log = structlog.get_logger(__name__)

#: The single field name the agent-credentials secret payload carries.
#: Both the write (register) and the read (scheduler) agree on this key.
SECRET_FIELD: str = "client_secret"

#: Remediation for a write Vault denied while the scheduler token is
#: **live** — the bound policy lacks ``create``/``update`` on the
#: agent-credentials path. Kept verbatim from the pre-#2652 MCP message.
SCHEDULER_VAULT_WRITE_DENIED_DETAIL: str = (
    "scheduler Vault write failed — VAULT_SCHEDULER_TOKEN policy must "
    "grant create/update on the agent-credentials path"
)

#: Remediation for a write Vault denied **because the token is dead**
#: (revoked / expired / lost lease) — indistinguishable from the case
#: above by status code alone, hence the ``lookup-self`` probe (#2652).
#: Three-clause shape per ``docs/codebase/error-message-shape.md``.
SCHEDULER_VAULT_TOKEN_INVALID_DETAIL: str = (
    "scheduler_vault_token_invalid: the scheduler Vault token is invalid "
    "or expired — Vault rejected auth/token/lookup-self for it, so the "
    "policy scope is not the fault. Re-mint VAULT_SCHEDULER_TOKEN (or "
    "refresh the file VAULT_SCHEDULER_TOKEN_FILE points at) and update "
    "the deployment secret per docs/cross-repo/vault-provisioning.md."
)


class SchedulerVaultBrokerError(Exception):
    """Base class for scheduler-service-token Vault broker failures.

    Raised on read/write failures that are *not* the
    not-configured case (which has its own subclass so callers can choose
    to fall back to the env-var path rather than fail).

    ``token_invalid`` carries the broker's ``lookup-self`` disposition
    (#2652) so every consuming surface — MCP, the two REST register
    routes, the UI banner — picks the right remediation from one shared
    diagnosis instead of re-probing Vault itself. ``True``: the token was
    403'd by ``auth/token/lookup-self`` and must be re-minted. ``False``:
    it is live, or its liveness could not be established — either way the
    pre-existing policy-scope remediation stands.
    """

    def __init__(self, *args: object, token_invalid: bool = False) -> None:
        super().__init__(*args)
        self.token_invalid = token_invalid


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


def _current_scheduler_token(settings: Settings) -> str:
    """Resolve the scheduler's Vault token from its live source (#2328).

    Read fresh on every call so a Vault-Agent sidecar (or an operator)
    that re-mints the token into the configured sink is picked up
    **without a pod restart** — the lived remediation that previously
    forced a restart is what this defuses. Resolution order:

    1. :attr:`Settings.vault_scheduler_token_file` — a file a sidecar
       rewrites on renewal/re-mint. Re-read on every call: the *path* is
       static config (safe to cache in settings), the *contents* are
       not. An unreadable or empty file falls through to the env var
       rather than failing hard.
    2. :attr:`Settings.vault_scheduler_token` — the static env-var token.

    Returns the stripped token, or ``""`` when neither source yields one.
    """
    file_path = settings.vault_scheduler_token_file.strip()
    if file_path:
        try:
            token = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
    return settings.vault_scheduler_token.strip()


def _scheduler_client(settings: Settings) -> hvac.Client:
    """Build a token-authenticated hvac client for the scheduler identity.

    Raises :class:`SchedulerVaultNotConfiguredError` when no scheduler
    token is resolvable from either source
    (:func:`_current_scheduler_token`) — the caller decides whether that
    is a hard failure (register) or a fall-back trigger (scheduler read).
    """
    token = _current_scheduler_token(settings)
    if not token:
        raise SchedulerVaultNotConfiguredError(
            "VAULT_SCHEDULER_TOKEN (or VAULT_SCHEDULER_TOKEN_FILE) is not "
            "set; the scheduler has no Vault read/write identity for agent "
            "client_credentials secrets"
        )
    # Reuse the auth.vault client builder so vault_addr / namespace /
    # timeout mapping stays single-sourced; bind the resolved token
    # instead of a JWT-login-issued one.
    return _build_client(settings, token=token)


def _renew_scheduler_token_blocking(client: hvac.Client) -> None:
    """Synchronously fire ``auth/token/renew-self`` for *client*'s token."""
    client.auth.token.renew_self()


async def _maybe_renew_scheduler_token(client: hvac.Client) -> None:
    """Best-effort renew of the scheduler's periodic Vault token (#2328).

    Called after a successful read/write. Renewing at scheduler-tick
    frequency keeps a periodic token (``-period=768h``) alive
    indefinitely while the process runs, defusing the ~32-day fuse.

    Best-effort by contract: the read/write already succeeded, so a
    failed renewal (non-renewable token, transient Vault error, revoked
    lease) is logged and swallowed rather than promoted to a new failure
    mode. No token or secret value ever enters the log event.
    """
    try:
        await asyncio.to_thread(_renew_scheduler_token_blocking, client)
    except (requests.exceptions.RequestException, hvac.exceptions.VaultError) as exc:
        _log.warning("scheduler_vault_token_renew_failed", reason=type(exc).__name__)
        return
    _log.debug("scheduler_vault_token_renewed")


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
        Vault is unreachable or rejected the write. On a **403** the
        error carries ``token_invalid=True`` when a follow-up
        ``lookup-self`` also 403s (the token is dead — re-mint it) and
        ``False`` when the token is live, leaving the policy scope at
        fault (#2652). No other status is evidence about the token, so
        each keeps ``False``.
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
    except hvac.exceptions.Forbidden as exc:
        # 403 is the one ambiguous rejection: a dead token and a live-but-
        # under-scoped policy look identical, yet the remediations are
        # opposites (re-mint vs. widen the policy). Split them with one
        # ``lookup-self`` on the *same* client that just failed and carry
        # the disposition so all four register surfaces inherit it
        # (#2652). Diagnosis only — the write is never retried.
        token_invalid = await _scheduler_token_rejected(client)
        if token_invalid:
            _log.error(
                "scheduler_vault_token_dead",
                reason=type(exc).__name__,
                check="agent_secret_write",
            )
        raise SchedulerVaultBrokerError(
            f"vault rejected agent-secret write at {api_path!r}: {type(exc).__name__}",
            token_invalid=token_invalid,
        ) from exc
    except hvac.exceptions.VaultError as exc:
        # Every other status hvac maps onto ``VaultError`` (429, 500, 502,
        # 503 sealed/down …) describes Vault, not the token — nothing to
        # disambiguate, so no probe; the policy-scope remediation stands.
        raise SchedulerVaultBrokerError(
            f"vault rejected agent-secret write at {api_path!r}: {type(exc).__name__}"
        ) from exc
    # The token just authenticated a write — renew it so the periodic
    # token never ages out (#2328). Best-effort; never raises.
    await _maybe_renew_scheduler_token(client)
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
    # The token just authenticated a read — renew it so the periodic
    # token never ages out (#2328). Best-effort; never raises.
    await _maybe_renew_scheduler_token(client)
    secret = _unwrap_secret(payload)
    if not secret:
        return None
    return secret


@dataclass(frozen=True)
class SchedulerTokenStatus:
    """Outcome of a scheduler Vault-token ``lookup-self`` (#2328).

    Returned by :func:`verify_scheduler_token`. ``configured`` is
    ``False`` when no scheduler token is wired (the documented
    env-var-fallback opt-out) — a non-failure. ``ok`` is ``True`` when
    Vault answered the ``lookup-self`` and the token is live; ``False``
    when the token is dead (403 / expired) or Vault was unreachable.
    ``ttl_seconds`` and ``expire_time`` echo Vault's own view so an
    operator (or #2327's ``/ready`` surface) can watch ``expire_time``
    advance across renewals. Never carries a token value.
    """

    configured: bool
    ok: bool
    detail: str
    ttl_seconds: int | None = None
    expire_time: str | None = None


def _lookup_self_blocking(client: hvac.Client) -> object:
    """Synchronously call ``auth/token/lookup-self`` for *client*'s token."""
    return client.auth.token.lookup_self()


async def _scheduler_token_rejected(client: hvac.Client) -> bool:
    """Is *client*'s token itself dead, rather than merely under-scoped?

    Called from the write-failure path (#2652) after Vault answered a
    write with 403. A revoked / expired / lost-lease token and a live
    token on a policy without ``create``+``update`` both produce that
    403, so the write response alone cannot name the remediation.
    ``auth/token/lookup-self`` can: Vault answers it for a live token
    granted ``read`` there (``meho-scheduler`` grants it — load-bearing,
    see ``docs/cross-repo/vault-provisioning.md``) and 403s an invalid one.

    Probes the **same** client that failed the write — the answer must
    describe the identity that was actually denied, and re-resolving
    could pick up a token re-minted between the two calls.

    Returns ``True`` only on a 403. hvac maps *every* Vault status onto a
    :class:`~hvac.exceptions.VaultError` subclass, so a sealed (503),
    overloaded (429) or broken (500/502) Vault would otherwise read as a
    dead token and have an operator re-mint a healthy one mid-outage.
    Those, like a transport failure, are inconclusive — ``False``, which
    keeps the caller on the policy-scope remediation.
    """
    try:
        await asyncio.to_thread(_lookup_self_blocking, client)
    except hvac.exceptions.Forbidden:
        return True
    except hvac.exceptions.VaultError as exc:
        _log.warning(
            "scheduler_vault_token_lookup_inconclusive",
            reason=type(exc).__name__,
            check="agent_secret_write",
        )
        return False
    except requests.exceptions.RequestException:
        _log.warning("scheduler_vault_token_lookup_unreachable", check="agent_secret_write")
        return False
    return False


def _unwrap_token_lifetime(payload: object) -> tuple[int | None, str | None]:
    """Pull ``(ttl_seconds, expire_time)`` from a lookup-self response.

    hvac's ``lookup_self`` returns ``{"data": {"ttl": <int>,
    "expire_time": <iso8601|None>, ...}}``. Returns ``(None, None)`` for
    any malformed shape rather than raising — the caller has already
    logged the token's liveness; the lifetime fields are informational.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, None
    ttl = data.get("ttl")
    expire_time = data.get("expire_time")
    ttl_int = int(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) else None
    expire_str = str(expire_time) if expire_time else None
    return ttl_int, expire_str


async def verify_scheduler_token(*, reason: str = "check") -> SchedulerTokenStatus:
    """Self-lookup the scheduler Vault token; log loudly when it's dead.

    Called at scheduler startup and on a slow cadence from the tick loop
    (:mod:`meho_backplane.scheduler.loop`). It does **not** fix the fuse
    (that is :func:`_maybe_renew_scheduler_token`; see the module docstring).

    Never raises. An unconfigured token returns ``configured=False``
    (the documented env-var-fallback opt-out) and any Vault failure is
    captured into ``ok=False`` with a ``detail`` class. Sibling #2327
    consumes the returned status for its ``/ready features.scheduler``
    skip-state surface.
    """
    settings = get_settings()
    token = _current_scheduler_token(settings)
    if not token:
        return SchedulerTokenStatus(configured=False, ok=True, detail="not_configured")
    client = _build_client(settings, token=token)
    try:
        payload = await asyncio.to_thread(_lookup_self_blocking, client)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        _log.error(
            "scheduler_vault_token_unreachable",
            reason=type(exc).__name__,
            check=reason,
        )
        return SchedulerTokenStatus(
            configured=True, ok=False, detail=f"unreachable:{type(exc).__name__}"
        )
    except hvac.exceptions.VaultError as exc:
        # Forbidden (403) is the dead/expired-token signature — the exact
        # failure this filing exists to make visible. Log it loudly.
        _log.error(
            "scheduler_vault_token_dead",
            reason=type(exc).__name__,
            check=reason,
        )
        return SchedulerTokenStatus(
            configured=True, ok=False, detail=f"denied:{type(exc).__name__}"
        )
    ttl, expire_time = _unwrap_token_lifetime(payload)
    _log.info(
        "scheduler_vault_token_verified",
        check=reason,
        ttl_seconds=ttl,
        expire_time=expire_time,
    )
    return SchedulerTokenStatus(
        configured=True,
        ok=True,
        detail="ok",
        ttl_seconds=ttl,
        expire_time=expire_time,
    )


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
