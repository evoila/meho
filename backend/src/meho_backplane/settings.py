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
from typing import Final

from pydantic import BaseModel, Field, HttpUrl, field_validator

__all__ = ["Settings", "get_settings"]

#: Driver schemes accepted on ``DATABASE_URL``. Both are async — the
#: backplane refuses to construct a sync engine because every database
#: I/O path off the request hot loop must be ``await``-able (ADR 0004).
#: ``postgresql+asyncpg://`` is the production driver; ``sqlite+aiosqlite://``
#: is the v0.1 dev/test driver. Adding a third scheme requires both an
#: ADR amendment and a confirmed async driver shipping with that prefix.
_SUPPORTED_DATABASE_URL_SCHEMES: Final[tuple[str, ...]] = (
    "postgresql+asyncpg://",
    "sqlite+aiosqlite://",
)


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
    vault_addr:
        Base URL of the Vault server, e.g. ``https://vault.evba.lab``.
        Required — the backplane refuses to start without it. The OIDC
        forward-auth chain hangs entirely off this endpoint.
    vault_oidc_role:
        Vault role bound to the JWT auth method that the backplane
        forwards tokens against. Default ``meho-mcp`` matches Goal #11's
        requirement letter; operators provisioning a different Vault
        role can override per environment.
    vault_oidc_mount_path:
        Mount path of Vault's JWT/OIDC auth method, **without** the
        ``auth/`` prefix. Vault's recommended convention is to mount
        the JWT method at ``jwt`` (the default) and the OIDC method at
        ``oidc``; either name works for this backplane because hvac's
        ``jwt_login`` calls the same ``POST /auth/{path}/login``
        endpoint regardless of the underlying handler. Override only
        when a Vault operator has chosen a non-standard mount path.
    vault_namespace:
        Vault Enterprise namespace for the JWT auth method, sent as
        the ``X-Vault-Namespace`` header. ``None`` (the default) for
        Vault OSS — the header is omitted, which is the correct shape
        for non-Enterprise deployments.
    vault_timeout_seconds:
        Timeout applied to every HTTP call into Vault (login, secret
        read, health probe). Kept tight: a hung Vault should
        fail-closed quickly rather than starve request capacity. The
        v0.1 dogfood load is per-request login, so the timeout governs
        worst-case request latency directly.
    database_url:
        SQLAlchemy URL for the PostgreSQL database, e.g.
        ``postgresql+asyncpg://meho:<password>@<host>:5432/meho``.
        Required — the backplane refuses to start without it. The
        ``+asyncpg`` driver is mandatory (per ADR 0004); a sync URL
        would silently work for the engine factory but the per-request
        session dependency would block the FastAPI event loop on every
        I/O call. Required also for Alembic — ``env.py`` reads this
        value rather than the static ``[alembic]`` ini setting so the
        migration runner's URL stays in lock-step with the running
        backplane.
    database_pool_size:
        Maximum number of connections SQLAlchemy keeps idle in the
        pool. Default 10 follows SQLAlchemy 2.x's published guidance
        for a single-replica web service; raise it when sustained
        request concurrency exceeds the default.
    database_pool_timeout:
        Seconds to wait for an available pool connection before
        raising :class:`sqlalchemy.exc.TimeoutError`. Default 30s
        gives a real PG outage time to recover before requests start
        failing fast; tune downward for traffic shapes where
        backpressure is preferred to long latency.
    jwt_tenant_claim_name:
        Name of the JWT claim that carries the operator's tenant UUID.
        Default ``tenant_id`` matches the Keycloak protocol-mapper
        recipe documented for G0.1 (Task #235); operators whose realm
        is configured to surface tenancy under a different claim name
        (``tid``, ``org_id``, etc.) override via env var. Read once
        per request by ``verify_jwt`` — the string itself never leaves
        :class:`Settings`.
    jwt_tenant_role_claim_name:
        Name of the JWT claim that carries the operator's
        :class:`~meho_backplane.auth.operator.TenantRole`. Default
        ``tenant_role`` matches the same protocol-mapper recipe.
        Override only when the realm exposes the role under a
        different attribute.
    """

    keycloak_issuer_url: HttpUrl
    keycloak_audience: str = Field(min_length=1)
    keycloak_jwks_cache_ttl_seconds: int = Field(default=300, gt=0)
    keycloak_jwt_leeway_seconds: int = Field(default=30, ge=0)
    jwt_tenant_claim_name: str = Field(default="tenant_id", min_length=1)
    jwt_tenant_role_claim_name: str = Field(default="tenant_role", min_length=1)
    vault_addr: HttpUrl
    vault_oidc_role: str = Field(default="meho-mcp", min_length=1)
    vault_oidc_mount_path: str = Field(default="jwt", min_length=1)
    vault_namespace: str | None = None
    vault_timeout_seconds: float = Field(default=10.0, gt=0)
    database_url: str = Field(min_length=1)
    database_pool_size: int = Field(default=10, gt=0)
    database_pool_timeout: float = Field(default=30.0, gt=0)

    @field_validator("database_url")
    @classmethod
    def _database_url_must_be_async(cls, value: str) -> str:
        """Reject sync SQLAlchemy DSNs at construction time.

        ADR 0004 mandates that every database I/O path off the request
        hot loop is ``await``-able. A sync DSN
        (``postgresql://`` / ``sqlite:///``) would silently work for
        engine construction but would block the FastAPI event loop on
        every checkout — the failure mode is a saturated worker that
        looks healthy on ``/healthz`` but starves at ``/api/...``. Fail
        fast at startup instead, with an actionable error message that
        names the supported schemes so the operator can fix the
        ``DATABASE_URL`` env var directly without grepping the codebase.
        """
        if not value.startswith(_SUPPORTED_DATABASE_URL_SCHEMES):
            supported = ", ".join(_SUPPORTED_DATABASE_URL_SCHEMES)
            raise ValueError(
                f"DATABASE_URL must use an async driver scheme; "
                f"supported: {supported}. Got: {value!r}",
            )
        return value


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
    vault_namespace_env = os.environ.get("VAULT_NAMESPACE")
    return Settings(
        keycloak_issuer_url=os.environ["KEYCLOAK_ISSUER_URL"],  # type: ignore[arg-type]
        keycloak_audience=os.environ["KEYCLOAK_AUDIENCE"],
        keycloak_jwks_cache_ttl_seconds=int(
            os.environ.get("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300"),
        ),
        keycloak_jwt_leeway_seconds=int(
            os.environ.get("KEYCLOAK_JWT_LEEWAY_SECONDS", "30"),
        ),
        jwt_tenant_claim_name=os.environ.get("JWT_TENANT_CLAIM_NAME", "tenant_id"),
        jwt_tenant_role_claim_name=os.environ.get(
            "JWT_TENANT_ROLE_CLAIM_NAME",
            "tenant_role",
        ),
        vault_addr=os.environ["VAULT_ADDR"],  # type: ignore[arg-type]
        vault_oidc_role=os.environ.get("VAULT_OIDC_ROLE", "meho-mcp"),
        vault_oidc_mount_path=os.environ.get("VAULT_OIDC_MOUNT_PATH", "jwt"),
        # ``VAULT_NAMESPACE`` distinguishes "unset" (OSS deployment, no
        # header) from empty-string (operator misconfiguration); the
        # latter is preserved so pydantic's ``min_length`` would reject
        # it — but we deliberately allow None|str without min_length
        # because OSS expects None. Empty-string is treated as None
        # here to match Vault's own CLI which silently drops empty
        # ``-namespace`` values.
        vault_namespace=vault_namespace_env if vault_namespace_env else None,
        vault_timeout_seconds=float(
            os.environ.get("VAULT_TIMEOUT_SECONDS", "10.0"),
        ),
        database_url=os.environ["DATABASE_URL"],
        database_pool_size=int(os.environ.get("DATABASE_POOL_SIZE", "10")),
        database_pool_timeout=float(
            os.environ.get("DATABASE_POOL_TIMEOUT", "30.0"),
        ),
    )
