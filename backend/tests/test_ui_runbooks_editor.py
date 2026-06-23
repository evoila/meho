# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the runbooks UI authoring editor.

Initiative #1381 (G10.6 Runbooks UI), Task #1383 (T2). Acceptance criteria
on issue #1383:

* ``GET /ui/runbooks/new`` and ``/ui/runbooks/<slug>/edit`` return 200 for a
  ``tenant_admin`` and 403 for an ``operator`` (server-enforced via
  ``require_ui_admin``).
* The form builds + submits a valid ``RunbookTemplateBody`` for both step
  kinds and both verify-gate kinds; a manual step omits ``op_id``/``params``,
  an operation_call step requires ``op_id``.
* Client + server validation block a bad slug / bad/duplicate step id / a
  disallowed ``${...}`` substitution; the server's 409 (duplicate) and 422
  (validation) render as inline errors without losing entered data; CSRF is
  re-minted on re-render.
* Creating a draft round-trips: ``POST`` 204 ``HX-Redirect`` -> the detail
  view shows the new draft (status ``draft``, version 1).
* Editing a published template forks a new draft and the UI surfaces
  ``forked_from`` + ``in_flight_run_count`` from the PATCH response.

Suite shape mirrors :mod:`backend.tests.test_ui_runbooks_list` (the T1 read
surface, #1382): a minimal FastAPI app wired with the BFF middlewares + the
UI surface router, SQLite-backed seeding via the real
:class:`~meho_backplane.runbooks.service.RunbookTemplateService`, a pre-set
session cookie, and a real RSA-signed JWT for the ``tenant_admin`` role lift
(the editor routes gate on ``require_ui_admin``, which re-verifies the
session's access token). The ``operator`` path seeds a plaintext token so the
admin gate's JWT decode fails -> 403.

The CSRF middleware is active, so every state-changing POST carries the
double-submit token via :func:`_csrf_kwargs` (the same token as both the
``X-CSRF-Token`` header and an explicit request cookie -- the editor's
``Secure`` CSRF cookie is not echoed by the plain-``http`` TestClient, so the
token is minted directly, the pattern :mod:`backend.tests.test_ui_kb_upload`
uses).
"""

from __future__ import annotations

import asyncio
import re
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
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import RunbookRun, Tenant
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DraftTemplateRequest,
    ManualStep,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import RunbookTemplateService
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
from tests.test_ui_broadcast_filters import (
    _XSS_OP_ID,
    _assert_no_xss_breakout,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_OPERATOR_SUB = "op-42"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (T1-suite baseline)."""
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
# Helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal FastAPI app wired for runbooks UI tests (T1-suite shape)."""
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
    """Insert one ``tenant`` row so FK constraints resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _manual_body() -> RunbookTemplateBody:
    """One manual step + confirm verify."""
    return RunbookTemplateBody(
        title="Drain node",
        description="Procedure for draining a node.",
        target_kind="k8s-node",
        steps=[
            ManualStep(
                id="drain",
                title="Drain the node",
                body="Run the **drain** command.",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Node drained?"),
            ),
        ],
    )


def _seed_template(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: RunbookTemplateBody | None = None,
    publish: bool = False,
) -> int:
    """Create a draft template (optionally publishing it) and return its version."""
    template_body = body if body is not None else _manual_body()

    async def _do() -> int:
        service = RunbookTemplateService()
        resp = await service.create_draft(
            tenant_id,
            _OPERATOR_SUB,
            DraftTemplateRequest(slug=slug, body=template_body),
        )
        if publish:
            await service.publish(
                tenant_id,
                PublishTemplateRequest(slug=slug, version=resp.version),
            )
        return resp.version

    return asyncio.run(_do())


def _seed_run(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    version: int,
    state: str = "in_progress",
) -> None:
    """Insert one ``runbook_runs`` row pinned to ``(slug, version)``."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(
                RunbookRun(
                    tenant_id=tenant_id,
                    template_slug=slug,
                    template_version=version,
                    assigned_to=_OPERATOR_SUB,
                    target="host-1",
                    params={},
                    state=state,
                    started_by=_OPERATOR_SUB,
                    started_at=datetime.now(UTC),
                )
            )
            await session.commit()

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str = "access-token-plaintext",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=_OPERATOR_SUB,
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


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-runbooks-editor-test-kid")
    return keypair, _public_jwks(keypair)


def _role_session(
    role: TenantRole,
    tenant_id: uuid.UUID = _TENANT_A,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real JWT carrying *role*.

    Returns the session id and the JWKS the ``require_ui_admin`` gate must
    reach to verify the token. A ``TENANT_ADMIN`` token passes the gate; an
    ``OPERATOR`` token decodes cleanly but fails the role rank check -> 403.
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


def _csrf_token(session_id: uuid.UUID) -> str:
    """Return a valid CSRF token for *session_id* (double-submit value).

    Minted directly rather than scraped from the render response: the editor
    sets the CSRF cookie with ``Secure``, and the TestClient speaks plain
    ``http://testserver``, so a ``Secure`` cookie is not echoed back on the
    follow-up POST. The double-submit contract is satisfied by passing the
    same token as both the ``X-CSRF-Token`` header and an explicit request
    cookie -- the pattern :mod:`backend.tests.test_ui_kb_upload` uses.
    """
    return mint_csrf_token(str(session_id))


def _csrf_kwargs(token: str) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check."""
    return {
        "headers": {CSRF_HEADER_NAME: token, "X-CSRF-Token": token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


# ---------------------------------------------------------------------------
# Admin gating — GET /ui/runbooks/new and /ui/runbooks/<slug>/edit
# ---------------------------------------------------------------------------


def test_editor_new_admin_renders_200() -> None:
    """``GET /ui/runbooks/new`` returns 200 + the editor for a tenant_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/new")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="runbook-editor-form"' in body
    assert 'name="slug"' in body
    # CodeMirror bundle wired (same as KB editor).
    assert "codemirror-bundle.min.js" in body
    # CSRF cookie set on render.
    assert CSRF_COOKIE_NAME in response.cookies


def test_editor_new_operator_forbidden_403() -> None:
    """``GET /ui/runbooks/new`` returns 403 for an operator (role below admin)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # A valid operator JWT decodes cleanly but ranks below tenant_admin -> 403.
    session_id, jwks = _role_session(TenantRole.OPERATOR)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/new")
    finally:
        mock.stop()

    assert response.status_code == 403, response.text


def test_editor_edit_admin_renders_prefilled_200() -> None:
    """``GET /ui/runbooks/<slug>/edit`` returns 200 prefilled for a tenant_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/drain-node/edit")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Prefilled top-level fields + the existing step id present in the
    # hydrated Alpine model JSON.
    assert "Drain node" in body
    assert "drain" in body
    assert 'id="runbook-editor-form"' in body


def test_editor_edit_operator_forbidden_403() -> None:
    """``GET /ui/runbooks/<slug>/edit`` returns 403 for an operator (role below admin)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    session_id, jwks = _role_session(TenantRole.OPERATOR)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/drain-node/edit")
    finally:
        mock.stop()

    assert response.status_code == 403, response.text


def test_editor_edit_missing_slug_404() -> None:
    """A tenant_admin editing a missing slug gets 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/does-not-exist/edit")
    finally:
        mock.stop()

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Draft create round-trip — POST /ui/runbooks/new
# ---------------------------------------------------------------------------


def _steps_json_manual_and_opcall() -> str:
    """A two-step payload: one manual+confirm, one operation_call+operation_call."""
    return (
        '[{"id":"prep","title":"Prepare","body":"Announce the window.",'
        '"type":"manual","op_id":"","params":"{}",'
        '"verify":{"type":"confirm","prompt":"Window announced?","op_id":"",'
        '"params":"{}","expect":"{}"}},'
        '{"id":"rotate","title":"Rotate","body":"Dispatch rotation.",'
        '"type":"operation_call","op_id":"vmware.cert.rotate",'
        '"params":"{\\"target\\": \\"${run.target}\\"}",'
        '"verify":{"type":"operation_call","prompt":"","op_id":"vmware.cert.status",'
        '"params":"{\\"target\\": \\"${run.target}\\"}","expect":"{\\"valid\\": true}"}}]'
    )


def test_create_draft_both_step_and_verify_kinds_roundtrips() -> None:
    """A valid two-step body (both step + verify kinds) creates a draft (204 + redirect)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/new",
            data={
                "slug": "rotate-cert",
                "title": "Rotate certificate",
                "description": "Rotate the serving cert.",
                "target_kind": "vmware.vcenter",
                "steps": _steps_json_manual_and_opcall(),
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/runbooks/rotate-cert"

    # The draft round-trips: it now resolves at version 1, status draft.
    async def _load() -> Any:
        return await RunbookTemplateService().show_template(_TENANT_A, "rotate-cert")

    tpl = asyncio.run(_load())
    assert tpl.version == 1
    assert tpl.status == "draft"
    assert [s.id for s in tpl.steps] == ["prep", "rotate"]
    # The operation_call step kept its op_id + params; the manual step has none.
    op_step = tpl.steps[1]
    assert op_step.type == "operation_call"
    assert op_step.op_id == "vmware.cert.rotate"


# ---------------------------------------------------------------------------
# Server-side validation — duplicate slug (409) and bad body (422)
# ---------------------------------------------------------------------------


def test_create_duplicate_slug_renders_inline_error_422() -> None:
    """A duplicate slug re-renders the editor inline (422) preserving entered data."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", body=_manual_body())
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/new",
            data={
                "slug": "rotate-cert",
                "title": "Rotate again",
                "description": "Another one.",
                "target_kind": "",
                "steps": _steps_json_manual_and_opcall(),
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    body = response.text
    # Inline error banner + the entered data preserved (title survives).
    assert "already has a version" in body
    assert "Rotate again" in body
    # CSRF re-minted on the error re-render (a fresh cookie is set).
    assert CSRF_COOKIE_NAME in response.cookies


def test_create_invalid_step_id_renders_inline_error_422() -> None:
    """A bad step id (dots are illegal) is rejected server-side -> inline 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    bad_steps = (
        '[{"id":"bad.id","title":"Prep","body":"x","type":"manual",'
        '"op_id":"","params":"{}",'
        '"verify":{"type":"confirm","prompt":"ok?","op_id":"","params":"{}","expect":"{}"}}]'
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/new",
            data={
                "slug": "bad-step",
                "title": "Bad step",
                "description": "Has a dotted step id.",
                "target_kind": "",
                "steps": bad_steps,
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    # Entered data preserved on the re-render.
    assert "Bad step" in response.text
    # No draft was persisted.

    async def _missing() -> bool:
        from meho_backplane.runbooks.service import TemplateNotFoundError

        try:
            await RunbookTemplateService().show_template(_TENANT_A, "bad-step")
        except TemplateNotFoundError:
            return True
        return False

    assert asyncio.run(_missing())


def test_create_disallowed_substitution_renders_inline_error_422() -> None:
    """A disallowed ``${...}`` substitution is rejected server-side -> inline 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    bad_steps = (
        '[{"id":"prep","title":"Prep","body":"Use ${run.secret} here.",'
        '"type":"manual","op_id":"","params":"{}",'
        '"verify":{"type":"confirm","prompt":"ok?","op_id":"","params":"{}","expect":"{}"}}]'
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/new",
            data={
                "slug": "subst-bad",
                "title": "Subst bad",
                "description": "Disallowed substitution.",
                "target_kind": "",
                "steps": bad_steps,
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    assert "disallowed substitution" in response.text


# ---------------------------------------------------------------------------
# Client-side validation parity — the editor markup carries the same rules
# ---------------------------------------------------------------------------


def test_editor_client_validation_mirrors_server_rules() -> None:
    """The rendered editor carries the slug / step-id / substitution rules client-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        body = client.get("/ui/runbooks/new").text
    finally:
        mock.stop()

    # Slug + step-id patterns + the substitution allowlist are all present in
    # the client script (validation parity with the server).
    assert "run.target" in body
    assert "run\\.params\\." in body
    assert "Duplicate step id." in body


# ---------------------------------------------------------------------------
# Fork-on-edit — editing a published template forks a new draft
# ---------------------------------------------------------------------------


def test_edit_published_forks_draft_and_surfaces_fork_notice() -> None:
    """Editing a published template forks a draft + surfaces forked_from + in-flight count."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(
        tenant_id=_TENANT_A,
        slug="rotate-cert",
        body=_manual_body(),
        publish=True,
    )
    # One in-flight run pins the published version (counts toward the fork notice).
    _seed_run(tenant_id=_TENANT_A, slug="rotate-cert", version=version, state="in_progress")
    session_id, jwks = _admin_session()

    edited_steps = (
        '[{"id":"drain","title":"Drain the node","body":"Run the drain command.",'
        '"type":"manual","op_id":"","params":"{}",'
        '"verify":{"type":"confirm","prompt":"Node drained?","op_id":"",'
        '"params":"{}","expect":"{}"}}]'
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/edit",
            data={
                "slug": "rotate-cert",
                "title": "Rotate certificate v2",
                "description": "Edited copy.",
                "target_kind": "k8s-node",
                "steps": edited_steps,
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    # Fork re-renders the editor (200) with the fork notice, not a redirect.
    assert response.status_code == 200, response.text
    body = response.text
    assert "Forked a new draft" in body
    # The new draft is v2; the source v1 had one in-flight run.
    assert "v2" in body
    assert re.search(r"rotate-cert v1", body)
    assert ">1<" in body or "1</strong>" in body

    # A new draft version now exists.
    async def _latest() -> int:
        tpl = await RunbookTemplateService().show_template(_TENANT_A, "rotate-cert")
        return tpl.version

    assert asyncio.run(_latest()) == version + 1


def test_edit_draft_in_place_redirects() -> None:
    """Editing an existing draft in place updates it + redirects (no fork notice)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    session_id, jwks = _admin_session()

    edited_steps = (
        '[{"id":"drain","title":"Drain the node (edited)","body":"x",'
        '"type":"manual","op_id":"","params":"{}",'
        '"verify":{"type":"confirm","prompt":"Done?","op_id":"","params":"{}","expect":"{}"}}]'
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/drain-node/edit",
            data={
                "slug": "drain-node",
                "title": "Drain node (edited)",
                "description": "Edited description.",
                "target_kind": "k8s-node",
                "steps": edited_steps,
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/runbooks/drain-node"

    async def _title() -> str:
        tpl = await RunbookTemplateService().show_template(_TENANT_A, "drain-node")
        return tpl.title

    assert asyncio.run(_title()) == "Drain node (edited)"


# ---------------------------------------------------------------------------
# Live preview — POST /ui/runbooks/preview
# ---------------------------------------------------------------------------


def test_editor_preview_renders_markdown_for_admin() -> None:
    """``POST /ui/runbooks/preview`` renders the posted body Markdown for an admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/preview",
            data={"body": "Run the **drain** command."},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert "<strong>drain</strong>" in response.text


def test_editor_preview_operator_forbidden() -> None:
    """``POST /ui/runbooks/preview`` is admin-gated (403 for an operator).

    A valid CSRF token is supplied so the CSRF middleware passes and the 403
    comes from ``require_ui_admin`` (the role rank check), not the CSRF gate.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _role_session(TenantRole.OPERATOR)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/preview",
            data={"body": "x"},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert "<strong>" not in response.text


# ---------------------------------------------------------------------------
# XSS hardening — attribute-breakout via runbook fields (Task #100)
# ---------------------------------------------------------------------------
#
# The editor binds operator-authored runbook fields into an Alpine
# ``x-data`` attribute. PR #1044 established the safe convention
# (single-quoted ``x-data`` + ``| tojson``) and the runbook editor was the
# lone data-bearing island that never received it: it interpolated
# ``initial_steps_json | safe`` and ``| tojson`` scalars inside a
# *double-quoted* attribute, so a ``"`` in any field terminated the
# attribute and grafted attacker markup onto the host element (stored XSS on
# the edit page, reflected XSS on the create / 422 re-render path).
#
# Both tests reuse the parser-grounded harness from
# :mod:`backend.tests.test_ui_broadcast_filters` (``_XSS_OP_ID`` +
# ``_assert_no_xss_breakout``): they feed the rendered HTML through a stdlib
# ``HTMLParser`` and assert no ``onfocus`` / ``autofocus`` / ``onerror``
# leaks as a parsed attribute and that the marker stays inside an ``x-data``
# value. They FAIL against the pre-fix double-quoted ``| safe`` template and
# PASS after the single-quote + ``| tojson`` fix.


def _xss_payload_body() -> RunbookTemplateBody:
    """A valid one-step body whose step + template titles carry the payload.

    ``_XSS_OP_ID`` contains every HTML metacharacter the hardening must
    neutralise but no ``${...}`` substitution, so it passes the template's
    only field-level validator (the substitution allowlist) and is stored
    verbatim -- exactly the stored-XSS precondition the fix must defang on
    re-render.
    """
    return RunbookTemplateBody(
        title=_XSS_OP_ID,
        description="Stored payload regression.",
        target_kind="k8s-node",
        steps=[
            ManualStep(
                id="drain",
                title=_XSS_OP_ID,
                body="Run the drain command.",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Node drained?"),
            ),
        ],
    )


def test_editor_edit_stored_xss_payload_cannot_break_out_of_x_data() -> None:
    """A stored runbook field with a breakout payload stays inert on the edit page.

    Regression for the stored-XSS finding (Task #100). The seeded template's
    ``title`` and step ``title`` carry ``_XSS_OP_ID``; ``GET .../edit``
    re-renders them into the Alpine ``x-data`` config. Fails on the pre-fix
    double-quoted ``| safe`` template (the ``"`` bytes terminate the
    attribute and the parser surfaces ``onfocus`` / ``autofocus`` /
    ``onerror`` as live attributes); passes once single-quoted + ``| tojson``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="xss-runbook", body=_xss_payload_body())
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/xss-runbook/edit")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    _assert_no_xss_breakout(response.text)


def test_editor_new_reflected_xss_payload_cannot_break_out_of_x_data() -> None:
    """A reflected breakout payload in ``title`` stays inert on the 422 re-render.

    Regression for the reflected-XSS finding (Task #100). The POST carries
    ``_XSS_OP_ID`` in ``title`` plus a structurally invalid step id, so the
    server rejects the body (422) and re-renders the editor with the entered
    ``title`` reflected into the Alpine ``x-data`` config. Same parser-
    grounded breakout check as the stored path.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id, jwks = _admin_session()

    # A dotted step id is illegal -> server 422 re-render, preserving title.
    bad_steps = (
        '[{"id":"bad.id","title":"Prep","body":"x","type":"manual",'
        '"op_id":"","params":"{}",'
        '"verify":{"type":"confirm","prompt":"ok?","op_id":"","params":"{}","expect":"{}"}}]'
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/new",
            data={
                "slug": "reflected-xss",
                "title": _XSS_OP_ID,
                "description": "Reflected payload regression.",
                "target_kind": "",
                "steps": bad_steps,
            },
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 422, response.text
    _assert_no_xss_breakout(response.text)
