# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-work-item short-lived, response-wrapped credential brokering (#3191).

Mechanism 3 of the satellite write-path composition
(:doc:`docs/decisions/satellite-write-path.md`, design §3): the write tier
never hands a fenced, less-trusted runner a **standing broad** vendor
credential (the T3 worst case). Instead, for one authorised work item, the
**centre** brokers a short-lived, **single-target-scoped** credential and
ships only a **single-use unwrap token** (Vault response-wrapping) to the
runner; the runner **unwraps just-in-time at execution**, uses the
credential for that one op, and never persists it.

This module **extends the backend-agnostic credential seam**
(:mod:`~meho_backplane.connectors._shared.credential_backend`,
:mod:`~meho_backplane.connectors._shared.vault_creds`) rather than forking a
parallel path — it registers a new backend under the ``wrapped`` scheme, so
a connector handler resolves a wrapped credential through the same
:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
call it already uses, unchanged.

The two halves
==============

**Centre broker** — :func:`broker_wrapped_credential`. Reads *one* target's
secret under the authorising operator (present at central mint), Vault-
response-wraps that single payload with a TTL bounded to the capability's
``expires_at``, and returns only the ``wrapped:<token>`` reference. The
credential value never leaves the centre.

**Runner backend** — :class:`WrappedCredentialBackend`. Registered under
kind ``wrapped``. At execution the runner presents the **single-use wrapping
token itself** to Vault's unwrap endpoint — *not* the acting operator's JWT
— so the DB-free runner resolves the credential without the empty-``raw_jwt``
operator identity the read path stumbles on (design §1.4). The unwrap is an
**outbound** dial (push-only preserved, #2877). Vault consumes the token on
the first unwrap, so a replay / redelivery unwraps a second time and Vault
refuses — the credential fails closed, expired or already-consumed.

No standing credential
======================

The runner holds only the ephemeral, single-use, TTL-bounded wrapping token,
delivered per work item over its runner-principal-authenticated poll channel.
It has no standing Vault read policy. :func:`screen_remote_write_credential`
is the edge fail-closed guard: a ``remote-write`` item whose target carries a
standing/broad ``secret_ref`` (anything but a ``wrapped:`` token) is refused,
so a config that would grant a standing runner credential fails closed.

Vault dependency
================

Response-wrapping is a Vault feature, so wrapped brokering requires Vault
(``VAULT_ADDR`` on the centre for the wrap; ``MEHO_RUNNER_VAULT_ADDR`` on the
runner for the outbound unwrap). A Vault-free (``gsm``) deployment has no
wrapped-brokering path today — an additive future concern, not built
speculatively here.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import hvac
import hvac.exceptions
import requests.exceptions
import structlog

from meho_backplane.auth.vault import vault_client_for_operator
from meho_backplane.connectors._shared.credential_backend import (
    CredentialsReadError,
    register_credential_backend,
)
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_vault_secret_data,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors._shared.vault_creds import BasicCredentialsTargetLike

__all__ = [
    "WRAPPED_CREDENTIAL_SCHEME",
    "WrappedCredential",
    "WrappedCredentialBackend",
    "WrappedCredentialError",
    "broker_wrapped_credential",
    "is_wrapped_credential_ref",
    "screen_remote_write_credential",
]

_log = structlog.get_logger(__name__)

#: The credential-seam scheme a per-work-item single-use wrapped credential
#: rides on: ``wrapped:<vault-wrapping-token>``. Parsed off the target's
#: ``secret_ref`` by the seam dispatcher exactly like ``vault:`` / ``gsm:``.
WRAPPED_CREDENTIAL_SCHEME = "wrapped"

#: The runner's outbound Vault address for the just-in-time unwrap. Read from
#: the environment (not the chassis ``Settings`` the runner cannot build);
#: fail-closed when unset (the runner needs a Vault to unwrap against).
_UNWRAP_VAULT_ADDR_ENV = "MEHO_RUNNER_VAULT_ADDR"
#: Optional Vault namespace (Enterprise) for the unwrap dial.
_UNWRAP_VAULT_NAMESPACE_ENV = "MEHO_RUNNER_VAULT_NAMESPACE"
#: Optional per-call timeout (seconds) for the unwrap dial.
_UNWRAP_VAULT_TIMEOUT_ENV = "MEHO_RUNNER_VAULT_TIMEOUT_SECONDS"
_DEFAULT_UNWRAP_TIMEOUT_SECONDS = 10.0


class WrappedCredentialError(CredentialsReadError):
    """Brokering- or unwrapping-phase failure for a wrapped credential.

    Subclasses the backend-neutral
    :class:`~meho_backplane.connectors._shared.credential_backend.CredentialsReadError`
    (#2642) so a caller that means "the credential could not be read"
    catches the base. Raised when the capability expiry gives no positive
    TTL, when Vault response-wrapping returns no token, or when the
    just-in-time unwrap fails (the single-use token is expired, already
    consumed, or invalid, or the outbound Vault dial failed). Never echoes a
    credential value.
    """


@dataclass(frozen=True)
class WrappedCredential:
    """The centre-brokered handle shipped to the runner in place of a secret.

    :attr:`unwrap_ref` is the ``wrapped:<token>`` reference set as the
    runner-bound target descriptor's ``secret_ref`` — the only credential
    artifact that touches the runner's disk. :attr:`expires_at` bounds the
    wrapping token's life to at most the capability's expiry;
    :attr:`wrap_ttl_seconds` is that bound in seconds.
    """

    unwrap_ref: str
    expires_at: datetime
    wrap_ttl_seconds: int


def is_wrapped_credential_ref(secret_ref: str | None) -> bool:
    """Return ``True`` when *secret_ref* is a ``wrapped:<token>`` reference."""
    if not secret_ref:
        return False
    kind, sep, token = secret_ref.partition(":")
    return kind == WRAPPED_CREDENTIAL_SCHEME and bool(sep) and bool(token)


def _bounded_wrap_ttl(
    capability_expires_at: datetime, requested_ttl_seconds: int | None, *, now: datetime
) -> int:
    """Return the wrap TTL, bounded so credential TTL ≤ capability ``expires_at``.

    The wrapping token must never outlive the capability it is bound to, so
    the TTL is capped at the headroom to ``capability_expires_at``. A
    caller-*requested* TTL only ever shortens it further. A capability that
    is already expired (non-positive headroom) cannot broker a credential →
    :class:`WrappedCredentialError`.
    """
    headroom = int((capability_expires_at - now).total_seconds())
    if headroom <= 0:
        raise WrappedCredentialError(
            "cannot broker a wrapped credential: capability expiry "
            f"{capability_expires_at.isoformat()} is not in the future"
        )
    if requested_ttl_seconds is None:
        return headroom
    if requested_ttl_seconds <= 0:
        raise WrappedCredentialError(
            f"requested wrap TTL must be positive, got {requested_ttl_seconds}"
        )
    return min(requested_ttl_seconds, headroom)


def _extract_wrap_token(response: object) -> str:
    """Pull the wrapping token out of a ``sys/wrapping/wrap`` response."""
    wrap_info = response.get("wrap_info") if isinstance(response, dict) else None
    token = wrap_info.get("token") if isinstance(wrap_info, dict) else None
    if not isinstance(token, str) or not token:
        raise WrappedCredentialError(
            "vault response-wrap returned no wrapping token; cannot broker a wrapped credential"
        )
    return token


async def _wrap_payload(operator: Operator, payload: dict[str, object], *, ttl_seconds: int) -> str:
    """Response-wrap *payload* under *operator*'s Vault identity, return the token.

    Opens a fresh operator-context Vault client and POSTs the payload to
    ``sys/wrapping/wrap`` with ``X-Vault-Wrap-TTL: ttl_seconds`` (hvac's
    ``client.sys.wrap``), off the event loop (hvac is synchronous).
    """
    async with vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.sys.wrap, payload=payload, ttl=ttl_seconds)
    return _extract_wrap_token(response)


