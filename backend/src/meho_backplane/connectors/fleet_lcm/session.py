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
shared :mod:`meho_backplane.connectors._shared.vcf_auth` scaffolding (the
#841 cross-cutting module) so the two impls share one credential-loading
contract:

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
  and production uses the default Vault read.
* :func:`load_credentials_from_vault` — the shared operator-context
  KV-v2 read, re-exported here so the package's public API reads
  cohesively.

The Bearer seam
---------------

The shared :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`
enforces the ``{"username": str, "password": str}`` contract (it raises
:exc:`RuntimeError` on a missing key), so the loader **always** returns
those two keys — the appliance also accepts ``basicAuth`` per the spec —
and may **additionally** carry an optional ``"token"`` key. When the
loaded credentials carry a non-empty ``"token"``,
:meth:`~meho_backplane.connectors.fleet_lcm.connector.FleetLcmConnector.auth_headers`
emits ``Authorization: Bearer <token>`` (the spec's primary scheme);
otherwise it falls back to ``Authorization: Basic <b64>`` off the
username/password pair. The default :func:`load_credentials_from_vault`
surfaces only the username/password pair today (the shared basic-creds
read), so the live default is the Basic alternative until the
Bearer-token provisioning seam lands — see the connector's class
docstring for the live-verify follow-up (a fleet-lcm loader surfacing a
Vault-stored token, or a POST-``basicAuth`` → mint-``bearerToken``
session flow gated on a stood-up appliance).
"""

from __future__ import annotations

from meho_backplane.connectors._shared.vcf_auth import (
    VcfCredentialsLoader,
    VcfTargetLike,
    load_credentials_from_vault,
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
