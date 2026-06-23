# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the topology console **temporal read** surface.

Initiative #1941 (G10.17 Topology console), Task #1955 (T3). Acceptance
criteria from the issue body:

* ``GET /ui/topology/timeline`` as an **operator** renders the chronological
  change feed; a follow-on request with the returned ``next_cursor`` renders
  the next page (keyset pagination round-trips).
* ``GET /ui/topology/history/{name}`` renders the per-resource history; a
  bare ``name`` resolving to >1 kind renders the **409 ``ambiguous_node``**
  recoverable banner with the candidate ``kinds`` (re-submit hint), NOT a
  dead error; an unknown name renders an empty / 404 panel.
* ``GET /ui/topology/diff?ts1=&ts2=`` renders the net created / updated /
  removed entries; an overflowing window renders the ``truncation_hint``
  banner (assert ``truncated`` surfaced), not a silently-clipped list.
* The in-process temporal reads bind ``audit_op_class="audit_query"`` (NOT
  ``read``) so the broadcast carries row-count only -- no per-row history
  payload leaks (mirrors the REST binding; precedent for a UI BFF binding the
  class: ``ui/routes/conventions/write.py``).
* A route-order test (FastAPI test client at construction) asserts
  ``/ui/topology/timeline``, ``/ui/topology/diff``, and
  ``/ui/topology/history/{name}`` resolve to their own handlers, registered
  ahead of ``detail.py``'s ``node/{node_id}`` param route.

The role-bearing JWKS client + per-tenant graph seed helpers mirror
:mod:`backend.tests.test_ui_topology_annotate`; the audit-contextvar capture
mirrors :mod:`backend.tests.test_ui_retrieval`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import GraphNode, GraphNodeHistory, Tenant
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.topology import build_router as build_topology_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")

_OP_OPERATOR = "op-operator"

#: The module path of the contextvars binder the temporal handlers call,
#: patched to capture the audit op_id / op_class without standing up the
#: chassis audit middleware (the ``test_ui_retrieval`` precedent).
_BIND_CONTEXTVARS = (
    "meho_backplane.ui.routes.topology.temporal.structlog.contextvars.bind_contextvars"
)


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors the UI-surface baseline (``test_ui_topology_annotate`` /
    ``test_ui_topology_queries``): chassis Keycloak / Vault / DB /
    encryption-key env + cache resets on both setup and teardown so a failing
    test cannot leak template / session-engine state.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    """Construct the minimal FastAPI app wired for the topology read tests."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant_row(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_node(
    *,
    tenant_id: uuid.UUID,
    kind: str,
    name: str,
    target_id: uuid.UUID | None = None,
) -> uuid.UUID:
    node_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                GraphNode(
                    id=node_id,
                    tenant_id=tenant_id,
                    kind=kind,
                    name=name,
                    target_id=target_id,
                    properties={},
                    discovered_by="test",
                    first_seen=datetime.now(UTC),
                    last_seen=datetime.now(UTC),
                ),
            )

    asyncio.run(_do())
    return node_id


def _seed_history_rows(
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    count: int,
    base_ts: datetime,
    change_kind: str = "updated",
    node_name: str | None = None,
    node_kind: str | None = None,
) -> None:
    """Append *count* ``graph_node_history`` rows for *node_id*.

    Each row is one minute apart (ascending ``valid_from``) so the timeline /
    history walks have a deterministic chronological order to page through.
    The diff-on-write hook normally writes these; the tests seed them
    directly because the read surface is what is under test. The ``after``
    snapshot carries the node's ``name`` / ``kind`` (when given) so the diff
    surface -- which derives its entry name/kind from the snapshot, not the
    live node -- renders a realistic entry.
    """
    after: dict[str, object] = {}
    if node_name is not None:
        after["name"] = node_name
    if node_kind is not None:
        after["kind"] = node_kind

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            for i in range(count):
                session.add(
                    GraphNodeHistory(
                        node_id=node_id,
                        tenant_id=tenant_id,
                        change_kind=change_kind,
                        snapshot={"after": {**after, "rev": i}},
                        valid_from=base_ts + timedelta(minutes=i),
                    ),
                )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OP_OPERATOR,
    access_token: str = "unused",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-topology-temporal-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_operator_client(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OP_OPERATOR,
) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient + respx mock for an **operator** session.

    The temporal reads reconstruct the operator via
    ``lift_operator_from_session`` -- which re-validates the BFF session's
    access token through the JWT chain -- so the JWKS endpoint must be mocked.
    The caller stops the mock in a ``finally``.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id,
        access_token=access_token,
        operator_sub=operator_sub,
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


# ---------------------------------------------------------------------------
# Route ordering (acceptance criterion: literals beat the {node_id} param)
# ---------------------------------------------------------------------------


def test_topology_ui_temporal_routes_register_before_node_param() -> None:
    """``timeline`` / ``diff`` / ``history/{name}`` resolve to their handlers.

    The literal-prefixed temporal routes must register BEFORE ``detail.py``'s
    ``/ui/topology/node/{node_id}`` param route so the first-match-wins lookup
    never binds them as a node id. Verified at construction, per the
    convention ``topology/__init__.py`` documents.
    """
    router = build_topology_router()
    paths = [route.path for route in router.routes if hasattr(route, "methods")]
    node_idx = paths.index("/ui/topology/node/{node_id}")
    for literal in (
        "/ui/topology/timeline",
        "/ui/topology/diff",
        "/ui/topology/history/{name}",
    ):
        assert literal in paths, paths
        assert paths.index(literal) < node_idx, paths

    # Resolve through a real app: each temporal route binds GET and is its own
    # handler, not the node-detail param route.
    app = _build_app()
    for literal in (
        "/ui/topology/timeline",
        "/ui/topology/diff",
        "/ui/topology/history/{name}",
    ):
        matched = [route for route in app.routes if getattr(route, "path", None) == literal]
        assert matched, f"{literal} not registered on the app"
        assert "GET" in matched[0].methods  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Timeline: render + keyset cursor round-trip (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_timeline_renders_change_feed_for_operator() -> None:
    """``GET /ui/topology/timeline`` renders the chronological change feed."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _seed_history_rows(tenant_id=_TENANT_A, node_id=node, count=3, base_ts=base)

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/timeline")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="timeline-feed"' in body
    # All three seeded changes render as feed rows.
    assert body.count('data-test="timeline-row"') == 3


