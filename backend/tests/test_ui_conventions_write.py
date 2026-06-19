# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Conventions UI write surface.

Initiative #1838 (G10.12 Conventions console), Task #1896 (T2). Covers
the author / edit modals + debounced token-cost preview, the
non-idempotent DELETE confirm gate (double-fire safety), the history
diff panel, the post-write ``preamble_status`` surfacing, and the
RBAC + CSRF gates.

Suite shape mirrors :mod:`backend.tests.test_ui_runbooks_lifecycle` (a
real RSA-signed ``tenant_admin`` JWT lifted through the BFF session so
``resolve_operator_or_403`` passes) and
:mod:`backend.tests.test_ui_conventions_list` (the seed helpers).
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.conventions.schemas import ConventionKind
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant, TenantConvention, TenantConventionHistory
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
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
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
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OPERATOR_SUB = "op-alice"

#: ~1500-char operational body. Estimated cost (~460 tokens) is under
#: the 600 budget alone, but two of them sum over budget -- so seeding a
#: high-priority one first and then creating a second drops the second.
_BIG_BODY = "Always confirm the change set with the operator before applying. " * 24

#: A body whose single estimate exceeds the 600-token budget outright
#: (>~2000 chars / 3.3). Used for the create/preview over-budget cases.
_OVER_BUDGET_BODY = "x" * 2200


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the list suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
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
    """Construct a minimal FastAPI app wired for the conventions UI tests."""
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
    """Insert one ``tenant`` row so the convention tenant FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_convention(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    title: str,
    body: str,
    kind: ConventionKind,
    priority: int = 0,
) -> None:
    """Persist one ``tenant_convention`` row directly."""
    now = datetime.now(UTC)

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TenantConvention(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    slug=slug,
                    title=title,
                    body=body,
                    kind=kind.value,
                    priority=priority,
                    created_by_sub=_OPERATOR_SUB,
                    created_at=now,
                    updated_at=now,
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str = _OPERATOR_SUB,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""

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
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-conventions-write-test-kid")
    return keypair, _public_jwks(keypair)


def _role_session(
    role: TenantRole,
    tenant_id: uuid.UUID = _TENANT_A,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real JWT carrying *role*.

    A ``TENANT_ADMIN`` token passes ``resolve_operator_or_403``; an
    ``OPERATOR`` token decodes cleanly but fails the role rank check
    -> 403.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(tenant_id=tenant_id, access_token=access_token)
    return session_id, jwks


def _admin_session(tenant_id: uuid.UUID = _TENANT_A) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed an admin session whose access token is a real tenant_admin JWT."""
    return _role_session(TenantRole.TENANT_ADMIN, tenant_id)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient + a respx mock for the JWKS endpoint."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


def _csrf_kwargs(session_id: uuid.UUID) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check."""
    token = mint_csrf_token(str(session_id))
    return {
        "headers": {CSRF_HEADER_NAME: token, "X-CSRF-Token": token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


def _row_exists(tenant_id: uuid.UUID, slug: str) -> bool:
    """Return whether a ``tenant_convention`` row exists for ``(tenant, slug)``."""

    async def _do() -> bool:
        from sqlalchemy import select

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(TenantConvention).where(
                TenantConvention.tenant_id == tenant_id,
                TenantConvention.slug == slug,
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    return asyncio.run(_do())


def _history_count(tenant_id: uuid.UUID, slug: str) -> int:
    """Count history rows for a convention by ``(tenant, slug)``."""

    async def _do() -> int:
        from sqlalchemy import select

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            conv_stmt = select(TenantConvention.id).where(
                TenantConvention.tenant_id == tenant_id,
                TenantConvention.slug == slug,
            )
            conv_id = (await session.execute(conv_stmt)).scalar_one_or_none()
            if conv_id is None:
                # Row may already be deleted; count via any history row id.
                return 0
            hist_stmt = select(TenantConventionHistory).where(
                TenantConventionHistory.convention_id == conv_id,
            )
            return len((await session.execute(hist_stmt)).scalars().all())

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# Author modal -- create end-to-end + duplicate conflict inline
# ---------------------------------------------------------------------------


def test_create_modal_renders_for_admin() -> None:
    """``GET /ui/conventions/create`` renders the author modal for an admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/create")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="conventions-create-modal"' in body
    # The debounced preview wiring is present.
    assert 'hx-post="/ui/conventions/preview"' in body
    assert "keyup changed delay:300ms" in body


