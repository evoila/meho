# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading + target shape for the vcf-fleet connector.

The hand-rolled
:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`
authenticates with HTTP Basic against Fleet's **local LCM user store** —
typically the ``admin@local`` account. There is no SSO federation; the
stored username is sent verbatim in the Basic auth header (consumer
wrapper ``scripts/vcf-fleet.sh`` header, 2026-05-21: *"HTTP Basic against
Fleet's own LCM-local user store (typical username `admin@local`). Fleet
does NOT federate with vCenter SSO out of the box."*).

The connector reuses the shared scaffolding in
:mod:`meho_backplane.connectors._shared.vcf_auth` (#841):

* :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
  for the load-once-per-target ``{"username": str, "password": str}``
  dict with the missing-key → :exc:`RuntimeError` contract.
* :func:`~meho_backplane.connectors._shared.vcf_auth.basic_auth_header`
  for the ``Authorization: Basic`` header value.
* :func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
  for the ``shared_service_account`` / ``None`` gate.

The :class:`VcfFleetTargetLike` Protocol captures the minimum target
shape the connector reads: ``name`` (per-target credential cache key),
``host`` / ``port`` (forwarded to :meth:`HttpConnector._base_url`),
``secret_ref`` (the Vault path the loader resolves), and ``auth_model``
(checked at the boundary). No ``sso_realm`` field — Fleet's Basic auth
header carries ``username:password`` directly with no realm suffix
(verified against ``scripts/vcf-fleet.sh`` — the wrapper passes
``-u "${FLEET_USERNAME}:${FLEET_PASSWORD}"`` with no additional
realm/domain decoration).

The default loader, :func:`load_credentials_from_vault`, raises
:exc:`NotImplementedError` until the operator-context Vault read lands.
Production deploys and tests override the loader on construction (same
posture as the Harbor / NSX / SDDC Manager / VCF Automation precedents
— all four currently raise the same shape under open Goal #214).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "SessionCredentials",
    "VcfFleetCredentialsLoader",
    "VcfFleetTargetLike",
    "load_credentials_from_vault",
]


class SessionCredentials(Protocol):
    """The dict shape :class:`VcfFleetCredentialsLoader` returns.

    Captured as a Protocol so the type checker can flag a loader that
    forgets a key. The two values map to the Basic-auth components the
    connector sends on every Fleet API request — no session token is
    established (HTTP Basic per request, same shape as the Harbor
    precedent).
    """

    username: str
    password: str


@runtime_checkable
class VcfFleetTargetLike(Protocol):
    """Minimum target shape :class:`VcfFleetConnector` reads.

    Structural Protocol — the concrete ``Target`` model in
    :mod:`meho_backplane.targets` (G0.3 #224 — closed) satisfies this
    Protocol unchanged. ``auth_model`` is checked at the boundary so a
    target tagged ``per_user`` or ``impersonation`` raises a clear error
    rather than silently authenticating as the shared service account.

    ``secret_ref`` is the Vault path the loader resolves to a
    :class:`SessionCredentials`-shaped dict. ``port`` is optional —
    Fleet defaults to 443 and :meth:`HttpConnector._base_url` already
    handles the ``port is None or 443`` case correctly.

    No ``sso_realm`` field — Fleet's local user store accepts
    ``admin@local`` as the literal Basic-auth username; no realm suffix
    is appended by the connector.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None


VcfFleetCredentialsLoader = Callable[[VcfFleetTargetLike], Awaitable[dict[str, str]]]
"""Async callable resolving a target to ``{"username": ..., "password": ...}``.

The connector's
:class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
invokes the loader on first ``auth_headers`` call per target and caches
the resulting dict under ``target.name``. The return type is the looser
``dict[str, str]`` (not :class:`SessionCredentials`) because Python
:class:`Protocol` instances aren't runtime-constructible without a
matching class — production code returns a plain dict and the connector
reads ``creds["username"]`` / ``creds["password"]`` by key.
"""


async def load_credentials_from_vault(
    target: VcfFleetTargetLike,
) -> dict[str, str]:
    """Default credential loader — Vault read by ``target.secret_ref``.

    Deliberate stub: the operator-context per-target Vault credential
    read is not yet wired for the VCF Fleet connector. Raising
    :exc:`NotImplementedError` here keeps the wiring shape stable — a
    production caller without an override receives a clear error rather
    than a silent fallback or a hallucinated credential pair. The
    supported workaround is to inject a custom ``credentials_loader``
    on ``VcfFleetConnector`` at construction time. The live read is
    tracked under the open Goal #214 (Connector parity).

    Once the read lands, this function becomes the live implementation
    that reads the ``vcf-fleet/<target.name>`` Vault path (or
    ``target.secret_ref`` directly when set) and returns the parsed
    ``{"username": ..., "password": ...}`` dict.
    """
    raise NotImplementedError(
        "load_credentials_from_vault is a deliberate stub: the operator-context "
        "per-target Vault credential read is not yet wired for the VCF Fleet "
        f"connector; target={target.name!r} secret_ref={target.secret_ref!r}. "
        "Workaround: inject a custom credentials_loader on VcfFleetConnector. "
        "Tracked under open Goal #214 (Connector parity): "
        "https://github.com/evoila/meho/issues/214"
    )
