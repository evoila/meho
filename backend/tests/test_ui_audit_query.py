# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit-query forensic console UI surface.

Initiative #1841 (G10.15 Audit-query forensic console), Task #1944 (T1).
The query page is the entry surface: a filter form over unbounded
``audit_log`` history, forward-cursor "Load more" paging, and one-click
pivots (who-touched / by-work-ref / replay).

Acceptance criteria on issue #1944 covered here:

* AC2(a): ``GET /ui/audit`` renders the filter form + first page for a
  session operator.
* AC2(b): ``GET /ui/audit/results?cursor=<next>`` returns the next page and
  the "Load more" link carries the new ``next_cursor``.
* AC2(c): a foreign-tenant row never appears (the substrate WHERE clause is
  ``tenant_id=session.tenant_id``).
* AC2(d): a tampered ``cursor`` resets to page 1, not a 500.
* AC2(e): a bad ``since`` shorthand renders an inline field error, not a 500.
* AC3: the replay pivot is ``TENANT_ADMIN``-only -- an operator-role lift
  renders it disabled; a ``tenant_admin`` lift renders it enabled and
  deep-linking to ``/ui/audit/sessions/{agent_session_id}/replay``.

Suite shape mirrors :mod:`backend.tests.test_ui_broadcast_feed` (the
session-cookie HTTP edge) and :mod:`backend.tests.test_ui_runbook_driver_opacity`
(the JWT-role-lift reconstruction for the admin/operator paths): a minimal
FastAPI app with the BFF middlewares + the UI router, SQLite-backed seeding,
and a pre-set session cookie.

The test module is named ``test_ui_audit_query`` (not ``test_ui_audit``):
``test_ui_audit.py`` is already taken by the unrelated BFF audit-thread
suite (#1216) that asserts every ``/ui/*`` GET writes an ``audit_log`` row.
This module follows the same flat ``test_ui_*.py`` convention.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, Tenant
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
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"

# Two stable tenant ids -- distinct values so the cross-tenant isolation
# assertion has concrete state.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

#: The session's operator -- the JWT ``sub`` for the role-lift paths (must
#: match the seeded session's ``operator_sub`` or the lift fails the identity
#: check and degrades to operator).
_OPERATOR_SUB = "op-self"

#: A fixed base timestamp so the ``occurred_at`` ordering is deterministic.
_BASE = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (read-surface baseline).

    Mirrors :func:`backend.tests.test_ui_runbook_driver_opacity._bff_env`.
    Cache + global-state resets run on setup and teardown so a failing test
    cannot leak ``_TEMPLATES`` / session-engine state into the next case.
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


# ---------------------------------------------------------------------------
# Builders / seeding
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the audit UI tests."""
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


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert one ``tenant`` row so FK + target-name joins resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_audit_row(
    *,
    tenant_id: uuid.UUID,
    second: int,
    op_id: str = "vsphere.vm.list",
    op_class: str = "read",
    operator_sub: str = "op-actor",
    status_code: int = 200,
    work_ref: str | None = None,
    agent_session_id: uuid.UUID | None = None,
    row_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one ``audit_log`` row at ``_BASE + second``; return its id."""
    resolved_id = row_id or uuid.uuid4()

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=resolved_id,
                    occurred_at=_BASE + timedelta(seconds=second),
                    operator_sub=operator_sub,
                    tenant_id=tenant_id,
                    method="POST",
                    path="/mcp",
                    status_code=status_code,
                    duration_ms=Decimal("1.0"),
                    payload={"op_id": op_id, "op_class": op_class},
                    work_ref=work_ref,
                    agent_session_id=agent_session_id,
                )
            )
        return resolved_id

    return asyncio.run(_do())


def _seed_rows(*, tenant_id: uuid.UUID, count: int, op_id: str = "vsphere.vm.list") -> None:
    """Seed *count* audit rows so paging has more than one page."""
    for i in range(count):
        _seed_audit_row(tenant_id=tenant_id, second=i, op_id=f"{op_id}.{i}")


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OPERATOR_SUB,
    access_token: str = "access-token-plaintext",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token*; return its UUID."""

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


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _role_session(
    role: TenantRole,
    *,
    operator_sub: str = _OPERATOR_SUB,
    tenant_id: uuid.UUID = _TENANT_A,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real JWT carrying *role*.

    Returns the session id and the JWKS the role lift must reach. A
    ``TENANT_ADMIN`` token lifts to admin; an ``OPERATOR`` token decodes
    cleanly but is not admin.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-audit-test-kid")
    jwks = _public_jwks(keypair)
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id, operator_sub=operator_sub, access_token=access_token
    )
    return session_id, jwks


def _tampered_cursor() -> str:
    """A syntactically-valid-base64 but undecodable cursor token."""
    return "this-is-not-a-valid-cursor"


# ===========================================================================
# Authentication boundary
# ===========================================================================


def test_audit_page_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/audit`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/audit")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ===========================================================================
# AC2(a): page renders the filter form + first result page
# ===========================================================================


def test_audit_page_renders_form_and_first_page() -> None:
    """AC2(a): the page extends base.html, renders the filter form + rows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, second=0, op_id="vsphere.vm.poweron")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Audit" in body
    # Sidebar active-state on the Audit link.
    assert 'aria-current="page"' in body
    assert "menu-active" in body
    # The filter form + its fields.
    assert 'id="audit-filter-form"' in body
    assert 'name="target"' in body
    assert 'name="op_class"' in body
    assert 'name="since"' in body
    assert 'name="work_ref"' in body
    # The first result page rendered the seeded row.
    assert "vsphere.vm.poweron" in body
    # The form ``hx-get``s, so the page pairs the CSRF cookie (chassis
    # convention) -- mirrors broadcast/topology.
    assert CSRF_COOKIE_NAME in response.cookies


