# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit + dispatch-level tests for the ``vrli.event.query`` typed op (#2295).

``vrli.event.query`` is vRLI's first ``source_kind="typed"`` op: a bound
method on :class:`VcfLogsConnector` that issues the events query directly on
the connector's authenticated session (through the retry-once seam
``_get_json_with_session_retry``), so it works on a fresh boot with **zero
catalog ingest** — no ingested ``endpoint_descriptor`` row for the events
query. It replaces the hand-edited production overlay whose two canonical-spec
blockers (#2066 ``{+path}`` render, #1796 ``servers[]`` base path) are fixed
in v0.20.0.

Coverage maps to the #2295 acceptance criteria:

* AC #1 — the typed op dispatches as ``source_kind="typed"`` on a fresh boot
  with zero catalog state, against a respx-mocked vRLI (``call_operation``
  dispatch level, not helper level).
* AC #2 — a 440 mid-session recovers via one re-login + retry, pinned on the
  **typed** dispatch path (the soak-famous #1135 / #1139 scenario).
* AC #3 — the #2262 registration invariant: the typed events op resolves via a
  ``source_kind="typed"`` descriptor + ``handler_ref``, never through an
  ``ingested`` row.

Handler-level unit tests (call shape, path building, limit flow, envelope
coercion) run against a fake connector so the assertions target the
request-builder contract without a live transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.vcf_logs import (
    VRLI_CONNECTOR_ID,
    VRLI_IMPL_ID,
    VRLI_TYPED_OPS,
    VRLI_VERSION,
    VcfLogsConnector,
)
from meho_backplane.connectors.vcf_logs.typed_ops import (
    VRLI_EVENT_QUERY_OP,
    build_event_query_path,
    event_query_impl,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.meta_tools import call_operation
from tests.acceptance._vrli_canary_fixtures import (
    VRLI_CANARY_BASE_URL,
    VRLI_CANARY_EVENTS,
    VRLI_CANARY_FINGERPRINT,
    VRLI_CANARY_OPERATOR_TENANT,
    VRLI_CANARY_SESSION_ID,
    VRLI_CANARY_SESSION_REFRESH_ID,
    VRLI_RESERVED_CONSTRAINT_VALUE,
    VRLI_RESERVED_CONSTRAINT_WIRE_PATH,
    _insert_vrli_descriptors,
    _register_vrli_routes,
    _vrli_credentials_loader,
)

_OPERATOR = Operator(
    sub="vrli-typed-event-query-test",
    name="vRLI Typed Event Query Test",
    email=None,
    raw_jwt="<vrli-typed-raw-jwt>",
    tenant_id=VRLI_CANARY_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_TARGET_NAME = "vrli-typed-event-query-target"


# ---------------------------------------------------------------------------
# Handler-level unit tests (fake connector)
# ---------------------------------------------------------------------------


@dataclass
class _Target:
    name: str = "vrli-unit"
    host: str = "vrli.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=lambda: UUID("00000000-0000-0000-0000-0000000000e1"))
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _make_operator() -> Operator:
    return Operator(
        sub="op-event-query",
        name="Event Query Unit",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeConnector:
    """Records the ``_get_json_with_session_retry`` call ``event_query_impl`` makes."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _get_json_with_session_retry(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        self.calls.append((path, params))
        return self._payload


def test_build_event_query_path_empty_constraint_is_the_base_events_path() -> None:
    assert build_event_query_path("") == "/api/v2/events/"


def test_build_event_query_path_keeps_reserved_constraint_slashes_literal() -> None:
    """A reserved constraint chain keeps ``/`` literal, encodes spaces (#2003)."""
    path = build_event_query_path(VRLI_RESERVED_CONSTRAINT_VALUE)
    assert path == VRLI_RESERVED_CONSTRAINT_WIRE_PATH
    # Structural slashes survived; only the space was percent-encoded.
    assert "%2F" not in path
    assert "%20" in path


@pytest.mark.asyncio
async def test_event_query_impl_builds_path_and_returns_envelope() -> None:
    conn = _FakeConnector({"events": [{"id": "ev-1"}, {"id": "ev-2"}], "complete": True})

    out = await event_query_impl(conn, _make_operator(), _Target(), {"constraints": ""})

    assert conn.calls == [("/api/v2/events/", None)]
    assert out == {"events": [{"id": "ev-1"}, {"id": "ev-2"}], "complete": True}


@pytest.mark.asyncio
async def test_event_query_impl_flows_limit_as_query_param() -> None:
    conn = _FakeConnector({"events": [], "complete": True})

    await event_query_impl(conn, _make_operator(), _Target(), {"constraints": "", "limit": 25})

    (path, params) = conn.calls[0]
    assert path == "/api/v2/events/"
    assert params == {"limit": 25}


@pytest.mark.asyncio
async def test_event_query_impl_renders_reserved_constraint_path() -> None:
    conn = _FakeConnector({"events": [{"id": "ev-1"}], "complete": False})

    await event_query_impl(
        conn, _make_operator(), _Target(), {"constraints": VRLI_RESERVED_CONSTRAINT_VALUE}
    )

    assert conn.calls[0][0] == VRLI_RESERVED_CONSTRAINT_WIRE_PATH


@pytest.mark.asyncio
async def test_event_query_impl_defaults_complete_true_and_coerces_missing_events() -> None:
    """A response missing ``events`` / ``complete`` yields [] and complete=True."""
    conn = _FakeConnector({"unexpected": "shape"})

    out = await event_query_impl(conn, _make_operator(), _Target(), {})

    assert out == {"events": [], "complete": True}


@pytest.mark.asyncio
async def test_event_query_impl_reads_complete_flag() -> None:
    conn = _FakeConnector({"events": [{"id": "ev-1"}], "complete": False})

    out = await event_query_impl(conn, _make_operator(), _Target(), {"constraints": ""})

    assert out["complete"] is False


@pytest.mark.asyncio
async def test_event_query_impl_rejects_non_string_constraints() -> None:
    conn = _FakeConnector({"events": []})

    with pytest.raises(ValueError, match="constraints"):
        await event_query_impl(conn, _make_operator(), _Target(), {"constraints": ["not", "str"]})


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_event_query_is_a_registered_typed_op() -> None:
    assert VRLI_EVENT_QUERY_OP in VRLI_TYPED_OPS
    assert VRLI_EVENT_QUERY_OP.op_id == "vrli.event.query"
    assert VRLI_EVENT_QUERY_OP.safety_level == "safe"
    assert VRLI_EVENT_QUERY_OP.requires_approval is False


def test_event_query_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VcfLogsConnector, VRLI_EVENT_QUERY_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_event_query_handler_signature_is_typed_not_composite() -> None:
    """Typed handler accepts operator but NOT dispatch_child (else it'd be composite)."""
    import inspect

    params = inspect.signature(VcfLogsConnector.event_query).parameters
    assert "operator" in params
    assert "dispatch_child" not in params
    assert "connector" not in params


def test_event_query_llm_instructions_advertise_the_jsonflux_handle() -> None:
    """The typed op's copy tells the agent to expect a JSONFlux handle (moved from #834)."""
    instructions = VRLI_EVENT_QUERY_OP.llm_instructions
    assert instructions is not None
    combined = f"{instructions['output_shape']} {instructions['next_step']}".lower()
    assert "handle" in combined or "jsonflux" in combined


# ---------------------------------------------------------------------------
# Dispatch-level tests (respx-mocked vRLI, fresh-boot zero catalog ingest)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def _captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


async def _seed_target() -> Any:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=VRLI_CANARY_OPERATOR_TENANT,
            name=_TARGET_NAME,
            aliases=[],
            product=VcfLogsConnector.product,
            host=VRLI_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="vrli/vrli-typed",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=VRLI_CANARY_FINGERPRINT,
            notes="seeded by test_connectors_vcf_logs_typed_event_query",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> VcfLogsConnector:
    registry = all_connectors_v2()
    connector_cls = registry.get((VcfLogsConnector.product, VRLI_VERSION, VRLI_IMPL_ID))
    assert connector_cls is VcfLogsConnector
    instance = get_or_create_connector_instance(connector_cls)
    instance._credentials._loader = _vrli_credentials_loader  # type: ignore[attr-defined]
    instance._session_tokens.clear()
    return instance


@dataclass(frozen=True)
class _Bundle:
    connector_instance: VcfLogsConnector
    db_target: Any


@pytest.fixture
async def vrli_typed_canary(_captured_events: list[Any]) -> AsyncIterator[_Bundle]:
    """Dispatcher-ready setup: descriptors (incl. the typed events row) + respx vRLI."""
    del _captured_events
    await _insert_vrli_descriptors()
    seeded_target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vrli_routes(mock)
        try:
            yield _Bundle(connector_instance=instance, db_target=seeded_target)
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


async def test_event_query_dispatches_as_typed_on_fresh_boot(
    vrli_typed_canary: _Bundle,
) -> None:
    """AC #1: vrli.event.query dispatches through the full stack, source_kind=typed."""
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VRLI_CONNECTOR_ID,
            "op_id": VRLI_EVENT_QUERY_OP.op_id,
            "target": {"name": _TARGET_NAME},
            "params": {"constraints": ""},
        },
    )
    assert result["status"] == "ok", f"typed event query did not dispatch ok: {result!r}"

    # The op the dispatcher resolved is a typed row with a handler_ref — never
    # an ingested descriptor (AC #3 / the #2262 registration invariant).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == VRLI_EVENT_QUERY_OP.op_id,
                    EndpointDescriptor.product == "vrli",
                )
            )
        ).scalar_one()
    assert row.source_kind == "typed", (
        f"vrli.event.query must resolve through a typed row, not source_kind={row.source_kind!r}"
    )
    assert row.handler_ref and row.handler_ref.endswith("VcfLogsConnector.event_query")


async def test_no_ingested_events_query_row_exists(vrli_typed_canary: _Bundle) -> None:
    """AC #3: no ``ingested`` descriptor targets the raw events query path.

    The typed op owns the events surface; the canary's ``_insert_vrli_descriptors``
    seeds the six ingested core ops (none of them the events query) plus the one
    typed events row. A ``source_kind='ingested'`` row on ``/api/v2/events/``
    would mean the curation still flips it — the #2262 violation.
    """
    del vrli_typed_canary
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        ingested_events = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.product == "vrli",
                        EndpointDescriptor.source_kind == "ingested",
                        EndpointDescriptor.path.like("/api/v2/events/%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert not ingested_events, (
        f"no ingested vRLI row may target the raw events path after #2295; "
        f"found {[r.op_id for r in ingested_events]!r}"
    )


async def test_event_query_limit_flows_to_the_wire(_captured_events: list[Any]) -> None:
    """A ``limit`` param reaches the appliance as a query param on the events GET.

    Standalone setup (own respx) so the captured events route is the one the
    dispatch actually hits — nesting a second ``respx.mock`` under the shared
    happy-path fixture would route the request to the fixture's uncaptured
    events route instead.
    """
    del _captured_events
    await _insert_vrli_descriptors()
    await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800}
        )
        events_route = mock.get("/api/v2/events/").respond(200, json=VRLI_CANARY_EVENTS)
        try:
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VRLI_CONNECTOR_ID,
                    "op_id": VRLI_EVENT_QUERY_OP.op_id,
                    "target": {"name": _TARGET_NAME},
                    "params": {"constraints": "", "limit": 5},
                },
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()

    assert result["status"] == "ok", f"limited event query did not dispatch ok: {result!r}"
    assert events_route.called
    assert dict(events_route.calls.last.request.url.params) == {"limit": "5"}