def test_topology_ui_timeline_cursor_round_trips_next_page() -> None:
    """The returned ``next_cursor`` load-more URL renders the next keyset page.

    Seed 80 history rows (> the 50/page default) so page one returns a
    ``next_cursor``; following that load-more URL renders the remaining 30
    rows as the ``_timeline_rows.html`` fragment (the HTMX "load more" swap).
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _seed_history_rows(tenant_id=_TENANT_A, node_id=node, count=80, base_ts=base)

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        first = client.get("/ui/topology/timeline")
        assert first.status_code == 200, first.text
        # Page one is full (50 rows) and exposes a load-more control carrying
        # the keyset cursor.
        assert first.text.count('data-test="timeline-row"') == 50
        assert 'data-test="timeline-load-more"' in first.text

        load_more_href = _extract_attr(first.text, 'data-test="timeline-load-more"', "hx-get")
        assert load_more_href and "cursor=" in load_more_href, first.text

        # Follow the cursor as an HTMX "load more" swap -> the rows fragment.
        second = client.get(load_more_href, headers={"HX-Request": "true"})
    finally:
        mock.stop()

    assert second.status_code == 200, second.text
    # The remaining 30 rows render; no surrounding page chrome on the fragment.
    assert second.text.count('data-test="timeline-row"') == 30
    assert "<html" not in second.text.lower()
    # The chain terminates: the last page has no further load-more control.
    assert 'data-test="timeline-load-more"' not in second.text


def test_topology_ui_timeline_unknown_target_filter_renders_recoverable_banner() -> None:
    """An unresolved ``target`` filter renders a near-miss banner, not a 500."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/timeline?target=does-not-exist")
    finally:
        mock.stop()

    assert response.status_code == 404, response.text
    assert 'data-test="timeline-target-not-found"' in response.text
    assert 'data-error-kind="no_target"' in response.text


# ---------------------------------------------------------------------------
# History: render + ambiguous + not-found (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_history_renders_resource_chronology() -> None:
    """``GET /ui/topology/history/{name}`` renders the per-resource history."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _seed_history_rows(tenant_id=_TENANT_A, node_id=node, count=4, base_ts=base)

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/history/vm-1")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="history-panel"' in body
    assert body.count('data-test="history-row"') == 4
    assert "vm-1" in body


def test_topology_ui_history_ambiguous_name_renders_409_banner_with_kinds() -> None:
    """A bare name resolving to >1 kind renders the 409 ambiguous banner."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # ``app`` exists as two kinds -> bare-name resolution is ambiguous.
    _seed_node(tenant_id=_TENANT_A, kind="target", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="app")

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/history/app")
    finally:
        mock.stop()

    assert response.status_code == 409, response.text
    body = response.text
    # Recoverable banner (NOT a dead 409) listing the candidate kinds.
    assert 'data-test="history-ambiguous"' in body
    assert 'data-test="ambiguous-kinds"' in body
    assert "target" in body
    assert "vm" in body
    # The re-submit hint links each candidate kind with ``?kind=``.
    assert "?kind=target" in body
    assert "?kind=vm" in body