async def broker_wrapped_credential(
    target: BasicCredentialsTargetLike,
    operator: Operator,
    *,
    capability_expires_at: datetime,
    requested_ttl_seconds: int | None = None,
    mount: str = DEFAULT_KV_MOUNT,
    now: datetime | None = None,
) -> WrappedCredential:
    """Broker a per-work-item single-use wrapped credential for *target*.

    The **centre** side of mechanism 3, called at authorised remote-write
    mint (where the operator is present with a real JWT). It:

    1. Reads *target*'s secret under *operator*'s identity through the shared
       credential seam
       (:func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`)
       — exactly one target's fields, so the wrapped credential is
       **single-target-scoped** by construction.
    2. Vault-response-wraps that one payload with a TTL bounded so the
       credential never outlives the capability
       (:func:`_bounded_wrap_ttl`) — credential TTL ≤ ``capability_expires_at``.
    3. Returns only the ``wrapped:<token>`` reference; the credential value
       never leaves the centre.

    The seam wiring — set the returned :attr:`WrappedCredential.unwrap_ref`
    as the runner-bound target descriptor's ``secret_ref`` at mint — is the
    caller's (the approval-bound mint, #3189). This function is the seam.

    Raises:
        WrappedCredentialError: the capability is already expired, a
            non-positive TTL was requested, or Vault returned no wrap token.
        meho_backplane.connectors._shared.credential_backend.CredentialsReadError:
            the underlying read of *target*'s secret failed.
        meho_backplane.auth.vault.VaultClientError: Vault is unreachable /
            unconfigured / denied the role (login-phase failure).
    """
    moment = now or datetime.now(UTC)
    ttl = _bounded_wrap_ttl(capability_expires_at, requested_ttl_seconds, now=moment)
    secret_data = await load_vault_secret_data(target, operator, mount=mount)
    token = await _wrap_payload(operator, dict(secret_data), ttl_seconds=ttl)

    _log.info(
        "wrapped_credential_brokered",
        target=target.name,
        wrap_ttl_seconds=ttl,
        fields=sorted(secret_data.keys()),
    )
    return WrappedCredential(
        unwrap_ref=f"{WRAPPED_CREDENTIAL_SCHEME}:{token}",
        expires_at=moment + timedelta(seconds=ttl),
        wrap_ttl_seconds=ttl,
    )


