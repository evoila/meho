# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Server-side store for in-flight PKCE ``code_verifier`` values.

Initiative #337 (G10.0 Frontend chassis), Task #865 (T4). The PKCE
``code_verifier`` must NOT live in a cookie -- a verifier alongside
the code on the redirect URI would defeat the property PKCE protects
(an intermediate that captures the code would also capture the
verifier). Server-side custody is the whole point.

This module ships :class:`PKCEVerifierStore` -- a per-process dict
keyed on the OAuth ``state`` parameter (which authlib generates per
flow). Entries land in
:func:`meho_backplane.ui.auth.flow.build_authorization_request` and
are popped exactly once by
:func:`meho_backplane.ui.auth.flow.exchange_code_for_tokens` -- a
second callback request with the same ``state`` finds no verifier
and the flow fails-closed.

Concurrency
-----------

The store is guarded by an :class:`asyncio.Lock`. Single-worker
uvicorn keeps the dict simple; a multi-worker deploy would need a
Redis-backed shared custody surface (tracked alongside the JWKS-cache
upgrade under Initiative #337).

Expired-entry sweep
-------------------

Every :meth:`put` call also drops entries older than
:data:`AUTHORIZATION_FLOW_TTL_SECONDS`. The sweep is bounded by the
map's size, runs under the lock, and amortises over the login
cadence (one drop pass per fresh login -- realistic under any
operator-facing traffic shape). No background task is needed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final

__all__ = [
    "AUTHORIZATION_FLOW_TTL_SECONDS",
    "PKCEVerifierStore",
    "PendingFlow",
    "get_verifier_store",
    "reset_verifier_store_for_testing",
]


#: Maximum age (in seconds) of a pending authorization-code flow before
#: the in-memory PKCE verifier is reaped. 10 minutes is the published
#: Keycloak default ``authentication-session-lifespan`` -- a verifier
#: older than that cannot complete the flow on the IdP side anyway, so
#: we drop it. Tests override to sub-second values to exercise the
#: expiry path without sleeping.
AUTHORIZATION_FLOW_TTL_SECONDS: Final[int] = 600


@dataclass(frozen=True)
class PendingFlow:
    """One unfinished authorization-code flow.

    Holds the PKCE ``code_verifier`` (server-side custody; never
    written to a cookie) and the originally-requested URL the
    callback should redirect to on success. ``created_at`` is the
    monotonic-clock value at registration so the reaper can drop
    abandoned entries without depending on wall-clock skew.
    """

    code_verifier: str
    return_to: str
    created_at: float


class PKCEVerifierStore:
    """Server-side store for in-flight PKCE ``code_verifier`` values.

    Keyed on ``state`` (the per-flow CSRF token authlib generates in
    :meth:`AsyncOAuth2Client.create_authorization_url`). Entries land
    in :func:`meho_backplane.ui.auth.flow.build_authorization_request`
    and are popped exactly once by
    :func:`meho_backplane.ui.auth.flow.exchange_code_for_tokens` -- a
    second callback request with the same ``state`` finds no verifier
    and the flow fails-closed.

    Tests reach :func:`reset_verifier_store_for_testing` between cases
    to clear state.
    """

    def __init__(self) -> None:
        self._flows: dict[str, PendingFlow] = {}
        self._lock = asyncio.Lock()

    async def put(self, state: str, *, code_verifier: str, return_to: str) -> None:
        """Register a fresh flow.

        Runs a bounded expired-entry sweep before the insert so the
        map never grows beyond the live-flow set.
        """
        now = time.monotonic()
        async with self._lock:
            # Reap stale entries first. Bounded by the current size of
            # the map; one pass per fresh login under any traffic shape.
            stale = [
                key
                for key, flow in self._flows.items()
                if now - flow.created_at > AUTHORIZATION_FLOW_TTL_SECONDS
            ]
            for key in stale:
                del self._flows[key]
            self._flows[state] = PendingFlow(
                code_verifier=code_verifier,
                return_to=return_to,
                created_at=now,
            )

    async def pop(self, state: str) -> PendingFlow | None:
        """Atomically remove and return the flow for *state*, or ``None``.

        Returns ``None`` when the state is unknown, already consumed,
        or past :data:`AUTHORIZATION_FLOW_TTL_SECONDS`. Callers
        translate ``None`` into a 400 -- the operator-facing shape is
        intentionally indistinguishable across the three causes
        (cookie/state replay, deep-link bookmark of an expired
        callback URL, ordinary browser-window race).
        """
        now = time.monotonic()
        async with self._lock:
            flow = self._flows.pop(state, None)
            if flow is None:
                return None
            if now - flow.created_at > AUTHORIZATION_FLOW_TTL_SECONDS:
                return None
            return flow

    def size(self) -> int:
        """Return the current number of pending flows.

        Test-only helper -- never used on the request hot path. The
        size is not held under the lock; readers tolerate the
        eventual-consistency view because the value is diagnostic, not
        load-bearing.
        """
        return len(self._flows)


#: Process-wide verifier store. One per backplane worker; reset under
#: test via :func:`reset_verifier_store_for_testing`.
_VERIFIER_STORE: PKCEVerifierStore = PKCEVerifierStore()


def get_verifier_store() -> PKCEVerifierStore:
    """Return the process-wide :class:`PKCEVerifierStore`.

    A function (rather than re-exporting :data:`_VERIFIER_STORE` as a
    module attribute) so test seams that swap the store can do so with
    a single :func:`monkeypatch.setattr` against this lookup.
    """
    return _VERIFIER_STORE


def reset_verifier_store_for_testing() -> None:
    """Replace the process-wide store with a fresh instance.

    Test-only -- production never resets the store under a running
    process. Tests that exercise multiple end-to-end flows under
    :class:`pytest.MonkeyPatch` call this between cases so a verifier
    from a prior flow never leaks into the current one.
    """
    global _VERIFIER_STORE
    _VERIFIER_STORE = PKCEVerifierStore()
