# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Memory UI create modal + scope-promotion flow.

Initiative #341 (G10.4 Memory UI), Task #878 (G10.4-T2). The
acceptance criteria on issue #878 are:

* "+" opens the create modal; the scope selector shows only writable
  scopes (RBAC); submit POSTs ``/api/v1/memory``; modal closes + list
  updates; the debounced server-side Markdown preview works.
* Scope-promotion modal: operator promotes own user-scoped ->
  user x tenant; ``tenant_admin`` -> tenant; calls G5.2 promote; the
  promotion writes an audit row (assert).
* Promotion is idempotent (re-promote to the same scope is a no-op,
  per G5.2).
* CSRF enforced; cross-user/cross-tenant isolation holds.

The suite mirrors :mod:`backend.tests.test_ui_memory_list`'s fixture
shape: ``_build_app`` wires a minimal FastAPI app with the chassis
middlewares (incl. :class:`AuditMiddleware` for the audit-row
assertion) + the BFF auth router + the UI router; ``_seed_session_sync``
writes a ``web_session`` row carrying a real Keycloak-minted access
token; ``_authenticated_client`` returns a TestClient + a respx mock
+ the CSRF token. Helpers diverge from the T1 suite in two ways:

1. The audit-row assertion (only in the promote tests) queries the
   ``audit_log`` table directly after the request commits so the
   "promotion writes an audit row" AC is provable, not implied.
2. The embedding service is mocked for every create + promote test
   because :meth:`MemoryService.remember` (called by the create
   handler) and :meth:`MemoryService.promote` (called by the
   promote handler -- which inserts the target row via
   :func:`index_document`) both trigger an embedding pass on the
   new body.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import patch as _patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, Document, Tenant
from meho_backplane.memory._internal import (
    MEMORY_SOURCE,
    build_metadata,
    encode_source_id,
)
from meho_backplane.memory.schemas import MemoryScope, kind_for_scope
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
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
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
# Fixtures
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"

#: Two stable tenant ids for the cross-tenant isolation assertion.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