async def test_event_query_reserved_constraint_keeps_slash_literal_on_wire(
    _captured_events: list[Any],
) -> None:
    """The typed path renders a reserved constraint with literal slashes (#2003).

    The overlay existed partly because the ingested ``{+path}`` render was
    broken; the typed handler builds the sub-path itself. A ``%2F``-mangled URL
    would miss the literal-slash respx route and 404 against the catch-all.
    """
    del _captured_events
    await _insert_vrli_descriptors()
    await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/v2/sessions").respond(
            200, json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800}
        )
        reserved_route = mock.get(VRLI_RESERVED_CONSTRAINT_WIRE_PATH).respond(
            200, json=VRLI_CANARY_EVENTS
        )
        try:
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VRLI_CONNECTOR_ID,
                    "op_id": VRLI_EVENT_QUERY_OP.op_id,
                    "target": {"name": _TARGET_NAME},
                    "params": {"constraints": VRLI_RESERVED_CONSTRAINT_VALUE},
                },
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()

    assert result["status"] == "ok", f"reserved-constraint dispatch failed: {result!r}"
    assert reserved_route.called, "the literal-slash wire route was never hit (over-encoded %2F)"
    assert (
        reserved_route.calls.last.request.url.path
        == "/api/v2/events/text/CONTAINS error/hostname/CONTAINS vcsa"
    )


