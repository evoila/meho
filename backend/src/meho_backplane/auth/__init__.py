# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator authentication — JWT validation, identity, and Vault forward-auth.

Public surface:

* :class:`Operator` — frozen pydantic v2 model carrying validated claims.
* :func:`verify_jwt` — FastAPI dependency that validates the
  ``Authorization: Bearer <jwt>`` header and yields an
  :class:`Operator` to the route handler.
* :func:`keycloak_readiness_probe` — registered against the readiness
  registry from :mod:`meho_backplane.health` so ``/ready`` flips to
  ``not_ready`` whenever the JWKS endpoint is unreachable.
* :func:`vault_client_for_operator` — async context manager yielding an
  authenticated :class:`hvac.Client` bound to the operator. Performs a
  Vault OIDC login on entry and revokes the per-request token on exit.
* :func:`vault_readiness_probe` — registered against the readiness
  registry; reports Vault reachability via ``/sys/health``.
* :class:`VaultClientError`, :class:`VaultUnreachableError`,
  :class:`VaultRoleDeniedError` — backplane-side exception hierarchy
  callers raise / catch without importing hvac.
* :func:`search_corpus` — async client that federates a ``search_docs``
  query to the external vendor-document corpus over httpx, forwarding the
  operator JWT (G4.5-T2 #1520). Returns a :class:`CorpusSearchResponse`
  of :class:`CorpusChunk`; fails closed with :class:`CorpusUnavailable`
  when the corpus is unconfigured, unreachable, or returns a non-2xx.

Downstream code should never import private symbols (``_`` prefix) from
:mod:`meho_backplane.auth.jwt` or :mod:`meho_backplane.auth.vault`; the
cache helpers and threadpool wrappers are test-only.
"""

from meho_backplane.auth.corpus import (
    CorpusChunk,
    CorpusSearchResponse,
    CorpusUnavailable,
    search_corpus,
)
from meho_backplane.auth.jwt import keycloak_readiness_probe, verify_jwt
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.auth.vault import (
    VaultClientError,
    VaultRoleDeniedError,
    VaultUnreachableError,
    vault_client_for_operator,
    vault_readiness_probe,
)

__all__ = [
    "CorpusChunk",
    "CorpusSearchResponse",
    "CorpusUnavailable",
    "Operator",
    "PrincipalKind",
    "VaultClientError",
    "VaultRoleDeniedError",
    "VaultUnreachableError",
    "keycloak_readiness_probe",
    "search_corpus",
    "vault_client_for_operator",
    "vault_readiness_probe",
    "verify_jwt",
]
