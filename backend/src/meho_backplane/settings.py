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

Boolean env vars are parsed by :func:`_parse_bool` which accepts the
canonical truthy spellings (``1``, ``true``, ``yes``, ``on``,
case-insensitive) and treats every other value (including the empty
string) as ``False``. The accept-list is deliberately tight so a
``MEHO_ENABLE_RBAC_TEST_ROUTE=disabled`` typo doesn't silently mount
the test routes in production — the documented contract is that
*anything other than the four truthy spellings* is off.
"""

import os
from functools import lru_cache
from typing import Final

from pydantic import BaseModel, Field, HttpUrl, field_validator

from meho_backplane.retrieval.embedding import (
    BAKED_MODEL_CACHE_DIR,
    DEFAULT_EMBEDDING_MODEL,
)

__all__ = ["Settings", "get_settings", "parse_bool_env"]

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

#: URL schemes accepted on ``BROADCAST_REDIS_URL``. Mirrors redis-py's
#: own :func:`redis.asyncio.from_url` accept-list (``redis://`` for TCP,
#: ``rediss://`` for TLS, ``unix://`` for local-socket dev). ``valkey://``
#: is **not** included even though the backend is Valkey: redis-py
#: rejects the scheme at URL-parse time, and Valkey itself is
#: wire-compatible under ``redis://``. Validating up front turns a
#: misconfigured env var into a fail-fast startup error rather than a
#: silent first-``/ready`` failure.
_SUPPORTED_BROADCAST_URL_SCHEMES: Final[tuple[str, ...]] = (
    "redis://",
    "rediss://",
    "unix://",
)

#: Truthy spellings accepted by :func:`_parse_bool`. Anything else
#: (including the empty string and "disabled") is treated as ``False``
#: so a misconfigured env var never silently enables a guarded surface.
_TRUTHY_ENV_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def parse_bool_env(value: str | None) -> bool:
    """Return ``True`` only for the canonical truthy spellings.

    Used for env-var-backed boolean :class:`Settings` fields and by
    callers (``main.py``) that need the same parsing rule without
    instantiating :class:`Settings` (which requires every chassis env
    var to be set). The accept-list is intentionally tight: a typo
    like ``MEHO_ENABLE_RBAC_TEST_ROUTE=disabled`` evaluates to
    ``False`` rather than the truthy-by-non-empty default Python's
    ``bool(str)`` would produce.
    """
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY_ENV_VALUES


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
    enable_rbac_test_route:
        When ``True``, mounts the ``/api/v1/rbac-test/*`` stub routes
        from :mod:`meho_backplane.api.v1.rbac_test` for end-to-end
        verification of :func:`~meho_backplane.auth.rbac.require_role`.
        Default ``False``: production deploys leave the routes
        unmounted (404). CI integration jobs flip the env var
        ``MEHO_ENABLE_RBAC_TEST_ROUTE=1`` for the RBAC pipeline only.
        The flag is read at FastAPI app construction time; flipping it
        after import has no effect — every test that needs the routes
        builds its own :class:`~fastapi.FastAPI` with the flag set.
    backplane_url:
        Canonical externally-visible URL of this backplane, e.g.
        ``https://meho.evba.lab``. Used to construct the MCP server's
        canonical URI (G0.5-T2) and the absolute URL the RFC 9728
        ``WWW-Authenticate: resource_metadata=...`` header points at.
        Default ``""`` is fail-closed for MCP: a request to ``/mcp``
        with the default value cannot validate (token audience can
        never equal the empty derived URI), and the
        ``/.well-known/oauth-protected-resource`` metadata document
        will surface an empty ``resource`` field that fails RFC 9728
        validation on the client side. Operators MUST set this before
        enabling MCP traffic. Leaving it empty in dev / chassis-only
        deployments keeps the chassis routes operational.
    mcp_resource_uri:
        The canonical URI of this backplane's MCP server, sent as the
        ``resource`` parameter on OAuth 2.1 authorisation / token
        requests per RFC 8707 and returned in the RFC 9728 ``resource``
        field. JWTs presented at ``/mcp`` MUST carry this value in
        their ``aud`` claim. Default ``""`` falls back to
        ``f"{backplane_url}/mcp"`` at use time; operators with non-
        standard MCP mounts (e.g. ``/api/mcp``) override per
        environment. Per the MCP 2025-06-18 spec's canonical-URI
        guidance, prefer the no-trailing-slash form.
    retrieval_embedding_model:
        fastembed-supported model identifier the
        :class:`~meho_backplane.retrieval.embedding.EmbeddingService`
        binds to (G0.4-T2 #259). Default ``BAAI/bge-small-en-v1.5`` —
        384-dim, Apache-2.0, English-optimised, matches v0.1-spec
        L391's chosen model. Operators on non-English tenancies can
        swap via ``RETRIEVAL_EMBEDDING_MODEL`` to another fastembed-
        supported identifier; **changing the output dimensionality
        requires a re-embed-everything migration** because the
        ``documents.embedding`` column is hard-pinned to
        ``vector(384)`` by migration ``0003``. Swap to a same-
        dimension model (e.g. a multilingual 384-dim variant) is
        zero-migration.
    retrieval_model_cache_dir:
        Filesystem path fastembed reads ONNX weights from. Default
        :data:`~meho_backplane.retrieval.embedding.BAKED_MODEL_CACHE_DIR`
        (``/opt/meho/model-cache``) is an **image layer** the
        ``backend/Dockerfile`` runtime stage bakes the default model
        into at build time (``python -m meho_backplane.retrieval.warm``),
        so the shipped default loads offline + version-locked with no
        runtime HuggingFace egress and no dependency on a persistent
        PVC. This deliberately replaced the old
        ``/var/cache/fastembed`` PVC-mount default: a populated-but-
        partial PVC (dangling HF symlink / truncated ``*.onnx`` blob)
        is never self-healed by fastembed and deterministically
        CrashLoops every fresh pod (evoila/meho#574). The optional
        ``retrieval.modelCache`` PVC remains available for operators who
        override ``RETRIEVAL_EMBEDDING_MODEL`` to a *non-default* model
        that is fetched at runtime. Dev/test overrides via
        ``RETRIEVAL_MODEL_CACHE_DIR`` typically point at
        ``$HOME/.cache/fastembed`` so a developer's existing cache is
        reused across runs.
    broadcast_redis_url:
        Connection URL for the Valkey (Redis-protocol-compatible)
        broadcast substrate the G6 activity-broadcast Initiative
        (#228) is built on. Default ``redis://localhost:6379`` keeps
        local development working without env-var wiring; production
        deploys point this at the in-cluster broadcast service
        rendered by the Helm chart (``redis://<release>-broadcast:6379``).
        Only ``redis://``, ``rediss://``, and ``unix://`` schemes are
        accepted — ``valkey://`` is rejected because redis-py itself
        rejects it at URL-parse time and Valkey serves the Redis wire
        protocol under ``redis://``. Validation runs at
        :class:`Settings` construction so a typo fails startup rather
        than the first ``/ready`` poll.
    broadcast_retention_hours:
        Server-side replay window for broadcast events, in hours.
        Default 24 matches the locked v0.2 decision-3 contract. T3
        (#309) will use this to set ``XADD MAXLEN`` / ``MINID`` trim
        on every publish; T1 only carries the knob.
    composite_max_depth:
        Hard cap on the recursion depth a composite operation
        (``source_kind='composite'``) may reach via successive
        ``dispatch_child(...)`` calls. Composite handlers orchestrate
        multi-step flows (e.g. vSphere VM provisioning ~5 atomic
        calls) and may legitimately nest a composite inside another
        composite, but unbounded recursion is a foot-gun: a handler
        that accidentally re-dispatches itself would spin forever
        and exhaust the audit-log + DB-pool capacity before failing.
        Default 8 -- four levels above any realistic v0.2 composite
        depth (sequential vSphere VM creation is depth-1, an
        operator-authored composite of composites is depth-2; 8
        gives 4x headroom for legitimate use while catching the
        runaway pattern in seconds rather than minutes). Operators
        whose connectors need deeper composition override via the
        ``COMPOSITE_MAX_DEPTH`` env var. Read once per
        ``dispatch_child`` call -- no caching beyond
        :func:`get_settings`'s :func:`lru_cache`.
    topology_refresh_interval_seconds:
        Cadence of the G9.1-T3 background topology-refresh loop, in
        seconds. The scheduler (registered in the FastAPI lifespan)
        sleeps this long between full sweeps of every tenant's
        targets. Default 3600 (1 h) matches Initiative #363's stated
        cadence; operators on fast-moving inventories tighten it via
        ``TOPOLOGY_REFRESH_INTERVAL_SECONDS``. Per-target failure
        backoff is derived from this value (2x, capped at 4 h) inside
        the scheduler, not a separate knob. Read once per loop
        iteration through :func:`get_settings`'s cache.
    """

    keycloak_issuer_url: HttpUrl
    keycloak_audience: str = Field(min_length=1)
    keycloak_jwks_cache_ttl_seconds: int = Field(default=300, gt=0)
    keycloak_jwt_leeway_seconds: int = Field(default=30, ge=0)
    jwt_tenant_claim_name: str = Field(default="tenant_id", min_length=1)
    jwt_tenant_role_claim_name: str = Field(default="tenant_role", min_length=1)
    enable_rbac_test_route: bool = False
    backplane_url: str = ""
    mcp_resource_uri: str = ""
    vault_addr: HttpUrl
    vault_oidc_role: str = Field(default="meho-mcp", min_length=1)
    vault_oidc_mount_path: str = Field(default="jwt", min_length=1)
    vault_namespace: str | None = None
    vault_timeout_seconds: float = Field(default=10.0, gt=0)
    database_url: str = Field(min_length=1)
    database_pool_size: int = Field(default=10, gt=0)
    database_pool_timeout: float = Field(default=30.0, gt=0)
    retrieval_embedding_model: str = Field(
        default=DEFAULT_EMBEDDING_MODEL,
        min_length=1,
    )
    retrieval_model_cache_dir: str = Field(
        default=BAKED_MODEL_CACHE_DIR,
        min_length=1,
    )
    broadcast_redis_url: str = Field(
        default="redis://localhost:6379",
        min_length=1,
    )
    broadcast_retention_hours: int = Field(default=24, gt=0)
    composite_max_depth: int = Field(default=8, gt=0)
    topology_refresh_interval_seconds: int = Field(default=3600, gt=0)

    @field_validator("broadcast_redis_url")
    @classmethod
    def _broadcast_url_must_use_supported_scheme(cls, value: str) -> str:
        """Reject schemes redis-py would refuse at runtime.

        :func:`redis.asyncio.from_url` raises :class:`ValueError` at
        URL-parse time for anything outside ``redis://`` / ``rediss://`` /
        ``unix://``. Pulling that validation up to :class:`Settings`
        construction converts a misconfigured ``BROADCAST_REDIS_URL``
        into a fail-fast startup error with an actionable message
        naming the supported schemes, rather than a deferred crash on
        the first :func:`get_broadcast_client` call.
        """
        if not value.startswith(_SUPPORTED_BROADCAST_URL_SCHEMES):
            supported = ", ".join(_SUPPORTED_BROADCAST_URL_SCHEMES)
            raise ValueError(
                f"BROADCAST_REDIS_URL must use a redis-py-supported scheme; "
                f"supported: {supported}. Got: {value!r}",
            )
        return value

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
        enable_rbac_test_route=parse_bool_env(
            os.environ.get("MEHO_ENABLE_RBAC_TEST_ROUTE"),
        ),
        backplane_url=os.environ.get("BACKPLANE_URL", ""),
        mcp_resource_uri=os.environ.get("MCP_RESOURCE_URI", ""),
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
        retrieval_embedding_model=os.environ.get(
            "RETRIEVAL_EMBEDDING_MODEL",
            DEFAULT_EMBEDDING_MODEL,
        ),
        retrieval_model_cache_dir=os.environ.get(
            "RETRIEVAL_MODEL_CACHE_DIR",
            BAKED_MODEL_CACHE_DIR,
        ),
        broadcast_redis_url=os.environ.get(
            "BROADCAST_REDIS_URL",
            "redis://localhost:6379",
        ),
        broadcast_retention_hours=int(
            os.environ.get("BROADCAST_RETENTION_HOURS", "24"),
        ),
        composite_max_depth=int(
            os.environ.get("COMPOSITE_MAX_DEPTH", "8"),
        ),
        topology_refresh_interval_seconds=int(
            os.environ.get("TOPOLOGY_REFRESH_INTERVAL_SECONDS", "3600"),
        ),
    )
