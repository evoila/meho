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
from uuid import UUID

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
    keycloak_cli_client_id:
        The OAuth ``client_id`` of the **public** device-code client
        that ``meho login`` uses to initiate the RFC 8628 device flow
        (suggested name ``meho-cli``). Surfaced through
        ``GET /api/v1/auth-config`` so the CLI can discover it without
        the operator hand-passing ``--client-id``. Must be a public
        client (no client secret) with the device-grant flow enabled
        and an audience mapper that injects ``keycloak_audience`` into
        the issued access token's ``aud`` claim — see
        ``deploy/values-examples/README.md`` for the realm-side recipe.
        Default ``""`` (unset) keeps backwards compatibility with the
        v0.3.1 endpoint shape: the field still appears on the response
        but the CLI surfaces an actionable error rather than silently
        misusing ``audience`` (which is the confidential resource-
        server identifier, not a public client).
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
    vault_scheduler_token:
        Static Vault token the **scheduler** authenticates with to read
        agent ``client_credentials`` secrets (G0.19-T2 #1478). The
        scheduler is operator-less — it has no Keycloak JWT to forward to
        Vault's JWT/OIDC auth method — so it reads under its own service
        identity instead. A token bound to a narrow policy that grants
        read/write on ``scheduler_agent_vault_path_pattern`` (default
        ``secret/data/agents/*/credentials``) is the lowest-friction
        service identity: it reuses the ``hvac.Client(token=…)`` primitive
        with no AppRole ``secret_id`` bootstrap. **Write** (``create`` +
        ``update``) is required because agent-principal registration mints
        the Keycloak client secret into Vault via
        :func:`~meho_backplane.scheduler.vault_credentials.write_agent_secret`;
        a read-only token denies registration with
        :class:`~meho_backplane.scheduler.vault_credentials.SchedulerVaultBrokerError`.
        The operator-runbook stanza is in
        ``docs/cross-repo/vault-provisioning.md``. Default ``""`` (unset)
        leaves Vault-sourced scheduling inoperative; the scheduler then
        falls back to the env-var path
        (:attr:`scheduler_agent_secret_env_pattern`). Never logged; never
        surfaced in API responses. Operators wanting AppRole instead of a
        static token can wrap a Vault Agent sidecar that writes the token
        to this env var — additive, no code change.
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
    jwt_principal_kind_claim_name:
        Name of the JWT claim that carries the principal-kind
        discriminator (``user`` / ``service`` / ``agent``) added by
        G11.2-T1 (#815). Default ``principal_kind`` matches the agent-
        client protocol-mapper recipe in
        ``docs/cross-repo/keycloak-agent-client.md``. The claim is
        **optional** — tokens that carry no claim resolve to ``user``
        (graceful fallback for all pre-G11.2 human-operator tokens).
    jwt_capabilities_claim_name:
        Name of the JWT claim that carries the tenant-provisioned
        capability keys (a JSON array of strings) added by G4.5-T1
        (#1519). Default ``capabilities``. Drives the MCP capability
        gate (:class:`~meho_backplane.mcp.registry.ToolDefinition`'s
        ``required_capability``). The claim is **optional** — tokens
        that carry no claim (or a malformed value) resolve to the empty
        set, so capability-gated tools are simply absent for that
        operator (fail-closed). Override only when the realm exposes the
        capability list under a different attribute.
    jwt_platform_admin_claim_name:
        Name of the JWT claim that carries the cross-tenant
        ``platform_admin`` flag (a JSON boolean). Default
        ``platform_admin``. The flag is orthogonal to
        :class:`~meho_backplane.auth.operator.TenantRole` and marks a
        genuine platform / cross-tenant operator. The claim is
        **optional** and **fail-closed** — tokens that carry no claim
        (or a malformed value) resolve to ``False``, so every existing
        token and every agent / service principal is non-platform-admin
        unless a realm explicitly grants the claim. Override only when
        the realm exposes the flag under a different attribute.
    keycloak_admin_url:
        Base URL of the Keycloak Admin REST API for the realm managing
        MEHO principals, e.g.
        ``https://keycloak.evba.lab/admin/realms/meho``. Used by the
        agent-principal lifecycle service (G11.2-T1) to register /
        list / revoke agent Keycloak clients. Default ``""`` (unset)
        leaves the admin surface inoperative (register/revoke return
        503) but does not prevent the backplane from starting.
    keycloak_admin_client_id:
        ``client_id`` of the confidential Keycloak client that the
        backplane authenticates against the Admin REST API. Must hold
        the ``manage-clients`` service-account role on the realm.
        Default ``""`` (unset). See
        ``docs/cross-repo/keycloak-agent-client.md`` §Admin-client setup.
    keycloak_admin_client_secret:
        Client secret for :attr:`keycloak_admin_client_id`. Default
        ``""`` (unset). Never logged; never surfaced in API responses.
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
    kb_ingest_root:
        Filesystem root the server-side kb bulk-ingest
        (``POST /api/v1/kb/ingest`` / ``meho kb ingest``) is confined
        to. The ingest ``directory`` argument is resolved (symlinks
        followed) and rejected unless it lands **within** this root —
        a ``tenant_admin`` cannot point the ingest at an arbitrary
        ``.md`` file elsewhere on the backplane host (path-traversal /
        local-file-inclusion). Default ``/opt/meho/kb-ingest`` sits
        under the same ``/opt/meho`` parent the image already uses for
        :attr:`retrieval_model_cache_dir`; the deploy mounts the
        operator's kb content (a checked-out consumer repo, a CI
        artefact, etc.) under that path, or the operator overrides the
        root via ``KB_INGEST_ROOT`` to wherever the content actually
        lives. The chart does **not** pre-create or mount this path —
        an ingest against a default deploy with no kb content mounted
        fails closed with ``directory_not_found`` rather than reading
        anything. The configured root itself need not exist at startup
        (it is validated per-request against the resolved argument), so
        a chassis-only deploy that never ingests carries the default
        harmlessly.
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
    result_handle_max_spill_rows:
        Upper bound on how many rows the reducer spills into the
        :class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`
        when materializing a large set-shaped response (G0.20-T7 #1507).
        The full set is what the ``result_query`` MCP read-back tool
        serves, but a pathological op could return millions of rows and
        blow the per-key Valkey value size — so the spill is capped here
        and the handle records both the stored count and the true total
        so a reader can tell the tail was truncated. Bounds the store's
        footprint on the row axis; the handle's ``ttl_seconds`` bounds it
        on the time axis (Valkey enforces the TTL server-side, so the
        store cannot grow without bound). Default 10000 covers every
        realistic connector list response with headroom; operators with
        genuinely larger sets raise it via
        ``RESULT_HANDLE_MAX_SPILL_ROWS``. Read once per reduce through
        :func:`get_settings`'s cache.
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
    agent_invoke_max_depth:
        Hard cap on how deep an agent-invokes-agent cascade
        (G11.1-T5 #812) may nest. The ``invoke_agent`` tool a running
        agent's loop carries lets one agent run another agent
        definition in the same tenant; without a cap, a definition that
        (directly or transitively) invokes itself would spawn an
        unbounded chain of LLM runs, burning the provider budget before
        anything stops it. The cap mirrors :attr:`composite_max_depth`:
        a per-task contextvar tracks the current invocation depth, the
        ``invoke_agent`` tool pre-increments + checks it against this
        ceiling before starting the child run, and an over-depth
        invocation raises a structured error the model receives as a
        :class:`pydantic_ai.ModelRetry` (it can pick a different action
        or stop) rather than spending. Default 4 -- a cheap-tier agent
        escalating to a deep-tier agent is depth-1; a two-hop
        escalation chain is depth-2; 4 gives headroom for legitimate
        composition while catching a runaway self-invocation in
        seconds. Distinct from the *budget* cap (the shared turn count
        threaded via ``usage=ctx.usage``, enforced by the loop's
        ``UsageLimits``): depth bounds the *tree height*, budget bounds
        the *total turns* across the whole cascade -- a cascade
        terminates on whichever it hits first. Operators whose agents
        compose more deeply override via ``AGENT_INVOKE_MAX_DEPTH``.
        Read once per ``invoke_agent`` call through
        :func:`get_settings`'s cache.
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
    memory_user_default_ttl_days:
        Default time-to-live for user-scoped (``kind="memory-user"``)
        memory entries, in days. G5.2-T2 (#624) consumes this on the
        ``POST /api/v1/memory`` handler to inject
        ``expires_at = now() + memory_user_default_ttl_days`` when the
        caller does not supply an explicit value. Range ``[1, 365]``:
        below one day defeats the auto-expiry contract; above one year
        is functionally "permanent" and operators wanting that should
        pass ``expires_at=null`` explicitly. Default 7 matches
        consumer-needs.md §G5 ("session-scoped hints expire after 7
        days unless re-pinned"). Carried here by G5.2-T1 so the
        chassis owns one settings shape; the write path follows in T2.
    memory_expiry_tick_interval_seconds:
        Cadence of the G5.2-T1 memory-expiry sweeper background loop,
        in seconds. The sweeper (registered in the FastAPI lifespan)
        sleeps this long between scans of the ``documents`` table for
        expired ``source="memory"`` rows. Default 86400 (24 h)
        matches Initiative #374's stated cadence; tests override to
        sub-second values via env-var monkeypatch + :func:`get_settings`
        cache-clear. Range ``[60, 86400]``: below one minute makes the
        sweeper compete with normal request load on a tight loop;
        above 24 hours is the operator-facing maximum (longer than one
        day risks accumulating soft-hidden rows that pollute
        :meth:`~meho_backplane.memory.service.MemoryService.search_memories`'s
        candidate pool). Read once per loop iteration through
        :func:`get_settings`'s cache.
    memory_expiry_enabled:
        Whether to start the G5.2-T1 memory-expiry sweeper background
        task in the FastAPI lifespan. Default ``True``: the in-process
        ``asyncio`` loop is the shipped cleanup mechanism. Operators
        running a different cleanup mechanism (k8s CronJob, etc.)
        flip ``MEMORY_EXPIRY_ENABLED=false`` so the chassis does not
        race the external job; expired rows still surface as soft-
        hidden through the read-side filter
        :func:`~meho_backplane.memory._internal.is_expired` until the
        external job reaps them. Read once at lifespan startup; toggling
        post-start requires a pod restart to take effect.
    ui_keycloak_client_id:
        OAuth ``client_id`` of the **confidential** Keycloak client the
        operator-console BFF login flow authenticates against. Initiative
        #337 (G10.0 Frontend chassis), Task #865. Distinct from
        :attr:`keycloak_cli_client_id` (the public device-code client
        ``meho login`` uses) and from :attr:`keycloak_audience` (the
        resource-server identifier the backplane validates JWT ``aud``
        claims against): the BFF needs a *confidential* client with a
        secret because the authorization-code flow runs server-side at
        ``/ui/auth/callback`` and the token-endpoint exchange carries
        ``client_id`` + ``client_secret`` in the request body. Suggested
        name ``meho-web``. Configured per the recipe in
        ``docs/cross-repo/keycloak-web-client.md``. Default ``""``
        (unset) keeps the chassis-only deploys booting; any ``/ui/auth/*``
        request with the default surfaces an actionable error rather than
        silently misusing one of the other client ids.
    ui_keycloak_client_secret:
        Client secret of the confidential ``meho-web`` Keycloak client.
        Initiative #337, Task #865. Sourced from Vault in production
        (same render-into-env chain that lands ``DATABASE_URL`` /
        ``UI_SESSION_ENCRYPTION_KEY``): the deploy renders the value
        into the pod's ``UI_KEYCLOAK_CLIENT_SECRET`` env var; this field
        reads it once at startup via :func:`get_settings`. The value
        leaves the pod environment only as the body of the POST to
        Keycloak's token endpoint in :mod:`meho_backplane.ui.auth.flow`
        — never logged, never surfaced in error bodies, never copied
        into structlog context. Default ``""`` (unset) is fail-fast: the
        BFF login flow rejects token-exchange attempts without an
        explicit secret rather than silently falling back to an empty
        body (which Keycloak would reject as ``invalid_client``, but the
        explicit precheck names the missing knob so operator remediation
        is unambiguous).
    ui_session_encryption_key:
        URL-safe base64-encoded 32-byte key used by
        :mod:`meho_backplane.ui.auth.session_store` to Fernet-encrypt
        the OAuth access + refresh tokens stored in the ``web_session``
        table. Initiative #337 (G10.0 Frontend chassis), Task #864.
        The chassis-locked decision #11 keeps tokens server-side; this
        key is the chassis-wide encryption seam that makes
        "server-side" mean "ciphertext at rest". Default ``""`` is
        fail-fast: any session-store call raises
        :class:`~meho_backplane.ui.auth.session_store.EncryptionKeyMissingError`
        until the key is provisioned. Production deploys render this
        from a Vault-managed secret into the pod's environment (same
        chain that lands ``DATABASE_URL`` / Keycloak client secrets
        in the env); dev/test pin a per-run key via
        :meth:`pytest.MonkeyPatch.setenv` (see the autouse fixture in
        ``backend/tests/conftest.py``). Generate one with
        ``python -c 'from cryptography.fernet import Fernet;
        print(Fernet.generate_key().decode())'``. Rotating the key is
        an Initiative-#337-follow-up surface (every active session
        becomes un-decryptable on key rotation; the operator-facing
        contract is "log everyone out, then bump the key"); v0.2
        ships one key end-to-end.
    ui_session_sliding_extension_seconds:
        Sliding-session window, in seconds. Initiative #338
        (G10.1 Activity broadcast UI), Task #869 (the broadcast
        wall-monitor's long-display requirement). The BFF session row's
        ``expires_at`` is set at login to roughly the access-token TTL,
        so a wall-monitor display left running for hours would otherwise
        cross ``expires_at`` mid-display; the next SSE reconnect to
        ``/ui/broadcast/stream`` would then be 302-redirected to login,
        and the browser ``EventSource`` permanently fails on a non-200
        response -- the feed dies silently. On every active ``/ui/*``
        request,
        :func:`meho_backplane.ui.auth.session_store.load_session`
        extends ``expires_at`` to ``now + this window`` when the row is
        within the window of expiry, so an in-use session never lapses
        mid-display. ``0`` disables the sliding extension (the session
        expires strictly at its login-time ``expires_at``). Default
        3600 (one hour) -- a value large enough that a wall display
        refreshing the page or reconnecting the stream within the hour
        keeps the session alive, small enough that an abandoned tab
        lapses within the hour. Always bounded by
        ``ui_session_absolute_lifetime_seconds`` so the extension is not
        unbounded.
    ui_session_absolute_lifetime_seconds:
        Hard ceiling, in seconds, on how long a BFF session may live
        from ``created_at`` regardless of sliding extension. Task #869.
        The sliding extension (above) keeps an actively-viewed session
        alive, but without an absolute cap a permanently-displayed wall
        monitor would create an immortal session -- a standard
        idle-vs-absolute session-timeout pairing (OWASP ASVS v4 §3.3).
        :func:`load_session` never extends ``expires_at`` past
        ``created_at + this cap``; once the cap is reached the session
        lapses on its next load and the operator re-authenticates.
        Default 43200 (12 hours) -- one working day of continuous wall
        display without forcing a mid-shift re-login, while still
        guaranteeing a daily re-auth. Must exceed
        ``ui_session_sliding_extension_seconds`` for the sliding window
        to have any effect.
    topology_history_retention_days:
        Maximum age (in days) of ``graph_node_history`` /
        ``graph_edge_history`` rows the G9.3-T6 (#858) retention prune
        task preserves. Rows whose ``valid_from`` is older than
        ``now() - topology_history_retention_days`` are deleted in one
        bounded batch per run. Default 90 matches Initiative #365
        work-item #8 ("quarterly ops review without unbounded growth").
        ``0`` is the opt-out sentinel: when set, the prune is a no-op
        and history grows forever (disk-growth tradeoff flagged in the
        Helm chart values comment + topology runbook). Range ``[0, 3650]``:
        the upper bound is 10 years, which is functionally permanent for
        a v0.2 chassis -- operators wanting longer retention export via
        ``meho topology timeline --json`` to cold storage rather than
        pinning to the chassis's prune cadence. Read once per prune-tick
        through :func:`get_settings`'s cache.
    topology_history_prune_interval_seconds:
        Cadence of the G9.3-T6 (#858) retention prune background loop,
        in seconds. The prune task (registered in the FastAPI lifespan)
        sleeps this long between scans of the history tables. Default
        604800 (7 d / weekly) matches Initiative #365's stated cadence
        ("a weekly background task prunes rows"). Range ``[60, 604800]``:
        below one minute the prune competes with normal write load; the
        ceiling is the documented weekly cadence -- operators wanting
        slower pruning raise ``topology_history_retention_days`` instead
        (it pushes the deletion horizon further out without changing the
        sweep cadence). Tests override to sub-second values via env-var
        monkeypatch + :func:`get_settings` cache-clear, mirroring the
        memory-expiry sweeper test pattern.
    topology_history_prune_enabled:
        Whether to start the G9.3-T6 (#858) retention prune background
        task in the FastAPI lifespan. Default ``True``: the in-process
        ``asyncio`` loop is the shipped retention mechanism. Operators
        running a different retention mechanism (k8s CronJob hitting the
        DB directly, archive-then-delete via cold storage, etc.) flip
        ``TOPOLOGY_HISTORY_PRUNE_ENABLED=false`` so the chassis does not
        race the external job. Distinct from
        ``topology_history_retention_days=0``: ``0`` keeps the loop
        running but every tick is a no-op (cheap heartbeat that proves
        retention is policy-driven); ``enabled=False`` skips starting
        the loop entirely (no audit-row noise, no log line). Read once
        at lifespan startup; toggling post-start requires a pod restart.
    anthropic_api_key:
        Anthropic API key the G11.1 agent runtime's bounded tool-use loop
        authenticates with. Empty (the default) is fail-closed: the seam's
        model factory (:func:`meho_backplane.agent.run.default_model_factory`)
        raises rather than starting a loop with no credentials, so a
        misconfigured deploy surfaces at first agent invocation rather than
        mid-loop. Set via ``ANTHROPIC_API_KEY``. The G11 initiative runs
        against Anthropic only; multi-provider routing (Bedrock, on-prem
        OpenAI-compatible, VCF Private AI Foundation) is G11.5.
    agent_default_model:
        Pinned Anthropic model id the agent loop uses when an
        ``AgentDefinition`` does not override it. A full id
        (``anthropic:claude-sonnet-4-6``), never a moving ``-latest`` tag,
        so a model swap is a deliberate config push. Set via
        ``AGENT_DEFAULT_MODEL``.
    bedrock_region:
        AWS region the Bedrock backend builder
        (:func:`meho_backplane.agent.models.bedrock_backend_builder`)
        constructs its provider against (G11.5-T2 #1076). Empty (the
        default) defers to boto3's region-resolution chain
        (``AWS_DEFAULT_REGION`` / ``AWS_REGION`` / shared profile); set
        ``BEDROCK_REGION`` to pin the region explicitly when none of
        those sources is wired (local dev, ad-hoc smoke tests).
        Credentials follow the same boto3 chain (env vars / IRSA role
        / EC2 instance profile / shared profile) — the backplane does
        not surface AWS-credential settings of its own. A no-region,
        no-credentials deployment that routes a tier to Bedrock
        fail-closes at first agent invocation with an ``AgentRunError``
        wrapping the underlying ``NoRegionError`` / auth error.
    bedrock_default_model:
        Pinned Bedrock model id the Converse loop uses when no per-tier
        override is set. A full Bedrock id including the geo-prefix and
        ``-v1:0`` suffix (e.g. ``us.anthropic.claude-sonnet-4-5-v1:0``),
        never an alias — same posture as :attr:`agent_default_model`.
        Set via ``BEDROCK_DEFAULT_MODEL``.
    agent_sync_timeout_seconds:
        Server-side timeout for a *synchronous* agent invocation
        (G11.1-T4 #811). A sync ``POST /api/v1/agents/{name}/run`` blocks
        up to this many seconds; a run still executing at the deadline
        converts to async — the call returns the run handle, the loop keeps
        running in the background, and the operator polls. Bounds how long
        the surface holds an HTTP connection open for a short interactive
        run before degrading to the pollable shape. Set via
        ``AGENT_SYNC_TIMEOUT_SECONDS``.
    agent_approval_wait_timeout_seconds:
        Overall wall-clock cap on the agent runtime's wait for a
        ``requires_approval`` operation's decision broadcast event
        (G11.1-T9 #1117). When an in-loop ``call_operation`` returns
        ``awaiting_approval``, the wrapped tool subscribes to
        ``approval.{approved,rejected}`` on the per-tenant Valkey stream
        and blocks until the decision arrives, this cap elapses, or the
        run is cancelled. Default 1800s = 30 minutes: long enough for
        human review across timezone-distant teams without tying up an
        agent loop indefinitely on a forgotten request. Set via
        ``AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS``.
    approval_allow_self_approval:
        Self-approval **emergency break-glass** switch (G11.7-T1 #1401).
        Default ``False`` (fail-closed): the operator who requested a
        parked approval may not approve their own request — requester !=
        approver is enforced in
        :func:`~meho_backplane.operations.approval_queue.approve_request`,
        so a compromised or careless single account cannot both ask for
        and grant a privileged connector write. Set
        ``APPROVAL_ALLOW_SELF_APPROVAL=true`` only for a genuine,
        audited break-glass; the self-approval still writes its decision
        audit row, so the use is forensically visible. This is **not**
        the single-operator answer — enabling it posture-wide re-opens,
        for every op, the single-account request+grant hole #1401
        closed. A single-operator tenant should instead park its
        four-eyes writes under an **agent-requester** (a distinct
        ``principal_kind=agent`` ``sub``, so requester != approver clears
        with no flag); see the "Single-operator tenants: use an
        agent-requester, not break-glass" section of
        ``docs/codebase/approvals.md``. Reject is always allowed
        regardless of this flag — an operator withdrawing their own
        pending request is never a privilege escalation.
    openai_api_key:
        Bearer token the OpenAI-compatible backend builder
        (G11.5-T3 #1077) authenticates with. Empty (the default) is
        fail-closed: a deploy whose policy never routes any tier to an
        OpenAI-compat backend never reads the value, so the default
        single-tenant Anthropic shape keeps working with no extra env
        var. Set via ``OPENAI_API_KEY`` for OpenAI SaaS, the on-prem
        proxy's bearer token for VCF PAIF, or any non-empty value for
        local vLLM/Ollama (most expect *some* string but don't enforce
        it). The builder reads this **once** at construction time per
        invocation, so rotating the key requires a resolver rebuild.
    openai_base_url:
        Endpoint base URL the OpenAI-compatible backend builder targets.
        Empty (the default) routes to the OpenAI SaaS endpoint
        (``api.openai.com``). A non-empty value points at an on-prem
        endpoint: vLLM exposes ``http://<host>:8000/v1``, Ollama
        ``http://<host>:11434/v1``, VCF Private AI Foundation a
        deploy-specific path under
        ``/api/v1/compatibility/openai/v1/``. The value is passed
        verbatim to :class:`pydantic_ai.providers.openai.OpenAIProvider`,
        which then constructs an underlying ``AsyncOpenAI`` client. Set
        via ``OPENAI_BASE_URL``. The setting is per-deploy: tenants
        that route to *different* on-prem endpoints construct their
        backend builders explicitly via
        :func:`~meho_backplane.agent.models.openai_compat_backend_builder`
        rather than the settings-driven default.
    openai_default_model:
        Pinned model id the OpenAI-compatible backend uses when an
        ``AgentDefinition`` does not override it. Full provider-qualified
        id (``openai:gpt-4o-mini`` for OpenAI SaaS;
        ``openai:meta-llama/Llama-3.1-8B-Instruct`` for a vLLM-hosted
        Llama, etc. — pydantic_ai's :class:`OpenAIChatModel` accepts the
        bare model name and the ``openai:`` prefix is stripped when
        present). Same posture as ``agent_default_model``: a full id,
        never a moving alias, so model swaps are deliberate config
        pushes. Set via ``OPENAI_DEFAULT_MODEL``.
    agent_budget_degrade_threshold:
        Fraction of a per-identity budget window's limit at which the
        graceful-degradation policy (G11.5-T6 #1080) downgrades the
        resolved tier one step before the model is built. Default
        ``0.8`` per Initiative #806 ("at 80% drop to a cheaper tier; at
        100% refuse"). Set via ``AGENT_BUDGET_DEGRADE_THRESHOLD``. The
        downgrade ladder is INVESTIGATE → SUMMARIZE → TRIAGE; TRIAGE
        cannot downgrade further (it is the cheapest tier) and is
        allowed to run until the hard cap fires. Values outside
        ``[0, 1)`` are rejected.
    agent_runs_disabled_global:
        Global kill switch (G11.5-T6 #1080). When ``true``, every agent
        run is refused before any model is resolved or any DB row is
        created, raising
        :class:`~meho_backplane.agent.run.BudgetExceededError`. Default
        ``false``. Set via ``AGENT_RUNS_DISABLED_GLOBAL``. The
        operator's emergency stop for an in-flight cost runaway when
        per-identity caps are not enough. Per-tenant gating is
        ``agent_runs_disabled_tenants``; per-identity gating is
        ``identity_budget.request_limit=0`` (set via the consumption
        service's ``set_limits``).
    agent_runs_disabled_tenants:
        Comma-separated tenant UUIDs whose agent runs are kill-
        switched (G11.5-T6 #1080). Case-insensitive; whitespace
        between values ignored. An empty string (the default) means no
        tenants are disabled. Set via ``AGENT_RUNS_DISABLED_TENANTS``.
    vcf_paif_base_url:
        Base URL of the VCF Private AI Foundation OpenAI-compatible
        endpoint (G11.5-T4 #1078), **including** the fixed sub-path
        ``/api/v1/compatibility/openai/v1/`` PAIF mounts the API under
        (Broadcom developer docs — see
        :data:`~meho_backplane.agent.models.VCF_PAIF_OPENAI_COMPAT_BASE_PATH`).
        Example: ``https://pais.airgap.local/api/v1/compatibility/openai/v1/``.
        Empty (the default) is fail-closed: a deploy whose policy
        never routes any tier to PAIF never reads the value. Set via
        ``VCF_PAIF_BASE_URL``. Per-tenant routing to *different* PAIF
        appliances uses
        :func:`~meho_backplane.agent.models.vcf_paif_backend_builder`
        directly rather than this single-knob default.
    vcf_paif_model:
        Pinned model id the PAIF backend uses when an
        ``AgentDefinition`` does not override it. Full provider-qualified
        id (``openai:meta-llama/Llama-3.1-8B-Instruct`` /
        ``openai:mistralai/Mixtral-8x7B-Instruct``, etc. — pydantic_ai's
        :class:`OpenAIChatModel` strips the ``openai:`` prefix). Set via
        ``VCF_PAIF_MODEL``.
    vcf_paif_oidc_token_url:
        The IdP token endpoint the bundled OIDC token provider POSTs
        the ``client_credentials`` grant against. Example (Keycloak):
        ``https://kc.airgap.local/realms/<realm>/protocol/openid-connect/token``.
        Empty is fail-closed for a deploy that registered the PAIF
        backend. Set via ``VCF_PAIF_OIDC_TOKEN_URL``.
    vcf_paif_oidc_client_id:
        OIDC client id registered with the IdP for the backplane →
        PAIF integration. Set via ``VCF_PAIF_OIDC_CLIENT_ID``.
    vcf_paif_oidc_client_secret:
        OIDC client secret. Sourced upstream from Vault / external-
        secret / sealed-secret the same way ``ANTHROPIC_API_KEY`` is
        wired today; **never** logged. Set via
        ``VCF_PAIF_OIDC_CLIENT_SECRET``.
    vcf_paif_oidc_scope:
        Optional ``scope`` to include in the OIDC token request.
        Empty (the default) sends no scope parameter — most IdPs
        accept this for ``client_credentials`` and issue a default
        scope. Deployments with fine-grained scope enforcement set
        this to the value the IdP expects. Set via
        ``VCF_PAIF_OIDC_SCOPE``.
    mcp_require_session_id:
        Whether ``POST /mcp`` rejects requests that omit the
        ``Mcp-Session-Id`` header (G8.2-T2 #1010 + G0.14-T6 #1147).
        **Strictly gates enforcement, not capture.** Capture is
        unconditional: any request that includes a parseable
        ``Mcp-Session-Id`` header has the value bound to the structlog
        contextvar and lands on
        ``audit_log.agent_session_id`` regardless of this knob (so
        G8.2 audit-replay lights up automatically on default deploys
        for any client that sends the header — Claude Code does, by
        default). Default ``False``: a missing or malformed header is
        accepted, the audit row's ``agent_session_id`` lands as NULL,
        and the call proceeds. Flip ``MCP_REQUIRE_SESSION_ID=true`` in
        deployments that mandate every agent call carry a session id
        (compliance environments); a missing/empty header then returns
        a JSON-RPC ``-32600`` Invalid Request before dispatch, so no
        audit row is written for the rejected call. A
        present-but-malformed header is **not** a rejection — the
        client did send a session id (just an unparseable one), so the
        require contract is satisfied at the transport layer; the row
        gets a NULL ``agent_session_id`` and structlog logs the
        malformation as a warning so the misbehaving client is
        observable. The current value is reported as
        ``"enforced"`` / ``"always"`` on ``GET /api/v1/health`` via
        :func:`~meho_backplane.mcp.server.mcp_session_id_capture_mode`
        so operators can confirm the deploy's audit-replay capture
        state without diffing env vars.
    """

    keycloak_issuer_url: HttpUrl
    keycloak_audience: str = Field(min_length=1)
    keycloak_cli_client_id: str = ""
    keycloak_jwks_cache_ttl_seconds: int = Field(default=300, gt=0)
    keycloak_jwt_leeway_seconds: int = Field(default=30, ge=0)
    jwt_tenant_claim_name: str = Field(default="tenant_id", min_length=1)
    jwt_tenant_role_claim_name: str = Field(default="tenant_role", min_length=1)
    jwt_principal_kind_claim_name: str = Field(default="principal_kind", min_length=1)
    jwt_capabilities_claim_name: str = Field(default="capabilities", min_length=1)
    jwt_platform_admin_claim_name: str = Field(default="platform_admin", min_length=1)
    keycloak_admin_url: str = ""
    keycloak_admin_client_id: str = ""
    keycloak_admin_client_secret: str = Field(default="", repr=False)
    enable_rbac_test_route: bool = False
    #: Test-only: when True, ``encode_endpoint_text`` returns a zero
    #: vector instead of computing the real fastembed embedding for a
    #: descriptor at registration time. Set via
    #: ``MEHO_TEST_STUB_DESCRIPTOR_EMBEDDING`` by the test conftest so
    #: the per-test app-lifespan boot (which re-runs
    #: ``run_typed_op_registrars`` against a fresh per-test DB) does not
    #: re-embed every typed-op descriptor — the dominant unit-job cost
    #: per #771. Tests that exercise operation semantic-search seed
    #: their own real embeddings and leave this off. NEVER set in
    #: production: stubbed descriptor vectors make operation search
    #: return meaningless rankings.
    test_stub_descriptor_embedding: bool = False
    #: Test-only: when True, ``run_typed_op_registrars`` snapshots the
    #: built-in descriptor + operation-group rows into a per-process
    #: cache on its first invocation, then on every subsequent
    #: invocation bulk-inserts that snapshot instead of re-running every
    #: connector's registrar callable. Set via
    #: ``MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS`` by the test conftest:
    #: the unit suite boots the FastAPI app ~200+ times (once per
    #: app-booting test) against a fresh per-test DB, and replaying a
    #: cached snapshot as two bulk inserts is ~100x cheaper than
    #: re-running ~90 per-op registrations (group resolve + natural-key
    #: lookup + INSERT + flush) on every boot. NEVER set in production:
    #: a pod boots once, so the cache never hits, and a stale cache from
    #: a prior boot would mask a connector's registration change. See
    #: ``run_typed_op_registrars`` for the snapshot/replay contract.
    test_amortize_typed_op_registrars: bool = False
    backplane_url: str = ""
    mcp_resource_uri: str = ""
    vault_addr: HttpUrl
    vault_oidc_role: str = Field(default="meho-mcp", min_length=1)
    vault_oidc_mount_path: str = Field(default="jwt", min_length=1)
    vault_namespace: str | None = None
    vault_timeout_seconds: float = Field(default=10.0, gt=0)
    vault_scheduler_token: str = Field(default="", repr=False)
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
    # L8 + L14 (#101) — root the server-side kb bulk-ingest is confined
    # to. The ingest ``directory`` argument is resolved (symlinks
    # followed) and rejected unless it lands within this root, so a
    # tenant_admin cannot read an arbitrary ``.md`` elsewhere on the
    # host (path-traversal / LFI). Default ``/opt/meho/kb-ingest``
    # mirrors the ``/opt/meho`` parent ``retrieval_model_cache_dir``
    # already uses; operators mount their kb content there or override
    # via ``KB_INGEST_ROOT``. See the field docstring for the full
    # deploy contract.
    kb_ingest_root: str = Field(default="/opt/meho/kb-ingest", min_length=1)
    broadcast_redis_url: str = Field(
        default="redis://localhost:6379",
        min_length=1,
    )
    broadcast_retention_hours: int = Field(default=24, gt=0)
    result_handle_max_spill_rows: int = Field(default=10000, gt=0)
    composite_max_depth: int = Field(default=8, gt=0)
    agent_invoke_max_depth: int = Field(default=4, gt=0)
    topology_refresh_interval_seconds: int = Field(default=3600, gt=0)
    memory_user_default_ttl_days: int = Field(default=7, ge=1, le=365)
    memory_expiry_tick_interval_seconds: int = Field(default=86400, ge=60, le=86400)
    memory_expiry_enabled: bool = True
    # G11.2-T6 #819 — grant-expiry sweeper knobs. ``GRANT_EXPIRY_ENABLED``
    # follows the same opt-out shape as ``MEMORY_EXPIRY_ENABLED``.
    # ``GRANT_EXPIRY_TICK_INTERVAL_SECONDS`` defaults to 300 (5 minutes)
    # so a change-window elevation expires within one tick of its nominal
    # end time — much tighter than the 24-hour memory-expiry cadence because
    # elevation windows are typically hours, not days.
    grant_expiry_tick_interval_seconds: int = Field(default=300, ge=60, le=86400)
    grant_expiry_enabled: bool = True
    # G11.7-T1 #1401 -- self-approval emergency break-glass. Default
    # False is fail-closed: the operator who requested a parked approval
    # may not also approve it (requester != approver, enforced in
    # ``approval_queue.approve_request``). Set
    # ``APPROVAL_ALLOW_SELF_APPROVAL=true`` only for a genuine, audited
    # break-glass; every self-approval still writes its decision audit
    # row, so the use is forensically visible. NOT the single-operator
    # answer -- single-operator tenants park four-eyes writes under an
    # agent-requester (distinct principal_kind=agent sub) instead; see
    # docs/codebase/approvals.md "Single-operator tenants: use an
    # agent-requester, not break-glass".
    approval_allow_self_approval: bool = False
    # G11.3-T4 #825 -- agent_run reaper knobs. Same opt-out shape as
    # GRANT_EXPIRY_ENABLED / MEMORY_EXPIRY_ENABLED so an operator
    # running an external lease-reclaim mechanism (DBOS Transact, a
    # workflow engine, etc.) can disable the in-tree reaper without
    # patching code.
    #
    # The default tick (30s) + the default lease TTL (60s) give a
    # worker two heartbeat windows of slack before reclaim -- a
    # transient ~20s GC pause / network blip does not cost a run. The
    # MAX_PER_TICK bound (50) keeps a post-outage backlog from
    # monopolising one Postgres backend; a 500-row backlog drains
    # across ~10 ticks.
    agent_run_reaper_enabled: bool = True
    agent_run_reaper_tick_interval_seconds: int = Field(default=30, ge=5, le=3600)
    agent_run_reaper_max_per_tick: int = Field(default=50, ge=1, le=1000)
    agent_run_lease_ttl_seconds: int = Field(default=60, ge=10, le=3600)
    ui_keycloak_client_id: str = ""
    ui_keycloak_client_secret: str = ""
    ui_session_encryption_key: str = ""
    # G10.1-T3 #869 — BFF sliding-session knobs for the broadcast
    # wall-monitor's long-display requirement. The sliding extension
    # keeps an actively-viewed session alive past its login-time
    # ``expires_at`` (so a wall display never logs out mid-stream);
    # the absolute cap bounds that extension so a permanent display
    # cannot create an immortal session. ``sliding=0`` disables the
    # extension. See the field docstrings above for the full rationale.
    ui_session_sliding_extension_seconds: int = Field(default=3600, ge=0)
    ui_session_absolute_lifetime_seconds: int = Field(default=43200, gt=0)
    # G9.3-T6 #858 — topology history retention prune knobs. ``days=0`` is
    # the opt-out sentinel ("keep forever"); ``enabled=False`` skips the
    # background task entirely. The two flags are deliberately distinct
    # (see field docstring): ``days=0`` keeps a cheap heartbeat that proves
    # retention is policy-driven, while ``enabled=False`` matches the
    # MEMORY_EXPIRY_ENABLED shape for operators with external retention.
    topology_history_retention_days: int = Field(default=90, ge=0, le=3650)
    topology_history_prune_interval_seconds: int = Field(default=604800, ge=60, le=604800)
    topology_history_prune_enabled: bool = True
    # G11.1-T1 #808 — agent runtime LLM access. The bounded tool-use loop
    # (``meho_backplane.agent``) runs against Anthropic for the G11
    # initiative; multi-provider routing is G11.5. ``anthropic_api_key``
    # empty is the fail-closed default — the seam's model factory raises
    # rather than starting a loop with no credentials. ``agent_default_model``
    # is the pinned model id the loop uses when an ``AgentDefinition`` does
    # not override it (full id in config, not a moving ``-latest`` tag).
    anthropic_api_key: str = ""
    agent_default_model: str = Field(default="anthropic:claude-sonnet-4-6", min_length=1)
    # G11.5-T2 #1076 — AWS Bedrock backend configuration. The Bedrock
    # builder (``meho_backplane.agent.models.bedrock_backend_builder``)
    # uses pydantic_ai's :class:`BedrockConverseModel` + ``boto3`` via
    # the ``[bedrock]`` extra. ``bedrock_region`` empty (the default)
    # defers to boto3's standard region-resolution chain
    # (``AWS_DEFAULT_REGION`` / ``AWS_REGION`` / shared profile);
    # ``boto3`` credentials follow the same chain (env vars / IRSA
    # role / instance profile / shared profile). Set ``BEDROCK_REGION``
    # to pin a region explicitly when the chain would otherwise miss
    # (no env var + no IRSA, e.g. local dev). ``bedrock_default_model``
    # is the pinned full Bedrock model id (geo-prefixed, ``-v1:0``-
    # suffixed) the loop uses when an ``AgentDefinition`` doesn't
    # override it. Like ``agent_default_model``, never a moving alias.
    bedrock_region: str = ""
    bedrock_default_model: str = Field(
        default="us.anthropic.claude-sonnet-4-5-v1:0",
        min_length=1,
    )
    # G11.1-T4 #811 — server-side timeout for a *synchronous* agent run.
    # A sync ``POST /api/v1/agents/{name}/run`` blocks up to this many
    # seconds; a run still going at the deadline converts to async (the
    # call returns the run handle, the loop keeps running, the operator
    # polls). Bounds how long the surface holds an HTTP connection open
    # for a short interactive run before degrading to the pollable shape.
    agent_sync_timeout_seconds: float = Field(default=30.0, gt=0)
    # G11.1-T9 #1117 — overall wall-clock cap on the agent runtime's wait
    # for a ``requires_approval`` operation's decision broadcast event.
    # When an in-loop ``call_operation`` returns ``awaiting_approval``, the
    # wrapped tool subscribes to ``approval.{approved,rejected}`` on the
    # per-tenant Valkey stream and blocks until the decision arrives, this
    # cap elapses, or the run is cancelled. Default 1800s = 30 minutes:
    # long enough for human review across timezone-distant teams without
    # tying up an agent loop indefinitely on a forgotten request. Set via
    # ``AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS``.
    agent_approval_wait_timeout_seconds: float = Field(default=1800.0, gt=0)
    # G11.5-T3 #1077 — OpenAI-compatible backend configuration for the
    # agent runtime's multi-provider resolver. Sources the credentials
    # the settings-driven default OpenAI backend builder consumes; tenants
    # that need a different base_url / api_key per backend construct
    # their builders via ``openai_compat_backend_builder(...)`` explicitly
    # and never touch these settings. Empty defaults are fail-closed: a
    # deploy whose policy never registers an OpenAI-compat backend reads
    # none of these knobs (the bare ``Anthropic``-only path is unchanged).
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_default_model: str = Field(default="openai:gpt-4o-mini", min_length=1)
    # G11.5-T6 #1080 — pre-execution budget enforcement knobs. The
    # threshold is the fraction of a window's limit at which the
    # graceful-degradation policy fires: at or above this ratio the
    # runtime downgrades the resolved tier one step (INVESTIGATE →
    # SUMMARIZE → TRIAGE) before resolving, so a high-cost backend
    # stops getting picked while there is still some headroom. Default
    # 0.8 mirrors the Initiative #806 acceptance criteria ("at 80% drop
    # to a cheaper tier; at 100% refuse"). Set via
    # ``AGENT_BUDGET_DEGRADE_THRESHOLD``; values outside [0, 1) are
    # rejected (0 = always-degrade, useless; ≥1 = never-degrade, defeats
    # the point — use the global kill switch for "no agent runs" instead).
    agent_budget_degrade_threshold: float = Field(default=0.8, ge=0, lt=1)
    # G11.5-T6 #1080 — global kill switch. When ``true``, every
    # ``AgentInvoker.run`` / ``run_scheduled`` / ``stream_events`` call
    # is refused before any model is resolved or any DB row is created,
    # raising :class:`~meho_backplane.agent.run.BudgetExceededError`.
    # Default ``false``. Set via ``AGENT_RUNS_DISABLED_GLOBAL`` — the
    # operator's emergency stop for an in-flight cost runaway when
    # per-identity caps are not enough. Per-tenant gating is the
    # comma-separated ``AGENT_RUNS_DISABLED_TENANTS`` list below;
    # per-identity gating is ``identity_budget.request_limit=0`` (which
    # the consumption service already exposes via ``set_limits``).
    agent_runs_disabled_global: bool = False
    # G11.5-T6 #1080 — per-tenant kill switch. Comma-separated tenant
    # UUIDs (case-insensitive, whitespace ignored). A run whose
    # operator's ``tenant_id`` matches any value here is refused
    # exactly like the global switch. An empty string (the default) is
    # "no tenants disabled". Set via ``AGENT_RUNS_DISABLED_TENANTS``.
    agent_runs_disabled_tenants: str = ""
    # G11.5-T4 #1078 — VCF Private AI Foundation backend. PAIF is the
    # air-gapped enterprise target: OpenAI-compatible wire format under
    # a fixed ``/api/v1/compatibility/openai/v1/`` sub-path, OpenID
    # bearer auth (the bundled provider runs the OAuth ``client_credentials``
    # grant against the IdP's token endpoint). Empty defaults are
    # fail-closed: a deploy whose policy never registers the PAIF
    # backend reads none of these knobs. Multi-PAIF deploys (per-tenant
    # routing to different appliances) construct their builders via
    # ``vcf_paif_backend_builder(...)`` explicitly and never touch
    # these settings.
    vcf_paif_base_url: str = ""
    vcf_paif_model: str = Field(default="openai:meta-llama/Llama-3.1-8B-Instruct", min_length=1)
    vcf_paif_oidc_token_url: str = ""
    vcf_paif_oidc_client_id: str = ""
    vcf_paif_oidc_client_secret: str = Field(default="", repr=False)
    vcf_paif_oidc_scope: str = ""
    # G11.3-T2 #823 — cron + one-off trigger scheduler. ``tick_interval``
    # bounds how often the loop scans for due triggers; the default
    # (30 s) is the consumer-doc-accepted granularity for cron triggers
    # (one minute is the finest cron-expression boundary, so a 30 s
    # tick guarantees the trigger fires inside its minute window). The
    # ``enabled`` flag mirrors the MEMORY_EXPIRY / TOPOLOGY_HISTORY_PRUNE
    # shape so operators using an external scheduler can opt out.
    scheduler_tick_interval_seconds: int = Field(default=30, ge=1, le=3600)
    scheduler_enabled: bool = True
    # G11.3-T2 #823 / G0.19-T2 #1478 — autonomous-agent credential
    # sourcing for the scheduler. ``run_scheduled`` (G11.2-T2 #1096)
    # wants ``(client_id, client_secret)``; the scheduler resolves
    # ``client_id`` from the trigger's :class:`AgentDefinition.identity_ref`
    # and resolves the secret **Vault-first** (see
    # :func:`meho_backplane.scheduler.credentials.resolve_agent_credentials`):
    # it reads the agent's secret from Vault at
    # ``scheduler_agent_vault_path_pattern`` under the scheduler's static
    # service token (:attr:`vault_scheduler_token`), and falls back to an
    # environment variable derived from this pattern only when the Vault
    # read yields nothing. ``{client_id}`` is substituted at fire time and
    # the result is uppercased + non-alphanumeric chars replaced with
    # underscores so an ``identity_ref`` like ``agent:reporter`` resolves
    # to ``MEHO_AGENT_SECRET_AGENT_REPORTER``.
    #
    # The env-var path is the documented **fallback / break-glass**: an
    # operator can wire an agent secret into the backplane pod's env (Helm
    # secret / external-secrets / sealed-secret) the same way
    # ``ANTHROPIC_API_KEY`` is wired when Vault is unavailable. The
    # Vault-first path is what makes an API-registered agent schedulable
    # with no pod env var + no redeploy: registration persists the
    # Keycloak secret to Vault at ``scheduler_agent_vault_path_pattern``
    # (see :meth:`AgentPrincipalService.register`) and the scheduler reads
    # it straight back.
    scheduler_agent_secret_env_pattern: str = Field(default="MEHO_AGENT_SECRET_{client_id}")
    # The Vault KV-v2 *API* path (mount + ``data/`` infix + logical path)
    # where agent ``client_credentials`` secrets live. Registration writes
    # here; the scheduler reads here -- both via ``vault_path_for_client_id``,
    # so the two cannot diverge. ``{client_id}`` is substituted with the
    # **sanitised, UPPER-CASED** identity_ref (non-alphanumeric chars to
    # ``_``, then ``upper()``), e.g. ``agent:ops-writer`` ->
    # ``secret/data/agents/AGENT_OPS_WRITER/credentials`` -- not the raw
    # ``agent:ops-writer`` key. The default addresses the ``secret/``
    # KV-v2 mount; the leading ``secret/data/`` is the raw API path Vault's
    # HTTP surface uses, which the read/write helpers split into hvac's
    # ``(mount_point, logical_path)`` form.
    scheduler_agent_vault_path_pattern: str = Field(
        default="secret/data/agents/{client_id}/credentials"
    )
    # G11.3-T3 #824 — event-outbox drain loop cadence. 10 s default
    # mirrors the consumer doc's accepted-latency target (the
    # LISTEN/NOTIFY wake hint drops the typical latency to sub-second;
    # this is the polled fall-back when no listener is connected).
    # ``enabled`` mirrors SCHEDULER_ENABLED so operators using an
    # external orchestrator (or running tests without the drain) opt out.
    event_drain_tick_interval_seconds: int = Field(default=10, ge=1, le=3600)
    event_drain_enabled: bool = True
    mcp_require_session_id: bool = False
    #: Absolute URL of the external vendor-document corpus search
    #: endpoint the ``search_docs`` add-on (G4.5 #1518) federates to via a
    #: forwarded operator JWT. Empty ("") means the add-on is not
    #: configured; the federation client
    #: (:func:`~meho_backplane.auth.corpus.search_corpus`) fails closed
    #: with :class:`~meho_backplane.auth.corpus.CorpusUnavailable` (→ 503
    #: at the T3 route) rather than returning a silent empty result.
    corpus_url: str = ""
    #: Optional RFC 8707 resource indicator (``aud``) the corpus binds
    #: the forwarded token to. Empty ("") forwards no audience.
    corpus_audience: str = ""
    #: Bound on the corpus HTTP request (connect / read / write), in
    #: seconds. A slow corpus raises ``CorpusUnavailable`` rather than
    #: blocking the event loop.
    corpus_timeout_seconds: float = Field(default=10.0, gt=0)
    #: Whether the ``search_docs`` route (T3, #1521) must reject a query
    #: that carries no product/version filter (REQUIRE_FILTERS). Default
    #: ``True`` — fail-closed scope discipline. Consumed by T3, not by the
    #: transport in this Task.
    corpus_require_filters: bool = True
    #: Application-layer tenant-scope guard for the agent-supplied
    #: ``vault.kv.*`` ops (#1643). A Python ``str.format`` template with a
    #: single ``{tenant_id}`` placeholder rendering the logical-path prefix
    #: an operator's KV calls must stay within — defense-in-depth *behind*
    #: the Vault ``meho-mcp`` policy, not a replacement for it
    #: (``docs/codebase/connectors-vault-tenant-scope.md``). When a caller
    #: requests a ``path`` outside their rendered prefix the handler raises
    #: :class:`~meho_backplane.connectors.vault.tenant_scope.VaultTenantScopeError`
    #: *before* the hvac call. **Default-on** as of this release: the
    #: canonical KV layout is now per-tenant
    #: (``secret/data/tenants/<tenant_id>/<target>``, #1723), so the guard
    #: ships enforcing the mount-pinned ``"secret/tenants/{tenant_id}/"``
    #: prefix. The mount segment is required because the guard matches a
    #: normalised ``<mount>/<path>`` candidate and the KV-v2 handlers default
    #: ``mount="secret"`` (``connectors/vault/ops.py`` ``_DEFAULT_KV_MOUNT``);
    #: a path-only ``"tenants/{tenant_id}/"`` would never match that
    #: candidate and would deny every legitimate per-tenant call. A deploy
    #: still mid-migration (secrets under the retired per-``sub`` layout)
    #: opts **out** by setting ``VAULT_KV_TENANT_SCOPE_PREFIX=""`` until its
    #: secrets are relocated. Pre-#1725 this defaulted empty (guard off).
    #: Set via ``VAULT_KV_TENANT_SCOPE_PREFIX``.
    vault_kv_tenant_scope_prefix: str = "secret/tenants/{tenant_id}/"

    @field_validator("vault_kv_tenant_scope_prefix")
    @classmethod
    def _vault_tenant_prefix_must_template_tenant_id(cls, value: str) -> str:
        """Reject a tenant-scope prefix that isn't a clean ``{tenant_id}`` template.

        The guard renders this prefix per-operator via
        ``value.format(tenant_id=...)``
        (:func:`~meho_backplane.connectors.vault.tenant_scope.rendered_tenant_prefix`).
        Pulled up to :class:`Settings` construction so two otherwise-silent
        misconfigurations of ``VAULT_KV_TENANT_SCOPE_PREFIX`` surface at pod
        startup rather than at first ``vault.kv.*`` call:

        * **Missing ``{tenant_id}`` placeholder**: ``str.format`` returns the
          literal prefix, so every operator shares one rendered prefix and
          tenant isolation silently collapses to a single shared namespace.
        * **Malformed template** — unbalanced braces, a positional ``{0}``,
          or an extra named placeholder (``{tenant_id}/{region}``): the
          render raises :class:`KeyError` / :class:`IndexError` /
          :class:`ValueError` at request time, denying every legitimate
          per-tenant call.

        The empty string is the **explicit-disable** sentinel
        (``VAULT_KV_TENANT_SCOPE_PREFIX=""`` opts a mid-migration deploy
        out of the guard) and is accepted verbatim. Same fail-closed-at-
        startup discipline as
        :meth:`_scheduler_secret_pattern_must_substitute_client_id`.
        """
        if not value.strip():
            # Explicit-disable sentinel — guard is a no-op; nothing to render.
            return value
        if "{tenant_id}" not in value:
            raise ValueError(
                f"VAULT_KV_TENANT_SCOPE_PREFIX must include '{{tenant_id}}' so "
                f"each operator's KV calls are scoped to their own tenant "
                f"namespace; got: {value!r}. Set it to the empty string to "
                f"disable the tenant-scope guard."
            )
        try:
            # A clean template renders with ONLY tenant_id; an extra named
            # placeholder raises KeyError, a positional {0} raises IndexError,
            # unbalanced braces raise ValueError.
            value.format(tenant_id="00000000-0000-0000-0000-000000000000")
        except (IndexError, KeyError, ValueError) as exc:
            raise ValueError(
                f"VAULT_KV_TENANT_SCOPE_PREFIX must be a valid str.format "
                f"template whose only placeholder is '{{tenant_id}}'; got: "
                f"{value!r}"
            ) from exc
        return value

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

    @field_validator("scheduler_agent_secret_env_pattern")
    @classmethod
    def _scheduler_secret_pattern_must_substitute_client_id(cls, value: str) -> str:
        """Reject env-var patterns that don't substitute ``{client_id}``.

        Pulled up to :class:`Settings` construction so three otherwise-
        silent failure shapes surface at pod startup rather than at
        first scheduled fire:

        * **Pattern lacks ``{client_id}``** (typo / copy-paste error):
          ``str.format`` returns the literal pattern, every agent
          resolves to the same env-var key, all scheduled runs share
          one secret. Cross-tenant principal-credential bleed.
        * **Pattern uses positional ``{0}`` instead of named
          ``{client_id}``**: ``str.format(client_id=...)`` raises
          :class:`KeyError` on first fire. The precondition gate logs
          ``scheduler_credentials_unresolved`` and skips forever.
        * **Pattern has unbalanced braces**: ``str.format`` raises
          :class:`ValueError` on first fire. Same skip-forever path.

        Same fail-closed-at-startup discipline as
        :meth:`_broadcast_url_must_use_supported_scheme` and
        :meth:`_database_url_must_be_async` -- a misconfigured env var
        should fail the import chain immediately with an actionable
        message, not days later under load.
        """
        if "{client_id}" not in value:
            raise ValueError(
                f"SCHEDULER_AGENT_SECRET_ENV_PATTERN must include "
                f"'{{client_id}}' so each agent resolves to its own env var; "
                f"got: {value!r}"
            )
        try:
            value.format(client_id="TEST_CLIENT_ID")
        except (IndexError, KeyError, ValueError) as exc:
            raise ValueError(
                f"SCHEDULER_AGENT_SECRET_ENV_PATTERN must be a valid "
                f"str.format pattern; got: {value!r}"
            ) from exc
        return value

    @field_validator("agent_runs_disabled_tenants")
    @classmethod
    def _agent_runs_disabled_tenants_must_be_uuid_csv(cls, value: str) -> str:
        """Reject ``AGENT_RUNS_DISABLED_TENANTS`` entries that aren't UUIDs.

        Pulled up to :class:`Settings` construction so a typo'd env var
        fails the pod start rather than silently matching nothing at
        gate-evaluation time (the kill switch is supposed to fire, and a
        deploy that thinks it's kill-switched but isn't is the worst-
        case outcome). Same fail-closed-at-startup discipline as
        :meth:`_broadcast_url_must_use_supported_scheme` and
        :meth:`_scheduler_secret_pattern_must_substitute_client_id`:
        misconfigured env vars surface at import time with an
        actionable message that names the offending entry.

        Empty values (the documented default, "no tenants disabled")
        and blank-separator artefacts (trailing comma, double comma)
        are tolerated -- the gate parser at
        :func:`~meho_backplane.operations.budget_enforcement.evaluate_pre_run_budget`
        skips empty chunks already, and this validator mirrors that
        leniency so the validator's accept-set is exactly the
        gate's accept-set.
        """
        for chunk in (part.strip() for part in value.split(",")):
            if not chunk:
                continue
            try:
                UUID(chunk)
            except ValueError as exc:
                raise ValueError(
                    f"AGENT_RUNS_DISABLED_TENANTS entries must be UUIDs; got {chunk!r}"
                ) from exc
        return value


# Flat env-var -> Settings constructor, one kwarg per field: the length is
# the field count, not branching complexity (McCabe is trivial). Extracting
# "helpers" would scatter the env-var contract this function deliberately
# keeps obvious in one place (see the docstring). #901 added one test-only
# kwarg, tipping it 2 lines over the 100-line guidance.
# code-quality-allow: flat one-kwarg-per-field Settings constructor (above).
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
        keycloak_cli_client_id=os.environ.get("KEYCLOAK_CLI_CLIENT_ID", ""),
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
        jwt_principal_kind_claim_name=os.environ.get(
            "JWT_PRINCIPAL_KIND_CLAIM_NAME",
            "principal_kind",
        ),
        jwt_capabilities_claim_name=os.environ.get(
            "JWT_CAPABILITIES_CLAIM_NAME",
            "capabilities",
        ),
        jwt_platform_admin_claim_name=os.environ.get(
            "JWT_PLATFORM_ADMIN_CLAIM_NAME",
            "platform_admin",
        ),
        keycloak_admin_url=os.environ.get("KEYCLOAK_ADMIN_URL", "").strip(),
        keycloak_admin_client_id=os.environ.get("KEYCLOAK_ADMIN_CLIENT_ID", "").strip(),
        keycloak_admin_client_secret=os.environ.get("KEYCLOAK_ADMIN_CLIENT_SECRET", "").strip(),
        enable_rbac_test_route=parse_bool_env(
            os.environ.get("MEHO_ENABLE_RBAC_TEST_ROUTE"),
        ),
        test_stub_descriptor_embedding=parse_bool_env(
            os.environ.get("MEHO_TEST_STUB_DESCRIPTOR_EMBEDDING"),
        ),
        test_amortize_typed_op_registrars=parse_bool_env(
            os.environ.get("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS"),
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
        vault_scheduler_token=os.environ.get("VAULT_SCHEDULER_TOKEN", "").strip(),
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
        kb_ingest_root=os.environ.get(
            "KB_INGEST_ROOT",
            "/opt/meho/kb-ingest",
        ).strip(),
        broadcast_redis_url=os.environ.get(
            "BROADCAST_REDIS_URL",
            "redis://localhost:6379",
        ),
        broadcast_retention_hours=int(
            os.environ.get("BROADCAST_RETENTION_HOURS", "24"),
        ),
        result_handle_max_spill_rows=int(
            os.environ.get("RESULT_HANDLE_MAX_SPILL_ROWS", "10000"),
        ),
        composite_max_depth=int(
            os.environ.get("COMPOSITE_MAX_DEPTH", "8"),
        ),
        agent_invoke_max_depth=int(
            os.environ.get("AGENT_INVOKE_MAX_DEPTH", "4"),
        ),
        topology_refresh_interval_seconds=int(
            os.environ.get("TOPOLOGY_REFRESH_INTERVAL_SECONDS", "3600"),
        ),
        memory_user_default_ttl_days=int(
            os.environ.get("MEMORY_USER_DEFAULT_TTL_DAYS", "7"),
        ),
        memory_expiry_tick_interval_seconds=int(
            os.environ.get("MEMORY_EXPIRY_TICK_INTERVAL_SECONDS", "86400"),
        ),
        memory_expiry_enabled=parse_bool_env(
            os.environ.get("MEMORY_EXPIRY_ENABLED", "true"),
        ),
        grant_expiry_tick_interval_seconds=int(
            os.environ.get("GRANT_EXPIRY_TICK_INTERVAL_SECONDS", "300"),
        ),
        grant_expiry_enabled=parse_bool_env(
            os.environ.get("GRANT_EXPIRY_ENABLED", "true"),
        ),
        # G11.7-T1 #1401 — fail-closed default: self-approval forbidden
        # unless this break-glass switch is explicitly enabled.
        approval_allow_self_approval=parse_bool_env(
            os.environ.get("APPROVAL_ALLOW_SELF_APPROVAL", "false"),
        ),
        agent_run_reaper_enabled=parse_bool_env(
            os.environ.get("AGENT_RUN_REAPER_ENABLED", "true"),
        ),
        agent_run_reaper_tick_interval_seconds=int(
            os.environ.get("AGENT_RUN_REAPER_TICK_INTERVAL_SECONDS", "30"),
        ),
        agent_run_reaper_max_per_tick=int(
            os.environ.get("AGENT_RUN_REAPER_MAX_PER_TICK", "50"),
        ),
        agent_run_lease_ttl_seconds=int(
            os.environ.get("AGENT_RUN_LEASE_TTL_SECONDS", "60"),
        ),
        ui_keycloak_client_id=os.environ.get("UI_KEYCLOAK_CLIENT_ID", "").strip(),
        ui_keycloak_client_secret=os.environ.get("UI_KEYCLOAK_CLIENT_SECRET", "").strip(),
        ui_session_encryption_key=os.environ.get("UI_SESSION_ENCRYPTION_KEY", "").strip(),
        ui_session_sliding_extension_seconds=int(
            os.environ.get("UI_SESSION_SLIDING_EXTENSION_SECONDS", "3600"),
        ),
        ui_session_absolute_lifetime_seconds=int(
            os.environ.get("UI_SESSION_ABSOLUTE_LIFETIME_SECONDS", "43200"),
        ),
        topology_history_retention_days=int(
            os.environ.get("TOPOLOGY_HISTORY_RETENTION_DAYS", "90"),
        ),
        topology_history_prune_interval_seconds=int(
            os.environ.get("TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS", "604800"),
        ),
        topology_history_prune_enabled=parse_bool_env(
            os.environ.get("TOPOLOGY_HISTORY_PRUNE_ENABLED", "true"),
        ),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        agent_default_model=os.environ.get(
            "AGENT_DEFAULT_MODEL",
            "anthropic:claude-sonnet-4-6",
        ),
        bedrock_region=os.environ.get("BEDROCK_REGION", "").strip(),
        bedrock_default_model=os.environ.get(
            "BEDROCK_DEFAULT_MODEL",
            "us.anthropic.claude-sonnet-4-5-v1:0",
        ),
        agent_sync_timeout_seconds=float(
            os.environ.get("AGENT_SYNC_TIMEOUT_SECONDS", "30.0"),
        ),
        agent_approval_wait_timeout_seconds=float(
            os.environ.get("AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS", "1800.0"),
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "").strip(),
        openai_default_model=os.environ.get(
            "OPENAI_DEFAULT_MODEL",
            "openai:gpt-4o-mini",
        ),
        agent_budget_degrade_threshold=float(
            os.environ.get("AGENT_BUDGET_DEGRADE_THRESHOLD", "0.8"),
        ),
        agent_runs_disabled_global=parse_bool_env(
            os.environ.get("AGENT_RUNS_DISABLED_GLOBAL"),
        ),
        agent_runs_disabled_tenants=os.environ.get(
            "AGENT_RUNS_DISABLED_TENANTS",
            "",
        ),
        vcf_paif_base_url=os.environ.get("VCF_PAIF_BASE_URL", "").strip(),
        vcf_paif_model=os.environ.get(
            "VCF_PAIF_MODEL",
            "openai:meta-llama/Llama-3.1-8B-Instruct",
        ),
        vcf_paif_oidc_token_url=os.environ.get("VCF_PAIF_OIDC_TOKEN_URL", "").strip(),
        vcf_paif_oidc_client_id=os.environ.get("VCF_PAIF_OIDC_CLIENT_ID", "").strip(),
        vcf_paif_oidc_client_secret=os.environ.get("VCF_PAIF_OIDC_CLIENT_SECRET", "").strip(),
        vcf_paif_oidc_scope=os.environ.get("VCF_PAIF_OIDC_SCOPE", "").strip(),
        scheduler_tick_interval_seconds=int(
            os.environ.get("SCHEDULER_TICK_INTERVAL_SECONDS", "30"),
        ),
        scheduler_enabled=parse_bool_env(
            os.environ.get("SCHEDULER_ENABLED", "true"),
        ),
        scheduler_agent_secret_env_pattern=os.environ.get(
            "SCHEDULER_AGENT_SECRET_ENV_PATTERN",
            "MEHO_AGENT_SECRET_{client_id}",
        ),
        scheduler_agent_vault_path_pattern=os.environ.get(
            "SCHEDULER_AGENT_VAULT_PATH_PATTERN",
            "secret/data/agents/{client_id}/credentials",
        ),
        event_drain_tick_interval_seconds=int(
            os.environ.get("EVENT_DRAIN_TICK_INTERVAL_SECONDS", "10"),
        ),
        event_drain_enabled=parse_bool_env(
            os.environ.get("EVENT_DRAIN_ENABLED", "true"),
        ),
        mcp_require_session_id=parse_bool_env(
            os.environ.get("MCP_REQUIRE_SESSION_ID"),
        ),
        corpus_url=os.environ.get("CORPUS_URL", "").strip(),
        corpus_audience=os.environ.get("CORPUS_AUDIENCE", "").strip(),
        corpus_timeout_seconds=float(
            os.environ.get("CORPUS_TIMEOUT_SECONDS", "10.0"),
        ),
        corpus_require_filters=parse_bool_env(
            os.environ.get("CORPUS_REQUIRE_FILTERS", "true"),
        ),
        vault_kv_tenant_scope_prefix=os.environ.get(
            "VAULT_KV_TENANT_SCOPE_PREFIX", "secret/tenants/{tenant_id}/"
        ).strip(),
    )
