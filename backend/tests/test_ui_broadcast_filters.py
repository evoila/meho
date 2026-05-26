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
from html.parser import HTMLParser

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


def test_target_dropdown_excludes_soft_deleted_targets() -> None:
    """The target dropdown filters ``deleted_at IS NULL``.

    Regression test for G0.14-T4 #1145: the dropdown must stay in parity
    with ``GET /api/v1/targets`` and the MCP ``list_targets`` tool, which
    both exclude soft-deleted rows. Without this, a tombstoned target
    name would surface in the dropdown and selecting it would produce an
    empty filtered feed with no UI explanation.

    Seeds one live + one soft-deleted target on the same tenant; the
    rendered page must contain the live name and omit the dead one.
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM

    _seed_target(tenant_id=_TENANT_A, name="live-target")
    _seed_target(tenant_id=_TENANT_A, name="dead-target")

    # Soft-delete the second target the same way the DELETE handler does.
    async def _soft_delete(name: str) -> None:
        from sqlalchemy import select as sa_select

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            row = (
                await session.execute(
                    sa_select(TargetORM).where(
                        TargetORM.tenant_id == _TENANT_A,
                        TargetORM.name == name,
                    )
                )
            ).scalar_one()
            row.deleted_at = datetime.now(UTC)

    asyncio.run(_soft_delete("dead-target"))

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    body = response.text
    assert 'value="live-target"' in body
    assert "dead-target" not in body


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


def test_op_id_filter_survives_server_side_filter_re_render() -> None:
    """op_id client filtering must survive an op_class/principal/target swap.

    Regression guard for review finding B1 on PR #1041. A server-side
    filter change (op_class/principal/target) ``hx-get``s the fragment
    route -- WITHOUT op_id (it is excluded from the form's
    ``hx-include``) -- and swaps ``#broadcast-feed``. The fresh fragment
    therefore seeds ``opIdFilter`` empty, and the re-mounted Alpine
    controller would drop the operator's active op_id filter even though
    the op_id input (outside the swapped fragment) still shows the typed
    value.

    The fix: the controller's ``init`` re-reads the live op_id input on
    every mount (initial load AND every swap), making the input the
    single source of truth so the client-side narrowing keeps applying.
    This test proves the two halves of that contract at the surface this
    repo exposes (no headless browser harness):

    1. The swap fragment for an op_class change (op_id absent) still
       mounts the ``broadcastFeed`` controller -- so Alpine re-runs
       ``init`` on the swapped-in node.
    2. The served controller JS re-reads ``input[name="op_id"]`` in
       ``init`` rather than relying solely on the server ``opIdFilter``
       seed (which is empty on a server re-render).
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # The exact swap that triggered B1: a server-side filter changes,
        # op_id is NOT carried (excluded from hx-include).
        fragment = client.get("/ui/broadcast/feed", params={"op_class": "write"})
        controller_js = client.get("/ui/static/src/app/broadcast-feed.js")
    assert fragment.status_code == 200, fragment.text
    # 1. The swapped fragment re-mounts the controller (so Alpine re-runs
    #    init on the swapped node).
    assert "broadcastFeed(" in fragment.text
    # op_id was not part of this server re-render, so the server seed is
    # empty -- the survival cannot come from the server context.
    assert "op_id=write" not in fragment.text

    assert controller_js.status_code == 200, controller_js.text
    js = controller_js.text
    # 2. init re-reads the live op_id input -- the source of truth that
    #    survives the swap -- rather than trusting only the server seed.
    assert "init()" in js
    assert "document.querySelector('input[name=\"op_id\"]')" in js


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


# ---------------------------------------------------------------------------
# op_id reflected-XSS hardening (review finding on PR #1044)
# ---------------------------------------------------------------------------

#: A crafted ``op_id`` query value that tries to break out of an
#: ``x-data`` attribute and inject live event handlers. It carries every
#: HTML metacharacter the hardening must neutralise: a double-quote (the
#: byte ``| tojson`` does NOT escape, which terminates a double-quoted
#: attribute early), a single-quote, ``<`` / ``>`` / ``&``, and a literal
#: ``</script>`` (which would close a data-island script element). The
#: ``autofocus onfocus=...`` payload is what would fire if the value
#: escaped the attribute and grafted itself onto the host element.
_XSS_OP_ID = (
    "\" autofocus onfocus=alert(document.cookie) x='1' <b>& </script><img src=x onerror=alert(2)>"
)