def test_audit_page_renders_empty_state_when_no_rows() -> None:
    """The empty-state copy renders when no audit rows match."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit")
    assert response.status_code == 200, response.text
    assert "No audit rows match these filters" in response.text


def test_audit_results_fragment_omits_page_chrome() -> None:
    """``GET /ui/audit/results`` returns the fragment, not the full page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, second=0, op_id="k8s.pods.list")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")
    assert response.status_code == 200, response.text
    body = response.text
    assert "k8s.pods.list" in body
    # Fragment: no full-page chrome (<title>) / no filter form.
    assert "<title>" not in body
    assert 'id="audit-filter-form"' not in body


# ===========================================================================
# AC2(b): forward-cursor paging -- next page + advanced Load-more cursor
# ===========================================================================


def test_audit_first_page_renders_load_more_when_more_rows() -> None:
    """A result set larger than one page renders a "Load more" with a cursor."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_rows(tenant_id=_TENANT_A, count=60)  # > _PAGE_SIZE (50)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Load more" in body
    assert "cursor=" in body
    assert "No more rows" not in body


def test_audit_cursor_returns_next_page_with_advanced_cursor() -> None:
    """AC2(b): ``?cursor=<next>`` returns the next page; Load-more advances.

    Page 1's "Load more" carries cursor C1. Re-fetching with that cursor
    returns the next page's rows (distinct from page 1) and the OOB pager
    re-render carries a NEW cursor C2 (strictly older), proving the forward
    cursor threads correctly.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # 60 rows: second i -> occurred_at = base + i. Newest-first ordering means
    # page 1 is seconds 59..10 and page 2 starts at second 9.
    _seed_rows(tenant_id=_TENANT_A, count=60)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        page1 = client.get("/ui/audit/results")
        body1 = page1.text
        assert "vsphere.vm.list.59" in body1  # newest row on page 1
        assert "vsphere.vm.list.9" not in body1  # first row of page 2

        cursor1 = _extract_cursor(body1)
        page2 = client.get(f"/ui/audit/results?cursor={cursor1}&partial=rows")

    assert page2.status_code == 200, page2.text
    body2 = page2.text
    # Page 2 carries the next rows, not page 1's newest.
    assert "vsphere.vm.list.9" in body2
    assert "vsphere.vm.list.59" not in body2
    # The OOB pager re-render advanced the cursor (or collapsed to "No more").
    cursor2 = _extract_cursor(body2) if "Load more" in body2 else None
    assert cursor2 != cursor1


def _extract_cursor(body: str) -> str:
    """Pull the ``cursor=`` value out of a rendered "Load more" hx-get URL."""
    import re

    match = re.search(r"cursor=([^&\"]+)&partial=rows", body)
    assert match, f"no Load-more cursor found in body: {body[:500]!r}"
    return match.group(1)


def test_audit_last_page_shows_no_more_rows() -> None:
    """A single-page result set renders "No more rows", no "Load more"."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_rows(tenant_id=_TENANT_A, count=3)  # < _PAGE_SIZE
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")
    assert response.status_code == 200, response.text
    assert "No more rows" in response.text
    assert "Load more" not in response.text


# ===========================================================================
# AC2(c): tenant isolation -- a foreign-tenant row never appears
# ===========================================================================


def test_audit_never_surfaces_foreign_tenant_rows() -> None:
    """AC2(c): a tenant-B row never appears on a tenant-A session's results."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_audit_row(tenant_id=_TENANT_A, second=0, op_id="tenant.a.only.op")
    _seed_audit_row(tenant_id=_TENANT_B, second=1, op_id="tenant.b.secret.op")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")

    assert response.status_code == 200, response.text
    body = response.text
    assert "tenant.a.only.op" in body
    assert "tenant.b.secret.op" not in body


# ===========================================================================
# AC2(d): a tampered cursor resets to page 1, not a 500
# ===========================================================================


