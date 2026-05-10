# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator authentication — JWT validation and identity primitives.

Public surface:

* :class:`Operator` — frozen pydantic v2 model carrying validated claims.
* :func:`verify_jwt` — FastAPI dependency that validates the
  ``Authorization: Bearer <jwt>`` header and yields an
  :class:`Operator` to the route handler.
* :func:`keycloak_readiness_probe` — registered against the readiness
  registry from :mod:`meho_backplane.health` so ``/ready`` flips to
  ``not_ready`` whenever the JWKS endpoint is unreachable.

Downstream code should never import private symbols (``_`` prefix) from
:mod:`meho_backplane.auth.jwt`; the cache helpers in particular are
test-only.
"""

from meho_backplane.auth.jwt import keycloak_readiness_probe, verify_jwt
from meho_backplane.auth.operator import Operator

__all__ = ["Operator", "keycloak_readiness_probe", "verify_jwt"]