#: Two stable operator subs for the cross-user isolation assertion.
_OP_A = "op-alice"
_OP_B = "op-bob"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors :func:`backend.tests.test_ui_memory_list._bff_env`.
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
    """Construct a minimal FastAPI app wired for the create / promote tests.

    Mirrors :func:`backend.tests.test_ui_memory_list._build_app` with
    one addition: :class:`AuditMiddleware` is wired so the promote
    audit-row assertion can read back the row the chassis middleware
    commits. The other UI tests don't need it because their handlers
    don't bind ``operator_sub`` -- T1 routes are pre-G10.4 audit
    wiring -- but T2's promote handler explicitly binds contextvars
    so the chassis middleware writes the row.
    """
    app = FastAPI()
    # Order is outer-to-inner via add_middleware: the LAST add_middleware
    # call is the OUTERMOST. The chassis production stack puts
    # AuditMiddleware closest to the app (writes after handler runs)
    # and the CSRF + UISession middlewares outside that. We mirror
    # that ordering here.
    app.add_middleware(AuditMiddleware)
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
    """Insert one ``tenant`` row so the document tenant FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_memory(
    *,
    tenant_id: uuid.UUID,
    scope: MemoryScope,
    slug: str,
    body: str,
    user_sub: str | None = None,
    target_name: str | None = None,
    tags: list[str] | None = None,
) -> uuid.UUID:
    """Persist one memory row directly via the documents table.

    Bypasses :class:`MemoryService.remember` so the test doesn't need
    to mock the embedding service for the seeded body. Returns the
    seeded :class:`Document.id`.
    """
    metadata = build_metadata(
        caller_metadata={"tags": list(tags)} if tags else None,
        scope=scope,
        user_sub=user_sub or "",
        target_name=target_name,
        expires_at=None,
    )
    source_id = encode_source_id(
        scope=scope,
        user_sub=user_sub or "",
        target_name=target_name,
        slug=slug,
    )
    doc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Document(
                    id=doc_id,
                    tenant_id=tenant_id,
                    source=MEMORY_SOURCE,
                    source_id=source_id,
                    kind=kind_for_scope(scope),
                    body=body,
                    body_hash=f"sha256:test:{doc_id}",
                    embedding=[0.0] * 384,
                    doc_metadata=metadata,
                    tokens=len(body.split()),
                ),
            )

    asyncio.run(_do())
    return doc_id


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""
    from datetime import timedelta

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-memory-create-promote-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
    with_csrf: bool = False,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + a respx mock + a CSRF token."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    if with_csrf:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Return the headers a state-changing HTMX request carries."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


def _stub_embedding_service() -> Any:
    """Return a context manager that stubs the embedding service.

    :func:`index_document` calls :func:`get_embedding_service` and
    invokes ``.encode_one(body)`` to compute the new row's
    embedding. The create + promote tests don't depend on the
    embedding model wheel; stubbing the factory to return a fixed
    384-dim vector keeps the tests fast and deterministic.
    """
    return _patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=type("_Stub", (), {"encode_one": AsyncMock(return_value=[0.0] * 384)})(),
    )


def _count_documents(tenant_id: uuid.UUID, scope: MemoryScope) -> int:
    """Return how many ``documents`` rows exist for ``(tenant, scope)``."""

    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.source == MEMORY_SOURCE,
                    Document.kind == kind_for_scope(scope),
                )
            )
            return len(list(result.scalars().all()))

    return asyncio.run(_do())


def _audit_rows_for_op(operator_sub: str, op_id: str) -> list[dict[str, Any]]:
    """Return audit rows the chassis middleware committed for ``(operator_sub, op_id)``.

    Used by the promote tests to assert "the promotion writes an
    audit row" -- the chassis :class:`AuditMiddleware` writes one
    row per request that binds ``operator_sub`` to contextvars; the
    promote handler explicitly binds ``operator_sub`` + ``tenant_id``
    + ``audit_op_id`` so the row exists in the table after the
    request commits.

    The chassis :func:`_resolve_audit_payload` strips the ``audit_``
    prefix from every contextvar before writing the payload dict, so
    a contextvar bound as ``audit_op_id="memory.promote"`` lands on
    the row as ``payload["op_id"] == "memory.promote"``. This helper
    filters on that stripped shape.
    """

    async def _do() -> list[dict[str, Any]]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.operator_sub == operator_sub)
            )
            return [
                {
                    "id": row.id,
                    "operator_sub": row.operator_sub,
                    "tenant_id": row.tenant_id,
                    "method": row.method,
                    "path": row.path,
                    "status_code": row.status_code,
                    "payload": dict(row.payload) if row.payload else {},
                }
                for row in result.scalars().all()
                if row.payload and row.payload.get("op_id") == op_id
            ]

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# Create modal -- GET (renders RBAC-filtered scope selector)
# ---------------------------------------------------------------------------


def test_create_modal_renders_writable_scopes_only_for_operator() -> None:
    """Operator role: scope selector lists only the scopes ``can_write`` allows."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/create", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # The modal carries a <dialog> with the modal-open class.
    assert 'id="memory-create-modal"' in body
    # Operator role: USER + USER_TENANT + USER_TARGET + TARGET are
    # writable; TENANT requires tenant_admin and must NOT appear.
    assert 'value="user"' in body
    assert 'value="user-tenant"' in body
    assert 'value="user-target"' in body
    assert 'value="target"' in body
    assert 'value="tenant"' not in body
    # Form posts to the right route + carries the markdown-preview
    # debounce wiring.
    assert 'hx-post="/ui/memory/create"' in body
    assert 'hx-post="/ui/memory/preview"' in body
    assert "delay:300ms" in body


def test_create_modal_renders_tenant_for_tenant_admin() -> None:
    """``tenant_admin`` sees the ``TENANT`` scope in the selector."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/create", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'value="tenant"' in body


def test_create_modal_renders_empty_state_for_read_only_role() -> None:
    """Read-only operator sees the empty-state alert, not the form."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.READ_ONLY.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/create", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    # No form element rendered; the empty-state alert is.
    assert 'id="memory-create-form"' not in body
    assert "does not allow writing any memory scope" in body


