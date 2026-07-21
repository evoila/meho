# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The in-process check-runner's own service-principal identity (#2642).

Background dispatch has no operator. The sensor check-runner
(:mod:`meho_backplane.checks.runner`) builds a synthetic per-tenant
:class:`~meho_backplane.auth.operator.Operator` and, until this module
existed, gave it ``raw_jwt=""`` — no bearer token at all. Every credential
backend that resolves a target secret under an *operator* identity then
fails closed, so a Sensor pointed at a credentialed target could never
evaluate: the same ``(connector_id, op_id, target)`` triple that succeeds
through ``POST /api/v1/operations/call`` (which carries the caller's
Keycloak JWT) returned a credential-read error on the runner's own tick.

On a ``credentialBackend=gsm`` deploy using per-operator Workload Identity
Federation, that is total: the GSM backend exchanges ``operator.raw_jwt``
at ``sts.googleapis.com``, and an empty subject token cannot be exchanged.
With no ambient GCP identity on the pod (on-prem Kubernetes: no GKE
Workload Identity, no mounted SA) there is no fallback either, so **no**
credentialed Sensor evaluates.

The identity
============

Configure a confidential Keycloak client for the backplane's background
dispatch (``CHECK_RUNNER_CLIENT_ID`` / ``CHECK_RUNNER_CLIENT_SECRET``) and
this module mints an access token for it with the OAuth
``client_credentials`` grant — the same primitive the agent-principal path
uses (:func:`~meho_backplane.auth.agent_token.get_client_credentials_grant`).
The runner presents that token as its synthetic operator's bearer token, so
scheduled evaluations carry a real, attributable identity end-to-end: GCP's
audit log names the check-runner principal rather than rejecting an empty
subject token, and a Vault install can bind the same principal to a JWT
role if it wants background dispatch to reach Vault secrets.

No key material enters the flow — the WIF model is intact, the runner
simply becomes a first-class OAuth client instead of an anonymous one.

Not the satellite-runner principal (#2415)
==========================================

Initiative #2415's runner principals (``runner:<name>``,
``principal_kind=runner``, :mod:`meho_backplane.auth.runner_principals`)
authenticate an **external** satellite runner *to* MEHO, and are registered
per runner through a REST lifecycle. This is the mirror image: one
deployment-level client that authenticates MEHO's **own** in-process
background dispatch *outward*. They share the ``client_credentials``
mechanism and nothing else.

Opt-in, no behaviour change when unset
======================================

Both settings empty (the default) ⇒ :func:`check_runner_jwt` returns ``""``
and the runner behaves exactly as before. A deployment that wants
background dispatch to resolve credentials opts in; nothing is minted, and
Keycloak is never contacted, otherwise.

Caching
=======

The token is cached in-process until shortly before it expires
(:data:`_EXPIRY_SKEW_SECONDS`). A check-runner tick fans out one evaluation
per due Sensor, so minting per evaluation would put the token endpoint on
the hot path of every dispatch. Refresh is guarded by an
:class:`asyncio.Lock` so a burst of concurrent evaluations after expiry
mints once rather than N times, mirroring the JWKS-cache discipline in
:mod:`meho_backplane.auth.jwt`.

Fail-soft
=========

A Keycloak blip must not crash the runner loop. A failed mint logs
``check_runner_token_failed`` (never the secret, never the token) and
returns ``""``, which degrades to exactly the pre-#2642 behaviour: the
dispatch fails its credential read and the Sensor reads ``unknown``. The
next evaluation retries.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from meho_backplane.auth.agent_token import AgentTokenError, get_client_credentials_grant
from meho_backplane.settings import get_settings

__all__ = ["check_runner_jwt", "reset_check_runner_token_cache"]

#: Seconds of headroom subtracted from the token's declared lifetime. A
#: token that expires mid-dispatch is indistinguishable from a
#: misconfiguration at the far end, so refresh early.
_EXPIRY_SKEW_SECONDS: float = 30.0

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _CachedToken:
    """A minted token and the monotonic deadline after which it is stale."""

    token: str
    good_until: float


_cache: _CachedToken | None = None
_refresh_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    """Return the module refresh lock, created on the running loop.

    Built lazily rather than at import: an :class:`asyncio.Lock` created at
    import time under one event loop is not usable from another, which is
    exactly what a test suite creating a fresh loop per test would hit.
    """
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def reset_check_runner_token_cache() -> None:
    """Drop the cached token (and the lock). For tests and lifespan restarts."""
    global _cache, _refresh_lock
    _cache = None
    _refresh_lock = None


async def check_runner_jwt() -> str:
    """Return the check-runner principal's access token, or ``""``.

    Empty when the principal is unconfigured (either setting blank) or when
    minting failed — both mean "background dispatch has no operator
    identity", which every credential backend already handles fail-closed.
    Never raises.
    """
    global _cache
    settings = get_settings()
    client_id = settings.check_runner_client_id
    client_secret = settings.check_runner_client_secret
    if not client_id or not client_secret:
        return ""

    cached = _cache
    now = time.monotonic()
    if cached is not None and now < cached.good_until:
        return cached.token

    async with _lock():
        # Re-read under the lock: a concurrent evaluation may have minted
        # while this one waited.
        cached = _cache
        now = time.monotonic()
        if cached is not None and now < cached.good_until:
            return cached.token
        try:
            # Request the backplane audience, mirroring the agent-principal
            # call site. Keycloak honours it only when the client carries an
            # audience mapper (the RFC 8707 param alone is ignored), so the
            # token's real ``aud`` is realm-side configuration — which is
            # also what a WIF provider's allowed-audience list must match.
            grant = await get_client_credentials_grant(
                issuer_url=str(settings.keycloak_issuer_url),
                client_id=client_id,
                client_secret=client_secret,
                audience=settings.keycloak_audience,
            )
        except AgentTokenError as exc:
            # Non-secret attribution only: the client id is operator config,
            # the failure code is a stable enum. Never the secret, never a
            # token, never the response body.
            _log.warning(
                "check_runner_token_failed",
                client_id=client_id,
                code=exc.code,
            )
            return ""
        _cache = _CachedToken(
            token=grant.access_token,
            good_until=time.monotonic() + max(grant.expires_in - _EXPIRY_SKEW_SECONDS, 0.0),
        )
        return grant.access_token
