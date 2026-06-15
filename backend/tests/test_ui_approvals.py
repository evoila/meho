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
                    created_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
                    expires_at=None,
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
# GET /ui/approvals -- pending panel
# ---------------------------------------------------------------------------


def test_panel_lists_pending_requests() -> None:
    """``GET /ui/approvals`` lists this tenant's pending requests with a Review action."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, op_id="vsphere.vm.create")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals")

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
    """An empty queue renders the no-pending-approvals state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals")

    assert response.status_code == 200, response.text
    assert "No pending approvals" in response.text


def test_panel_hides_cross_tenant_requests() -> None:
    """A request owned by another tenant never appears in the panel."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_request(tenant_id=_TENANT_B, op_id="secret.cross.tenant")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals")

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
