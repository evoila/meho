# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast filters + event-detail drawer.

Initiative #338 (G10.1 Activity broadcast UI), Task #868 (G10.1-T2).
Acceptance criteria on issue #868:

* All 4 filters work; combined filters narrow correctly; the re-rendered
  feed's SSE subscription carries the active filter (assert URL).
* Event click → drawer with full payload (non-aggregate), request_id,
  audit_id link, event_id; Alpine click-outside dismisses.
* ``credential_read`` / aggregate-only events render 🔒 + the placeholder.
* The target dropdown is tenant-scoped (from the tenant's targets).
* ``ruff`` + ``mypy`` clean; ``pytest -n auto`` passes.

The op_class/principal/target filters are the three the stream bridge
supports; they ride into the feed fragment's ``sse-connect`` URL so the
server drops non-matching events. op_id has no stream parameter, so it is
the client-side substring filter the ``broadcastFeed`` Alpine controller
applies -- exercised here by asserting the controller wiring + the
op_id-filter seed reaches the page (the substring narrowing itself runs
in-browser and is verified via the JS surface, not a server round-trip).

Two test surfaces (mirroring :mod:`backend.tests.test_ui_broadcast_feed`):

* **HTTP edge** -- a minimal app wired with the UI session + CSRF
  middlewares, an authenticated client via a seeded ``web_session`` row.
  Covers the filter fragment route, the target dropdown, the event
  drawer (happy / aggregate-only / 404 / cross-tenant), and the unauth
  redirect.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
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
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF + broadcast env vars for every test.

    Mirrors :func:`backend.tests.test_ui_broadcast_feed._bff_env`. Cache
    + global-state resets run on setup and teardown so a failing test
    cannot leak ``_TEMPLATES`` / session-engine / broadcast-client state
    into the next case.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_broadcast_client_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the broadcast UI tests.

    Mirrors the production wiring + the chassis/topology/feed suites:
    StaticFiles at ``/ui/static``, BFF auth router + UI surface router
    (which includes the broadcast routes ahead of the stubs),
    ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
    """
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


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row directly and return its UUID."""
    from meho_backplane.db.engine import get_sessionmaker

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _seed_target(*, tenant_id: uuid.UUID, name: str, product: str = "vmware") -> None:
    """Insert one ``targets`` row for the dropdown tenant-scoping test."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TargetORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product=product,
                    host=f"{name}.test",
                ),
            )

    asyncio.run(_do())


def _seed_audit_row(
    *,
    tenant_id: uuid.UUID,
    payload: dict[str, object],
    operator_sub: str = "op-42",
    method: str = "POST",
    path: str = "/api/v1/call",
    status_code: int = 200,
    request_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one ``audit_log`` row and return its id (the drawer key)."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import AuditLog

    audit_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=audit_id,
                    occurred_at=datetime.now(UTC),
                    operator_sub=operator_sub,
                    method=method,
                    path=path,
                    status_code=status_code,
                    request_id=request_id or uuid.uuid4(),
                    duration_ms=Decimal("12.50"),
                    payload=payload,
                    tenant_id=tenant_id,
                ),
            )

    asyncio.run(_do())
    return audit_id


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_feed_fragment_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/broadcast/feed`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/broadcast/feed")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_event_drawer_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/broadcast/event/<id>`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(f"/ui/broadcast/event/{uuid.uuid4()}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Filter bar -- full page render
# ---------------------------------------------------------------------------


def test_page_renders_filter_bar_with_all_four_controls() -> None:
    """The full page renders the op_class / principal / target / op_id controls."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'name="op_class"' in body
    assert 'name="principal"' in body
    assert 'name="target"' in body
    assert 'name="op_id"' in body
    # The filter bar HTMX-submits the three server filters to the fragment route.
    assert 'hx-get="/ui/broadcast/feed"' in body
    assert 'hx-target="#broadcast-feed"' in body


def test_page_op_class_options_cover_the_closed_vocabulary() -> None:
    """The op_class dropdown offers All + the six sensitivity classes."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    assert ">All<" in body
    for op_class in (
        "read",
        "write",
        "credential_read",
        "credential_mint",
        "audit_query",
        "other",
    ):
        assert f'value="{op_class}"' in body


def test_target_dropdown_is_tenant_scoped() -> None:
    """The target dropdown lists only the session tenant's target names."""
    _seed_target(tenant_id=_TENANT_A, name="rdc-vcenter")
    _seed_target(tenant_id=_TENANT_A, name="lab-k8s")
    # A target in tenant B must NOT appear on tenant A's dropdown.
    _seed_target(tenant_id=_TENANT_B, name="other-tenant-target")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    assert 'value="rdc-vcenter"' in body
    assert 'value="lab-k8s"' in body
    assert "other-tenant-target" not in body


# ---------------------------------------------------------------------------
# Filter fragment -- SSE URL carries the active filters
# ---------------------------------------------------------------------------


def test_fragment_no_filters_streams_unfiltered() -> None:
    """No filters → the fragment's sse-connect is the bare bridge URL."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/feed")
    assert response.status_code == 200, response.text
    body = response.text
    # Bare bridge URL -- no query string -> stream everything.
    assert 'sse-connect="/ui/broadcast/stream"' in body
    # The fragment is the swap target root.
    assert 'id="broadcast-feed"' in body
    # Fragment only -- no full-page chrome.
    assert "<title>" not in body


def test_fragment_single_filter_embedded_in_sse_url() -> None:
    """A single op_class filter rides into the sse-connect query string."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/feed", params={"op_class": "write"})
    body = response.text
    assert 'sse-connect="/ui/broadcast/stream?op_class=write"' in body


