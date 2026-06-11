# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared auth scaffolding for the four VCF management-plane connectors.

The three skeleton connectors landing under G3.6 — VCF Operations (vROps,
#829), VCF Operations for Logs (vRLI, #830), and VCF Fleet (#831) — share a
small set of auth helpers:

* HTTP Basic ``Authorization`` header construction (vROps, Fleet send Basic
  on every request; vRLI sends it once at session-login).
* The ``auth_model`` boundary gate (accept ``SHARED_SERVICE_ACCOUNT`` enum
  member, its string value, or ``None`` — the pre-G0.3
  column-not-yet-populated sentinel — and reject everything else).
* A Vault-credentials-loader protocol + first-use-cached per-target
  ``{"username": str, "password": str}`` dict with the
  "missing-key → ``RuntimeError`` naming target" contract. The default
  loader (:func:`load_credentials_from_vault`) performs a **live**
  operator-context KV-v2 read via the shared
  :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  helper (G3.10-T2 #946 wired the three VCF consumers to State 2).
* A flexible session-login helper for products that POST credentials and
  consume a token from the response (vRLI specifically; vROps doesn't
  establish a session, Fleet doesn't either).

This module is the common subset — what's genuinely identical across the
three consumers. Anything per-connector (response-token shape, downstream
path, the 401-driven re-login loop around downstream calls) stays in the
per-connector module.

VCF Automation (#832) is intentionally **NOT** a consumer of this module.
Its dual-plane auth (provider Basic → ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` JWT
+ tenant JSON login → ``{token: ...}`` + vhost routing via ``--fqdn``) is
bespoke and stays in the Automation connector.

Lift sources
------------

Each helper is a verbatim lift from an existing connector, with the
connector-specific names generalised:

* ``basic_auth_header`` ← Harbor's ``_basic_auth_header``.
* ``is_acceptable_auth_model`` ← Harbor's ``_is_acceptable_auth_model``.
* The credentials cache + missing-key error shape ← Harbor's
  ``HarborConnector._load_credentials`` (``connector.py`` L209-238).
* ``vcf_session_login`` ← the session-create round-trip inside
  :meth:`meho_backplane.connectors.nsx.connector.NsxConnector._session_token`
  (``connector.py`` L213-279), pulled out of the 401-retry loop. The retry
  loop stays in the consumer because the downstream call paths differ
  per connector.

Harbor / NSX / SDDC Manager keep their inlined copies — migrating them is
out of scope per #369's cross-cutting note ("opportunistic later refactor
only"). The downstream G3.6 skeletons import from here.

References
----------

* Task: https://github.com/evoila/meho/issues/841
* Parent initiative: https://github.com/evoila/meho/issues/369
* Parent goal: https://github.com/evoila/meho/issues/214
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_basic_credentials,
)
from meho_backplane.connectors.schemas import AuthModel

__all__ = [
    "CredentialsCache",
    "SessionLoginError",
    "VcfCredentialsLoader",
    "VcfTargetLike",
    "basic_auth_header",
    "is_acceptable_auth_model",
    "load_credentials_from_vault",
    "vcf_session_login",
]

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Basic auth header
# ---------------------------------------------------------------------------


def basic_auth_header(username: str, password: str) -> str:
    """Return the ``Authorization: Basic <b64>`` header value.

    Encodes ``f"{username}:{password}"`` as UTF-8 then base64. Lifted from
    :func:`meho_backplane.connectors.harbor.connector._basic_auth_header`
    verbatim — VCF management-plane products that use HTTP Basic (vROps,
    Fleet, and the *login round-trip* of vRLI) all encode the same way.
    """
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# Auth-model gating
# ---------------------------------------------------------------------------


def is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is ``SHARED_SERVICE_ACCOUNT`` (any form) or ``None``.

    Accepts:

    * the :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` enum member,
    * its string value (``"shared_service_account"``),
    * ``None`` — the "auth_model column not yet populated" sentinel for
      pre-G0.3 targets.

    Anything else returns ``False`` and the caller raises with a clear
    message naming both the target and the requested mode. Same predicate
    the Harbor / NSX / SDDC Manager connectors use today; lifted into this
    module so the four VCF skeletons share one canonical implementation.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


# ---------------------------------------------------------------------------
# Target shape Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VcfTargetLike(Protocol):
    """Minimum target shape the VCF management-plane connectors read.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224) satisfies this unchanged.

    Fields:

    * ``id`` / ``tenant_id`` — the tenant-unique cache key
      (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`).
      Keying the credential / session cache on ``(tenant_id, id)`` instead
      of ``name`` keeps two same-named targets in different tenants from
      collapsing onto one cached credential (#1642).
    * ``name`` — used in audit-log / error messages (no longer a cache key).
    * ``host`` / ``port`` — forwarded to
      :meth:`meho_backplane.connectors.adapters.http.HttpConnector._base_url`.
    * ``secret_ref`` — Vault path the loader resolves to a
      ``{"username": str, "password": str}`` dict. ``str | None`` matches
      the concrete ``Target.secret_ref`` column (nullable) and the
      shared :class:`~meho_backplane.connectors._shared.vault_creds.BasicCredentialsTargetLike`
      the loader forwards to; an unset ``secret_ref`` is rejected with
      a clear error inside the loader, never a bare ``KeyError``.
    * ``auth_model`` — checked at the boundary by
      :func:`is_acceptable_auth_model`.
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


# ---------------------------------------------------------------------------
# Credentials loader Protocol + default stub
# ---------------------------------------------------------------------------


VcfCredentialsLoader = Callable[[VcfTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a ``(target, operator)`` pair to credentials.

Returns ``{"username": ..., "password": ...}``. Injected on connector
construction so:

* Production deploys use the default loader
  (:func:`load_credentials_from_vault`) which forwards the operator's
  validated Keycloak JWT to Vault's JWT/OIDC auth method via the shared
  :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
  helper.
* Unit tests pass a stub accepting ``(target, operator)`` returning canned
  credentials.
* Integration tests pass a loader yielding lab-account credentials.

The ``operator`` parameter carries the full
:class:`~meho_backplane.auth.operator.Operator` (frozen) so the live
loader reads the per-target secret under the operator's identity via
``vault_client_for_operator(operator)`` — the locked decision in
``docs/architecture/connector-auth.md``.

The return type is the looser ``dict[str, str]`` rather than a
``SessionCredentials`` Protocol because Python ``Protocol`` instances aren't
runtime-constructible without a matching concrete class — production code
returns a plain dict and consumers read by key.
"""


async def load_credentials_from_vault(target: VcfTargetLike, operator: Operator) -> dict[str, str]:
    """Default credential loader — live operator-context Vault KV-v2 read.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to
    Vault's JWT/OIDC auth method) and returns the service-account
    ``{"username": ..., "password": ...}`` pair every VCF management-plane
    consumer (vROps, vRLI, Fleet) sends as HTTP Basic on the wire (vROps,
    Fleet) or via session-establish (vRLI). Delegates to the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper (G3.9-T2 #941) so the read, the no-secret-in-logs discipline,
    and the two-phase error contract live in one place every REST
    connector reuses.

    The error contract is the helper's:

    * :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
      — read-phase failure (empty ``operator.raw_jwt`` for a
      system-initiated call, unset ``target.secret_ref``, a malformed
      KV-v2 payload, or a missing ``username``/``password`` field). Never
      a bare ``KeyError``.
    * :class:`~meho_backplane.auth.vault.VaultClientError` (and its
      subclasses) — login-phase failure (Vault unreachable, role denied).
      Propagated verbatim so callers can distinguish login from read.

    A custom :data:`VcfCredentialsLoader` can still be injected via
    ``credentials_loader`` on the consuming connector (tests do exactly
    that); this default is what production targets at rubric State 2
    (``shared_service_account``) use.
    """
    return await load_basic_credentials(target, operator)


# ---------------------------------------------------------------------------
# Credentials cache (load-once-per-target with missing-key error contract)
# ---------------------------------------------------------------------------


class CredentialsCache:
    """Per-target ``{"username": str, "password": str}`` cache.

    Composes into a connector via ``self._creds = CredentialsCache(loader)``
    rather than inheritance — each VCF connector keeps a flat layout and the
    cache is one of several collaborating helpers.

    The lock serialises concurrent first-use callers for the same target;
    subsequent calls take the fast path under the same lock. Missing keys
    in the loader-returned dict raise :exc:`RuntimeError` naming the target
    and the missing key, so operators can identify a misconfigured Vault
    path (a typo in a production loader otherwise surfaces as a confusing
    ``KeyError`` deep inside the consuming code).

    The cache is keyed on the tenant-unique ``(tenant_id, target.id)``
    tuple (:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`,
    #1642) so two same-named targets in different tenants never share a
    cached credential. Production deploys with multiple connector instances
    each get their own cache — there is no process-global cache by design,
    so connector-instance teardown (``aclose``) clears credentials
    immediately.
    """

    def __init__(
        self,
        loader: VcfCredentialsLoader,
        *,
        product_label: str,
    ) -> None:
        """Construct an empty cache.

        ``product_label`` is the connector identifier (e.g. ``"vrops"``,
        ``"vrli"``, ``"vcf-fleet"``) used in error messages and the
        ``structlog`` ``connector=...`` field so operators reading audit
        logs can attribute "credentials loaded" events to a specific
        connector.
        """
        self._loader = loader
        self._product_label = product_label
        self._cache: dict[tuple[str, str], dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def get(self, target: VcfTargetLike, operator: Operator) -> dict[str, str]:
        """Return the cached credentials for *target*, loading on first use.

        ``operator`` is forwarded to the loader so the live default
        (:func:`load_credentials_from_vault`) performs the Vault read
        under the operator's Identity entity via
        :func:`~meho_backplane.auth.vault.vault_client_for_operator`. The
        cache is keyed on the tenant-unique ``(tenant_id, target.id)``
        tuple (#1642) — a single per-target credential pair is shared
        across operators of the same tenant because the
        ``shared_service_account`` auth model authenticates the connector
        with a Vault-sourced service account, not the operator's OIDC
        token. ``per_user`` / ``impersonation`` are explicitly out of
        scope for State 2 (the auth-model boundary in each connector's
        :meth:`auth_headers` rejects them before this method is reached).
        See ``docs/architecture/connector-auth.md`` § "Cache scoping under
        ``shared_service_account``" for the contract.

        Raises :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
        when ``operator.raw_jwt`` is empty -- defense-in-depth fail-closed
        check that mirrors the loader path's pre-Vault guard at
        :func:`~meho_backplane.connectors._shared.vault_creds._resolve_secret_ref`.
        The primary fail-closed gate against empty ``raw_jwt`` is the
        loader's ``vault_client_for_operator`` / ``load_basic_credentials``
        call chain; this cache fast-path enforces the same invariant so a
        future regression in the loader cannot return cached credentials
        to an unauthenticated caller via a cache hit. Each consuming
        connector's :meth:`auth_headers` enforces only the ``auth_model``
        boundary (rejects ``per_user`` / ``impersonation`` under
        ``shared_service_account`` scoping; see the ``auth_model`` block
        above). Raised before the cache lookup so a primed entry from an
        authenticated caller cannot leak to a system-initiated caller.
        See ``docs/architecture/connector-auth.md`` § "Cache scoping under
        ``shared_service_account``" for the contract.

        Raises :exc:`RuntimeError` if the loader returns a dict missing
        ``"username"`` or ``"password"``. The error message names both the
        target and the missing key.
        """
        if not operator.raw_jwt:
            raise VaultCredentialsReadError(
                "operator-context credential read requires an authenticated operator; "
                f"target={target.name!r} has no operator JWT (system-initiated calls "
                "cannot read per-target vendor credentials)"
            )
        cache_key = target_cache_key(target)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
            raw = await self._loader(target, operator)
            for required in ("username", "password"):
                if required not in raw:
                    raise RuntimeError(
                        f"{self._product_label} credentials loader for target "
                        f"{target.name!r} returned a dict missing required key "
                        f"{required!r}; need "
                        "{'username': str, 'password': str}"
                    )
            self._cache[cache_key] = raw
            _log.info(
                "vcf_credentials_loaded",
                connector=self._product_label,
                target=target.name,
                host=target.host,
            )
            return raw

    async def invalidate(self, target: VcfTargetLike) -> None:
        """Drop the cached credentials for *target*.

        Coroutine acquiring ``self._lock`` so the mutation is serialised
        against in-flight :meth:`get` callers for the same target; otherwise
        a concurrent ``invalidate(t)`` between ``get(t)``'s load and its
        cache write would silently re-introduce a stale entry. Used by the
        consuming connector after a credentials rotation event (rare;
        surfaced via a future admin endpoint).
        """
        async with self._lock:
            self._cache.pop(target_cache_key(target), None)

    async def clear(self) -> None:
        """Drop all cached credentials.

        Coroutine acquiring ``self._lock`` for the same reason
        :meth:`invalidate` does — a concurrent ``clear()`` racing an
        in-flight ``get(t)`` could otherwise leave the just-loaded entry
        in the cache after ``clear()`` returned. Called by the consuming
        connector's ``aclose`` so credentials don't outlive the connector
        instance; Harbor's ``aclose`` at
        ``backend/src/meho_backplane/connectors/harbor/connector.py:471-481``
        and SDDC Manager's at
        ``backend/src/meho_backplane/connectors/sddc_manager/connector.py:317-318``
        are the in-tree precedent (each wraps a ``dict.clear`` in
        ``async with self._creds_lock``).
        """
        async with self._lock:
            self._cache.clear()

    @property
    def cached_targets(self) -> frozenset[tuple[str, str]]:
        """Frozen view of the cached ``(tenant_id, id)`` keys (read-only, for tests/audit)."""
        return frozenset(self._cache)


# ---------------------------------------------------------------------------
# Session-login helper (vRLI consumer)
# ---------------------------------------------------------------------------


class SessionLoginError(RuntimeError):
    """Raised when a VCF session-login POST fails or returns an unusable response.

    Wraps the underlying :exc:`httpx.HTTPStatusError` (when the appliance
    returned a non-2xx) or signals a structurally invalid 2xx response
    (missing token in headers or body). Carries the target name so
    operator-facing audit rows can identify which target failed.
    """


SessionTokenExtractor = Callable[[httpx.Response], str | None]
"""Pull the session token out of a :class:`httpx.Response`.

Returns ``None`` if the token isn't present; the helper then raises
:exc:`SessionLoginError`. Consumers pass an extractor matching their
product's response shape — vRLI reads ``response.headers["sessionId"]``,
a hypothetical product reading from the body uses
``lambda r: r.json().get("sessionId")``, etc.
"""


SessionPayloadBuilder = Callable[[str, str], dict[str, Any]]
"""Build the JSON body for the session-login POST from ``(username, password)``.

vRLI's body shape is
``{"username": ..., "password": ..., "provider": "Local"}`` (or
``"ActiveDirectory"``); a different consumer using basic ``{"username":
..., "password": ...}`` passes that. The connector owns the choice — this
helper is just the round-trip.
"""


async def vcf_session_login(
    client: httpx.AsyncClient,
    path: str,
    *,
    username: str,
    password: str,
    target_name: str,
    payload_builder: SessionPayloadBuilder | None = None,
    token_extractor: SessionTokenExtractor,
    request_headers: dict[str, str] | None = None,
) -> str:
    """POST credentials to *path* and return the extracted session token.

    The login round-trip itself — the helper most callers want. The
    surrounding "single-retry-on-401 around *downstream* calls" loop stays
    in the consumer because the downstream paths and the request mechanics
    (form-encoded vs JSON, header vs body) differ per connector.

    Parameters:

    * ``client`` — a pre-pooled :class:`httpx.AsyncClient` bound to the
      target's base URL. The connector owns the client pool; this helper
      doesn't create or close clients.
    * ``path`` — the session-login path (e.g. ``"/api/v2/sessions"`` for
      vRLI).
    * ``username`` / ``password`` — the credentials.
    * ``target_name`` — for the error message + the structlog event.
    * ``payload_builder`` — builds the JSON body. If ``None``, the default
      is ``{"username": username, "password": password}``. vRLI passes a
      builder that adds the ``"provider"`` field.
    * ``token_extractor`` — pulls the token out of the response (header or
      body). Required — no sensible default exists because the four
      products diverge here.
    * ``request_headers`` — optional headers to set on the POST (e.g.
      ``Content-Type: application/json``, ``Accept: application/json``).
      The client's default headers are not modified.

    Raises:

    * :exc:`SessionLoginError` if the POST returns a non-2xx (the cause
      chain carries the underlying :exc:`httpx.HTTPStatusError`) or if
      the 2xx response doesn't yield a non-empty token via
      ``token_extractor``. The error message names the target.

    Returns the non-empty session token string.

    Does **not** raise on transient network errors at this layer — the
    consuming connector's tenacity retry decorator on
    :meth:`HttpConnector._request_json` already covers connection-error
    retries; the session-login POST going through ``client.post`` directly
    bypasses that decorator on purpose so this helper's contract is "one
    attempt, surface the failure cleanly".
    """
    if payload_builder is not None:
        body = payload_builder(username, password)
    else:
        body = {"username": username, "password": password}
    try:
        resp = await client.post(path, json=body, headers=request_headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SessionLoginError(
            f"vcf session-login failed for target {target_name!r}: "
            f"POST {path} returned HTTP {exc.response.status_code}"
        ) from exc
    token = token_extractor(resp)
    if not token:
        raise SessionLoginError(
            f"vcf session-login for target {target_name!r}: "
            f"POST {path} returned {resp.status_code} but token_extractor "
            f"yielded no token"
        )
    _log.info(
        "vcf_session_established",
        target=target_name,
        path=path,
    )
    return token
