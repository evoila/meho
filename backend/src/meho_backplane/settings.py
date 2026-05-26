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
    agent_sync_timeout_seconds:
        Server-side timeout for a *synchronous* agent invocation
        (G11.1-T4 #811). A sync ``POST /api/v1/agents/{name}/run`` blocks
        up to this many seconds; a run still executing at the deadline
        converts to async — the call returns the run handle, the loop keeps
        running in the background, and the operator polls. Bounds how long
        the surface holds an HTTP connection open for a short interactive
        run before degrading to the pollable shape. Set via
        ``AGENT_SYNC_TIMEOUT_SECONDS``.
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
    # G11.1-T4 #811 — server-side timeout for a *synchronous* agent run.
    # A sync ``POST /api/v1/agents/{name}/run`` blocks up to this many
    # seconds; a run still going at the deadline converts to async (the
    # call returns the run handle, the loop keeps running, the operator
    # polls). Bounds how long the surface holds an HTTP connection open
    # for a short interactive run before degrading to the pollable shape.
    agent_sync_timeout_seconds: float = Field(default=30.0, gt=0)
    # G11.3-T2 #823 — cron + one-off trigger scheduler. ``tick_interval``
    # bounds how often the loop scans for due triggers; the default
    # (30 s) is the consumer-doc-accepted granularity for cron triggers
    # (one minute is the finest cron-expression boundary, so a 30 s
    # tick guarantees the trigger fires inside its minute window). The
    # ``enabled`` flag mirrors the MEMORY_EXPIRY / TOPOLOGY_HISTORY_PRUNE
    # shape so operators using an external scheduler can opt out.
    scheduler_tick_interval_seconds: int = Field(default=30, ge=1, le=3600)
    scheduler_enabled: bool = True
    # G11.3-T2 #823 — autonomous-agent credential sourcing for the
    # scheduler. ``run_scheduled`` (G11.2-T2 #1096) wants
    # ``(client_id, client_secret)``; the scheduler resolves
    # ``client_id`` from the trigger's :class:`AgentDefinition.identity_ref`
    # and reads the matching secret from an environment variable whose
    # name is derived from this pattern. ``{client_id}`` is substituted
    # at fire time and the result is uppercased + non-alphanumeric chars
    # replaced with underscores so an ``identity_ref`` like ``agent:reporter``
    # resolves to ``MEHO_AGENT_SECRET_AGENT_REPORTER``.
    #
    # Why env-var sourcing rather than Vault: ``vault_client_for_operator``
    # is JWT/OIDC-bound and the scheduler is operator-less. Until a
    # scheduler-service-token Vault auth path lands (G11.2 follow-up),
    # operators wire agent secrets into the backplane pod's env (Helm
    # secret / external-secrets / sealed-secret) the same way
    # ``ANTHROPIC_API_KEY`` is wired today. The
    # ``scheduler_agent_vault_path_pattern`` setting below is reserved
    # for the future Vault path; it ships configured but unused so the
    # transition is a code swap, not an env-var rename.
    scheduler_agent_secret_env_pattern: str = Field(default="MEHO_AGENT_SECRET_{client_id}")
    # Forward-compat: the Vault KVv2 path the scheduler will read once
    # service-token auth lands. Configured but unused in v0.2.
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
        agent_sync_timeout_seconds=float(
            os.environ.get("AGENT_SYNC_TIMEOUT_SECONDS", "30.0"),
        ),
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
    )