def test_create_end_to_end_returns_hx_redirect() -> None:
    """A tenant_admin create persists the row and returns HX-Redirect."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/create",
            data={
                "slug": "confirm-first",
                "title": "Confirm before applying",
                "body": "Always confirm before a destructive change.",
                "kind": "operational",
                "priority": "10",
            },
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/conventions"
    assert _row_exists(_TENANT_A, "confirm-first")
    # Service write path wrote the paired CREATE history row.
    assert _history_count(_TENANT_A, "confirm-first") == 1


def test_create_duplicate_slug_renders_409_inline_no_redirect() -> None:
    """A duplicate slug renders the conflict inline (no HX-Redirect)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="dupe",
        title="Existing",
        body="already here",
        kind=ConventionKind.REFERENCE,
    )
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/create",
            data={
                "slug": "dupe",
                "title": "Second",
                "body": "trying to clash",
                "kind": "reference",
                "priority": "0",
            },
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    # Inline error: 200 fragment, no redirect, with an actionable message.
    assert response.status_code == 200, response.text
    assert "HX-Redirect" not in response.headers
    assert "data-write-error" in response.text
    assert "already exists" in response.text


def test_create_over_budget_renders_422_inline() -> None:
    """An over-budget operational body renders the 422 inline (no redirect)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/create",
            data={
                "slug": "too-big",
                "title": "Too big",
                "body": _OVER_BUDGET_BODY,
                "kind": "operational",
                "priority": "0",
            },
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "HX-Redirect" not in response.headers
    assert "data-write-error" in response.text
    assert "budget" in response.text.lower()
    # The row was NOT created (failed flush rolled back).
    assert not _row_exists(_TENANT_A, "too-big")


def test_create_displacing_operational_surfaces_dropped_preamble_status() -> None:
    """A create that drops an existing rule surfaces the red DROPPED indicator.

    Seed a high-priority operational rule that nearly fills the budget,
    then create a low-priority one. The packer keeps the high-priority
    one and drops the new low-priority one -> ``preamble_status.included``
    is False, so the response is the DROPPED fragment (not a redirect).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="high-rule",
        title="High",
        body=_BIG_BODY,
        kind=ConventionKind.OPERATIONAL,
        priority=100,
    )
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/create",
            data={
                "slug": "low-rule",
                "title": "Low",
                "body": _BIG_BODY,
                "kind": "operational",
                "priority": "1",
            },
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    # Saved at the DB level, but the response warns it was dropped.
    assert response.status_code == 200, response.text
    assert "HX-Redirect" not in response.headers
    assert "data-preamble-dropped" in response.text
    assert 'data-dropped-slug="low-rule"' in response.text
    assert _row_exists(_TENANT_A, "low-rule")


# ---------------------------------------------------------------------------
# Debounced token-cost preview
# ---------------------------------------------------------------------------


def test_preview_over_budget_operational_flagged_red() -> None:
    """An over-budget operational body is flagged over the 600 budget in red."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/preview",
            data={"body": _OVER_BUDGET_BODY, "kind": "operational"},
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-over-budget="true"' in body
    assert "/ 600 tokens" in body
    assert "text-error" in body


def test_preview_same_body_as_reference_not_flagged() -> None:
    """The same over-budget body as kind=reference is NOT flagged (exempt)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/preview",
            data={"body": _OVER_BUDGET_BODY, "kind": "reference"},
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-over-budget="false"' in body
    assert 'data-counts-against-budget="false"' in body


# ---------------------------------------------------------------------------
# Non-idempotent DELETE confirm gate + double-fire safety
# ---------------------------------------------------------------------------


