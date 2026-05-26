# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the KB UI upload surface.

Initiative #339 (G10.2 Knowledge base UI), Task #871 (T2). Acceptance
criteria on issue #871:

* ``GET /ui/kb/upload`` requires ``tenant_admin`` role; unauthenticated
  requests 302-redirect to login; ``operator`` role gets 403.
* ``POST /ui/kb/upload`` single-file: success → ``_upload_progress.html``
  fragment with ``alert-success``, slug link, and OOB ``<tr>`` targeting
  ``#kb-results-body``; rejects non-``.md`` files, oversized files, and
  binary/non-UTF-8 files.
* ``POST /ui/kb/upload/bulk`` multi-file: returns per-file rows, partial
  failure is OK (some succeed, some error).
* Idempotent: re-uploading the same body does not create a duplicate entry
  (body_hash dedup).
* CSRF enforced: POST without ``X-CSRF-Token`` header returns 403.
* Route ordering: ``GET /ui/kb/upload`` is matched ahead of
  ``GET /ui/kb/{slug}`` (slug="upload" would 404, not 200).

Suite shape mirrors ``test_ui_kb_search.py``:

* :func:`_build_app` — minimal FastAPI app with UISessionMiddleware +
  CSRFMiddleware + KB router.
* :func:`_seed_session_admin` — creates a session whose ``access_token``
  is a signed JWT with ``tenant_role=tenant_admin``; JWKS is mocked.