# ---------------------------------------------------------------------------
# Create submit -- POST persists + HX-Redirect
# ---------------------------------------------------------------------------


def test_create_submit_persists_user_scoped_memory_and_redirects() -> None:
    """POST /ui/memory/create persists the row + returns HX-Redirect."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/create",
                data={
                    "scope": "user",
                    "body": "# new memory\n\nbody text",
                    "slug": "wine-pref",
                    "tags": "wine, preference",
                },
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/memory"
    # The row landed in the documents table.
    assert _count_documents(_TENANT_A, MemoryScope.USER) == 1


def test_create_submit_with_blank_slug_generates_one() -> None:
    """Blank slug -> auto-generated; the redirect still fires."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/create",
                data={"scope": "user", "body": "auto-slugged", "slug": ""},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert _count_documents(_TENANT_A, MemoryScope.USER) == 1


def test_create_submit_tenant_scope_as_operator_returns_403() -> None:
    """An operator (non-admin) cannot create a tenant-scoped memory."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/create",
                data={"scope": "tenant", "body": "shared rule"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_create_submit_empty_body_returns_422() -> None:
    """Empty body fails the 'must not be empty' guard."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/create",
            data={"scope": "user", "body": "   "},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text


def test_create_submit_target_scoped_without_target_name_returns_422() -> None:
    """target_name required for user-target / target scopes."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/create",
                data={"scope": "user-target", "body": "needs target"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text


def test_create_submit_missing_csrf_token_returns_403() -> None:
    """A POST without the CSRF token is rejected by the chassis middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    # NOTE: with_csrf=False -- no cookie + no header.
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/memory/create",
            data={"scope": "user", "body": "x"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Markdown preview -- debounced server-side render
# ---------------------------------------------------------------------------


def test_preview_renders_markdown_to_html() -> None:
    """POST /ui/memory/preview renders Markdown body -> HTML."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/preview",
            data={"body": "# Heading\n\n**bold**"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    text = response.text
    assert "<h1>Heading</h1>" in text
    assert "<strong>bold</strong>" in text
    assert 'id="memory-create-preview"' in text


def test_preview_empty_body_renders_placeholder() -> None:
    """Empty body returns the placeholder pane (no error)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/preview",
            data={"body": ""},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "Preview will render here" in response.text


def test_preview_escapes_raw_html_in_body() -> None:
    """A raw <script> in the previewed body renders as escaped text."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/memory/preview",
            data={"body": '<script>alert("x")</script>'},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200
    # The body is escaped; no raw script tag survives the renderer.
    assert "&lt;script&gt;" in response.text


# ---------------------------------------------------------------------------
# Promote modal -- GET (renders legal target scopes)
# ---------------------------------------------------------------------------


def test_promote_modal_for_user_scope_lists_user_tenant_and_user_target() -> None:
    """Source scope 'user' -> two legal targets (user-tenant + user-target)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="r",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/wine-pref/promote", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="memory-promote-modal"' in body
    assert 'value="user-tenant"' in body
    assert 'value="user-target"' in body
    # Terminal scopes (tenant + target) must NOT appear as a target
    # for a USER source -- the ladder forbids the cross-ladder leap.
    assert 'value="tenant"' not in body
    assert 'value="target"' not in body


def test_promote_modal_for_tenant_scope_returns_400() -> None:
    """Terminal scope 'tenant' -> 400 (no legal target)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="team-rule",
        body="shared",
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/tenant/team-rule/promote")
    finally:
        mock.stop()
    assert response.status_code == 400, response.text


def test_promote_modal_cross_user_user_scoped_returns_404() -> None:
    """Operator B cannot open a promote modal for A's user-scoped memory."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="alice-secret",
        body="for-a",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_B,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_B
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/alice-secret/promote")
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Promote submit -- POST persists target + redirects + writes audit row
# ---------------------------------------------------------------------------


def test_promote_submit_user_to_user_tenant_persists_target_and_redirects() -> None:
    """An operator promotes own user-scoped to user-tenant; target row lands."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="riesling",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user/wine-pref/promote",
                data={"to": "user-tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/memory/user-tenant/wine-pref"
    # The source row remains (move=False); the target row landed.
    assert _count_documents(_TENANT_A, MemoryScope.USER) == 1
    assert _count_documents(_TENANT_A, MemoryScope.USER_TENANT) == 1


def test_promote_submit_writes_audit_row() -> None:
    """The promotion writes an audit row classified as ``memory.promote``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="riesling",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user/wine-pref/promote",
                data={"to": "user-tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 204
    rows = _audit_rows_for_op(_OP_A, "memory.promote")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["method"] == "POST"
    assert row["path"] == "/ui/memory/user/wine-pref/promote"
    assert row["status_code"] == 204
    assert row["tenant_id"] == _TENANT_A
    payload = row["payload"]
    # The chassis `_resolve_audit_payload` strips the ``audit_`` prefix
    # from every contextvar before writing the payload dict.
    assert payload["op_class"] == "write"
    assert payload["scope"] == "user"
    assert payload["slug"] == "wine-pref"
    assert payload["promotion_target_scope"] == "user-tenant"


def test_promote_submit_idempotent_re_promote_returns_same_redirect() -> None:
    """Re-running the same promotion is a no-op at the substrate level."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="riesling",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            first = client.post(
                "/ui/memory/user/wine-pref/promote",
                data={"to": "user-tenant"},
                headers=_csrf_headers(csrf),
            )
            second = client.post(
                "/ui/memory/user/wine-pref/promote",
                data={"to": "user-tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert first.status_code == 204
    assert second.status_code == 204
    assert first.headers["HX-Redirect"] == second.headers["HX-Redirect"]
    # Idempotency: still ONE target row, not two.
    assert _count_documents(_TENANT_A, MemoryScope.USER_TENANT) == 1


def test_promote_submit_user_to_tenant_as_operator_returns_403() -> None:
    """An operator cannot promote to TENANT; G5.2 raises 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER_TENANT,
        slug="team-pref",
        body="ours",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user-tenant/team-pref/promote",
                data={"to": "tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_promote_submit_user_tenant_to_tenant_as_admin_succeeds() -> None:
    """``tenant_admin`` can promote user-tenant -> tenant."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER_TENANT,
        slug="team-pref",
        body="ours",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user-tenant/team-pref/promote",
                data={"to": "tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/memory/tenant/team-pref"
    assert _count_documents(_TENANT_A, MemoryScope.TENANT) == 1


def test_promote_submit_invalid_ladder_step_returns_400() -> None:
    """user -> tenant directly is not a ladder step -> 400."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="r",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user/wine-pref/promote",
                data={"to": "tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 400, response.text


def test_promote_submit_cross_user_returns_404() -> None:
    """Operator B cannot promote A's user-scoped memory (404)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="alice-secret",
        body="for-a",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_B,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_B
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user/alice-secret/promote",
                data={"to": "user-tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


def test_promote_submit_cross_tenant_returns_404() -> None:
    """Operator in tenant B cannot promote tenant A's memory (404)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="a-mem",
        body="for-a",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,  # same sub, different tenant
        tenant_id=str(_TENANT_B),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_B, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        with _stub_embedding_service():
            response = client.post(
                "/ui/memory/user/a-mem/promote",
                data={"to": "user-tenant"},
                headers=_csrf_headers(csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# UI integration -- list page + detail page render the new buttons
# ---------------------------------------------------------------------------


def test_list_page_renders_create_button() -> None:
    """The list page now renders the '+' Create button + modal container."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/ui/memory/create"' in body
    assert 'id="memory-modal-container"' in body


def test_detail_page_renders_promote_button_for_user_scope() -> None:
    """Detail page on a user-scoped row renders the Promote button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="riesling",
        user_sub=_OP_A,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/wine-pref")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/ui/memory/user/wine-pref/promote"' in body


def test_detail_page_omits_promote_button_for_terminal_tenant_scope() -> None:
    """Detail page on a tenant-scoped row does NOT render the Promote button."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="rule",
        body="shared",
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/tenant/rule")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "tenant/rule/promote" not in body
