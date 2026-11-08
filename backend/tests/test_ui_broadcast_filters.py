# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Broadcast filters.

Initiative #338 (G10.1 Activity broadcast UI), Task #868 (G10.1-T2), and
#3762 (announcements only):

* The principal and target filters ride into the re-rendered feed's SSE
  URL, alone and combined.
* The target dropdown is tenant-scoped (from the tenant's targets).
* No operation affordance is left: no op class or op id filter, no
  operation drawer, and the page says people read and agents publish.

HTTP edge only: a minimal app wired with the UI session + CSRF
middlewares, an authenticated client via a seeded ``web_session`` row.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

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

from ._ui_role_helpers import allow_ui_operator_role


@pytest.fixture(autouse=True)
def _ui_operator_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console pages under test sit behind the ``operator`` role check."""
    allow_ui_operator_role(monkeypatch)


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


# ---------------------------------------------------------------------------
# Filter bar -- full page render
# ---------------------------------------------------------------------------


def test_page_renders_filter_bar_with_both_controls() -> None:
    """The full page renders the principal and target controls."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'name="principal"' in body
    assert 'name="target"' in body
    # The filter bar HTMX-submits the two server filters to the fragment route.
    assert 'hx-get="/ui/broadcast/feed"' in body
    assert 'hx-target="#broadcast-feed"' in body
    # #229: the select is named explicitly in the trigger; a bare
    # `from:find select` binds only the first matching descendant.
    assert "change from:find select[name=target]" in body
    assert "from:find select," not in body


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
    """No filters → the fragment's sse-connect carries only the page tenant marker."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/feed")
    assert response.status_code == 200, response.text
    body = response.text
    # No filter in the URL -> stream everything. Only the page's tenant
    # marker rides along (#5240).
    assert f'sse-connect="/ui/broadcast/stream?meho_tenant={_TENANT_A}"' in body
    # The fragment is the swap target root.
    assert 'id="broadcast-feed"' in body
    # Fragment only -- no full-page chrome.
    assert "<title>" not in body


def test_fragment_single_filter_embedded_in_sse_url() -> None:
    """A single target filter rides into the sse-connect query string."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/broadcast/feed", params={"target": "vc-01"})
    body = response.text
    assert f'sse-connect="/ui/broadcast/stream?target=vc-01&amp;meho_tenant={_TENANT_A}"' in body


def test_fragment_combined_filters_all_embedded_in_sse_url() -> None:
    """Combined principal + target both ride the sse-connect URL."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/feed",
            params={"principal": "op-7", "target": "vc-01"},
        )
    body = response.text
    # The two server filters are both present in the embedded URL.
    assert "principal=op-7" in body
    assert "target=vc-01" in body
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


def test_fragment_filter_values_echoed_for_selection_preservation() -> None:
    """The fragment echoes filter values so a re-render keeps the selection."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/broadcast/feed",
            params={"principal": "op-7", "target": "vc-01"},
        )
    body = response.text
    # The selections are echoed into the filter bar's inputs.
    assert 'value="op-7"' in body or "principal=op-7" in body
    assert "target=vc-01" in body


# ---------------------------------------------------------------------------
# No operation affordances (#3762)
# ---------------------------------------------------------------------------


def test_operation_filters_are_not_offered_or_carried() -> None:
    """Broadcast shows announcements only: no op class or op id filter.

    An old bookmarked URL with ``op_class`` / ``op_id`` still renders, but
    neither value reaches the page or the stream URL.
    """
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        page = client.get("/ui/broadcast", params={"op_class": "write", "op_id": "vm.list"})
        fragment = client.get("/ui/broadcast/feed", params={"op_class": "write"})
    assert page.status_code == 200, page.text
    assert fragment.status_code == 200, fragment.text
    for body in (page.text, fragment.text):
        assert 'name="op_class"' not in body
        assert 'name="op_id"' not in body
        assert "op_class=" not in body
        assert "vm.list" not in body
        assert "opIdFilter" not in body
        assert "badgeClasses" not in body
    assert f'sse-connect="/ui/broadcast/stream?meho_tenant={_TENANT_A}"' in fragment.text


def test_operation_drawer_route_is_gone() -> None:
    """The operation detail drawer left with the operation rows."""
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        page = client.get("/ui/broadcast")
        drawer = client.get(f"/ui/broadcast/event/{uuid.uuid4()}")
    assert 'id="event-drawer"' not in page.text
    assert drawer.status_code == 404


def test_page_says_people_read_and_agents_publish() -> None:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        body = client.get("/ui/broadcast").text
    assert "Only trusted agents can post" in body
    assert 'href="/ui/audit"' in body
    # No way to publish from the page.
    assert "meho_broadcast_announce" not in body
    assert 'hx-post="/ui/broadcast"' not in body