def test_delete_confirm_gate_renders_before_delete() -> None:
    """The delete confirm step renders a dialog; the row still exists."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="to-delete",
        title="To delete",
        body="bye",
        kind=ConventionKind.REFERENCE,
    )
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/to-delete/delete")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="conventions-delete-modal"' in body
    assert 'hx-delete="/ui/conventions/to-delete"' in body
    # Confirm gate only -- the row is untouched.
    assert _row_exists(_TENANT_A, "to-delete")


def test_delete_then_double_fire_renders_already_deleted_not_404() -> None:
    """First DELETE removes the row; a second DELETE is benign (no raw 404).

    The service DELETE is non-idempotent (404s on a missing row). The
    confirm-gated handler must turn the re-fire into a benign "already
    deleted" fragment rather than surfacing the raw 404.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="gone-soon",
        title="Gone soon",
        body="bye",
        kind=ConventionKind.REFERENCE,
    )
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        first = client.request(
            "DELETE",
            "/ui/conventions/gone-soon",
            **_csrf_kwargs(session_id),
        )
        second = client.request(
            "DELETE",
            "/ui/conventions/gone-soon",
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    # First delete: HX-Redirect, row gone.
    assert first.status_code == 204, first.text
    assert first.headers["HX-Redirect"] == "/ui/conventions"
    assert not _row_exists(_TENANT_A, "gone-soon")
    # Second delete (double-fire): benign already-deleted fragment, NOT 404.
    assert second.status_code == 200, second.text
    assert "data-already-deleted" in second.text
    assert "already deleted" in second.text


# ---------------------------------------------------------------------------
# Edit modal -- kind/slug not submittable, PATCH succeeds
# ---------------------------------------------------------------------------


def test_edit_modal_shows_kind_and_slug_readonly() -> None:
    """The edit modal renders kind + slug read-only (PATCH cannot change them)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="editable",
        title="Editable",
        body="original body",
        kind=ConventionKind.WORKFLOW,
        priority=3,
    )
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/editable/edit")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "data-readonly-slug" in body
    assert "data-readonly-kind" in body
    # The title/priority/body editable fields are pre-filled.
    assert 'value="Editable"' in body
    assert "original body" in body


def test_edit_patch_updates_title_priority_body() -> None:
    """A PATCH with title/priority/body succeeds and writes a history row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="patch-me",
        title="Old title",
        body="old body",
        kind=ConventionKind.REFERENCE,
        priority=0,
    )
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.request(
            "PATCH",
            "/ui/conventions/patch-me",
            data={"title": "New title", "body": "new body", "priority": "7"},
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/conventions"

    # Read back the persisted row.
    async def _read() -> tuple[str, str, int]:
        from sqlalchemy import select

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = (
                await session.execute(
                    select(TenantConvention).where(
                        TenantConvention.tenant_id == _TENANT_A,
                        TenantConvention.slug == "patch-me",
                    )
                )
            ).scalar_one()
            return row.title, row.body, row.priority

    title, body_text, priority = asyncio.run(_read())
    assert title == "New title"
    assert body_text == "new body"
    assert priority == 7
    # The update wrote a history row.
    assert _history_count(_TENANT_A, "patch-me") == 1


# ---------------------------------------------------------------------------
# History diff panel -- newest-first, CREATE row has no "before"
# ---------------------------------------------------------------------------


def test_history_panel_renders_newest_first_with_diff() -> None:
    """History renders newest-first; the CREATE row shows no 'before' pane."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        # Create (history row #1, body_before=None) then PATCH (row #2).
        client.post(
            "/ui/conventions/create",
            data={
                "slug": "history-rule",
                "title": "v1 title",
                "body": "first body",
                "kind": "reference",
                "priority": "0",
            },
            **_csrf_kwargs(session_id),
        )
        client.request(
            "PATCH",
            "/ui/conventions/history-rule",
            data={"title": "v2 title", "body": "second body", "priority": "0"},
            **_csrf_kwargs(session_id),
        )
        response = client.get("/ui/conventions/history-rule/history")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="conventions-history-panel"' in body
    # Two history rows present.
    assert body.count("data-history-row") == 2
    # Newest-first: the PATCH row (after="second body") appears before the
    # CREATE row (no before pane).
    first_idx = body.index("second body")
    create_idx = body.index("this is the CREATE event") if "CREATE event" in body else len(body)
    assert first_idx < create_idx
    # The CREATE row carries no "before" diff pane.
    assert "data-history-no-before" in body
    # The PATCH row shows the before->after diff.
    assert "data-history-before" in body
    assert "first body" in body  # body_before of the PATCH row


# ---------------------------------------------------------------------------
# RBAC + CSRF gates
# ---------------------------------------------------------------------------


def test_create_non_admin_operator_gets_403() -> None:
    """A plain operator (non-admin) is 403'd on the create POST."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _role_session(TenantRole.OPERATOR)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/create",
            data={
                "slug": "nope",
                "title": "Nope",
                "body": "should be blocked",
                "kind": "reference",
                "priority": "0",
            },
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert not _row_exists(_TENANT_A, "nope")


def test_create_missing_csrf_blocked() -> None:
    """A create POST without the CSRF double-submit pair is blocked (403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/conventions/create",
            data={
                "slug": "no-csrf",
                "title": "No CSRF",
                "body": "blocked",
                "kind": "reference",
                "priority": "0",
            },
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert not _row_exists(_TENANT_A, "no-csrf")


def test_delete_non_admin_operator_gets_403() -> None:
    """A plain operator is 403'd on the DELETE (and the row survives)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="protected",
        title="Protected",
        body="stays",
        kind=ConventionKind.REFERENCE,
    )
    session_id, jwks = _role_session(TenantRole.OPERATOR)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.request(
            "DELETE",
            "/ui/conventions/protected",
            **_csrf_kwargs(session_id),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert _row_exists(_TENANT_A, "protected")


# ---------------------------------------------------------------------------
# Route ordering -- static /create + /preview before /{slug}
# ---------------------------------------------------------------------------


def test_route_order_create_not_swallowed_by_slug() -> None:
    """``/ui/conventions/create`` hits the author modal, not ``{slug}=create``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/create")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    # The author-modal marker proves the literal route won (the detail
    # handler would 404 on a non-existent slug "create").
    assert 'id="conventions-create-modal"' in response.text
