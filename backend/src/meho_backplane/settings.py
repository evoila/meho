# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runtime configuration sourced from environment variables.

The backplane is configured exclusively through env vars in v0.1 — there
is no on-disk config file, no config server, and no live reload. Every
field has a documented default where one is sensible; required fields
(``keycloak_issuer_url``, ``keycloak_audience``) raise at startup if the
operator forgot to set them, which is the correct fail-closed behaviour
for a security-critical surface.

Settings are accessed through :func:`get_settings`, which caches a single
:class:`Settings` instance for the process lifetime. This keeps the
constructor's env-var parsing cost to once-per-process and gives every
module (including FastAPI dependencies) a stable singleton without
shipping a global. Tests override config by monkey-patching env vars and
clearing the cache via ``get_settings.cache_clear()``.

Pydantic v2 is the model engine; the ``BaseModel`` validators run at
construction time so a missing or malformed env var fails the import
chain immediately rather than days later under load.
"""

import os
from functools import lru_cache

from pydantic import BaseModel, Field, HttpUrl

__all__ = ["Settings", "get_settings"]


class Settings(BaseModel):
    """Process-wide configuration knobs.

    Attributes
    ----------
    keycloak_issuer_url:
        The Keycloak realm's issuer URL — typically
        ``https://<host>/realms/<realm>``. Must match the ``iss`` claim
        of every accepted JWT exactly. Required (no default); the
        backplane refuses to start without it.
    keycloak_audience:
        The OIDC ``aud`` claim every accepted JWT must carry. The
        backplane is registered as a Keycloak client (e.g.
        ``meho-backplane``); only tokens whose ``aud`` matches that
        client id are honoured. Required.
    keycloak_jwks_cache_ttl_seconds:
        Maximum age of a cached JWKS document before it must be
        refetched. The cache also refreshes on a kid-miss (key
        rotation), so this TTL is a *safety net* against silent rotation
        of the same kid — a low-probability but high-impact attack
        surface — rather than the primary refresh trigger. Default 300
        (5 minutes) follows the OIDC ecosystem norm.
    keycloak_jwt_leeway_seconds:
        Clock-skew tolerance applied to ``exp`` and ``nbf`` claim
        validation. Real-world deployments routinely drift a few seconds
        between Keycloak and backplane hosts; default 30s absorbs that
        without giving meaningful runway to a stolen-token replay.
    """

    keycloak_issuer_url: HttpUrl
    keycloak_audience: str = Field(min_length=1)
    keycloak_jwks_cache_ttl_seconds: int = Field(default=300, gt=0)
    keycloak_jwt_leeway_seconds: int = Field(default=30, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Reads env vars on first call; subsequent calls return the cached
    instance. Tests that need to swap config call
    ``get_settings.cache_clear()`` after mutating ``os.environ``.

    The function deliberately does **not** use ``pydantic-settings`` —
    the backplane has only four knobs in v0.1 and the explicit
    ``os.environ.get`` mapping below makes the env-var contract obvious
    in code review. When the surface grows, switching to
    ``BaseSettings`` is a one-commit refactor.
    """
    return Settings(
        keycloak_issuer_url=os.environ["KEYCLOAK_ISSUER_URL"],  # type: ignore[arg-type]
        keycloak_audience=os.environ["KEYCLOAK_AUDIENCE"],
        keycloak_jwks_cache_ttl_seconds=int(
            os.environ.get("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300"),
        ),
        keycloak_jwt_leeway_seconds=int(
            os.environ.get("KEYCLOAK_JWT_LEEWAY_SECONDS", "30"),
        ),
    )