class _XDataAttrCollector(HTMLParser):
    """Collect the parsed attribute list of every element carrying ``x-data``.

    The browser's HTML parser is the ground truth for "did the attribute
    break out": if the injected ``op_id`` terminated ``x-data`` early, the
    parser sees ``autofocus`` / ``onfocus`` as *separate attributes* on the
    host element rather than as bytes inside the single ``x-data`` value.
    We parse the response the same way and assert no such stray attribute
    appears. Parser-grounded so the test holds for either safe rendering
    (single-quoted ``x-data`` or a data-island refactor).
    """

    def __init__(self) -> None:
        super().__init__()
        # One entry per element that has an ``x-data`` attribute: the full
        # list of (name, value) attribute pairs the parser saw on it.
        self.x_data_elements: list[list[tuple[str, str | None]]] = []
        # Every attribute NAME the parser saw across the whole document
        # (lower-cased), to catch a handler injected onto ANY element.
        self.all_attr_names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name.lower() for name, _ in attrs}
        self.all_attr_names |= names
        if "x-data" in names:
            self.x_data_elements.append(attrs)


def _assert_no_xss_breakout(body: str) -> None:
    """Assert the rendered HTML did not let ``op_id`` break out of ``x-data``.

    Fails on the pre-fix ``{{ op_id | tojson }}`` inside a *double-quoted*
    ``x-data="..."`` (the double-quote bytes ``tojson`` leaves raw
    terminate the attribute, so the parser grafts ``autofocus`` / ``onfocus``
    onto the element); passes once the attribute is single-quoted (or the
    config moves to a data island).
    """
    parser = _XDataAttrCollector()
    parser.feed(body)

    # No event handler / autofocus leaked onto ANY element as a parsed
    # attribute. If the attribute broke out, the parser would surface these.
    assert "onfocus" not in parser.all_attr_names, (
        "op_id broke out of x-data: 'onfocus' parsed as a live attribute"
    )
    assert "autofocus" not in parser.all_attr_names, (
        "op_id broke out of x-data: 'autofocus' parsed as a live attribute"
    )
    assert "onerror" not in parser.all_attr_names, (
        "op_id broke out: 'onerror' parsed as a live attribute"
    )

    # The op_id payload must live ENTIRELY inside an x-data attribute value
    # -- never as a stray attribute and never as injected element markup.
    # At least one x-data element must carry the marker substring inside its
    # x-data value (proving it stayed contained), and no x-data element may
    # carry it as a separate attribute.
    contained_somewhere = False
    for attrs in parser.x_data_elements:
        for name, value in attrs:
            if name.lower() == "x-data" and value and "onfocus=alert" in value:
                contained_somewhere = True
            else:
                # The marker must not appear in any OTHER attribute on an
                # x-data element (that would mean it escaped the value).
                assert value is None or "onfocus=alert" not in value, (
                    f"op_id leaked into a non-x-data attribute {name!r}"
                )
    assert contained_somewhere, (
        "expected the op_id payload to stay contained inside an x-data value"
    )

    # No premature script-element close from the </script> in the payload
    # (guards the data-island alternative too). The only </script> tags in
    # the document must pair with a <script ...> the templates author.
    assert body.count("</script>") == body.count("<script"), (
        "a </script> in op_id closed a script element early"
    )


def test_feed_fragment_op_id_xss_payload_cannot_break_out_of_x_data() -> None:
    """A breakout ``op_id`` cannot escape ``x-data`` on the feed fragment.

    Regression for the reflected-XSS finding on PR #1044. The fragment
    interpolates the reflected ``op_id`` into the ``broadcastFeed`` Alpine
    controller config. ``| tojson`` does not escape the double-quote, so a
    crafted ``op_id`` would terminate a double-quoted ``x-data="..."`` and
    inject live handlers. Asserts (via the HTML parser) no handler leaks
    onto the element and no script closes early. Fails on the pre-fix
    double-quoted attribute; passes once single-quoted.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/feed", params={"op_id": _XSS_OP_ID})
    assert response.status_code == 200, response.text
    _assert_no_xss_breakout(response.text)


def test_full_page_op_id_xss_payload_cannot_break_out_of_x_data() -> None:
    """A breakout ``op_id`` cannot escape ``x-data`` on the full page.

    The full page (``GET /ui/broadcast``) reflects ``op_id`` into TWO
    double-quote-vulnerable sinks pre-fix: the feed fragment's
    ``broadcastFeed(...)`` config AND the filter bar's standalone
    ``x-data="{ opId: ... }"`` island. Both must be breakout-proof; the
    parser-grounded assertion covers every ``x-data`` element on the page.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast", params={"op_id": _XSS_OP_ID})
    assert response.status_code == 200, response.text
    _assert_no_xss_breakout(response.text)
    # The reflected value still reaches the controller config (behaviour
    # preserved -- the op_id filter seed is not dropped by the hardening).
    assert "opIdFilter" in response.text
