# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the approvals UI surface.

Initiative #1775 (G10.7 Operator-console hardening), Task #1778.
Acceptance criteria on issue #1778:

* A bell in the app-shell shows a count badge fed by ``/ui/approvals/badge``
  (the live count); ``GET /ui/approvals/badge`` returns the tenant's pending
  count, and the badge bubble appears only when there is at least one.
* Clicking the bell opens a panel (``GET /ui/approvals``) listing pending
  requests; a request opens a modal (``GET /ui/approvals/{id}``) showing
  op_id / connector_id / proposed_effect / requester principal_sub /
  created_at.
* **Approve** -> ``POST /ui/approvals/{id}/approve`` (session + CSRF) calls
  ``approve_request`` in-process and re-dispatches the parked op; **Deny**
  -> ``POST /ui/approvals/{id}/reject`` calls ``reject_request``.
* Self-approval: when ``operator.sub == request.principal_sub`` the Approve
  button is disabled in the UI **and** the BFF rejects a forced
  self-approve (403) unless ``APPROVAL_ALLOW_SELF_APPROVAL``; Deny stays
  allowed.
* Tenant isolation: the bell + modal only surface the session tenant's
  requests; a cross-tenant id is a 404.
* No write goes to the Bearer ``/api/v1/approvals/*`` from the browser --
  the surface POSTs to the ``/ui/approvals`` BFF, which calls the service
  in-process.
* CSRF: a decision POST without the double-submit token is rejected 403.

Suite shape mirrors ``tests/test_ui_corpus.py``: ``_build_app`` wires
StaticFiles + the BFF auth router + the UI surface router (approvals ahead
of stubs) + ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
``ApprovalRequest`` rows are seeded into the autouse SQLite engine; the
operator-reconstruction seam
(:func:`~meho_backplane.ui.routes.approvals.routes._resolve_operator`) is
patched to return a constructed :class:`Operator` so the tests do not need
a live Keycloak / JWKS round-trip, and the post-approve re-dispatch
(:func:`~meho_backplane.ui.routes.approvals.routes.resume_dispatch_after_approval`)
is mocked (its behaviour is covered by the service + REST suites).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    Tenant,
)
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
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_REQUESTER_SUB = "agent-7"
_REVIEWER_SUB = "op-42"

#: HTMX request headers. The bell's modal-open ``hx-get /ui/approvals``
#: carries ``HX-Request: true``; the content-negotiated index route returns
#: the pending **panel** fragment for an HTMX fetch and the full-page
#: console for a normal navigation (#1827).
_HX_HEADERS = {"HX-Request": "true"}

#: The route-module symbol patched so the handlers get a reconstructed
#: operator without a live JWKS round-trip.
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.approvals.routes._resolve_operator"

#: The route-module symbol the approve handler calls after committing the
#: decision; mocked so the test does not drive the live dispatcher.
_RESUME_DISPATCH = "meho_backplane.ui.routes.approvals.routes.resume_dispatch_after_approval"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the corpus UI suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
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
# Helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal FastAPI app wired for approvals UI tests.

    Mirrors production + the corpus / kb UI tests: StaticFiles at
    ``/ui/static``, BFF auth router, UI surface router (approvals ahead of
    stubs), ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
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


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert one ``tenant`` row so session + approval FKs resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_request(
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID | None = None,
    op_id: str = "vsphere.vm.create",
    connector_id: str = "vsphere-1.x",
    principal_sub: str = _REQUESTER_SUB,
    status_value: str = ApprovalRequestStatus.PENDING.value,
    proposed_effect: dict[str, object] | None = None,
    reviewed_by: str | None = None,
    decided_at: datetime | None = None,
    work_ref: str | None = None,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """Insert one ``approval_request`` row; return its id."""
    rid = request_id or uuid.uuid4()
    effect = proposed_effect or {"op_id": op_id, "connector_id": connector_id}

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                ApprovalRequest(
                    id=rid,
                    tenant_id=tenant_id,
                    run_id=None,
                    principal_sub=principal_sub,
                    op_id=op_id,
                    connector_id=connector_id,
                    target_id=None,
                    params_hash="0" * 64,
                    params={"flavor": "small"},
                    proposed_effect=effect,
                    status=status_value,
                    reviewed_by=reviewed_by,
                    decided_at=decided_at,
                    work_ref=work_ref,
                    created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
                    expires_at=expires_at,
                ),
            )
        return rid

    return asyncio.run(_do())


def _request_status(request_id: uuid.UUID) -> str:
    """Read back the persisted status of an approval request (decision proof)."""

    async def _do() -> str:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            return row.status

    return asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _REVIEWER_SUB,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row and return its UUID (bypasses OAuth)."""

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


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _csrf_token(session_id: uuid.UUID) -> str:
    """Return a valid CSRF token for *session_id*."""
    return mint_csrf_token(str(session_id))


def _operator(
    *,
    tenant_id: uuid.UUID,
    sub: str = _REVIEWER_SUB,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    """Build an :class:`Operator` the patched ``_resolve_operator`` returns."""
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=role,
    )


def _dispatch_result() -> SimpleNamespace:
    """A minimal stand-in for the dispatcher result the approve path logs."""
    return SimpleNamespace(status="ok", op_id="vsphere.vm.create", result={}, error=None)


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_badge_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/approvals/badge`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/approvals/badge")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_approve_unauthenticated_is_blocked() -> None:
    """``POST /ui/approvals/{id}/approve`` without a session never reaches the handler.

    The CSRF middleware rejects a state-changing request with no session
    cookie at 403; either way an unauthenticated caller cannot decide.
    """
    rid = uuid.uuid4()
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.post(f"/ui/approvals/{rid}/approve")
    assert response.status_code in (302, 403)


# ---------------------------------------------------------------------------
# GET /ui/approvals/badge -- live count
# ---------------------------------------------------------------------------


def test_badge_zero_when_no_pending() -> None:
    """An empty queue renders a zero count with no badge bubble."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/badge")

    assert response.status_code == 200, response.text
    normalised = " ".join(response.text.split())
    assert 'data-pending-count="0"' in normalised
    # No badge bubble at zero.
    assert "badge-error" not in normalised


def test_badge_counts_pending_for_tenant() -> None:
    """The badge counts the tenant's pending requests and shows the bubble."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A)
    _seed_request(tenant_id=_TENANT_A, op_id="k8s.deploy.scale")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/badge")

    assert response.status_code == 200, response.text
    normalised = " ".join(response.text.split())
    assert 'data-pending-count="2"' in normalised
    assert "badge-error" in normalised
    assert ">2<" in normalised


def test_badge_excludes_decided_and_cross_tenant() -> None:
    """Only this tenant's *pending* requests count toward the badge."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_request(tenant_id=_TENANT_A)  # counts
    _seed_request(tenant_id=_TENANT_A, status_value=ApprovalRequestStatus.APPROVED.value)
    _seed_request(tenant_id=_TENANT_B)  # other tenant -- invisible
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/badge")

    assert response.status_code == 200, response.text
    assert 'data-pending-count="1"' in " ".join(response.text.split())


# ---------------------------------------------------------------------------
# GET /ui/approvals -- pending panel (bell modal; HX-Request fragment, #1827)
# ---------------------------------------------------------------------------


def test_panel_lists_pending_requests() -> None:
    """The bell's HX-Request fetch lists pending requests with a Review action."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, op_id="vsphere.vm.create")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    assert "Pending approvals" in body
    assert "vsphere.vm.create" in body
    assert _REQUESTER_SUB in body
    # The Review button opens the detail modal for this id.
    assert f'hx-get="/ui/approvals/{rid}"' in body
    # Fragment, not a full page.
    assert "<!doctype html>" not in body.lower()


def test_panel_empty_state() -> None:
    """An empty queue renders the no-pending-approvals state in the bell modal."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    assert "No pending approvals" in response.text


def test_panel_hides_cross_tenant_requests() -> None:
    """A request owned by another tenant never appears in the bell panel."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_request(tenant_id=_TENANT_B, op_id="secret.cross.tenant")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    assert "secret.cross.tenant" not in response.text
    assert "No pending approvals" in response.text


# ---------------------------------------------------------------------------
# GET /ui/approvals/{id} -- detail modal
# ---------------------------------------------------------------------------


def test_detail_modal_renders_request_fields() -> None:
    """The detail modal shows op_id, connector_id, proposed_effect, requester, created_at."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(
        tenant_id=_TENANT_A,
        op_id="vsphere.vm.create",
        connector_id="vsphere-1.x",
        proposed_effect={"op_id": "vsphere.vm.create", "host": "esx-3"},
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "vsphere.vm.create" in body
    assert "vsphere-1.x" in body
    assert _REQUESTER_SUB in body
    # proposed_effect rendered (the host field is in the JSON dump).
    assert "esx-3" in body
    # The approve + deny forms post to the BFF, carrying the CSRF header.
    assert f'hx-post="/ui/approvals/{rid}/approve"' in body
    assert f'hx-post="/ui/approvals/{rid}/reject"' in body
    assert "X-CSRF-Token" in body
    # CSRF cookie re-set on the modal render so the pair lines up.
    assert CSRF_COOKIE_NAME in response.cookies


def test_detail_modal_approve_enabled_for_other_operator() -> None:
    """A reviewer who is NOT the requester sees an enabled Approve button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    # The Approve button is not disabled for a non-requester reviewer.
    approve_button = response.text.split('data-action="approve"')[1].split("</button>")[0]
    assert "disabled" not in approve_button


def test_detail_modal_self_approval_disables_approve_but_not_deny() -> None:
    """When the reviewer IS the requester, Approve is disabled (Deny stays enabled).

    The disabled button's aria-label names the ``APPROVAL_ALLOW_SELF_APPROVAL``
    break-glass flag (#1401).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # Requester == reviewer.
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REVIEWER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    body = response.text
    approve_button = body.split('data-action="approve"')[1].split("</button>")[0]
    deny_button = body.split('data-action="reject"')[1].split("</button>")[0]
    assert "disabled" in approve_button
    assert "APPROVAL_ALLOW_SELF_APPROVAL" in approve_button
    assert "disabled" not in deny_button


def test_detail_modal_unknown_id_is_404() -> None:
    """A detail request for an id in another tenant (or absent) is 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    # Owned by tenant B; tenant-A operator must not see it.
    rid = _seed_request(tenant_id=_TENANT_B)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# POST /ui/approvals/{id}/approve -- in-process approve + re-dispatch
# ---------------------------------------------------------------------------


def test_approve_through_bff_decides_and_redispatches() -> None:
    """Approve POSTs to the BFF, flips the row to approved, and re-dispatches."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    resume_mock = AsyncMock(return_value=_dispatch_result())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME_DISPATCH, resume_mock),
        ):
            response = client.post(
                f"/ui/approvals/{rid}/approve",
                data={"reason": "looks safe"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # Terminal confirmation fragment + the bell-driving trigger header.
    assert "Request approved" in response.text
    assert response.headers.get("HX-Trigger") == "meho:approval-decided"
    # The row is durably approved.
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    # The parked op was re-dispatched via the shared resume helper.
    resume_mock.assert_awaited_once()


def test_reject_through_bff_decides_without_redispatch() -> None:
    """Deny POSTs to the BFF, flips the row to rejected, and does NOT re-dispatch."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    resume_mock = AsyncMock(return_value=_dispatch_result())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME_DISPATCH, resume_mock),
        ):
            response = client.post(
                f"/ui/approvals/{rid}/reject",
                data={"reason": "too risky"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    assert "Request denied" in response.text
    assert response.headers.get("HX-Trigger") == "meho:approval-decided"
    assert _request_status(rid) == ApprovalRequestStatus.REJECTED.value
    # A rejection never re-dispatches the parked op.
    resume_mock.assert_not_awaited()


def test_approve_already_decided_surfaces_conflict_in_modal() -> None:
    """Approving an already-decided request re-renders the modal with a 409 banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(
        tenant_id=_TENANT_A,
        principal_sub=_REQUESTER_SUB,
        status_value=ApprovalRequestStatus.REJECTED.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME_DISPATCH, new_callable=AsyncMock, return_value=_dispatch_result()),
        ):
            response = client.post(
                f"/ui/approvals/{rid}/approve",
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "409" in body
    assert "already" in body.lower()
    # Still on the modal, not the success fragment.
    assert "Request approved" not in body


# ---------------------------------------------------------------------------
# Self-approval -- server-side enforcement (never trust the disabled button)
# ---------------------------------------------------------------------------


def test_forced_self_approve_is_rejected_server_side() -> None:
    """A forged self-approve (bypassing the disabled button) 403s; the row stays pending.

    The disabled Approve button is UX only; the BFF re-checks the
    requester != approver invariant (#1401) via ``approve_request`` and
    re-renders the modal with a 403 banner.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # Requester == reviewer, break-glass OFF (default).
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REVIEWER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    resume_mock = AsyncMock(return_value=_dispatch_result())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME_DISPATCH, resume_mock),
        ):
            response = client.post(
                f"/ui/approvals/{rid}/approve",
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "403" in body
    assert "cannot approve your own request" in body.lower()
    # The row is NOT approved, and nothing was re-dispatched.
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value
    resume_mock.assert_not_awaited()


def test_self_deny_is_allowed_server_side() -> None:
    """An operator may deny their own request -- withdrawal is not an escalation."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REVIEWER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.post(
                f"/ui/approvals/{rid}/reject",
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    assert "Request denied" in response.text
    assert _request_status(rid) == ApprovalRequestStatus.REJECTED.value


def test_self_approve_allowed_when_break_glass_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``APPROVAL_ALLOW_SELF_APPROVAL=true`` a self-approve succeeds."""
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REVIEWER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    resume_mock = AsyncMock(return_value=_dispatch_result())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RESUME_DISPATCH, resume_mock),
        ):
            response = client.post(
                f"/ui/approvals/{rid}/approve",
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    assert "Request approved" in response.text
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    resume_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_approve_rejected_without_csrf_token() -> None:
    """A decision POST without the CSRF header is rejected 403 by the middleware.

    The decision never reaches the handler, so the row stays pending.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        # No X-CSRF-Token header -> the double-submit pair is incomplete.
        response = client.post(f"/ui/approvals/{rid}/approve")

    assert response.status_code == 403
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value


def test_reject_rejected_without_csrf_token() -> None:
    """A deny POST without the CSRF header is rejected 403; the row stays pending."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_REVIEWER_SUB)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(f"/ui/approvals/{rid}/reject")

    assert response.status_code == 403
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value


# ---------------------------------------------------------------------------
# App-shell wiring -- bell, nav, dashboard tile, SSE filter
# ---------------------------------------------------------------------------


def test_app_shell_renders_bell_and_sse_filter() -> None:
    """Every console page carries the approvals bell + the op_class=approval SSE sink."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/")

    assert response.status_code == 200, response.text
    body = response.text
    # The bell button loads the panel into the shared modal container.
    assert 'hx-get="/ui/approvals"' in body
    assert 'id="meho-approvals-modal-container"' in body
    # The bell icon is present.
    assert 'data-icon="bell"' in body
    # The badge target seeds itself from the live count endpoint.
    assert 'hx-get="/ui/approvals/badge"' in body
    # The notifications bell subscribes to the SSE bridge filtered to approvals.
    assert 'sse-connect="/ui/broadcast/stream?op_class=approval"' in body
    # The app-shell controller script is loaded on every page.
    assert "/ui/static/src/app/approvals-bell.js" in body


def test_dashboard_surface_grid_and_nav_include_approvals() -> None:
    """The dashboard surface-tile grid + sidebar nav link to ``/ui/approvals``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/ui/approvals"' in body
    assert "Approvals" in body


# ---------------------------------------------------------------------------
# GET /ui/approvals -- full-page console (#1827)
# ---------------------------------------------------------------------------


def test_index_normal_navigation_renders_full_page() -> None:
    """A normal navigation (no HX-Request) renders the chrome'd console page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="vsphere.vm.create")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals")

    assert response.status_code == 200, response.text
    body = response.text
    # Full page: the base-layout chrome is present, not the bare modal.
    assert "<!doctype html>" in body.lower()
    assert "MEHO Operator Console" in body
    # Status tabs + history list + the request row.
    assert 'role="tablist"' in body
    assert 'hx-get="/ui/approvals/list?tab=approved"' in body
    assert "vsphere.vm.create" in body
    # Sidebar highlights the Approvals surface.
    assert 'aria-current="page"' in body


def test_index_bell_click_returns_panel_fragment() -> None:
    """The bell's HX-Request fetch still returns the pending panel modal fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, op_id="vsphere.vm.create")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    # The unchanged pending-panel modal fragment, not the full page.
    assert "Pending approvals" in body
    assert "<!doctype html>" not in body.lower()
    assert f'hx-get="/ui/approvals/{rid}"' in body
    # The console's status tabs do NOT leak into the bell fragment.
    assert 'role="tablist"' not in body


# ---------------------------------------------------------------------------
# GET /ui/approvals/list -- decision-history partial (#1827)
# ---------------------------------------------------------------------------


def test_history_default_tab_is_pending_only() -> None:
    """The list partial defaults to the Pending tab (decided rows excluded)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="pending.op")
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="approved.op",
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by=_REVIEWER_SUB,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    assert "pending.op" in body
    assert "approved.op" not in body


def test_history_approved_tab_shows_decided_rows_with_reviewer() -> None:
    """The Approved tab lists decided rows with the reviewer + status pill."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="pending.op")
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="approved.op",
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by=_REVIEWER_SUB,
        decided_at=datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=approved", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    assert "approved.op" in body
    assert "pending.op" not in body
    # The reviewer (who decided) is surfaced.
    assert _REVIEWER_SUB in body
    # Approved rows carry the success pill.
    assert "badge-success" in body


def test_history_all_tab_includes_every_status() -> None:
    """The All tab passes status=None and returns pending + decided rows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="pending.op")
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="rejected.op",
        status_value=ApprovalRequestStatus.REJECTED.value,
        reviewed_by=_REVIEWER_SUB,
    )
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="expired.op",
        status_value=ApprovalRequestStatus.EXPIRED.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=all", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    assert "pending.op" in body
    assert "rejected.op" in body
    assert "expired.op" in body


def test_history_work_ref_filter_narrows_results() -> None:
    """A work_ref filter narrows the list to that change ticket."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="ticketed.op", work_ref="gh:evoila/meho#1")
    _seed_request(tenant_id=_TENANT_A, op_id="unticketed.op", work_ref=None)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/approvals/list?tab=all&work_ref=gh:evoila/meho%231",
            headers=_HX_HEADERS,
        )

    assert response.status_code == 200, response.text
    body = response.text
    assert "ticketed.op" in body
    assert "unticketed.op" not in body


def test_history_push_url_preserves_work_ref_filter() -> None:
    """``hx-push-url`` carries the active ``work_ref`` so the filtered view round-trips.

    Regression guard for #1827 M1: the tab buttons and the work_ref input
    pushed ``/ui/approvals?tab=...`` WITHOUT the active filter, so switching
    tabs / bookmarking lost it. ``GET /ui/approvals`` already accepts
    ``work_ref``, so the restored URL must carry the url-encoded value.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="ticketed.op", work_ref="gh:evoila/meho#1")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(
            "/ui/approvals/list?tab=all&work_ref=gh:evoila/meho%231",
            headers=_HX_HEADERS,
        )

    assert response.status_code == 200, response.text
    body = response.text
    # The pushed URL preserves the (url-encoded) work_ref alongside the tab,
    # so a tab switch / bookmark keeps the filter. ``#`` -> ``%23``.
    assert "work_ref=gh%3Aevoila/meho%231" in body
    # And it is not pushed bare (would drop the filter on navigation).
    assert 'hx-push-url="/ui/approvals?tab=all"' not in body


def test_history_push_url_is_filter_free_when_no_work_ref() -> None:
    """With no ``work_ref`` filter the pushed URL is the bare tab URL.

    The work_ref segment is conditional, so an unfiltered view does not push
    an empty ``&work_ref=`` that would round-trip as a blank filter box.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="ticketed.op")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=all", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    assert 'hx-push-url="/ui/approvals?tab=all"' in body
    assert "work_ref=" not in body.split('hx-push-url="/ui/approvals?tab=all"')[1].split('"')[0]


def test_history_offset_pager_is_not_capped_at_the_badge_limit() -> None:
    """The history pager uses offset; page 2 surfaces rows past the first page.

    The page size is 25, so seeding 26 pending requests must spill onto a
    second page reachable via ``offset`` -- proving the history is NOT
    silently truncated at the badge's 50-row glance cap.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # 26 rows: page 1 = 25, page 2 = 1. created_at is constant in the seed,
    # so order is stable for the assertion on count, not identity.
    for i in range(26):
        _seed_request(tenant_id=_TENANT_A, op_id=f"op.{i:02d}")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        page1 = client.get("/ui/approvals/list?tab=pending", headers=_HX_HEADERS)
        page2 = client.get(
            "/ui/approvals/list?tab=pending&offset=25&partial=rows", headers=_HX_HEADERS
        )

    assert page1.status_code == 200, page1.text
    assert page2.status_code == 200, page2.text
    # Page 1 offers "Load more" whose URL advances the offset to the next page.
    assert "Load more" in page1.text
    assert "offset=25" in page1.text
    # Page 2 is the tail: exactly one row, no further "Load more".
    assert page2.text.count("<li ") == 1
    assert "Load more" not in page2.text


def test_history_load_more_partial_returns_rows_only_not_a_nested_console() -> None:
    """The ``partial=rows`` append response is rows + an OOB pager, never a console.

    Regression guard for the #1827 B1 defect: the "Load more" button targets
    the rows ``<ul>`` with ``hx-swap="beforeend"``. If the offset fetch
    returned the full ``_history.html`` console (tabs + filter + a nested
    ``<ul id="approvals-history-rows">`` + pager), HTMX would append a whole
    second console *inside* the list and duplicate every id. The
    ``partial=rows`` response must therefore carry ONLY the page's ``<li>``
    rows plus an out-of-band pager re-render -- no second
    ``id="approvals-history"`` and no second ``id="approvals-history-rows"``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    for i in range(26):
        _seed_request(tenant_id=_TENANT_A, op_id=f"op.{i:02d}")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # Page 1 (full console) then the "Load more" append fetch.
        page1 = client.get("/ui/approvals/list?tab=pending", headers=_HX_HEADERS)
        rows = client.get(
            "/ui/approvals/list?tab=pending&offset=25&partial=rows", headers=_HX_HEADERS
        )

    assert page1.status_code == 200, page1.text
    assert rows.status_code == 200, rows.text
    body = rows.text
    # No nested console / list region -- the defect would leak these ids in.
    assert 'id="approvals-history"' not in body
    assert 'id="approvals-history-rows"' not in body
    # No tabs / filter leak into the append payload either.
    assert 'role="tablist"' not in body
    assert "Change ticket" not in body
    # Exactly one page-2 <li> row IS present (it appends onto the existing
    # list). All 26 rows share a constant created_at in the seed, so the
    # second page's identity is order-dependent -- assert the count, not a
    # particular op_id.
    assert body.count("<li ") == 1
    # The pager is re-rendered out of band so the button updates by id.
    assert 'id="approvals-history-pager"' in body
    assert 'hx-swap-oob="outerHTML"' in body
    # The first page's full console DID carry the list region (sanity).
    assert 'id="approvals-history-rows"' in page1.text


def test_history_load_more_partial_advances_offset_then_ends() -> None:
    """Each ``partial=rows`` append re-renders the pager with the NEXT offset.

    The #1827 B1 fix moves the pager out of the rows ``<ul>`` and re-renders
    it out of band on every append, so repeated "Load more" clicks advance
    the offset (page 2 -> page 3) instead of re-requesting the same page
    forever, and the affordance collapses to "No more requests" on the last
    page.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # 60 rows -> 3 pages of 25 / 25 / 10.
    for i in range(60):
        _seed_request(tenant_id=_TENANT_A, op_id=f"op.{i:02d}")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # Append page 2 (offset 25): its OOB pager must point at offset 50.
        page2 = client.get(
            "/ui/approvals/list?tab=pending&offset=25&partial=rows", headers=_HX_HEADERS
        )
        # Append page 3 (offset 50): the tail, no further "Load more".
        page3 = client.get(
            "/ui/approvals/list?tab=pending&offset=50&partial=rows", headers=_HX_HEADERS
        )

    assert page2.status_code == 200, page2.text
    assert page3.status_code == 200, page3.text
    # Page 2's re-rendered pager advances the Load-more URL to offset 50.
    assert "Load more" in page2.text
    assert "offset=50" in page2.text
    assert "offset=25" not in page2.text
    # Page 3 is the tail: 10 rows, the pager collapses to "No more requests".
    assert page3.text.count("<li ") == 10
    assert "Load more" not in page3.text
    assert "No more requests" in page3.text


def test_history_unknown_partial_is_422() -> None:
    """An unknown ``partial`` query value fails loud (422), not a silent default."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=pending&partial=bogus", headers=_HX_HEADERS)

    assert response.status_code == 422, response.text


def test_history_unknown_tab_is_422() -> None:
    """An unknown status-tab query value fails loud (422), not a silent default."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=bogus", headers=_HX_HEADERS)

    assert response.status_code == 422, response.text


def test_history_empty_state_per_tab() -> None:
    """An empty tab renders a tab-specific empty state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=rejected", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    assert "No rejected requests" in response.text


def test_history_hides_cross_tenant_rows() -> None:
    """The history list never surfaces another tenant's decided rows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_request(
        tenant_id=_TENANT_B,
        op_id="secret.cross.tenant",
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by="op-other",
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=all", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    assert "secret.cross.tenant" not in response.text


def test_history_re_fetches_on_live_decision_events() -> None:
    """The history region re-fetches its active tab on the bell's live events."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="pending.op")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/list?tab=pending", headers=_HX_HEADERS)

    assert response.status_code == 200, response.text
    body = response.text
    # The region re-fetches the ACTIVE tab (not just the badge) on the
    # body-wide events the app-shell bell dispatches.
    assert "meho:approval-bump from:body" in body
    assert "meho:approval-decided from:body" in body
    assert 'hx-get="/ui/approvals/list?tab=pending"' in body


# ---------------------------------------------------------------------------
# Badge stays pending-only (#1827 -- a hard requirement)
# ---------------------------------------------------------------------------


def test_badge_stays_pending_only_after_console_upgrade() -> None:
    """The badge still counts ONLY pending requests, never decided history."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A)  # pending -> counts
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="approved.op",
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by=_REVIEWER_SUB,
    )
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="rejected.op",
        status_value=ApprovalRequestStatus.REJECTED.value,
        reviewed_by=_REVIEWER_SUB,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals/badge")

    assert response.status_code == 200, response.text
    # Exactly one pending request -> count is 1, not 3.
    assert 'data-pending-count="1"' in " ".join(response.text.split())


# ---------------------------------------------------------------------------
# Detail modal -- decided rows render read-only (#1827)
# ---------------------------------------------------------------------------


def test_detail_modal_decided_row_is_read_only() -> None:
    """A decided request renders a decision banner and NO approve/deny forms."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(
        tenant_id=_TENANT_A,
        op_id="approved.op",
        status_value=ApprovalRequestStatus.APPROVED.value,
        reviewed_by=_REVIEWER_SUB,
        decided_at=datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub="op-99")

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    body = response.text
    # Decision banner names the outcome + reviewer.
    assert "Approved" in body
    assert _REVIEWER_SUB in body
    # No decision forms on a closed request.
    assert f'hx-post="/ui/approvals/{rid}/approve"' not in body
    assert f'hx-post="/ui/approvals/{rid}/reject"' not in body


def test_detail_modal_expired_row_shows_expiry_timestamp() -> None:
    """An expired request's banner renders the ``expires_at`` deadline as its "when".

    Regression guard for #1827 m1: an expired row is closed by timeout, not a
    decision, so ``decided_at`` is null and ``expires_at`` carries the time.
    The banner previously checked only ``decided_at``, so the expired branch
    read "Expired" with no timestamp. The fix falls back to ``expires_at``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    expiry = datetime(2026, 6, 17, 8, 30, tzinfo=UTC)
    rid = _seed_request(
        tenant_id=_TENANT_A,
        op_id="expired.op",
        status_value=ApprovalRequestStatus.EXPIRED.value,
        decided_at=None,
        expires_at=expiry,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub="op-99")

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    body = response.text
    # The banner names the outcome AND its time (the expiry deadline). The
    # date+time-of-day substring is asserted (not the full isoformat) so the
    # test is robust to whether the SQLite round-trip preserves the tz offset.
    assert "Expired" in body
    assert "2026-06-17T08:30:00" in body
    # The timestamp is rendered inside a <time> element for the expired branch.
    assert 'datetime="2026-06-17T08:30:00' in body
    # Read-only: no decision forms on a closed request.
    assert f'hx-post="/ui/approvals/{rid}/approve"' not in body
    assert f'hx-post="/ui/approvals/{rid}/reject"' not in body


def test_detail_modal_pending_row_still_offers_decisions() -> None:
    """A pending request still renders the approve/deny forms (regression guard)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{rid}")

    assert response.status_code == 200, response.text
    body = response.text
    assert f'hx-post="/ui/approvals/{rid}/approve"' in body
    assert f'hx-post="/ui/approvals/{rid}/reject"' in body


# ---------------------------------------------------------------------------
# params / params_hash never leak (#1827 -- a hard requirement)
# ---------------------------------------------------------------------------


def test_internal_params_never_reach_the_history_view() -> None:
    """The internal params / params_hash columns are never projected to the UI."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # The seed sets params={"flavor": "small"} + params_hash="0"*64.
    _seed_request(tenant_id=_TENANT_A, op_id="op.with.params")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        list_resp = client.get("/ui/approvals/list?tab=all", headers=_HX_HEADERS)
        page_resp = client.get("/ui/approvals")

    for response in (list_resp, page_resp):
        assert response.status_code == 200, response.text
        body = response.text
        assert "flavor" not in body
        assert "params_hash" not in body
        assert "0" * 64 not in body
