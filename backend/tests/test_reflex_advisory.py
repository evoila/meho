# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time reflex advisory (#3133, Initiative #3128).

The third in-band ``extras`` fragment on successful dispatch responses,
beside #2550 ``target_activity_advisory`` and #2718
``checks_alert_advisory``. It nudges an agent session toward the
coordination discipline the backplane exposes but does not otherwise
enforce -- read the feed before acting, announce before mutating -- with
one compact ``extras["reflex_advisory"]`` one-liner per fired heuristic,
deduped per (session, heuristic) via Valkey ``SET NX EX``. Advisory only:
it never gates a dispatch and every failure mode fails open.

Coverage mirrors the acceptance criteria on the issue, by name:

* the ``0`` window knob short-circuits before session resolution / I/O;
* the fragment is session-scoped -- a dispatch with no agent session
  (CLI / non-MCP) gets no nudge;
* read-before-act fires when the session has no prior
  ``meho_broadcast_recent`` audit row and stays silent once it does, and
  fires for a direct MCP session resolved via the ``Mcp-Session-Id``
  header fallback -- the live field shape (#3149);
* announce-before-mutate fires for a write-class op with no covering
  claim and stays silent when a claim covers it; read-class ops never
  trip it;
* read-before-act has priority over announce-before-mutate, and the two
  dedupe independently (a deduped read falls through to announce with the
  announce key untouched);
* a forced builder exception leaves the dispatch byte-identical and
  emits a ``reflex_advisory_failed`` structlog event;
* dispatch integration -- a successful ``dispatch()`` carries the
  fragment once per session+window, absent thereafter, and the three
  advisory fragments coexist on one response.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import structlog

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.announce_gate import reset_announce_gate_cache_for_testing
from meho_backplane.broadcast.history import ADVISORY_EXTRAS_KEY
from meho_backplane.broadcast.reflex import (
    _ANNOUNCE_NUDGE,
    _READ_NUDGE,
    REFLEX_ADVISORY_EXTRAS_KEY,
    build_reflex_advisory,
)
from meho_backplane.checks.advisory import CHECKS_ADVISORY_EXTRAS_KEY
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, CheckDashboard, Tenant
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-000000003133")
_SESSION = UUID("33333333-3333-3333-3333-333333333333")
_RECENT_PATH = "/mcp/tools/call/meho_broadcast_recent"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin required settings + a stub broadcast URL; reset caches."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_announce_gate_cache_for_testing()
    yield
    reset_broadcast_client_for_testing()
    reset_announce_gate_cache_for_testing()
    get_settings.cache_clear()


def _operator(sub: str = "user-b", *, tenant_id: UUID = _TENANT) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@contextmanager
def _bound_session(session_id: UUID | None) -> Iterator[None]:
    """Bind (or clear) the agent-run session contextvar for the block."""
    token = agent_session_id_var.set(session_id)
    try:
        yield
    finally:
        agent_session_id_var.reset(token)


@contextmanager
def _bound_mcp_session(session_id: UUID) -> Iterator[None]:
    """Bind only the ``mcp_session_id`` header contextvar for the block.

    Reproduces a *direct* MCP client's request context: no in-process
    agent loop bound :data:`agent_session_id_var`, so the session is
    carried solely by the ``mcp_session_id`` structlog contextvar the
    transport binds from the ``Mcp-Session-Id`` header
    (:func:`meho_backplane.mcp.server._bind_mcp_session_id`). This is the
    live field shape :func:`resolve_agent_session_id` resolves via its
    header fallback (#3149).
    """
    with structlog.contextvars.bound_contextvars(mcp_session_id=str(session_id)):
        yield


async def _seed_tenant(tenant_id: UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Tenant"))
            await session.commit()


async def _seed_broadcast_recent(session_id: UUID, *, sub: str = "user-b") -> None:
    """Insert an ``audit_log`` row standing for a prior broadcast_recent call."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                operator_sub=sub,
                method="MCP",
                path=_RECENT_PATH,
                status_code=200,
                tenant_id=_TENANT,
                agent_session_id=session_id,
            )
        )
        await session.commit()


async def _seed_dashboard(*, name: str = "prod-health", state: str = "critical") -> UUID:
    dashboard_id = uuid.uuid4()
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=_TENANT,
                name=name,
                created_by_sub="op-admin",
                last_rollup_state=state,
            )
        )
        await session.commit()
    return dashboard_id


class _FakeSetStore:
    """Dict-backed ``SET NX EX`` double with the real claim contract.

    ``set`` returns ``True`` when the key was absent (claimed) and ``None``
    when it already existed -- ``redis-py``'s NX shape. ``calls`` and
    ``ex_values`` record every attempt so a test can assert claim count
    and the TTL separately.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ex_values: list[int] = []
        self.calls: int = 0

    async def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        assert nx, "the reflex advisory must claim with NX"
        assert ex is not None and ex > 0, "the reflex advisory must set a TTL"
        self.calls += 1
        self.ex_values.append(ex)
        if name in self.store:
            return None
        self.store[name] = value
        return True


def _patch_set(store: _FakeSetStore) -> Any:
    return patch.object(get_broadcast_client(), "set", new=store.set)


class _FakeNxPipeline:
    """Pipelined ``SET NX`` double for the sibling checks advisory (#2718).

    The checks fragment claims through ``client.pipeline(transaction=True)``
    rather than a bare ``set``; the three-fragment coexistence test needs
    that path stubbed too so the checks fragment is actually produced. All
    keys are claimed (this test only asserts the fragment rides the merge,
    not its dedupe).
    """

    def __init__(self) -> None:
        self._staged: list[tuple[str, str]] = []

    def pipeline(self, transaction: bool = True) -> _FakeNxPipeline:
        self._staged = []
        return self

    async def __aenter__(self) -> _FakeNxPipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> _FakeNxPipeline:
        self._staged.append((name, value))
        return self

    async def execute(self) -> list[Any]:
        staged, self._staged = self._staged, []
        return [True for _ in staged]


# ---------------------------------------------------------------------------
# Disable knob + session-scoping
# ---------------------------------------------------------------------------


async def test_window_zero_short_circuits_no_session_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0`` disables the feature before any session resolution or I/O."""
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    db_stub = MagicMock(side_effect=AssertionError("DB must not be touched"))
    set_stub = MagicMock(side_effect=AssertionError("Valkey must not be touched"))
    with (
        patch("meho_backplane.broadcast.reflex.get_sessionmaker", new=db_stub),
        patch.object(get_broadcast_client(), "set", new=set_stub),
        _bound_session(_SESSION),
    ):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {}
    db_stub.assert_not_called()


async def test_no_agent_session_yields_empty_and_no_io() -> None:
    """A dispatch with no agent session (CLI / non-MCP) gets no nudge."""
    db_stub = MagicMock(side_effect=AssertionError("DB must not be touched"))
    with (
        patch("meho_backplane.broadcast.reflex.get_sessionmaker", new=db_stub),
        _bound_session(None),
    ):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {}
    db_stub.assert_not_called()


# ---------------------------------------------------------------------------
# read-before-act
# ---------------------------------------------------------------------------


async def test_read_nudge_fires_when_session_never_read_broadcast() -> None:
    """No prior broadcast_recent row for the session -> the read nudge fires."""
    store = _FakeSetStore()
    with _patch_set(store), _bound_session(_SESSION):
        first = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
        second = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert first == {REFLEX_ADVISORY_EXTRAS_KEY: _READ_NUDGE}
    # Deduped: same (session, heuristic) is nudged once per window; the
    # read op does not qualify for the announce heuristic, so no fragment.
    assert second == {}
    assert store.ex_values[0] == 30 * 60


async def test_read_nudge_silent_once_session_has_read_broadcast() -> None:
    """A prior broadcast_recent row satisfies the discipline -- no nudge."""
    await _seed_broadcast_recent(_SESSION)
    store = _FakeSetStore()
    with _patch_set(store), _bound_session(_SESSION):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {}
    # Heuristic did not qualify, so no claim was ever attempted.
    assert store.calls == 0


async def test_read_nudge_is_per_session() -> None:
    """A prior read by one session does not satisfy another session."""
    await _seed_broadcast_recent(_SESSION)
    other = UUID("44444444-4444-4444-4444-444444444444")
    store = _FakeSetStore()
    with _patch_set(store), _bound_session(other):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {REFLEX_ADVISORY_EXTRAS_KEY: _READ_NUDGE}


async def test_read_nudge_fires_for_direct_mcp_session_via_header_fallback() -> None:
    """Live field shape (#3149): a direct MCP session -- no agent-run
    contextvar, session carried only by the ``Mcp-Session-Id`` header --
    still fires the read nudge.

    :func:`resolve_agent_session_id` resolves this session through its
    ``mcp_session_id`` fallback rather than :data:`agent_session_id_var`;
    the sibling read tests only exercise the agent-run path. The F5 field
    test (#3143) saw ``extras: {}`` for exactly this shape *not* because
    the resolution fails but because the lab ran ``v0.30.0``, which
    predates the feature (PR #3141 landed after the tag). This locks in
    that a header-resolved session emits, so the next deploy past
    ``v0.30.0`` is the only thing the field re-verification waits on.
    """
    store = _FakeSetStore()
    with _patch_set(store), _bound_session(None), _bound_mcp_session(_SESSION):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {REFLEX_ADVISORY_EXTRAS_KEY: _READ_NUDGE}
    assert store.ex_values[0] == 30 * 60


# ---------------------------------------------------------------------------
# announce-before-mutate
# ---------------------------------------------------------------------------


async def test_announce_nudge_fires_for_write_without_claim() -> None:
    """A write-class op with no covering claim fires the announce nudge."""
    await _seed_broadcast_recent(_SESSION)  # satisfy read so announce is reached
    store = _FakeSetStore()
    with (
        _patch_set(store),
        patch(
            "meho_backplane.broadcast.reflex.caller_has_active_announce_claim",
            new=AsyncMock(return_value=False),
        ),
        _bound_session(_SESSION),
    ):
        advisory = await build_reflex_advisory(
            _operator(), op_id="vault.kv.delete", target_name="vault-1"
        )
    assert advisory == {REFLEX_ADVISORY_EXTRAS_KEY: _ANNOUNCE_NUDGE}


async def test_announce_nudge_silent_when_claim_covers_op() -> None:
    """An active covering claim suppresses the announce nudge."""
    await _seed_broadcast_recent(_SESSION)
    store = _FakeSetStore()
    with (
        _patch_set(store),
        patch(
            "meho_backplane.broadcast.reflex.caller_has_active_announce_claim",
            new=AsyncMock(return_value=True),
        ),
        _bound_session(_SESSION),
    ):
        advisory = await build_reflex_advisory(
            _operator(), op_id="vault.kv.delete", target_name="vault-1"
        )
    assert advisory == {}
    assert store.calls == 0


async def test_read_class_op_never_trips_announce() -> None:
    """A read-class op never trips announce, even with no claim."""
    await _seed_broadcast_recent(_SESSION)
    store = _FakeSetStore()
    claim = AsyncMock(return_value=False)
    with (
        _patch_set(store),
        patch("meho_backplane.broadcast.reflex.caller_has_active_announce_claim", new=claim),
        _bound_session(_SESSION),
    ):
        advisory = await build_reflex_advisory(
            _operator(), op_id="vault.kv.list", target_name="vault-1"
        )
    assert advisory == {}
    # Read-class short-circuits before the (expensive) claim scan.
    claim.assert_not_called()


# ---------------------------------------------------------------------------
# Priority + independent dedupe
# ---------------------------------------------------------------------------


async def test_read_has_priority_then_announce_on_next_call() -> None:
    """Read wins first; the announce key is untouched so it fires next call."""
    store = _FakeSetStore()
    with (
        _patch_set(store),
        patch(
            "meho_backplane.broadcast.reflex.caller_has_active_announce_claim",
            new=AsyncMock(return_value=False),
        ),
        _bound_session(_SESSION),
    ):
        # First write-class dispatch: read qualifies (no prior recent) AND
        # announce qualifies, but read wins and the announce key is left
        # unclaimed.
        first = await build_reflex_advisory(
            _operator(), op_id="vault.kv.delete", target_name="vault-1"
        )
        # Second dispatch: read is now deduped, so it falls through to the
        # still-unclaimed announce heuristic.
        second = await build_reflex_advisory(
            _operator(), op_id="vault.kv.delete", target_name="vault-1"
        )
    assert first == {REFLEX_ADVISORY_EXTRAS_KEY: _READ_NUDGE}
    assert second == {REFLEX_ADVISORY_EXTRAS_KEY: _ANNOUNCE_NUDGE}


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


async def test_fail_open_on_db_error() -> None:
    """A DB teardown mid-read yields ``{}`` and warn-logs; never raises."""
    with (
        patch(
            "meho_backplane.broadcast.reflex.get_sessionmaker",
            new=MagicMock(side_effect=RuntimeError("db down")),
        ),
        patch("meho_backplane.broadcast.reflex._log") as mock_log,
        _bound_session(_SESSION),
    ):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {}
    assert any(
        call.args and call.args[0] == "reflex_advisory_failed"
        for call in mock_log.warning.call_args_list
    )


async def test_fail_open_on_valkey_error() -> None:
    """A Valkey teardown mid-claim yields ``{}`` and warn-logs; never raises."""
    from redis import exceptions as redis_exceptions

    with (
        patch.object(
            get_broadcast_client(),
            "set",
            new=AsyncMock(side_effect=redis_exceptions.ConnectionError("refused")),
        ),
        patch("meho_backplane.broadcast.reflex._log") as mock_log,
        _bound_session(_SESSION),
    ):
        advisory = await build_reflex_advisory(_operator(), op_id="vault.kv.list", target_name=None)
    assert advisory == {}
    assert any(
        call.args and call.args[0] == "reflex_advisory_failed"
        for call in mock_log.warning.call_args_list
    )


# ---------------------------------------------------------------------------
# Dispatch integration + three-fragment coexistence
# ---------------------------------------------------------------------------


class _NoOpVaultConnector(Connector):
    """Connector class satisfying resolver lookups in typed-dispatch tests."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _module_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params}


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    def __init__(self, *, name: str = "vault-1") -> None:
        self.product = "vault"
        self.fingerprint = _FakeFingerprint(version=None)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = name
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


@pytest.fixture(autouse=True)
def _reset_dispatch_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def _quiet_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _register_op(
    op_id: str, stub_embedding_service: AsyncMock, *, safety_level: str = "safe"
) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_module_handler,
        summary="Test op.",
        description="Test op.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
        safety_level=safety_level,  # type: ignore[arg-type]
    )


async def test_dispatch_carries_reflex_fragment_then_dedupes(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """A read-class MCP-session dispatch carries the read nudge, then dedupes."""
    await _seed_tenant()
    await _register_op("vault.kv.list", stub_embedding_service)
    store = _FakeSetStore()
    with _patch_set(store), _bound_session(_SESSION):
        first = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(),
            params={"path": "/secret"},
        )
        second = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(),
            params={"path": "/secret"},
        )
    assert first.status == "ok"
    assert first.extras[REFLEX_ADVISORY_EXTRAS_KEY] == _READ_NUDGE
    assert second.status == "ok"
    assert REFLEX_ADVISORY_EXTRAS_KEY not in second.extras


async def test_dispatch_without_session_carries_no_reflex_fragment(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """An operator CLI-shaped dispatch (no session) carries no reflex nudge."""
    await _seed_tenant()
    await _register_op("vault.kv.list", stub_embedding_service)
    store = _FakeSetStore()
    with _patch_set(store), _bound_session(None):
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(),
            params={"path": "/secret"},
        )
    assert result.status == "ok"
    assert REFLEX_ADVISORY_EXTRAS_KEY not in result.extras
    assert store.calls == 0


async def test_three_advisory_fragments_coexist_on_one_response(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """#2550 + #2718 + #3133 fragments ride one ``extras`` -- distinct keys.

    A write-class dispatch on a target with peer activity (#2550) by a
    caller whose tenant has a critical Dashboard (#2718) in an MCP session
    that never read the feed (#3133 read nudge) carries all three
    fragments -- the plain dict merge is a clobber-free union.
    """
    await _seed_dashboard(state="critical")
    await _register_op("vault.kv.delete", stub_embedding_service)
    peer_event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=_TENANT,
        principal_sub="peer-a",
        target_name="vault-1",
        op_id="vault.kv.delete",
        op_class="write",
        result_status="ok",
        audit_id=uuid.uuid4(),
        actor_sub=None,
        payload={"op_class": "write", "params": {}, "result_status": "ok"},
    )

    async def _xrevrange(name: str, **kwargs: Any) -> list[tuple[str, dict[str, str]]]:
        return [("1747800001000-0", {"event": peer_event.model_dump_json()})]

    store = _FakeSetStore()
    pipe = _FakeNxPipeline()
    bc = get_broadcast_client()
    with (
        _patch_set(store),
        patch.object(bc, "pipeline", new=pipe.pipeline),
        patch.object(bc, "xrevrange", new=_xrevrange),
        _bound_session(_SESSION),
    ):
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.delete",
            target=_FakeTarget(),
            params={},
        )
    assert result.status == "ok"
    assert result.extras[ADVISORY_EXTRAS_KEY][0]["principal_sub"] == "peer-a"
    assert result.extras[CHECKS_ADVISORY_EXTRAS_KEY][0]["state"] == "critical"
    assert result.extras[REFLEX_ADVISORY_EXTRAS_KEY] == _READ_NUDGE
