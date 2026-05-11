# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault OIDC forward-auth — hvac client + per-request ``jwt_login``.

This module is the **federation chain's middle link**. After
:mod:`meho_backplane.auth.jwt` validates a Keycloak-issued JWT and
yields an :class:`~meho_backplane.auth.operator.Operator`, callers use
:func:`vault_client_for_operator` to forward that *same validated* JWT
to Vault's JWT/OIDC auth method. Vault verifies the JWT against its own
configured trust of Keycloak (bound to the ``meho-mcp`` role per Goal
#11) and issues a Vault token bound to the operator's identity. Every
secret read the backplane performs on behalf of an operator therefore
appears in Vault's audit log tagged with that operator's ``sub`` —
completing the audit trail the requirement letter mandates.

Per-request login is deliberate for v0.1: each authenticated request
that needs a Vault read does (1) JWT validate, (2) Vault OIDC login,
(3) read secret, (4) revoke the Vault token. v0.2 may cache per-operator
across requests with token-TTL respect; the simpler shape is acceptable
under dogfood load.

Library choice (per ADR 0004 — see Initiative #21 / Task #13): hvac.
hvac is **synchronous** (built on ``requests``); FastAPI does *not*
auto-offload blocking I/O inside ``async def`` callables, so every hvac
call from this module is wrapped in ``asyncio.to_thread`` to keep the
event loop responsive. The wrapper helpers
(:func:`_to_thread_jwt_login`, :func:`_to_thread_revoke_self`,
:func:`_to_thread_read_health`) are the only seam tests need to mock.

Error mapping:

* Vault returns 403 when the JWT is valid but the role denies access
  (or when the role is missing). hvac's adapter raises
  :class:`hvac.exceptions.Forbidden` — surfaced here as
  :class:`VaultRoleDeniedError`.
* The TCP / TLS layer fails (DNS, connection refused, TLS handshake,
  read timeout) before any Vault status code lands. ``requests``
  raises one of :class:`requests.exceptions.ConnectionError` /
  :class:`requests.exceptions.Timeout` — surfaced as
  :class:`VaultUnreachableError`.
* Anything else hvac raises (:class:`hvac.exceptions.VaultError`
  subclasses for 400/404/429/500/501/502/503) is wrapped in the
  generic :class:`VaultClientError` so callers don't have to import
  hvac to catch them.

Token revocation on context exit is best-effort. A failed
``revoke_self`` is logged in the future (G2.4 audit middleware) but
does **not** propagate — the request has already returned its body to
the caller, and the Vault token will time out on its own TTL anyway.
The revoke is a defence-in-depth measure that limits the blast radius
if a token leaks via a future log-instrumentation regression.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import hvac
import hvac.exceptions
import requests.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.health import ProbeResult
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "VaultClientError",
    "VaultRoleDeniedError",
    "VaultUnreachableError",
    "vault_client_for_operator",
    "vault_readiness_probe",
]


class VaultClientError(Exception):
    """Base class for backplane-side Vault failures.

    Raised by :func:`vault_client_for_operator` when the JWT forward to
    Vault fails for any reason. Subclasses carry more specific intent;
    callers can catch the base class to surface a single error response
    shape (G2.2-T3 wires this into the ``/api/v1/health`` route).
    """


class VaultUnreachableError(VaultClientError):
    """Vault is not reachable — DNS, TCP, TLS, or read-timeout failure.

    Distinguishes infrastructure failure from auth failure. Operators
    chasing a 5xx in production should see this when the Vault pod is
    down; the readiness probe registered via :func:`vault_readiness_probe`
    will be flapping in lock-step.
    """


class VaultRoleDeniedError(VaultClientError):
    """Vault rejected the JWT for the configured role.

    Either the role does not exist on the configured mount path, the
    role's bound claims (``bound_audiences``, ``bound_subject``,
    ``role_type``, ``user_claim``) do not match the incoming JWT, or
    Vault's own trust of the Keycloak issuer is misconfigured. Surfaces
    as a 401/403 to the caller in G2.2-T3 — never a 5xx.
    """


def _build_client(settings: Settings, *, token: str | None = None) -> hvac.Client:
    """Construct an unauthenticated :class:`hvac.Client` from settings.

    Centralises the ``vault_addr`` / ``vault_namespace`` /
    ``vault_timeout_seconds`` mapping so the readiness probe and the
    per-operator login share one construction path. ``token`` is
    threaded through so tests can build a client bound to a fixture
    token without round-tripping through ``jwt_login``.
    """
    return hvac.Client(
        url=str(settings.vault_addr).rstrip("/"),
        namespace=settings.vault_namespace,
        timeout=settings.vault_timeout_seconds,
        token=token,
    )


def _do_jwt_login(client: hvac.Client, *, role: str, jwt: str, mount_path: str) -> None:
    """Synchronously perform ``client.auth.jwt.jwt_login`` and bind the token.

    hvac's ``jwt_login`` returns the raw JSON response from Vault; with
    ``use_token=True`` (the default) it also assigns the issued token
    onto ``client.token`` so subsequent calls authenticate. This helper
    re-asserts that contract and raises :class:`VaultRoleDeniedError`
    when Vault returns a 403 — the most operator-actionable failure
    mode and the one Task #25 will exercise comprehensively.

    The ``Forbidden`` catch is intentionally narrow. Any other
    :class:`hvac.exceptions.VaultError` (bad mount path → 404, server
    error → 500, sealed Vault → 503) propagates as
    :class:`VaultClientError` from the calling context manager so the
    caller can choose its 401 / 5xx mapping without inspecting hvac
    internals.
    """
    try:
        client.auth.jwt.jwt_login(role=role, jwt=jwt, path=mount_path)
    except hvac.exceptions.Forbidden as exc:
        raise VaultRoleDeniedError(f"vault role denied: {exc}") from exc


def _do_revoke_self(client: hvac.Client) -> None:
    """Best-effort revoke of the per-request Vault token.

    Swallows every exception class hvac and requests can raise. The
    request has already returned to the operator; a failed revoke does
    not invalidate the secret read that succeeded a moment earlier, and
    the Vault token will expire on its own TTL. Defence-in-depth
    measure — its failure is not actionable.
    """
    with suppress(hvac.exceptions.VaultError, requests.exceptions.RequestException):
        client.auth.token.revoke_self()


async def _to_thread_jwt_login(
    client: hvac.Client, *, role: str, jwt: str, mount_path: str
) -> None:
    """Run :func:`_do_jwt_login` off the event loop."""
    await asyncio.to_thread(_do_jwt_login, client, role=role, jwt=jwt, mount_path=mount_path)


async def _to_thread_revoke_self(client: hvac.Client) -> None:
    """Run :func:`_do_revoke_self` off the event loop."""
    await asyncio.to_thread(_do_revoke_self, client)


def _do_read_health(client: hvac.Client) -> Any:
    """Synchronously call ``client.sys.read_health_status(method="GET")``.

    Factored out of :func:`vault_readiness_probe` so the blocking call
    can be offloaded to a worker thread via :func:`_to_thread_read_health`
    without inlining ``asyncio.to_thread(...)`` boilerplate at the probe
    call site. The seam is also what tests substitute when validating
    the probe's exception-mapping branches.

    ``method="GET"`` returns the JSON body when status is 200 and a
    :class:`requests.Response` (with a readable status code) when Vault
    is sealed / uninitialised / standby. The HEAD default returns no
    body — fine for a load balancer but loses the ``sealed`` flag we
    surface in :attr:`ProbeResult.detail`.
    """
    return client.sys.read_health_status(method="GET")


async def _to_thread_read_health(client: hvac.Client) -> Any:
    """Run :func:`_do_read_health` off the event loop."""
    return await asyncio.to_thread(_do_read_health, client)


@asynccontextmanager
async def vault_client_for_operator(operator: Operator) -> AsyncIterator[hvac.Client]:
    """Yield an authenticated :class:`hvac.Client` bound to *operator*.

    Performs the JWT/OIDC login on every entry — v0.1 has no per-operator
    token cache. The caller drives any Vault operations the role allows
    (typically a KV v2 read) and the context manager revokes the issued
    token on exit, capping the token's lifetime at one request even
    when Vault's role TTL is generous.

    Raises
    ------
    VaultUnreachableError:
        TCP / TLS / timeout failure reaching Vault. The caller should
        translate this into a 503 from any HTTP route that needed Vault.
    VaultRoleDeniedError:
        Vault returned 403 — JWT valid but role bindings did not match
        or the role is missing.
    VaultClientError:
        Any other Vault-side failure (4xx / 5xx that isn't 403, sealed
        Vault, malformed response, etc.).
    """
    settings = get_settings()
    client = _build_client(settings)

    try:
        await _to_thread_jwt_login(
            client,
            role=settings.vault_oidc_role,
            jwt=operator.raw_jwt,
            mount_path=settings.vault_oidc_mount_path,
        )
    except VaultClientError:
        # Already a backplane error — propagate verbatim. (Caught by the
        # subclass check before the base ``VaultError`` branch below.)
        raise
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as exc:
        raise VaultUnreachableError(f"vault unreachable: {type(exc).__name__}") from exc
    except hvac.exceptions.VaultError as exc:
        raise VaultClientError(f"vault error: {type(exc).__name__}: {exc}") from exc

    try:
        yield client
    finally:
        await _to_thread_revoke_self(client)


def _classify_health_response(payload: Any) -> tuple[bool, str]:
    """Translate hvac's ``read_health_status`` return into (ok, detail).

    hvac's ``JSONAdapter`` returns a plain dict for HTTP 200 responses
    and a :class:`requests.Response` for any non-200 (because non-200
    bodies are not guaranteed to be JSON). For ``/sys/health`` Vault
    happens to always return JSON regardless of status code, but the
    adapter only auto-decodes 200s. We coerce both shapes here.

    Vault's ``/sys/health`` HTTP contract:

    ===== =========================================================
    200   initialized + unsealed + active
    429   initialized + unsealed + standby
    472   DR secondary
    473   performance standby
    501   not initialized
    503   sealed
    ===== =========================================================

    For readiness purposes we treat 200/429 as **ok** (Vault is serving
    requests); 472/473 are also "ok" for OSS dogfood (we don't run a
    DR / performance-standby topology in v0.1, but a future operator
    could). 501 and 503 are **not ok** — Vault cannot honour an OIDC
    login while sealed or uninitialized.
    """
    if isinstance(payload, dict):
        sealed = bool(payload.get("sealed", False))
        initialized = bool(payload.get("initialized", True))
        if sealed:
            return False, "sealed"
        if not initialized:
            return False, "uninitialized"
        return True, f"sealed={sealed}"

    # Non-200 path — payload is a ``requests.Response`` instance. We
    # cannot import its concrete type for an isinstance check without
    # tightening the test contract; the duck-typed ``status_code``
    # attribute is sufficient and matches what hvac itself does.
    status_code = getattr(payload, "status_code", None)
    if status_code in (429, 472, 473):
        # Vault is reachable and serving — just not the active leader.
        return True, f"http_{status_code}"
    if status_code == 501:
        return False, "uninitialized"
    if status_code == 503:
        return False, "sealed"
    return False, f"unexpected_status: {status_code}"


async def vault_readiness_probe() -> ProbeResult:
    """Readiness probe — confirm Vault's ``/sys/health`` is reachable.

    Registered with :mod:`meho_backplane.health` at app startup. Hits
    Vault on every call (no caching); ``/sys/health`` is unauthenticated
    and explicitly designed to be cheap by HashiCorp's own contract.

    Async because hvac (the only seam to Vault we have) is synchronous
    and would block the FastAPI event loop on every ``/ready`` poll if
    called inline. The actual HTTP call is offloaded to a worker thread
    via :func:`_to_thread_read_health`, mirroring the same pattern the
    per-request login uses (:func:`_to_thread_jwt_login`).

    The probe distinguishes three failure shapes in ``detail``:

    * ``unreachable: <ExceptionClassName>`` — TCP/TLS/DNS/timeout.
    * ``sealed`` / ``uninitialized`` — Vault answered, but is not
      serving. ``/api/v1/health`` callers will get a 503 next.
    * ``unexpected_status: <code>`` — the contract changed (or a proxy
      injected an unexpected response). Visible in
      :mod:`/ready <meho_backplane.health>` for operators to chase.

    Detail strings never echo Vault's URL or the operator's namespace —
    those are operator-controlled inputs and a 503 payload should not
    surface them.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        return ProbeResult(
            name="vault",
            ok=False,
            detail=f"settings_unavailable: {type(exc).__name__}",
        )

    client = _build_client(settings)
    try:
        payload = await _to_thread_read_health(client)
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as exc:
        return ProbeResult(
            name="vault",
            ok=False,
            detail=f"unreachable: {type(exc).__name__}",
        )
    except hvac.exceptions.VaultError as exc:
        # ``read_health_status`` is called with ``raise_exception=False``
        # internally, so reaching this branch implies a Vault response
        # the adapter could not classify (e.g. wholly malformed body).
        return ProbeResult(
            name="vault",
            ok=False,
            detail=f"vault_error: {type(exc).__name__}",
        )

    ok, detail = _classify_health_response(payload)
    return ProbeResult(name="vault", ok=ok, detail=detail)