def _unwrap_timeout() -> float:
    """Return the unwrap dial timeout (seconds) from the environment."""
    raw = os.environ.get(_UNWRAP_VAULT_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_UNWRAP_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError as exc:
        raise WrappedCredentialError(
            f"{_UNWRAP_VAULT_TIMEOUT_ENV} must be a number, got {raw!r}"
        ) from exc


def _build_unwrap_client(wrapping_token: str) -> hvac.Client:
    """Construct a Vault client that authenticates *as the wrapping token*.

    The runner presents the single-use wrapping token itself (self-
    authorising unwrap) — no standing runner Vault credential, and no acting-
    operator JWT. The Vault address is read from the runner's environment
    (``MEHO_RUNNER_VAULT_ADDR``), fail-closed when unset.
    """
    addr = os.environ.get(_UNWRAP_VAULT_ADDR_ENV)
    if not addr or not addr.strip():
        raise WrappedCredentialError(
            f"cannot unwrap a wrapped credential: {_UNWRAP_VAULT_ADDR_ENV} is not "
            "set; the satellite runner needs an outbound Vault address to unwrap "
            "per-work-item credentials"
        )
    namespace = os.environ.get(_UNWRAP_VAULT_NAMESPACE_ENV) or None
    return hvac.Client(
        url=addr.strip().rstrip("/"),
        namespace=namespace,
        timeout=_unwrap_timeout(),
        token=wrapping_token,
    )


def _unwrap_payload(response: object, *, target_name: str) -> dict[str, object]:
    """Pull the secret-field dict out of a ``sys/wrapping/unwrap`` response."""
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise WrappedCredentialError(
            f"unwrap for target {target_name!r} returned a malformed payload: "
            "expected a 'data' object holding the wrapped secret fields"
        )
    return data


class WrappedCredentialBackend:
    """Runner-side unwrap of a per-work-item single-use wrapped credential.

    Registered under kind ``wrapped`` on the credential seam, so a connector
    handler resolving a ``wrapped:<token>`` ``secret_ref`` dispatches here.
    It presents the wrapping token itself to Vault's unwrap endpoint — the
    ``operator`` argument (empty ``raw_jwt`` on the runner) is deliberately
    **ignored**, which is how mechanism 3 closes the empty-JWT gap (§1.4):
    the runner never needs the acting operator's Vault identity.

    Vault consumes the wrapping token on the first unwrap, so the single-use
    property is enforced by Vault, not by local state: a second unwrap of the
    same token (a replay or spool redelivery) fails closed with
    :class:`WrappedCredentialError`.
    """

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str = DEFAULT_KV_MOUNT,
    ) -> dict[str, object]:
        """Unwrap *secret_ref* (the wrapping token) outbound, just-in-time.

        *secret_ref* is the wrapping token — the ``wrapped:`` scheme is already
        stripped by the seam dispatcher. Builds an outbound Vault client that
        authenticates as the token and POSTs to ``sys/wrapping/unwrap`` off the
        event loop, then returns the unwrapped secret-field dict.

        Raises :class:`WrappedCredentialError` when the token is expired,
        already consumed, or invalid, when the outbound dial fails, or when the
        unwrapped payload is malformed.
        """
        client = _build_unwrap_client(secret_ref)
        try:
            response = await asyncio.to_thread(client.sys.unwrap)
        except (hvac.exceptions.VaultError, requests.exceptions.RequestException) as exc:
            raise WrappedCredentialError(
                f"unwrap failed for target {target_name!r}: the single-use wrapped "
                "credential is expired, already consumed, or invalid, or Vault was "
                f"unreachable ({type(exc).__name__})"
            ) from exc
        return _unwrap_payload(response, target_name=target_name)