def test_audit_tampered_cursor_resets_to_page_one() -> None:
    """AC2(d): a tampered ``cursor`` resets to page 1 (200), never a 500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, second=0, op_id="audit.reset.probe")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/results?cursor={_tampered_cursor()}")
    assert response.status_code == 200, response.text
    # Page 1 rows rendered despite the bad cursor (treated as "start over").
    assert "audit.reset.probe" in response.text


# ===========================================================================
# AC2(e): a bad ``since`` shorthand renders an inline field error, not a 500
# ===========================================================================


def test_audit_bad_since_renders_inline_error_not_500() -> None:
    """AC2(e): a bad ``since`` shorthand renders an inline error, not a 500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, second=0)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results?since=not-a-duration")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Could not parse the time window" in body


def test_audit_results_rejects_unknown_partial() -> None:
    """A foreign ``partial`` value is a 422, not a silently-coerced render."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results?partial=bogus")
    assert response.status_code == 422, response.text


# ===========================================================================
# AC3: the replay pivot is TENANT_ADMIN-only
# ===========================================================================


def test_replay_pivot_disabled_for_operator() -> None:
    """AC3: an operator-role lift renders the replay pivot disabled.

    A plaintext access token cannot be JWT-verified, so the role lift fails
    soft to operator and the replay pivot renders disabled with a tooltip --
    not an enabled deep-link.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    _seed_audit_row(
        tenant_id=_TENANT_A, second=0, op_id="agent.session.op", agent_session_id=session_uuid
    )
    # Plaintext token -> soft role lift fails to operator.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")

    assert response.status_code == 200, response.text
    body = response.text
    # The disabled pivot renders (btn-disabled + tooltip); no enabled
    # deep-link to the replay surface.
    assert "btn-disabled" in body
    assert "tenant_admin role" in body
    assert f"/ui/audit/sessions/{session_uuid}/replay" not in body


def test_replay_pivot_enabled_for_tenant_admin() -> None:
    """AC3: a tenant_admin lift renders the replay pivot enabled + deep-linked."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_uuid = uuid.uuid4()
    _seed_audit_row(
        tenant_id=_TENANT_A, second=0, op_id="agent.session.op", agent_session_id=session_uuid
    )
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The enabled pivot deep-links to the T3 replay surface for this session.
    assert f"/ui/audit/sessions/{session_uuid}/replay" in body


def test_replay_pivot_absent_when_row_has_no_session() -> None:
    """A row with no ``agent_session_id`` renders no replay pivot at all."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_audit_row(tenant_id=_TENANT_A, second=0, op_id="no.session.op")
    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    # No replay affordance (enabled or disabled) for a session-less row.
    assert "/replay" not in response.text


# ===========================================================================
# Pivots: who-touched / by-work-ref deep-links
# ===========================================================================


def test_who_touched_and_work_ref_pivots_render() -> None:
    """A row with a target + work_ref renders both filter-bound pivots."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # A target name resolves via the LEFT JOIN; seed a target row + an audit
    # row referencing it so target_name is denormalized onto the entry.
    target_id = _seed_target(tenant_id=_TENANT_A, name="rdc-vcenter")
    _seed_audit_row_with_target(
        tenant_id=_TENANT_A,
        second=0,
        target_id=target_id,
        work_ref="gh:evoila/meho#1",
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/audit/results")

    assert response.status_code == 200, response.text
    body = response.text
    # who-touched pivot binds the target; by-work-ref binds the ref.
    assert "/ui/audit?target=rdc-vcenter" in body
    assert "work_ref=gh" in body  # urlencoded; the gh: prefix survives


def _seed_target(*, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert one ``targets`` row so the audit-row LEFT JOIN resolves a name."""
    from meho_backplane.db.models import Target

    target_id = uuid.uuid4()

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Target(
                    id=target_id,
                    tenant_id=tenant_id,
                    name=name,
                    product="vsphere",
                    host="vcenter.example.com",
                )
            )
        return target_id

    return asyncio.run(_do())


def _seed_audit_row_with_target(
    *,
    tenant_id: uuid.UUID,
    second: int,
    target_id: uuid.UUID,
    work_ref: str | None = None,
) -> uuid.UUID:
    """Insert one audit row bound to *target_id* (so target_name resolves)."""
    row_id = uuid.uuid4()

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=row_id,
                    occurred_at=_BASE + timedelta(seconds=second),
                    operator_sub="op-actor",
                    tenant_id=tenant_id,
                    target_id=target_id,
                    method="POST",
                    path="/mcp",
                    status_code=200,
                    duration_ms=Decimal("1.0"),
                    payload={"op_id": "vsphere.vm.poweron", "op_class": "write"},
                    work_ref=work_ref,
                )
            )
        return row_id

    return asyncio.run(_do())