def test_topology_ui_history_ambiguous_resolves_with_kind_pin() -> None:
    """Re-requesting with the disambiguating ``?kind=`` then renders history."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    target_node = _seed_node(tenant_id=_TENANT_A, kind="target", name="app")
    _seed_node(tenant_id=_TENANT_A, kind="vm", name="app")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    _seed_history_rows(tenant_id=_TENANT_A, node_id=target_node, count=2, base_ts=base)

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/history/app?kind=target")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert 'data-test="history-panel"' in response.text
    assert response.text.count('data-test="history-row"') == 2


def test_topology_ui_history_unknown_name_renders_empty_404_panel() -> None:
    """An unknown name renders the empty / 404 panel, not a 500."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/history/no-such-node")
    finally:
        mock.stop()

    assert response.status_code == 404, response.text
    body = response.text
    assert 'data-test="history-not-found"' in body
    assert 'data-error-kind="node_not_found"' in body
    assert "no-such-node" in body


# ---------------------------------------------------------------------------
# Diff: render + truncation banner (acceptance criterion)
# ---------------------------------------------------------------------------


def test_topology_ui_diff_renders_net_entries() -> None:
    """``GET /ui/topology/diff?ts1=&ts2=`` renders the net created entries."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    # One ``created`` row inside the [ts1, ts2] window. The diff entry's
    # name/kind derive from the snapshot, so the snapshot carries them.
    in_window = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    _seed_history_rows(
        tenant_id=_TENANT_A,
        node_id=node,
        count=1,
        base_ts=in_window,
        change_kind="created",
        node_name="vm-1",
        node_kind="vm",
    )

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get(
            "/ui/topology/diff",
            params={
                "ts1": "2026-06-01T12:00:00+00:00",
                "ts2": "2026-06-01T13:00:00+00:00",
            },
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-test="diff-panel"' in body
    assert 'data-test="diff-entry"' in body
    # The created entry surfaces (snapshot-derived name + the ``created`` badge).
    assert "vm-1" in body
    assert "created" in body
    # A non-overflowing diff carries no truncation banner.
    assert 'data-test="diff-truncated"' not in body


def test_topology_ui_diff_pickers_echo_valid_datetime_local() -> None:
    """The ``datetime-local`` pickers echo a no-offset ``YYYY-MM-DDTHH:MM`` value.

    Regression for #2014: the submitted window is echoed back into the two
    ``datetime-local`` inputs so a refine-submit starts from the current
    bounds. ``isoformat()`` of the tz-aware (UTC) bounds yields a trailing
    ``+00:00`` offset, which is not a valid normalized local date-and-time
    string -- the user agent rejects it and the picker renders blank. Assert
    each picker's ``value`` is a valid ``datetime-local`` string (offset- and
    seconds-stripped), not the offset-bearing ``isoformat()``.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    in_window = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    _seed_history_rows(
        tenant_id=_TENANT_A,
        node_id=node,
        count=1,
        base_ts=in_window,
        change_kind="created",
        node_name="vm-1",
        node_kind="vm",
    )

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get(
            "/ui/topology/diff",
            params={
                # Offset-bearing isoformat input -- the same shape the picker
                # round-trips. The echoed ``value`` must NOT carry the offset.
                "ts1": "2026-06-01T12:00:00+00:00",
                "ts2": "2026-06-01T13:00:00+00:00",
            },
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text

    # A valid ``datetime-local`` value: YYYY-MM-DDTHH:MM, no offset, no seconds.
    dt_local = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
    for data_test, expected in (("diff-ts1", "2026-06-01T12:00"), ("diff-ts2", "2026-06-01T13:00")):
        match = re.search(
            rf'<input[^>]*data-test="{data_test}"[^>]*>',
            body,
        ) or re.search(
            rf'<input[^>]*value="(?P<v>[^"]*)"[^>]*data-test="{data_test}"',
            body,
        )
        assert match is not None, f"{data_test} input not found in:\n{body}"
        value_match = re.search(r'value="(?P<v>[^"]*)"', match.group(0))
        assert value_match is not None, f"{data_test} has no value= attribute"
        value = value_match.group("v")
        assert dt_local.match(value), (
            f"{data_test} value {value!r} is not a valid datetime-local string "
            f"(expected no offset / no seconds, e.g. {expected!r})"
        )
        assert value == expected, f"{data_test} echoed {value!r}, expected {expected!r}"
        assert "+00:00" not in value, f"{data_test} value still carries an offset: {value!r}"


def test_topology_ui_diff_missing_required_ts_is_422() -> None:
    """``ts1`` / ``ts2`` are REQUIRED query params -> 422 when absent."""
    _seed_tenant_row(_TENANT_A, "tenant-a")

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        response = client.get("/ui/topology/diff")
    finally:
        mock.stop()

    assert response.status_code == 422, response.text


def test_topology_ui_diff_overflow_renders_truncation_hint_banner() -> None:
    """An overflowing window renders the truncation banner (``truncated`` surfaced).

    The diff service caps at 1000 rows (``_DIFF_HARD_CAP``); patch the cap low
    so a handful of seeded changes overflow it deterministically, then assert
    the panel surfaces the truncation hint rather than a silently-clipped list.
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    # Three distinct nodes, each ``created`` inside the window -> 3 net diff
    # entries; with the cap patched to 2 the diff is truncated.
    base = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    for i in range(3):
        n = _seed_node(tenant_id=_TENANT_A, kind="vm", name=f"vm-{i}")
        _seed_history_rows(
            tenant_id=_TENANT_A,
            node_id=n,
            count=1,
            base_ts=base + timedelta(seconds=i),
            change_kind="created",
        )

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        with patch("meho_backplane.topology.query._DIFF_HARD_CAP", 2):
            response = client.get(
                "/ui/topology/diff",
                params={
                    "ts1": "2026-06-01T12:00:00+00:00",
                    "ts2": "2026-06-01T13:00:00+00:00",
                },
            )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The truncation banner surfaced ``truncated`` -- not a clipped list.
    assert 'data-test="diff-truncated"' in body
    assert 'data-truncated="true"' in body
    assert "narrow the" in body.lower()


# ---------------------------------------------------------------------------
# Audit footgun: all three reads bind audit_op_class="audit_query" (NOT read)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_op_id"),
    [
        ("/ui/topology/timeline", "topology.timeline"),
        ("/ui/topology/history/vm-1", "topology.history"),
        (
            "/ui/topology/diff?ts1=2026-06-01T12:00:00%2B00:00&ts2=2026-06-01T13:00:00%2B00:00",
            "topology.diff",
        ),
    ],
)
def test_topology_ui_temporal_reads_bind_audit_query_class(
    path: str,
    expected_op_id: str,
) -> None:
    """Each temporal read binds ``audit_op_class="audit_query"`` (NOT ``read``).

    Load-bearing: the broadcast event must carry row-count only, never the
    per-row history payload. Capture every ``bind_contextvars`` call the
    handler makes and assert the audit class is ``audit_query`` bound under
    the canonical op_id -- and that ``read`` never appears (the downgrade that
    would leak the payload).
    """
    _seed_tenant_row(_TENANT_A, "tenant-a")
    node = _seed_node(tenant_id=_TENANT_A, kind="vm", name="vm-1")
    base = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    _seed_history_rows(tenant_id=_TENANT_A, node_id=node, count=2, base_ts=base)

    bound_payloads: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        bound_payloads.append(dict(kwargs))

    client, mock = _authenticated_operator_client(tenant_id=_TENANT_A)
    try:
        with patch(_BIND_CONTEXTVARS, side_effect=_capture):
            response = client.get(path)
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    merged: dict[str, object] = {}
    for payload in bound_payloads:
        merged.update(payload)
    # The audit class is bound as ``audit_query`` under the canonical op_id.
    assert merged.get("audit_op_class") == "audit_query"
    assert merged.get("audit_op_id") == expected_op_id
    # ``read`` never appears in ANY bound payload -- no downgrade.
    for payload in bound_payloads:
        assert payload.get("audit_op_class") != "read"
    # The aggregate row count is the only request-derived signal exposed.
    assert "audit_row_count" in merged


# ---------------------------------------------------------------------------
# Auth: an unauthenticated request is bounced
# ---------------------------------------------------------------------------


def test_topology_ui_timeline_requires_session() -> None:
    """An unauthenticated request to the timeline is redirected to login."""
    _seed_tenant_row(_TENANT_A, "tenant-a")
    client = TestClient(_build_app(), follow_redirects=False)
    response = client.get("/ui/topology/timeline")
    # ``require_ui_session`` bounces a cookieless request (302 to the login
    # flow), never serving the feed.
    assert response.status_code in (302, 303, 307, 401), response.text


# ---------------------------------------------------------------------------
# Small text-extraction helper
# ---------------------------------------------------------------------------


def _extract_attr(html: str, marker: str, attr: str) -> str:
    """Pull the value of *attr* from the element carrying *marker*.

    A tiny string scan -- the tests only need to read one attribute (the
    load-more ``hx-get`` URL) off a known element without pulling a full HTML
    parser into the suite.
    """
    idx = html.index(marker)
    # Scan backwards to the opening ``<`` of the element, forwards to its ``>``.
    start = html.rindex("<", 0, idx)
    end = html.index(">", idx)
    element = html[start:end]
    needle = f'{attr}="'
    attr_start = element.index(needle) + len(needle)
    attr_end = element.index('"', attr_start)
    return element[attr_start:attr_end]