async def test_event_query_440_recovers_on_the_typed_path(
    _captured_events: list[Any],
) -> None:
    """AC #2: a 440 mid-session recovers via one re-login + retry on the typed path.

    The soak-famous #1135 / #1139 scenario, pinned at the dispatch level: the
    typed handler routes through ``_get_json_with_session_retry``, so a 440 on
    the events GET invalidates the cached token, re-logs in, and retries once.
    """
    del _captured_events
    await _insert_vrli_descriptors()
    target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VRLI_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        session_route = mock.post("/api/v2/sessions")
        session_route.side_effect = [
            httpx.Response(200, json={"sessionId": VRLI_CANARY_SESSION_ID, "ttl": 1800}),
            httpx.Response(200, json={"sessionId": VRLI_CANARY_SESSION_REFRESH_ID, "ttl": 1800}),
        ]
        events_route = mock.get("/api/v2/events/")
        events_route.side_effect = [
            httpx.Response(440, json={"errorMessage": "Login Timeout"}),
            httpx.Response(200, json=VRLI_CANARY_EVENTS),
        ]
        try:
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VRLI_CONNECTOR_ID,
                    "op_id": VRLI_EVENT_QUERY_OP.op_id,
                    "target": {"name": _TARGET_NAME},
                    "params": {"constraints": ""},
                },
            )
            # Read the token cache before aclose() clears it.
            cached = instance._session_tokens.get(target_cache_key(target))
        finally:
            await instance.aclose()
            reset_dispatcher_caches()

    assert result["status"] == "ok", f"440 recovery on the typed path failed: {result!r}"
    assert session_route.call_count == 2, "expected initial + post-440 re-login"
    assert events_route.call_count == 2, "expected 440 + retry"
    assert cached == VRLI_CANARY_SESSION_REFRESH_ID, (
        f"post-retry token should be the refreshed id; got {cached!r}"
    )
