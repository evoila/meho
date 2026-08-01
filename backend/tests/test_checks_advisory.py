# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time checks-alert advisory (#2718, Initiative #2716).

A successful dispatch by a caller whose tenant has a Dashboard with a
``degraded``/``critical`` ``last_rollup_state`` memo carries a compact
``extras["checks_alert_advisory"]`` naming it -- once per (caller,
dashboard, state) window via a Valkey ``SET NX EX`` dedupe. Advisory
only: it never gates a dispatch, and every failure mode fails open.

Coverage mirrors the acceptance criteria on the issue, by name:

* the ``0`` window knob short-circuits before any DB or Valkey I/O;
* a green tenant (``NULL`` / ``ok`` memos) yields no advisory and no
  Valkey call;
* NX dedupe -- the same caller sees a given (dashboard, state) once per
  window; a state change mints a new key and re-announces;
* per-caller isolation -- distinct principals each get their own
  reminder;
* the ``_ADVISORY_MAX_DASHBOARDS`` cap bounds both the fragment and the
  ``SET`` commands staged when a whole tenant goes red, and the claims
  cost exactly one awaited round-trip however many rows survive it;
* a Valkey teardown and a DB teardown both fail open (``{}``,
  warn-logged, dispatch unaffected);
* dispatch integration -- a successful ``dispatch()`` response carries
  the fragment (read-class op included -- the deliberate divergence
  from the write-gated #2550 precedent) and an immediate second
  dispatch by the same caller does not;
* coexistence -- the #2550 ``target_activity_advisory`` and this
  fragment ride the same ``extras`` on one write-class response.

The Valkey pipeline is faked with real NX-per-key semantics (a dict)
and real buffering, so the dedupe assertions exercise the actual claim
protocol and the round-trip count is observable; the broadcast client
never opens a socket (the stub URL + per-test method patches, mirroring
``test_broadcast_target_advisory``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import update

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.history import ADVISORY_EXTRAS_KEY
from meho_backplane.checks.advisory import (
    _ADVISORY_MAX_DASHBOARDS,
    CHECKS_ADVISORY_EXTRAS_KEY,
    build_checks_alert_advisory,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard, Tenant
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000c0c0")
_AUDIT_ID = UUID("55555555-5555-5555-5555-555555555555")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin required settings + a stub broadcast URL; reset the client cache.

    Mirrors ``test_broadcast_target_advisory``: the Valkey client is
    rebuilt against a stub URL and every command it would issue is
    patched per-test, so no socket ever opens.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("CHECKS_ALERT_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
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


async def _seed_dashboard(
    *,
    name: str = "prod-health",
    state: str | None = "critical",
    tenant_id: UUID = _TENANT,
) -> UUID:
    """Insert a tenant (idempotent) + one dashboard with the given memo."""
    dashboard_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Tenant"))
        session.add(
            CheckDashboard(
                id=dashboard_id,
                tenant_id=tenant_id,
                name=name,
                created_by_sub="op-admin",
                last_rollup_state=state,
            )
        )
        await session.commit()
    return dashboard_id


async def _set_memo(dashboard_id: UUID, state: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(CheckDashboard)
            .where(CheckDashboard.id == dashboard_id)
            .values(last_rollup_state=state)
        )
        await session.commit()


class _FakeNxStore:
    """Dict-backed pipelined ``SET NX`` double with the real contract.

    Buffers like redis-py 8.0.1's async ``Pipeline``: ``pipe.set(...)``
    stages the command and returns the pipeline synchronously, and the
    awaited ``execute()`` is the one round-trip that replies with a
    result per staged command. Results carry ``parse_set_result``'s
    shape -- ``True`` when the key was absent (claimed), ``None`` when it
    already existed. TTL is not modelled: window expiry is Valkey's
    contract, not this feature's logic.

    ``ex_values`` records every ``SET`` staged and ``round_trips`` every
    awaited ``execute()``, so a test can assert the fan-out's *size* and
    its *cost* separately -- the two halves of the bound.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ex_values: list[int] = []
        self.round_trips: int = 0
        self._staged: list[tuple[str, str, int]] = []

    def pipeline(self, transaction: bool = True) -> _FakeNxStore:
        assert transaction, "the advisory claims in one MULTI/EXEC batch"
        self._staged = []
        return self

    async def __aenter__(self) -> _FakeNxStore:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> _FakeNxStore:
        assert nx, "the advisory must claim with NX"
        assert ex is not None and ex > 0, "the advisory must set a TTL"
        self._staged.append((name, value, ex))
        return self

    async def execute(self) -> list[Any]:
        self.round_trips += 1
        staged, self._staged = self._staged, []
        results: list[Any] = []
        for name, value, ex in staged:
            self.ex_values.append(ex)
            if name in self.store:
                results.append(None)
            else:
                self.store[name] = value
                results.append(True)
        return results


def _patch_nx(store: _FakeNxStore) -> Any:
    return patch.object(get_broadcast_client(), "pipeline", new=store.pipeline)


# ---------------------------------------------------------------------------
# Disable knob + green tenant
# ---------------------------------------------------------------------------


async def test_window_zero_short_circuits_no_db_no_valkey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0`` disables the feature -- no sessionmaker call, no Valkey call."""
    monkeypatch.setenv("CHECKS_ALERT_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    db_stub = MagicMock(side_effect=AssertionError("DB must not be touched"))
    valkey_pipeline = MagicMock(side_effect=AssertionError("Valkey must not be touched"))
    with (
        patch("meho_backplane.checks.advisory.get_sessionmaker", new=db_stub),
        patch.object(get_broadcast_client(), "pipeline", new=valkey_pipeline),
    ):
        advisory = await build_checks_alert_advisory(_operator())
    assert advisory == {}
    db_stub.assert_not_called()


async def test_green_tenant_yields_empty_and_no_valkey_call() -> None:
    """``NULL`` and ``ok`` memos never match -- no fragment, no ``SET``."""
    await _seed_dashboard(name="never-transitioned", state=None)
    await _seed_dashboard(name="recovered", state="ok")
    valkey_pipeline = MagicMock()
    with patch.object(get_broadcast_client(), "pipeline", new=valkey_pipeline):
        advisory = await build_checks_alert_advisory(_operator())
    assert advisory == {}
    valkey_pipeline.assert_not_called()


# ---------------------------------------------------------------------------
# NX dedupe + state-change re-announce + per-caller isolation
# ---------------------------------------------------------------------------


async def test_non_green_dashboard_surfaces_once_per_window() -> None:
    """First call carries the fragment; the same caller's second call doesn't."""
    dashboard_id = await _seed_dashboard(state="critical")
    store = _FakeNxStore()
    with _patch_nx(store):
        first = await build_checks_alert_advisory(_operator())
        second = await build_checks_alert_advisory(_operator())
    assert first == {
        CHECKS_ADVISORY_EXTRAS_KEY: [
            {"dashboard_id": str(dashboard_id), "name": "prod-health", "state": "critical"}
        ]
    }
    assert second == {}
    # The dedupe key carries the 30-minute window as the TTL.
    assert store.ex_values == [30 * 60, 30 * 60]
    # One batch per dispatch -- never one per row.
    assert store.round_trips == 2


async def test_state_change_mints_new_key_and_re_announces() -> None:
    """Escalation to a different state re-announces inside the window."""
    dashboard_id = await _seed_dashboard(state="degraded")
    store = _FakeNxStore()
    with _patch_nx(store):
        first = await build_checks_alert_advisory(_operator())
        await _set_memo(dashboard_id, "critical")
        escalated = await build_checks_alert_advisory(_operator())
    assert first[CHECKS_ADVISORY_EXTRAS_KEY][0]["state"] == "degraded"
    assert escalated[CHECKS_ADVISORY_EXTRAS_KEY][0]["state"] == "critical"
    # Two distinct dedupe keys were claimed -- one per state.
    assert len(store.store) == 2


async def test_per_caller_isolation_each_principal_reminded_once() -> None:
    """Distinct principals each claim their own key for the same dashboard."""
    dashboard_id = await _seed_dashboard(state="critical")
    store = _FakeNxStore()
    with _patch_nx(store):
        for sub in ("user-a", "user-b"):
            advisory = await build_checks_alert_advisory(_operator(sub))
            assert advisory[CHECKS_ADVISORY_EXTRAS_KEY][0]["dashboard_id"] == str(dashboard_id)
        # Both callers are now deduped independently.
        assert await build_checks_alert_advisory(_operator("user-a")) == {}
        assert await build_checks_alert_advisory(_operator("user-b")) == {}
    assert len(store.store) == 2


# ---------------------------------------------------------------------------
# Bounded fan-out (M1, PR #2726 review iteration 1)
# ---------------------------------------------------------------------------


async def test_tenant_wide_outage_caps_fragment_and_valkey_fanout() -> None:
    """More non-green Dashboards than the cap costs exactly the cap.

    The finding this pins: without ``.limit()`` the SELECT and the one-
    ``SET``-per-row fan-out are O(non-green Dashboards) on *every*
    successful dispatch, and that load correlates with incidents -- a
    correlated failure reddens many Dashboards at once. Asserting on
    ``len(store.ex_values)`` (``SET`` commands actually issued) rather
    than only on the fragment length is what makes this a fan-out test:
    a cap applied in Python after the claims would satisfy the entry
    count and still leave the fan-out unbounded.

    ``round_trips`` is the other half of the bound, and the one the NX
    dedupe cannot help with: a claim is issued before anyone knows
    whether it will succeed, so an unpipelined loop pays one awaited
    round-trip per surviving row on *every* successful dispatch for as
    long as the tenant is red.

    Names are zero-padded so ``ORDER BY name`` is the numeric order, which
    makes the surviving head assertable.
    """
    overflow = _ADVISORY_MAX_DASHBOARDS + 3
    for i in range(overflow):
        await _seed_dashboard(name=f"dash-{i:04d}", state="degraded")
    store = _FakeNxStore()
    with _patch_nx(store):
        advisory = await build_checks_alert_advisory(_operator())
    entries = advisory[CHECKS_ADVISORY_EXTRAS_KEY]
    assert len(entries) == _ADVISORY_MAX_DASHBOARDS
    assert len(store.ex_values) == _ADVISORY_MAX_DASHBOARDS
    assert store.round_trips == 1
    assert [e["name"] for e in entries] == [
        f"dash-{i:04d}" for i in range(_ADVISORY_MAX_DASHBOARDS)
    ]


async def test_cap_is_nudge_sized_so_the_fragment_stays_out_of_the_way() -> None:
    """The cap is nudge-sized, not audit-sized -- pinned as a contract.

    ``extras`` is attached to the ``OperationResult`` after the
    dispatcher has already reduced the payload, so this fragment never
    passes through JSONFlux/result-handle reduction: the cap is the only
    thing bounding the unsolicited context an agent is handed on a
    successful dispatch. Pinning the magnitude (not just "some cap
    exists") is what keeps a later widening an explicit decision.
    """
    assert _ADVISORY_MAX_DASHBOARDS <= 10


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


async def test_fail_open_on_valkey_error() -> None:
    """A Valkey teardown yields ``{}`` and warn-logs; it never raises."""
    from redis import exceptions as redis_exceptions

    await _seed_dashboard(state="critical")
    with (
        patch.object(
            get_broadcast_client(),
            "pipeline",
            new=MagicMock(side_effect=redis_exceptions.ConnectionError("refused")),
        ),
        # Assert on the module logger directly (not capture_logs) -- the
        # production ``cache_logger_on_first_use`` config makes
        # capture_logs miss module-cached BoundLoggers intermittently
        # (see test_broadcast_target_advisory for the incident note).
        patch("meho_backplane.checks.advisory._log") as mock_log,
    ):
        advisory = await build_checks_alert_advisory(_operator())
    assert advisory == {}
    assert any(
        call.args and call.args[0] == "checks_alert_advisory_failed"
        for call in mock_log.warning.call_args_list
    )


async def test_fail_open_on_db_error() -> None:
    """A DB teardown yields ``{}`` and warn-logs; it never raises."""
    with (
        patch(
            "meho_backplane.checks.advisory.get_sessionmaker",
            new=MagicMock(side_effect=RuntimeError("db down")),
        ),
        patch("meho_backplane.checks.advisory._log") as mock_log,
    ):
        advisory = await build_checks_alert_advisory(_operator())
    assert advisory == {}
    assert any(
        call.args and call.args[0] == "checks_alert_advisory_failed"
        for call in mock_log.warning.call_args_list
    )


# ---------------------------------------------------------------------------
# Dispatch integration (the merge site) + #2550 coexistence
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
    """Module-level typed handler so ``import_handler`` can round-trip it."""
    return {"echo": params}


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """Minimal duck-typed target (mirrors ``test_operations_dispatcher``)."""

    def __init__(self, *, name: str = "test-target") -> None:
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
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def _quiet_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record broadcast events instead of publishing to Valkey."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _register_op(op_id: str, stub_embedding_service: AsyncMock) -> None:
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
    )


async def test_dispatch_success_carries_fragment_then_dedupes(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """A read-class dispatch carries the fragment once per caller+window.

    Read-class is the point: the checks advisory deliberately diverges
    from the write-gated #2550 precedent -- ambient awareness applies to
    every op class. The immediate second dispatch by the same caller is
    deduped by the NX claim.
    """
    dashboard_id = await _seed_dashboard(state="critical")
    await _register_op("vault.kv.list", stub_embedding_service)
    store = _FakeNxStore()
    with _patch_nx(store):
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
    assert first.extras[CHECKS_ADVISORY_EXTRAS_KEY] == [
        {"dashboard_id": str(dashboard_id), "name": "prod-health", "state": "critical"}
    ]
    assert second.status == "ok"
    assert CHECKS_ADVISORY_EXTRAS_KEY not in second.extras


async def test_both_advisory_fragments_coexist_on_one_response(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """The #2550 fragment and the checks fragment ride one ``extras``.

    A write-class dispatch on a target with peer activity (the #2550
    trigger) by a caller whose tenant has a critical Dashboard (this
    feature's trigger) carries BOTH fragments -- distinct keys, plain
    dict merge, neither clobbers the other.
    """
    from datetime import UTC, datetime

    await _seed_dashboard(state="critical")
    await _register_op("vault.kv.delete", stub_embedding_service)
    peer_event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=_TENANT,
        principal_sub="peer-a",
        target_name="test-target",
        op_id="vault.kv.delete",
        op_class="write",
        result_status="ok",
        audit_id=_AUDIT_ID,
        actor_sub=None,
        payload={"op_class": "write", "params": {}, "result_status": "ok"},
    )

    async def _xrevrange(name: str, **kwargs: Any) -> list[tuple[str, dict[str, str]]]:
        return [("1747800001000-0", {"event": peer_event.model_dump_json()})]

    store = _FakeNxStore()
    bc = get_broadcast_client()
    with _patch_nx(store), patch.object(bc, "xrevrange", new=_xrevrange):
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


async def test_advisory_never_gates_dispatch_on_total_failure(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """A dead advisory path (DB error mid-build) leaves the dispatch ok."""
    await _register_op("vault.kv.list", stub_embedding_service)
    with patch(
        "meho_backplane.checks.advisory.get_sessionmaker",
        new=MagicMock(side_effect=RuntimeError("db down")),
    ):
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.list",
            target=_FakeTarget(),
            params={"path": "/secret"},
        )
    assert result.status == "ok"
    assert CHECKS_ADVISORY_EXTRAS_KEY not in result.extras
