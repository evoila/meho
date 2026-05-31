# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G6.3-T2 broadcast-override resolver + cache.

Coverage matrix (Task #379 acceptance criteria):

* Precedence ladder -- per-call request override, tenant rule,
  static default -- all three branches verified.
* Opt-in-only request override -- a ``read`` op stays ``"full"``
  regardless of any per-call value (no "weaken via header" path).
* Tenant rule downgrades a normally-full op when ``op_id_pattern``
  and scope match.
* Glob ``op_id_pattern`` (``vault.kv.*``) matches both
  ``vault.kv.read`` and ``vault.kv.list``.
* Most-specific-wins -- scoped rule beats op-wide rule; id-order
  tie-break is deterministic.
* Per-tenant cache -- first lookup issues one DB query, subsequent
  lookups within the TTL window are DB-free (verified via a wrapper
  on :func:`_load_tenant_rules`).
* :func:`invalidate_tenant_cache` clears the entry for that tenant
  only; other tenants are untouched.
* Fail-open -- a DB failure during cache load logs and drops to the
  default branch.

The tests run against ``sqlite+aiosqlite`` via the shared engine cache
that the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` already pre-migrates to ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast.overrides import (
    compute_effective_broadcast_detail,
    invalidate_tenant_cache,
    read_request_override,
    reset_overrides_cache_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BroadcastOverride, Tenant
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_overrides_cache() -> Iterator[None]:
    """Reset the module-level resolver cache between tests.

    The cache is process-wide state -- without this fixture a row added
    in one test would shadow into the next test's lookup. The
    bracketing reset (before and after) also guards against tests that
    forget the post-test reset and would otherwise leak state into the
    next module.
    """
    reset_overrides_cache_for_testing()
    yield
    reset_overrides_cache_for_testing()


@pytest.fixture(autouse=True)
def _clear_structlog_contextvars() -> Iterator[None]:
    """Clear structlog contextvars between tests.

    :func:`read_request_override` reads from structlog contextvars; a
    case that binds ``broadcast_detail_override`` must not leak into a
    later case. The fixture is autouse so every test starts from a
    clean slate.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


async def _seed_tenant(session: AsyncSession, slug: str) -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its id."""
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
    await session.commit()
    return tenant_id


async def _seed_override(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    op_id_pattern: str,
    detail: str,
    scope_field: str | None = None,
    scope_value: str | None = None,
    override_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one :class:`BroadcastOverride` row and return its id."""
    rule_id = override_id or uuid.uuid4()
    session.add(
        BroadcastOverride(
            id=rule_id,
            tenant_id=tenant_id,
            op_id_pattern=op_id_pattern,
            scope_field=scope_field,
            scope_value=scope_value,
            detail=detail,
            created_by_sub="op-test",
        ),
    )
    await session.commit()
    return rule_id


# ---------------------------------------------------------------------------
# Precedence ladder -- the three branches in order.
# ---------------------------------------------------------------------------


class TestPrecedenceLadder:
    """Every entry in the resolver's precedence ladder is honoured."""

    @pytest.mark.asyncio
    async def test_default_branch_aggregate_for_credential_read(self) -> None:
        """No overrides + no request_override → static classify_op default."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="default-cred")

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vault.kv.read",
            tenant_id=tenant_id,
            raw_params={"path": "secret/foo"},
            request_override=None,
        )
        assert op_class == "credential_read"
        assert detail == "aggregate"
        assert origin == "default"

    @pytest.mark.asyncio
    async def test_default_branch_full_for_read(self) -> None:
        """Non-sensitive class defaults to full detail."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="default-read")

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vsphere.vm.list",
            tenant_id=tenant_id,
            raw_params={"folder": "prod"},
            request_override=None,
        )
        assert op_class == "read"
        assert detail == "full"
        assert origin == "default"

    @pytest.mark.asyncio
    async def test_request_override_upgrades_credential_read_to_full(self) -> None:
        """``request_override="full"`` on a sensitive class → full + request_override origin."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="req-override-cred")

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vault.kv.read",
            tenant_id=tenant_id,
            raw_params={"path": "secret/foo"},
            request_override="full",
        )
        assert op_class == "credential_read"
        assert detail == "full"
        assert origin == "request_override"

    @pytest.mark.asyncio
    async def test_request_override_upgrades_audit_query_to_full(self) -> None:
        """``request_override="full"`` on audit_query → full + request_override origin."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="req-override-audit")

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="audit.query",
            tenant_id=tenant_id,
            raw_params={"filter": "principal=op-1"},
            request_override="full",
        )
        assert op_class == "audit_query"
        assert detail == "full"
        assert origin == "request_override"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("op_id", "expected_class"),
        [
            ("vault.kv.put", "credential_write"),
            ("vault.auth.userpass.write", "credential_write"),
            ("vault.token.create", "credential_mint"),
            ("vault.auth.approle.generate_secret_id", "credential_mint"),
        ],
    )
    async def test_request_override_cannot_upgrade_secret_material_class(
        self, op_id: str, expected_class: str
    ) -> None:
        """``request_override="full"`` is ignored on secret-material classes (G11.7-T1 #1401).

        ``credential_write`` (request secret) and ``credential_mint``
        (response secret) are non-upgradeable: no per-call override may
        surface the credential on the feed. The resolver stays at the
        aggregate default with ``origin="default"`` — the upgrade branch
        is skipped entirely.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug=f"noupgrade-{expected_class}-{op_id[-6:]}")

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id=op_id,
            tenant_id=tenant_id,
            raw_params={"data": {"password": "leak-me"}},
            request_override="full",
        )
        assert op_class == expected_class
        assert detail == "aggregate"
        assert origin == "default"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("op_id", "expected_class"),
        [
            ("vault.kv.put", "credential_write"),
            ("vault.token.create", "credential_mint"),
        ],
    )
    async def test_tenant_rule_cannot_upgrade_secret_material_class(
        self, op_id: str, expected_class: str
    ) -> None:
        """A ``detail="full"`` tenant rule is clamped to aggregate on secret-material classes.

        Even an explicit per-tenant override row may not surface a
        minted/written credential on the feed (G11.7-T1 #1401); the
        resolver clamps the rule's ``full`` back to ``aggregate`` while
        still attributing the matched rule in ``origin``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug=f"clamp-{expected_class}-{op_id[-6:]}")
            rule_id = await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern=op_id,
                detail="full",
            )

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id=op_id,
            tenant_id=tenant_id,
            raw_params={"data": {"password": "leak-me"}},
            request_override=None,
        )
        assert op_class == expected_class
        assert detail == "aggregate"
        assert origin == f"tenant_rule:{rule_id}"

    @pytest.mark.asyncio
    async def test_request_override_ignored_on_non_sensitive_class(self) -> None:
        """``request_override="full"`` on a ``read`` op is a no-op (already full).

        Pins the AC: per-call override only fires on sensitive classes.
        The origin stays ``"default"`` because the override branch was
        not entered.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="req-override-noop")

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vsphere.vm.list",
            tenant_id=tenant_id,
            raw_params={},
            request_override="full",
        )
        assert op_class == "read"
        assert detail == "full"
        assert origin == "default"

    @pytest.mark.asyncio
    async def test_tenant_rule_downgrades_read_to_aggregate(self) -> None:
        """Matching rule with ``detail="aggregate"`` collapses a read op.

        Pins the canonical AC example: configure
        ``op_id_pattern="k8s.configmap.info"``,
        ``scope_field="namespace"``, ``scope_value="kube-system"``,
        ``detail="aggregate"`` → publish ``k8s.configmap.info`` with
        ``namespace=kube-system`` → resolver returns aggregate.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="rule-downgrade")
            rule_id = await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="k8s.configmap.info",
                scope_field="namespace",
                scope_value="kube-system",
                detail="aggregate",
            )

        op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="k8s.configmap.info",
            tenant_id=tenant_id,
            raw_params={"namespace": "kube-system"},
            request_override=None,
        )
        assert op_class == "read"
        assert detail == "aggregate"
        assert origin == f"tenant_rule:{rule_id}"


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------


class TestScopeMatching:
    """Scope field / value pairs key against raw_params correctly."""

    @pytest.mark.asyncio
    async def test_namespace_mismatch_falls_through(self) -> None:
        """Rule scoped to namespace=A; request has namespace=B → no match."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="ns-mismatch")
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="k8s.configmap.info",
                scope_field="namespace",
                scope_value="kube-system",
                detail="aggregate",
            )

        _op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="k8s.configmap.info",
            tenant_id=tenant_id,
            raw_params={"namespace": "default"},
            request_override=None,
        )
        assert detail == "full"
        assert origin == "default"

    @pytest.mark.asyncio
    async def test_target_name_match(self) -> None:
        """``scope_field="target_name"`` reads ``raw_params["target"]``.

        The publisher merges request params and response summary; the
        connector's target alias lands under the ``target`` key.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="target-name")
            rule_id = await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vsphere.vm.list",
                scope_field="target_name",
                scope_value="rdc-vcenter",
                detail="aggregate",
            )

        _op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vsphere.vm.list",
            tenant_id=tenant_id,
            raw_params={"target": "rdc-vcenter", "folder": "prod"},
            request_override=None,
        )
        assert detail == "aggregate"
        assert origin == f"tenant_rule:{rule_id}"

    @pytest.mark.asyncio
    async def test_null_scope_matches_every_request(self) -> None:
        """Op-wide rule (scope_field IS NULL) always matches the pattern."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="op-wide")
            rule_id = await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="k8s.configmap.info",
                scope_field=None,
                scope_value=None,
                detail="aggregate",
            )

        _op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="k8s.configmap.info",
            tenant_id=tenant_id,
            raw_params={"namespace": "anything"},
            request_override=None,
        )
        assert detail == "aggregate"
        assert origin == f"tenant_rule:{rule_id}"

    @pytest.mark.asyncio
    async def test_unknown_scope_field_is_non_matching(self) -> None:
        """A rule with an unknown scope_field is policy drift -- treat as no-match.

        T4's API layer enforces the small allowlist
        (``namespace`` / ``target_name``); a row with anything else is a
        manual-INSERT artifact. The resolver logs the drift and falls
        through rather than crashing the publish path.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="unknown-scope")
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vsphere.vm.list",
                scope_field="not_an_allowed_field",
                scope_value="x",
                detail="aggregate",
            )

        _op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vsphere.vm.list",
            tenant_id=tenant_id,
            raw_params={},
            request_override=None,
        )
        assert detail == "full"
        assert origin == "default"


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


class TestGlobMatching:
    """fnmatch.fnmatchcase semantics on op_id_pattern."""

    @pytest.mark.asyncio
    async def test_glob_matches_multiple_ops(self) -> None:
        """A single ``vault.kv.*`` rule matches both ``.read`` and ``.list``."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="glob")
            rule_id = await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vault.kv.*",
                detail="full",
            )

        for op_id in ("vault.kv.read", "vault.kv.list"):
            _op_class, detail, origin = await compute_effective_broadcast_detail(
                op_id=op_id,
                tenant_id=tenant_id,
                raw_params={},
                request_override=None,
            )
            assert detail == "full", op_id
            assert origin == f"tenant_rule:{rule_id}", op_id

    @pytest.mark.asyncio
    async def test_literal_pattern_only_matches_exact_op(self) -> None:
        """A literal pattern doesn't match anything but the exact string."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="literal-only")
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="k8s.configmap.info",
                detail="aggregate",
            )

        # Different op_id -- the rule does not match.
        _op_class, _detail, origin = await compute_effective_broadcast_detail(
            op_id="k8s.configmap.list",
            tenant_id=tenant_id,
            raw_params={},
            request_override=None,
        )
        assert origin == "default"


# ---------------------------------------------------------------------------
# Most-specific-wins + deterministic tie-break
# ---------------------------------------------------------------------------


class TestMostSpecificWins:
    """Scoped rule beats op-wide; id-order tie-break."""

    @pytest.mark.asyncio
    async def test_scoped_rule_beats_op_wide(self) -> None:
        """Op-wide aggregate + scoped full → scoped wins → full."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="scoped-wins")
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vsphere.vm.list",
                scope_field=None,
                scope_value=None,
                detail="aggregate",
            )
            scoped_id = await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vsphere.vm.list",
                scope_field="target_name",
                scope_value="rdc-vcenter",
                detail="full",
            )

        _op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vsphere.vm.list",
            tenant_id=tenant_id,
            raw_params={"target": "rdc-vcenter"},
            request_override=None,
        )
        assert detail == "full"
        assert origin == f"tenant_rule:{scoped_id}"

    @pytest.mark.asyncio
    async def test_id_order_tie_break(self) -> None:
        """Two op-wide rules → the lexicographically-smaller id wins.

        Two op-wide rules with conflicting detail values is a policy
        config error (T4's CRUD verbs use the composite unique index
        to prevent it). When it happens, the resolver picks a stable
        winner so two workers see the same verdict.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="id-tiebreak")
            # Use deterministic ids to assert which wins.
            low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
            high_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vault.kv.*",
                detail="full",
                override_id=low_id,
            )
            # NOTE: the composite unique index has (op_id_pattern,
            # scope_field, scope_value) so two op-wide rules with the
            # same pattern would conflict. Use distinct patterns that
            # both match the same op so the unique index passes.
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vault.*",
                detail="aggregate",
                override_id=high_id,
            )

        _op_class, detail, origin = await compute_effective_broadcast_detail(
            op_id="vault.kv.read",
            tenant_id=tenant_id,
            raw_params={},
            request_override=None,
        )
        # Both rules are op-wide; lexicographically-smaller id (low_id)
        # wins -> detail="full".
        assert detail == "full"
        assert origin == f"tenant_rule:{low_id}"


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestPerTenantCache:
    """Lookup hot path: one DB query on first call, none after."""

    @pytest.mark.asyncio
    async def test_first_lookup_issues_one_query_subsequent_are_cached(self) -> None:
        """Cache miss → 1 DB pull; cache hits within TTL → 0 pulls.

        Counts ``get_sessionmaker`` calls inside
        :func:`_load_tenant_rules` -- the helper only invokes the
        sessionmaker on the cache-miss path, so its call count is the
        DB-query count.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_id = await _seed_tenant(session, slug="cache-count")
            await _seed_override(
                session,
                tenant_id=tenant_id,
                op_id_pattern="vault.kv.*",
                detail="full",
            )

        with patch(
            "meho_backplane.broadcast.overrides.get_sessionmaker",
            wraps=get_sessionmaker,
        ) as sessionmaker_spy:
            # First call hydrates the cache.
            await compute_effective_broadcast_detail(
                op_id="vault.kv.read",
                tenant_id=tenant_id,
                raw_params={},
                request_override=None,
            )
            # Second + third calls within TTL window hit the cache.
            for _ in range(2):
                await compute_effective_broadcast_detail(
                    op_id="vault.kv.list",
                    tenant_id=tenant_id,
                    raw_params={},
                    request_override=None,
                )

        # Only one DB query fired -- the cache absorbed the next two
        # resolver calls.
        assert sessionmaker_spy.call_count == 1


class TestCacheInvalidation:
    """invalidate_tenant_cache + cross-tenant isolation."""

    @pytest.mark.asyncio
    async def test_invalidate_tenant_cache_clears_only_named_tenant(self) -> None:
        """Tenant A's cache is dropped; tenant B's remains intact.

        Asserts the cache state at three points: post-hydrate (both in
        cache), post-invalidate (A removed, B intact), post-reload (A
        repopulated, B unchanged). Pins the AC: "invalidate_tenant_cache
        clears the cache for that tenant only; other tenants' cached
        rows untouched".
        """
        from meho_backplane.broadcast.overrides import _TENANT_CACHE

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            tenant_a = await _seed_tenant(session, slug="cache-invalidate-a")
            tenant_b = await _seed_tenant(session, slug="cache-invalidate-b")
            await _seed_override(
                session,
                tenant_id=tenant_a,
                op_id_pattern="vault.*",
                detail="full",
            )
            await _seed_override(
                session,
                tenant_id=tenant_b,
                op_id_pattern="vault.*",
                detail="full",
            )

        # Hydrate both tenants' caches.
        for tid in (tenant_a, tenant_b):
            await compute_effective_broadcast_detail(
                op_id="vault.kv.read",
                tenant_id=tid,
                raw_params={},
                request_override=None,
            )
        assert tenant_a in _TENANT_CACHE
        assert tenant_b in _TENANT_CACHE

        # Invalidate only tenant A.
        invalidate_tenant_cache(tenant_a)
        assert tenant_a not in _TENANT_CACHE
        assert tenant_b in _TENANT_CACHE

        # Next resolver call for tenant A repopulates A's cache; B
        # remains untouched.
        await compute_effective_broadcast_detail(
            op_id="vault.kv.read",
            tenant_id=tenant_a,
            raw_params={},
            request_override=None,
        )
        assert tenant_a in _TENANT_CACHE
        assert tenant_b in _TENANT_CACHE

    @pytest.mark.asyncio
    async def test_invalidate_missing_tenant_is_silent_noop(self) -> None:
        """Calling :func:`invalidate_tenant_cache` for an uncached tenant is fine.

        T4's CRUD verbs might call invalidate on a tenant that hasn't
        published an event since process start (no cache entry yet);
        the helper silently no-ops instead of raising.
        """
        invalidate_tenant_cache(uuid.uuid4())  # should not raise


# ---------------------------------------------------------------------------
# Fail-open on DB failure
# ---------------------------------------------------------------------------


class TestFailOpen:
    """A DB failure during the cache load drops to the default branch."""

    @pytest.mark.asyncio
    async def test_db_failure_inside_load_returns_default_branch(self) -> None:
        """The cache-load wrapper catches DB exceptions internally.

        :func:`_load_tenant_rules` wraps its ``session.execute`` call in
        a try/except: a DB failure logs ``broadcast_override_cache_load_failed``
        and returns an empty list (no cache write -- caching a
        degraded read would extend the failure into a 60s window). The
        resolver then takes the default branch with ``origin="default"``.
        Pins the AC: "Python BPs §8 -- resolver fails open when DB
        unreachable: return the static classify_op default + log;
        never blow up the publish path".
        """
        tenant_id = uuid.uuid4()  # no DB row needed; loader raises before reaching it

        with patch(
            "meho_backplane.broadcast.overrides.get_sessionmaker",
        ) as broken:
            broken.side_effect = RuntimeError("simulated sessionmaker outage")
            op_class, detail, origin = await compute_effective_broadcast_detail(
                op_id="vault.kv.read",
                tenant_id=tenant_id,
                raw_params={},
                request_override=None,
            )
        assert op_class == "credential_read"
        assert detail == "aggregate"
        assert origin == "default"

    @pytest.mark.asyncio
    async def test_failed_load_is_not_cached(self) -> None:
        """A failed DB load does not populate the cache.

        Caching the empty rule list would extend a transient failure
        into a 60s "no overrides" window. The loader returns the empty
        list to the resolver without writing to ``_TENANT_CACHE``.
        """
        from meho_backplane.broadcast.overrides import _TENANT_CACHE

        tenant_id = uuid.uuid4()
        with patch(
            "meho_backplane.broadcast.overrides.get_sessionmaker",
        ) as broken:
            broken.side_effect = RuntimeError("simulated sessionmaker outage")
            await compute_effective_broadcast_detail(
                op_id="vault.kv.read",
                tenant_id=tenant_id,
                raw_params={},
                request_override=None,
            )
        assert tenant_id not in _TENANT_CACHE


# ---------------------------------------------------------------------------
# read_request_override -- contextvar plumbing shim
# ---------------------------------------------------------------------------


class TestReadRequestOverride:
    """Opt-in-only filter on the contextvar value."""

    def test_no_contextvar_returns_none(self) -> None:
        assert read_request_override() is None

    def test_full_value_passes_through(self) -> None:
        import structlog

        structlog.contextvars.bind_contextvars(broadcast_detail_override="full")
        try:
            assert read_request_override() == "full"
        finally:
            structlog.contextvars.unbind_contextvars("broadcast_detail_override")

    def test_aggregate_value_filtered_to_none(self) -> None:
        """A "weaken-via-header" request is rejected -- maps to None."""
        import structlog

        structlog.contextvars.bind_contextvars(broadcast_detail_override="aggregate")
        try:
            assert read_request_override() is None
        finally:
            structlog.contextvars.unbind_contextvars("broadcast_detail_override")