def screen_remote_write_credential(target_descriptor: object) -> str | None:
    """Edge fail-closed guard: a remote-write item must be wrapped, or refuse.

    Returns a refusal reason string when *target_descriptor* does not carry a
    per-work-item single-use ``wrapped:`` credential — a missing target, or a
    standing/broad ``secret_ref`` (schemeless, ``vault:``, ``gsm:``, …) that
    would let the runner perform a standing read. Returns ``None`` when the
    descriptor carries a wrapped credential and the item may proceed.

    This is mechanism 3's edge check that "standing broad runner credentials
    are rejected for the write tier": a config that ships a standing
    ``secret_ref`` to a ``remote-write`` op fails closed here. It composes
    with (does not replace) the composed remote-write gate
    (:func:`~meho_backplane.runner.satellite_tier.evaluate_remote_write_gate`,
    owned by #3189) — the edge screen calls both.
    """
    secret_ref = getattr(target_descriptor, "secret_ref", None)
    if is_wrapped_credential_ref(secret_ref):
        return None
    return (
        "remote-write item refused: it must carry a per-work-item single-use "
        f"wrapped credential ({WRAPPED_CREDENTIAL_SCHEME}:<token>), not a standing "
        "broad credential; no standing runner credential is permitted on the "
        "write tier"
    )


#: The wrapped backend is stateless (it builds a fresh client per unwrap), so
#: one shared instance serves every runner unwrap. Registered at import under
#: ``wrapped``; :mod:`meho_backplane.connectors._shared` imports this module so
#: the kind is present before any credential resolution (mould: ``gsm_creds``).
register_credential_backend(WRAPPED_CREDENTIAL_SCHEME, WrappedCredentialBackend())