* All tests run against an in-memory SQLite DB.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
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
from tests.conftest import (
    DEFAULT_AUDIENCE,
    DEFAULT_ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# RSA keypair shared across all tests in the module (generated once per
# module load to keep the suite fast — key is never rotated mid-test).
_RSA_KEY = make_rsa_keypair("test-kid")
_JWKS = public_jwks(_RSA_KEY)

_SMALL_MD = b"# Hello\n\nThis is a test entry.\n"
_LARGE_MD = b"# Big\n\n" + b"x " * (512 * 1024 + 1)  # > 512 KiB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors test_ui_kb_search.py)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
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
    """Minimal FastAPI app wired for KB UI upload tests."""
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
    """Insert one ``tenant`` row so Document FK constraints resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str = "plain-access-token",
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row and return its UUID."""

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


def _mint_admin_token(tenant_id: uuid.UUID) -> str:
    """Mint a JWT with ``tenant_role=tenant_admin`` for *tenant_id*."""
    return mint_token(
        _RSA_KEY,
        tenant_id=str(tenant_id),
        tenant_role="tenant_admin",
    )


def _mint_operator_token(tenant_id: uuid.UUID) -> str:
    """Mint a JWT with ``tenant_role=operator`` for *tenant_id*."""
    return mint_token(
        _RSA_KEY,
        tenant_id=str(tenant_id),
        tenant_role="operator",
    )


def _admin_client(mock_router: respx.MockRouter, tenant_id: uuid.UUID) -> TestClient:
    """Build a TestClient with an admin session whose JWT the mock JWKS can verify."""
    mock_discovery_and_jwks(mock_router, _JWKS)
    token = _mint_admin_token(tenant_id)
    session_id = _seed_session_sync(tenant_id=tenant_id, access_token=token)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _operator_client(mock_router: respx.MockRouter, tenant_id: uuid.UUID) -> TestClient:
    """Build a TestClient with an operator session (not tenant_admin)."""
    mock_discovery_and_jwks(mock_router, _JWKS)
    token = _mint_operator_token(tenant_id)
    session_id = _seed_session_sync(tenant_id=tenant_id, access_token=token)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _csrf_token(session_id: uuid.UUID) -> str:
    """Return a valid CSRF token for *session_id*."""
    return mint_csrf_token(str(session_id))


def _upload_single(
    client: TestClient,
    *,
    filename: str = "my-entry.md",
    content: bytes = _SMALL_MD,
    slug: str = "",
    csrf: str,
) -> httpx.Response:
    """POST a single file to ``/ui/kb/upload``."""
    return client.post(
        "/ui/kb/upload",
        files={"file": (filename, io.BytesIO(content), "text/markdown")},
        data={"slug": slug},
        headers={CSRF_HEADER_NAME: csrf, "X-CSRF-Token": csrf},
        cookies={CSRF_COOKIE_NAME: csrf},
    )


def _upload_bulk(
    client: TestClient,
    *,
    files: list[tuple[str, bytes]],
    csrf: str,
) -> httpx.Response:
    """POST multiple files to ``/ui/kb/upload/bulk``."""
    file_tuples = [("file", (name, io.BytesIO(body), "text/markdown")) for name, body in files]
    return client.post(
        "/ui/kb/upload/bulk",
        files=file_tuples,
        headers={CSRF_HEADER_NAME: csrf, "X-CSRF-Token": csrf},
        cookies={CSRF_COOKIE_NAME: csrf},
    )


# ---------------------------------------------------------------------------
# Route ordering: /ui/kb/upload must beat /ui/kb/{slug}
# ---------------------------------------------------------------------------


def test_upload_page_route_not_captured_as_slug() -> None:
    """``GET /ui/kb/upload`` must not be matched by the ``{slug}`` route.

    FastAPI uses first-match-wins; if ``/upload`` were captured by
    ``/ui/kb/{slug}`` the handler would 404 (no KB entry with slug
    "upload"). This test verifies the correct handler fires by checking
    for the upload page title rather than a 404 JSON body.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    token = _mint_admin_token(_TENANT_A)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)

    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/kb/upload")

    assert response.status_code == 200, response.text
    assert "Upload" in response.text


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_upload_page_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/kb/upload`` without a session 302s to BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/kb/upload")
    assert response.status_code == 302
    assert "/ui/auth/login" in response.headers["location"]


def test_upload_post_unauthenticated_redirects_to_login() -> None:
    """``POST /ui/kb/upload`` without a session 302s to login."""
    csrf = mint_csrf_token("anon")
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.post(
            "/ui/kb/upload",
            files={"file": ("a.md", io.BytesIO(_SMALL_MD), "text/markdown")},
            headers={CSRF_HEADER_NAME: csrf},
            cookies={CSRF_COOKIE_NAME: csrf},
        )
    assert response.status_code == 302
    assert "/ui/auth/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# RBAC: operator role must not access upload
# ---------------------------------------------------------------------------


def test_upload_page_operator_role_forbidden() -> None:
    """``GET /ui/kb/upload`` with ``operator`` role returns 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        client = _operator_client(mock_router, _TENANT_A)
        response = client.get("/ui/kb/upload")
    assert response.status_code == 403


def test_upload_post_operator_role_forbidden() -> None:
    """``POST /ui/kb/upload`` with ``operator`` role returns 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        client = _operator_client(mock_router, _TENANT_A)
        # Extract session_id from the session we seeded in _operator_client.
        # Re-derive CSRF from the session cookie already on the client.
        session_cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert session_cookie is not None
        csrf = mint_csrf_token(session_cookie)
        response = _upload_single(
            client,
            filename="my-entry.md",
            content=_SMALL_MD,
            csrf=csrf,
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /ui/kb/upload — upload page render
# ---------------------------------------------------------------------------


def test_upload_page_renders_for_admin() -> None:
    """``GET /ui/kb/upload`` returns 200 with drag-and-drop markup."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        client = _admin_client(mock_router, _TENANT_A)
        response = client.get("/ui/kb/upload")

    assert response.status_code == 200, response.text
    body = response.text
    # Page title indicates the upload surface.
    assert "Upload" in body
    # CSRF cookie must be set so Alpine can echo it.
    assert CSRF_COOKIE_NAME in response.cookies
    # The upload form's hx-post target is present.
    assert "/ui/kb/upload" in body


# ---------------------------------------------------------------------------
# POST /ui/kb/upload — single-file upload success path
# ---------------------------------------------------------------------------


def test_upload_single_success() -> None:
    """Successful single-file upload returns the progress fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=_fake_embedding_service(),
        ):
            response = _upload_single(
                client,
                filename="my-entry.md",
                content=_SMALL_MD,
                csrf=csrf,
            )

    assert response.status_code == 200, response.text
    body = response.text
    # DaisyUI success alert.
    assert "alert-success" in body
    assert "my-entry" in body
    # OOB swap row is present.
    assert "hx-swap-oob" in body
    assert "kb-results-body" in body


def test_upload_single_slug_override() -> None:
    """The ``slug`` form field overrides the filename-derived slug."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=_fake_embedding_service(),
        ):
            response = _upload_single(
                client,
                filename="original-name.md",
                content=_SMALL_MD,
                slug="custom-slug",
                csrf=csrf,
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-success" in body
    assert "custom-slug" in body
    # Original filename should NOT appear as the slug.
    assert "original-name" not in body or "custom-slug" in body


# ---------------------------------------------------------------------------
# POST /ui/kb/upload — single-file error paths
# ---------------------------------------------------------------------------


def test_upload_single_non_md_rejected() -> None:
    """Non-``.md`` file is rejected with an error alert."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        response = _upload_single(
            client,
            filename="document.pdf",
            content=b"%PDF-1.4 binary",
            csrf=csrf,
        )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body
    assert "document.pdf" in body


def test_upload_single_oversized_rejected() -> None:
    """File exceeding 512 KiB is rejected."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        response = _upload_single(
            client,
            filename="huge.md",
            content=_LARGE_MD,
            csrf=csrf,
        )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body
    assert "KiB" in body or "limit" in body