def test_fragment_combined_filters_all_embedded_in_sse_url() -> None:
    """Combined op_class + principal + target all ride the sse-connect URL."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/feed",
            params={"op_class": "read", "principal": "op-7", "target": "rdc-vcenter"},
        )
    body = response.text
    # The three server filters are all present in the embedded URL.
    assert "op_class=read" in body
    assert "principal=op-7" in body
    assert "target=rdc-vcenter" in body
    assert 'sse-connect="/ui/broadcast/stream?' in body


def test_fragment_principal_with_special_chars_is_url_encoded() -> None:
    """A principal sub with reserved chars is percent-encoded in the URL.

    A raw ``&`` / ``=`` / space in the value would corrupt the query
    string; ``urlencode`` percent-encodes it so the bridge parses one
    coherent ``principal`` parameter.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/feed",
            params={"principal": "a&b=c d"},
        )
    body = response.text
    # The raw value never appears unencoded inside the sse-connect URL.
    assert "principal=a%26b%3Dc+d" in body


def test_fragment_op_id_filter_seed_reaches_controller() -> None:
    """op_id is a client-side filter; its seed is passed into the controller.

    op_id has no stream parameter -- it never rides the sse-connect URL.
    Instead the seed reaches the Alpine ``broadcastFeed`` controller so a
    copy-pasted filtered URL renders the narrowed view client-side.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/feed", params={"op_id": "vm.list"})
    body = response.text
    # op_id is NOT a stream param -- it must not appear in the SSE URL.
    assert "op_id=vm.list" not in body
    # It IS seeded into the controller for client-side narrowing.
    assert "vm.list" in body
    assert "opIdFilter" in body


def test_fragment_filter_values_echoed_for_selection_preservation() -> None:
    """The fragment echoes filter values so a re-render keeps the selection."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/feed",
            params={"op_class": "credential_read"},
        )
    body = response.text
    # The op_class selection is reflected in the embedded SSE URL.
    assert "op_class=credential_read" in body


# ---------------------------------------------------------------------------
# Event detail drawer
# ---------------------------------------------------------------------------


