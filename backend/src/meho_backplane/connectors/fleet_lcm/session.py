# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential loading + target shape for the modern ``fleet-lcm`` connector.

The hand-rolled
:class:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector`
authenticates against the VCF 9 **Fleet LCM Service** API
(``https://vcf.broadcom.com/fleet-lcm`` → ``/v1/*``). Per the pinned
``fleet-lcm-9.0/fleet-lcm-openapi.yaml`` ``securitySchemes``, the global
scheme is ``bearerToken`` (HTTP Bearer), with ``basicAuth`` (HTTP Basic)
defined as an alternative — a genuine departure from the legacy
``fleet-rest`` impl, which is HTTP-Basic-only against the vRSLCM
``/lcm/*`` surface.

This module mirrors the legacy ``vcf_fleet.session`` shape and reuses the
shared :mod:`meho_backplane.connectors._shared.vault_creds` scaffolding so
the two impls share one credential-loading contract:

* :data:`FleetLcmTargetLike` — the minimum target shape the connector
  reads. Aliased to the shared
  :class:`~meho_backplane.connectors._shared.vcf_auth.VcfTargetLike`
  Protocol (``name`` / ``host`` / ``port`` / ``secret_ref`` /
  ``auth_model`` + the ``id`` / ``tenant_id`` cache-key pair); the
  concrete ``Target`` model in :mod:`meho_backplane.targets` satisfies it
  structurally.
* :data:`FleetLcmCredentialsLoader` — the async ``(target, operator) ->
  dict[str, str]`` loader type. Injectable on connector construction so
  unit tests pass a stub, integration tests pass a lab-account loader,
  and production uses :func:`load_credentials_from_vault`.
* :func:`load_credentials_from_vault` — the fleet-lcm default loader: a
  Vault KV-v2 read that surfaces the ``{username, password}`` pair plus an
  **optional** ``token`` (the token-provisioning seam, below).

The Bearer token-provisioning seam (#3047)
==========================================

The connector's
:meth:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector.auth_headers`
emits ``Authorization: Bearer <token>`` when the loaded credentials carry
a non-empty ``"token"`` (the spec's primary ``bearerToken`` scheme), and
otherwise falls back to ``Authorization: Basic <b64>`` off the
username/password pair (the spec's ``basicAuth`` alternative, which the
appliance also accepts).

:func:`load_credentials_from_vault` is what makes the Bearer path
reachable in production: it reads the raw KV-v2 payload via
:func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`
and surfaces a ``token`` field **when the operator has staged one**,
alongside the always-required username/password pair. So an operator opts
a target into Bearer simply by adding a ``token`` to its Vault secret — no
target-row / ``auth_model`` change. This mirrors the field-discriminator
loaders the proxmox (``token_id`` + ``token_secret`` vs ``username`` +
``password``) and github (App-installation vs PAT) connectors ship, which
also pick the upstream credential protocol by inspecting which fields the
operator stored rather than surfacing it on ``auth_model``.

**Seam — the live ``basicAuth`` → mint-``bearerToken`` exchange is
deferred.** This loader surfaces a *pre-staged* Vault token only; it does
**not** perform the alternative provisioning path where the connector
POSTs ``basicAuth`` to the appliance's token endpoint to mint a
short-lived ``bearerToken`` on the fly (the github-App JWT→installation-
token shape). That live exchange — and confirming whether the 9.x
appliance is Bearer-only or also honours ``basicAuth`` — is the #3047
live-verify follow-up, gated on a reachable Fleet LCM appliance
(#1002 / #995). Until it lands, a target with no staged token
authenticates with the Basic alternative.
"""

from __future__ import annotations

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    VcfTargetLike,
)

__all__ = [
    "FleetLcmCredentialsLoader",
    "FleetLcmTargetLike",
    "load_credentials_from_vault",
]

#: Minimum target shape :class:`FleetLcmConnector` reads. Aliased to the
#: shared VCF-family Protocol — the modern impl reads exactly the same
#: fields the legacy impl does (``name`` / ``host`` / ``port`` /
#: ``secret_ref`` / ``auth_model`` + the ``(tenant_id, id)`` cache key),
#: so a bespoke duplicate Protocol would only drift.
FleetLcmTargetLike = VcfTargetLike

#: Async ``(target, operator) -> dict[str, str]`` credential loader.
#: Aliased to the shared VCF loader type; re-exported under a
#: fleet-lcm-flavoured name so ``FleetLcmConnector(credentials_loader=...)``
#: reads cohesively without exposing the shared module at the boundary.
FleetLcmCredentialsLoader = VcfCredentialsLoader

#: KV-v2 secret field names the fleet-lcm loader reads. ``username`` +
#: ``password`` are **required** (the appliance accepts ``basicAuth`` and the
#: shared ``CredentialsCache`` contract requires the pair); ``token`` is
#: **optional** and, when present, activates the spec's primary
#: ``bearerToken`` scheme. Kept as module constants so the loader, the
#: connector, and the tests share one source of truth for what an operator
#: stores under ``target.secret_ref``.
_USERNAME_FIELD = "username"
_PASSWORD_FIELD = "password"
_TOKEN_FIELD = "token"


async def load_credentials_from_vault(
    target: FleetLcmTargetLike,
    operator: Operator,
) -> dict[str, str]:
    """Default fleet-lcm loader — basic pair + optional pre-staged Bearer token.

    Reads ``target.secret_ref`` as a KV-v2 secret **under the operator's
    identity** (the operator's validated Keycloak JWT is forwarded to
    Vault's JWT/OIDC auth method) via
    :func:`~meho_backplane.connectors._shared.vault_creds.load_vault_secret_data`,
    then surfaces:

    * the always-required ``{username, password}`` pair (the ``basicAuth``
      alternative the appliance accepts, and the shared
      :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
      contract), and
    * an **optional** ``token`` when the operator has staged one — which
      opts the target into the spec's primary ``bearerToken`` scheme
      (:meth:`FleetLcmConnector.auth_headers` emits
      ``Authorization: Bearer <token>`` whenever a non-empty ``token`` is
      present).

    A missing / blank username-or-password pair raises
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`
    naming the target, so a half-configured secret fails closed with an
    operator-actionable message rather than a bare ``KeyError`` at request
    time. Vault login-phase failures
    (:class:`~meho_backplane.auth.vault.VaultClientError` subclasses)
    propagate verbatim.

    See the module docstring for the deferred live ``basicAuth`` →
    mint-``bearerToken`` exchange (the #3047 live-verify follow-up); this
    loader surfaces a *pre-staged* token only.
    """
    secret_data = await load_vault_secret_data(target, operator)

    username = _optional_field(secret_data, _USERNAME_FIELD)
    password = _optional_field(secret_data, _PASSWORD_FIELD)
    if not (username and password):
        raise VaultCredentialsReadError(
            f"fleet-lcm target {target.name!r}: secret at secret_ref must carry a "
            f"{_USERNAME_FIELD!r} + {_PASSWORD_FIELD!r} pair (the appliance accepts "
            f"basicAuth). Optionally add a {_TOKEN_FIELD!r} field to authenticate via "
            f"the spec's primary Bearer scheme instead."
        )

    credentials = {_USERNAME_FIELD: username, _PASSWORD_FIELD: password}
    token = _optional_field(secret_data, _TOKEN_FIELD)
    if token:
        credentials[_TOKEN_FIELD] = token
    return credentials


def _optional_field(secret_data: dict[str, object], field: str) -> str | None:
    """Return *field* from the KV-v2 payload as a stripped str, or ``None``.

    A missing key, an empty string, or a whitespace-only value all map to
    ``None`` so the loader treats a blank field as "not configured" rather
    than a valid-but-empty credential — the proxmox ``_optional_field``
    shape. Present values run through
    :func:`~meho_backplane.connectors._shared.vault_creds.strip_credential_value`
    so a trailing newline (the most common secret-storage artifact) never
    reaches a Basic-auth header or a ``Bearer`` token verbatim.
    """
    raw = secret_data.get(field)
    if raw is None:
        return None
    value = strip_credential_value(raw)
    return value or None
