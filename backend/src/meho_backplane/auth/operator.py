# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The :class:`Operator` model — validated identity passed to routes.

An :class:`Operator` is what every authenticated route handler receives
via ``Depends(verify_jwt)``. The model is **frozen** (pydantic v2
``ConfigDict(frozen=True)``) so a route handler can stash the operator
on request state, log it, and forward it to downstream services without
fear of mutation creating a confused-deputy bug.

Field choices reflect what G2.2 / G2.3 consumers actually need:

* ``sub`` — the OIDC subject identifier; the stable operator id.
  Required.
* ``name`` / ``email`` — soft identity bound from the matching JWT
  claims when present. Used by audit middleware (G2.3) for
  human-readable rows and by the CLI for the ``meho status`` greeting.
* ``raw_jwt`` — the original Bearer token string. Required because
  G2.2-T2's Vault forward-auth passes the original JWT (not a re-issued
  one) to Vault's OIDC auth method; the chain of custody must be
  preserved bit-for-bit.

Email validation uses pydantic's ``EmailStr`` (powered by
``email-validator``); a malformed ``email`` claim from Keycloak is a
configuration bug and surfaces as a 401 rather than silently propagating
garbage downstream.
"""

from pydantic import BaseModel, ConfigDict, EmailStr

__all__ = ["Operator"]


class Operator(BaseModel):
    """Validated operator identity extracted from a verified JWT."""

    model_config = ConfigDict(frozen=True)

    sub: str
    name: str | None = None
    email: EmailStr | None = None
    raw_jwt: str