def test_drawer_renders_full_detail_for_non_aggregate_op() -> None:
    """A non-sensitive op renders the full payload + identifiers."""
    request_id = uuid.uuid4()
    audit_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        payload={"op_id": "vsphere.vm.list", "params": {"datacenter": "dc-1"}},
        request_id=request_id,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            f"/ui/broadcast/event/{audit_id}",
            params={"event_id": "evt-9000"},
        )
    assert response.status_code == 200, response.text
    body = response.text
    # Identifiers: audit_id, request_id, broadcast event_id.
    assert str(audit_id) in body
    assert str(request_id) in body
    assert "evt-9000" in body
    # Full payload params rendered (non-aggregate op).
    assert "datacenter" in body
    assert "dc-1" in body
    # No PII placeholder for a non-sensitive op.
    assert "aggregate-only" not in body
    # Drawer carries the Alpine click-outside dismiss island.
    assert 'id="event-drawer"' in body
    assert "click.outside" in body


def test_drawer_strips_internal_payload_keys() -> None:
    """The drawer hides audit-only keys from the rendered request payload."""
    audit_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        payload={
            "op_id": "vsphere.vm.create",
            "params": {"name": "vm-new"},
            "broadcast_detail_origin": "tenant_rule:abc-secret-uuid",
            "broadcast_detail_effective": "full",
        },
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/broadcast/event/{audit_id}")
    body = response.text
    assert "vm-new" in body
    # Internal forensic keys never reach the drawer payload view.
    assert "broadcast_detail_origin" not in body
    assert "tenant_rule:abc-secret-uuid" not in body


def test_drawer_credential_read_renders_lock_and_placeholder() -> None:
    """A credential_read op renders 🔒 + the aggregate-only placeholder.

    The drawer reads the canonical (unredacted) audit row; for a
    sensitive op it must withhold the payload exactly as the feed row
    does (decision #3 / work item #7) -- the secret params must never
    surface even on click.
    """
    audit_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        payload={"op_id": "vault.kv.read", "params": {"path": "secret/prod/db"}},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/broadcast/event/{audit_id}")
    assert response.status_code == 200, response.text
    body = response.text
    # The 🔒 marker (rendered as the HTML entity) + the exact placeholder copy.
    assert "&#x1F512;" in body
    assert "aggregate-only — credential read; details not broadcast" in body
    # The secret path must NEVER reach the drawer for a credential read.
    assert "secret/prod/db" not in body


def test_drawer_honours_effective_aggregate_verdict_for_audit_query() -> None:
    """An audit_query op (aggregate effective) withholds the payload."""
    audit_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        payload={
            "op_id": "audit.query",
            "params": {"filter": "operator=alice"},
            "broadcast_detail_effective": "aggregate",
        },
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/broadcast/event/{audit_id}")
    body = response.text
    assert "aggregate-only" in body
    assert "operator=alice" not in body


def test_drawer_404_for_unknown_audit_id() -> None:
    """A non-existent audit id renders the not-found fragment with 404."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/broadcast/event/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "Event not found" in response.text


def test_drawer_cross_tenant_audit_id_is_opaque_404() -> None:
    """A tenant-B audit id returns 404 for a tenant-A operator.

    Cross-tenant isolation: the tenant boundary is opaque, so a row that
    exists only in another tenant is indistinguishable from a missing id.
    """
    audit_id = _seed_audit_row(
        tenant_id=_TENANT_B,
        payload={"op_id": "vsphere.vm.list", "params": {}},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/broadcast/event/{audit_id}")
    assert response.status_code == 404
    assert "Event not found" in response.text


def test_drawer_falls_back_to_http_op_id_heuristic() -> None:
    """A row with no payload op_id classifies via the http.{method}:{path} form.

    A chassis HTTP route audit row carries no ``op_id``; the drawer must
    classify off the same ``http.{method.lower()}:{path}`` string the
    publisher used so the verdict matches. A plain GET path classifies
    ``other`` -> full detail.
    """
    audit_id = _seed_audit_row(
        tenant_id=_TENANT_A,
        payload={"params": {"q": "search-term"}},
        method="GET",
        path="/api/v1/connectors",
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/broadcast/event/{audit_id}")
    assert response.status_code == 200, response.text
    body = response.text
    # ``other`` class -> full detail; the params render.
    assert "search-term" in body
    assert "aggregate-only" not in body