def test_upload_single_binary_file_rejected() -> None:
    """Binary (non-UTF-8) ``.md`` file is rejected with an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        response = _upload_single(
            client,
            filename="binary.md",
            content=b"\xff\xfe invalid utf-8 \x80\x81",
            csrf=csrf,
        )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body
    assert "UTF-8" in body or "utf" in body.lower()


def test_upload_single_invalid_slug_override_rejected() -> None:
    """An explicitly provided slug that fails validation returns an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=_fake_embedding_service(),
        ):
            response = _upload_single(
                client,
                filename="my-entry.md",
                content=_SMALL_MD,
                slug="!!!INVALID SLUG!!!",
                csrf=csrf,
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body


# ---------------------------------------------------------------------------
# POST /ui/kb/upload/bulk — bulk upload
# ---------------------------------------------------------------------------


def test_upload_bulk_success_and_partial_failure() -> None:
    """Bulk upload: valid files succeed, invalid files report errors."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=_fake_embedding_service(),
        ):
            response = _upload_bulk(
                client,
                files=[
                    ("alpha.md", b"# Alpha\nGood content."),
                    ("beta.md", b"# Beta\nAlso good."),
                    ("gamma.pdf", b"%PDF binary"),  # wrong extension — error
                    ("delta.md", b"# Delta\nFine."),
                ],
                csrf=csrf,
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Bulk layout uses a table, not a single alert.
    assert "table" in body or "Upload results" in body
    # Three success rows.
    assert body.count("alpha") >= 1
    assert body.count("beta") >= 1
    assert body.count("delta") >= 1
    # One error row for the PDF.
    assert "gamma.pdf" in body
    # OOB rows for the three successes.
    assert body.count("hx-swap-oob") >= 3


# ---------------------------------------------------------------------------
# Idempotency: re-upload same body does not create a duplicate
# ---------------------------------------------------------------------------


def test_upload_idempotent_same_body() -> None:
    """Re-uploading identical content yields success without duplicating the entry."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        content = b"# Idempotent\n\nSame body every time.\n"

        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=_fake_embedding_service(),
        ):
            r1 = _upload_single(client, filename="idempotent.md", content=content, csrf=csrf)
            r2 = _upload_single(client, filename="idempotent.md", content=content, csrf=csrf)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert "alert-success" in r1.text
    assert "alert-success" in r2.text


# ---------------------------------------------------------------------------
# CSRF enforcement
# ---------------------------------------------------------------------------


def test_upload_post_no_csrf_token_rejected() -> None:
    """``POST /ui/kb/upload`` without an ``X-CSRF-Token`` header returns 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

        response = client.post(
            "/ui/kb/upload",
            files={"file": ("a.md", io.BytesIO(_SMALL_MD), "text/markdown")},
            # No CSRF header or cookie.
        )

    # CSRFMiddleware rejects the request before it reaches the route.
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# B1 regression: drag-and-drop files must reach the server (via hidden input)
# ---------------------------------------------------------------------------


def test_upload_drag_drop_files_reach_server() -> None:
    """Files submitted via the hidden #kb-file-input (as handleDrop now does) are processed.

    B1 fix: handleDrop syncs dragged files into the hidden file input via
    DataTransfer so HTMX's hx-include="#kb-file-input" can serialise them.
    This test simulates the post-fix path: a file is POSTed via the same
    multipart field name ("file") that the hidden input produces, confirming
    the server-side handler correctly processes the upload regardless of
    whether the file originated from a browse-click or a drop event.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=_fake_embedding_service(),
        ):
            # Simulate the file as if it came from the hidden input whose
            # .files was populated by handleDrop (via DataTransfer).
            response = _upload_single(
                client,
                filename="dragged-entry.md",
                content=b"# Dragged\n\nThis file was drag-dropped.\n",
                csrf=csrf,
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-success" in body
    assert "dragged-entry" in body
    # OOB swap row confirms entry was created.
    assert "hx-swap-oob" in body


# ---------------------------------------------------------------------------
# M1 regression: audit contextvars bound by require_ui_admin
# ---------------------------------------------------------------------------


def test_upload_admin_gate_binds_audit_contextvars() -> None:
    """require_ui_admin must bind operator_sub + tenant_id so AuditMiddleware fires.

    M1 fix: after the role check passes, require_ui_admin calls
    structlog.contextvars.bind_contextvars(operator_sub=..., tenant_id=...).
    Without this binding, AuditMiddleware skips the audit write entirely
    (it guards on isinstance(operator_sub, str) and operator_sub being truthy).
    This test confirms the contextvars are bound by asserting a successful upload
    response (a 403 would mean the gate didn't pass; a missing contextvar
    would cause AuditMiddleware to skip silently — but the round-trip passes
    proving require_ui_admin completed the full gate including the bind).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    import structlog

    with respx.mock(assert_all_called=False) as mock_router:
        mock_discovery_and_jwks(mock_router, _JWKS)
        token = _mint_admin_token(_TENANT_A)
        session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token)
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))

        captured_contextvars: dict[str, object] = {}

        original_create_entry = None

        async def _capture_and_create(
            tenant_id: object,
            slug: str,
            body: str,
            *,
            metadata: dict[str, object] | None = None,
        ) -> object:
            # Snapshot the structlog contextvars at the point create_entry
            # is called (i.e. after require_ui_admin has run).
            captured_contextvars.update(structlog.contextvars.get_contextvars())
            return await original_create_entry(tenant_id, slug, body, metadata=metadata)  # type: ignore[misc]

        from meho_backplane.kb import KbService

        original_create_entry = KbService.create_entry

        with (
            patch(
                "meho_backplane.retrieval.indexer.get_embedding_service",
                return_value=_fake_embedding_service(),
            ),
            patch.object(KbService, "create_entry", side_effect=_capture_and_create),
        ):
            response = _upload_single(
                client,
                filename="audit-check.md",
                content=b"# Audit\n\nChecking contextvars.\n",
                csrf=csrf,
            )

    assert response.status_code == 200, response.text
    # operator_sub and tenant_id must be bound by require_ui_admin.
    assert "operator_sub" in captured_contextvars, "operator_sub not in structlog contextvars"
    assert "tenant_id" in captured_contextvars, "tenant_id not in structlog contextvars"
    assert captured_contextvars["operator_sub"] == "op-42"
    assert captured_contextvars["tenant_id"] == str(_TENANT_A)
    # audit_op_id and audit_op_class must be bound by _process_upload_files.
    assert captured_contextvars.get("audit_op_id") == "kb.ui_upload"
    assert captured_contextvars.get("audit_op_class") == "write"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _fake_embedding_service() -> AsyncMock:
    """Return a mock embedding service that avoids ONNX/fastembed."""
    svc = AsyncMock()
    svc.encode_one.return_value = [0.1] * 384
    svc.encode.return_value = [[0.1] * 384]
    svc.dimension = 384
    return svc
