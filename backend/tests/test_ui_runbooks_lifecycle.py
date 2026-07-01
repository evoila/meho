# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the runbooks UI lifecycle controls.

Initiative #1381 (G10.6 Runbooks UI), Task #1384 (T3). Acceptance criteria on
issue #1384:

* A ``tenant_admin`` can publish a ``draft`` from the UI (status -> published)
  and deprecate a ``published`` version (status -> deprecated); an ``operator``
  can neither see nor invoke the action -- a forged POST gets 403.
* Publishing a non-draft (400 ``TemplateNotDraftError``) and deprecating a
  non-published (400 ``TemplateNotPublishedError``) render as inline DaisyUI
  alerts; an idempotent re-action returns 200 and refreshes the badge with no
  error.
* Each action is gated by an Alpine confirm dialog naming slug + version; the
  CSRF token is required (missing token -> blocked by ``CSRFMiddleware``).
* The published-template "Edit (forks draft)" affordance surfaces the
  in-flight run count pinned to the version.

Suite shape mirrors :mod:`backend.tests.test_ui_runbooks_editor` (the T2
authoring surface, #1383): a minimal FastAPI app wired with the BFF
middlewares + the UI surface router, SQLite-backed seeding via the real
:class:`~meho_backplane.runbooks.service.RunbookTemplateService`, a pre-set
session cookie, and a real RSA-signed JWT for the ``tenant_admin`` role lift
(the lifecycle routes gate on ``require_ui_admin``, which re-verifies the
session's access token). The ``operator`` path mints an operator-role JWT that
decodes cleanly but fails the role rank check -> 403.

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
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import RunbookRun, Tenant
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DeprecateTemplateRequest,
    DraftTemplateRequest,
    ManualStep,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import RunbookTemplateService
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.errors import ui_session_expired_exception_handler
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.auth.refresh import SESSION_EXPIRED_DETAIL
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
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_OPERATOR_SUB = "op-42"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (T1/T2-suite baseline)."""
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
    """Minimal FastAPI app wired for runbooks UI tests (T1/T2-suite shape)."""
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
    publish: bool = False,
    deprecate: bool = False,
) -> int:
    """Create a draft (optionally publish + deprecate) and return its version."""

    async def _do() -> int:
        service = RunbookTemplateService()
        resp = await service.create_draft(
            tenant_id,
            _OPERATOR_SUB,
            DraftTemplateRequest(slug=slug, body=_manual_body()),
        )
        if publish or deprecate:
            await service.publish(
                tenant_id, PublishTemplateRequest(slug=slug, version=resp.version)
            )
        if deprecate:
            await service.deprecate(
                tenant_id, DeprecateTemplateRequest(slug=slug, version=resp.version)
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
        keypair = _make_rsa_keypair("ui-runbooks-lifecycle-test-kid")
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
    """Return a valid CSRF token for *session_id* (double-submit value)."""
    return mint_csrf_token(str(session_id))


def _csrf_kwargs(token: str) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check."""
    return {
        "headers": {CSRF_HEADER_NAME: token, "X-CSRF-Token": token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


def _status(tenant_id: uuid.UUID, slug: str, version: int) -> str:
    """Read the persisted status of ``(slug, version)`` for assertions."""

    async def _do() -> str:
        tpl = await RunbookTemplateService().show_template(tenant_id, slug, version=version)
        return tpl.status

    return asyncio.run(_do())


#: Pull the ``meho_csrf`` value out of a raw ``Set-Cookie`` header. The cookie
#: is ``Secure``, so the plain-``http`` TestClient does not auto-store it -- the
#: real browser cookie/header round-trip is reconstructed by reading the
#: Set-Cookie the server emitted and presenting it back verbatim (this is the
#: exact path B1 broke: the list fragment minted a fresh header token but never
#: refreshed this cookie).
_CSRF_SETCOOKIE_RE = re.compile(rf"{CSRF_COOKIE_NAME}=([^;]+)")

#: Pull the ``X-CSRF-Token`` the row-action button echoes via ``hx-headers`` out
#: of the rendered ``_list.html`` fragment. This is the *header* half of the
#: double-submit pair the catalog row POST will present.
_ROW_HX_HEADERS_RE = re.compile(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'')


def _extract_csrf_cookie(response: Any) -> str:
    """Return the ``meho_csrf`` value the response set, or fail the test.

    Reads the raw ``Set-Cookie`` header rather than the TestClient cookie jar so
    the ``Secure`` attribute (which the http TestClient honours by NOT storing
    the cookie) does not hide it -- the value is exactly what a browser would
    send back on the next request.
    """
    set_cookie = response.headers.get("set-cookie", "")
    match = _CSRF_SETCOOKIE_RE.search(set_cookie)
    assert match, f"no {CSRF_COOKIE_NAME} cookie set; got Set-Cookie={set_cookie!r}"
    return match.group(1)


def _extract_row_hx_token(body: str) -> str:
    """Return the ``X-CSRF-Token`` the row-action button echoes via hx-headers."""
    match = _ROW_HX_HEADERS_RE.search(body)
    assert match, "no row-action hx-headers X-CSRF-Token found in the list fragment"
    return match.group(1)


# ---------------------------------------------------------------------------
# Publish — admin 200, operator forged 403
# ---------------------------------------------------------------------------


def test_publish_draft_admin_flips_status() -> None:
    """A tenant_admin publishes a draft -> 200, status flips to published, badge swaps."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The OOB header-badge refresh + the new published badge are in the fragment.
    assert 'id="runbook-status-badge"' in body
    assert 'hx-swap-oob="true"' in body
    assert "published" in body
    # A published template offers Deprecate next, not Publish.
    assert "Deprecate" in body
    # Persisted state actually flipped.
    assert _status(_TENANT_A, "rotate-cert", version) == "published"


def test_publish_operator_forged_post_forbidden_403() -> None:
    """An operator's forged publish POST (valid CSRF) is 403 at require_ui_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _role_session(TenantRole.OPERATOR)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # The action did not fire — still a draft.
    assert _status(_TENANT_A, "rotate-cert", version) == "draft"


def test_publish_missing_csrf_blocked() -> None:
    """A publish POST with no CSRF token is blocked by CSRFMiddleware (not 200)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        # No CSRF header / cookie supplied.
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # The action did not fire — still a draft.
    assert _status(_TENANT_A, "rotate-cert", version) == "draft"


# ---------------------------------------------------------------------------
# Deprecate — admin 200
# ---------------------------------------------------------------------------


def test_deprecate_published_admin_flips_status() -> None:
    """A tenant_admin deprecates a published version -> 200, status flips."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", publish=True)
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/deprecate",
            data={"version": str(version)},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert "deprecated" in response.text
    assert _status(_TENANT_A, "rotate-cert", version) == "deprecated"


# ---------------------------------------------------------------------------
# Typed 400 — inline alert rendering
# ---------------------------------------------------------------------------


def test_publish_non_draft_renders_inline_alert() -> None:
    """Publishing a deprecated version (400 not-draft) renders an inline alert."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", deprecate=True)
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    # The handler maps the typed 400 to an inline DaisyUI alert (HTTP 200 carries
    # the re-rendered fragment so HTMX swaps the alert in).
    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body
    assert "not draft" in body
    # The status is unchanged (still deprecated).
    assert _status(_TENANT_A, "rotate-cert", version) == "deprecated"


def test_deprecate_non_published_renders_inline_alert() -> None:
    """Deprecating a draft (400 not-published) renders an inline alert."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/deprecate",
            data={"version": str(version)},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body
    assert "not published" in body
    assert _status(_TENANT_A, "rotate-cert", version) == "draft"


# ---------------------------------------------------------------------------
# Idempotent re-action — 200 no-op, badge refreshed, no error
# ---------------------------------------------------------------------------


def test_republish_already_published_is_idempotent_no_error() -> None:
    """Re-publishing an already-published version is a no-op (200, no alert)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", publish=True)
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Badge refreshed to published; no error alert.
    assert "published" in body
    assert "alert-error" not in body
    assert _status(_TENANT_A, "rotate-cert", version) == "published"


# ---------------------------------------------------------------------------
# 404 — missing slug / version
# ---------------------------------------------------------------------------


def test_publish_missing_version_404() -> None:
    """Publishing a (slug, version) that doesn't exist is 404 (stale detail)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": "99"},
            **_csrf_kwargs(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Detail view — controls visible for admin, hidden for operator
# ---------------------------------------------------------------------------


def test_detail_publish_control_visible_for_admin_draft() -> None:
    """A draft detail shows the Publish control to a tenant_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/rotate-cert")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "Publish" in body
    # The confirm dialog names the slug + version.
    assert "rotate-cert" in body
    assert 'hx-post="/ui/runbooks/rotate-cert/publish"' in body


def test_detail_lifecycle_controls_hidden_for_operator() -> None:
    """An operator who has completed a run sees the steps but no lifecycle controls."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", publish=True)
    # A completed run unlocks the operator's step view (opacity floor), so the
    # page renders fully -- the lifecycle controls must still be absent.
    _seed_run(tenant_id=_TENANT_A, slug="rotate-cert", version=version, state="completed")
    session_id, jwks = _role_session(TenantRole.OPERATOR)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/rotate-cert")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # No publish/deprecate POST controls for an operator.
    assert "/ui/runbooks/rotate-cert/deprecate" not in body
    assert 'hx-post="/ui/runbooks/rotate-cert/publish"' not in body


# ---------------------------------------------------------------------------
# Fork-on-edit affordance — surfaces in_flight_run_count on a published detail
# ---------------------------------------------------------------------------


def test_detail_published_surfaces_fork_affordance_with_in_flight_count() -> None:
    """A published detail shows the Edit (forks draft) affordance + in-flight count."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", publish=True)
    # Two in-flight runs pinned to the published version.
    _seed_run(tenant_id=_TENANT_A, slug="rotate-cert", version=version, state="in_progress")
    _seed_run(tenant_id=_TENANT_A, slug="rotate-cert", version=version, state="in_progress")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/rotate-cert")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "Edit (forks draft)" in body
    assert 'href="/ui/runbooks/rotate-cert/edit"' in body
    # The in-flight count is surfaced (2 runs pinned to this version).
    assert "2 in-flight runs" in body


# ---------------------------------------------------------------------------
# Catalog list — admin row actions present, operator's absent
# ---------------------------------------------------------------------------


def test_catalog_row_action_visible_for_admin() -> None:
    """The catalog shows a Publish row action on a draft row for a tenant_admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'hx-post="/ui/runbooks/rotate-cert/publish"' in body


def test_catalog_row_action_hidden_for_operator() -> None:
    """The catalog hides the lifecycle row actions from an operator."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _role_session(TenantRole.OPERATOR)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'hx-post="/ui/runbooks/rotate-cert/publish"' not in body


# ---------------------------------------------------------------------------
# B1 regression — catalog row action survives the REAL cookie/header CSRF path
# ---------------------------------------------------------------------------


def test_catalog_row_action_real_csrf_cookie_header_pair_200() -> None:
    """Row action POST through the REAL list-fragment cookie/header CSRF path -> 200.

    Regression for B1: ``_render_list_fragment`` re-mints the CSRF token into the
    row-action button's ``hx-headers`` but (before the fix) never refreshed the
    ``meho_csrf`` cookie, so the next row POST presented a header token that no
    longer matched the stale cookie and the CSRFMiddleware ``value_mismatch``
    check 403'd. This test does NOT hand-mint a matching token into both header
    and cookie (the way the other tests do, which is exactly why they missed B1).
    Instead it drives the genuine flow: GET the HTMX list fragment, capture the
    ``Set-Cookie`` ``meho_csrf`` value AND the token the row button echoes via
    ``hx-headers``, assert they are the same minted value, then POST the row
    action presenting THAT cookie + THAT header. A 200 proves the cookie/header
    pair is internally consistent; a 403 would be the B1 desync.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)

        # GET the HTMX list fragment (the path filter swaps + post-action
        # runbooks-refresh both render through here).
        list_resp = client.get("/ui/runbooks/list", headers={"HX-Request": "true"})
        assert list_resp.status_code == 200, list_resp.text

        cookie_token = _extract_csrf_cookie(list_resp)
        header_token = _extract_row_hx_token(list_resp.text)
        # The fix guarantees the cookie and the echoed header are the SAME minted
        # token; without it the cookie would be a stale (or absent) value.
        assert cookie_token == header_token, (
            "list-fragment CSRF cookie does not match the row button's "
            "hx-headers token -- the B1 desync"
        )

        # POST the row action presenting the captured pair verbatim (the real
        # browser round-trip), with no hand-minted override.
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            headers={
                CSRF_HEADER_NAME: header_token,
                "HX-Request": "true",
                "HX-Target": "runbook-row-alert-1",
            },
            cookies={CSRF_COOKIE_NAME: cookie_token},
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    # The action actually fired through the gate -- the draft is now published.
    assert _status(_TENANT_A, "rotate-cert", version) == "published"


# ---------------------------------------------------------------------------
# M1 regression — catalog typed-400 renders inline, not swallowed by a refresh
# ---------------------------------------------------------------------------


def test_catalog_row_action_typed_400_renders_inline_alert() -> None:
    """A typed-400 on the catalog row path returns an inline alert (M1).

    Regression for M1: the catalog row button used to dispatch ``runbooks-refresh``
    on every ``htmx:after-request`` regardless of outcome, so a typed-400
    (publishing a non-draft on a stale row) was swallowed under the list reload.
    The fix targets a per-row alert slot and returns the minimal
    ``_row_alert.html`` fragment carrying the alert when the POST originated from
    the catalog (``HX-Target`` points at ``runbook-row-alert-…``). This asserts
    the catalog-shaped response surfaces the alert inline (and does NOT leak the
    detail-surface action row / OOB badge into a list row).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # Deprecated version -> publishing it is a typed-400 (not draft).
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", deprecate=True)
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            headers={**_csrf_kwargs(csrf)["headers"], "HX-Target": "runbook-row-alert-1"},
            cookies=_csrf_kwargs(csrf)["cookies"],
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The typed-400 surfaces inline as a catalog alert.
    assert "alert-error" in body
    assert "not draft" in body
    # The catalog response is the minimal slot -- NOT the detail action row /
    # OOB header-badge (those would be nonsense swapped into a list row).
    assert 'id="runbook-status-badge"' not in body
    assert "Edit (forks draft)" not in body
    # Status is unchanged.
    assert _status(_TENANT_A, "rotate-cert", version) == "deprecated"


def test_catalog_row_action_success_returns_empty_slot() -> None:
    """A successful catalog row action returns an empty alert slot (no error)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        csrf = _csrf_token(session_id)
        response = client.post(
            "/ui/runbooks/rotate-cert/publish",
            data={"version": str(version)},
            headers={**_csrf_kwargs(csrf)["headers"], "HX-Target": "runbook-row-alert-1"},
            cookies=_csrf_kwargs(csrf)["cookies"],
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # No error alert on success; the button's success-only handler then fires
    # runbooks-refresh to reload the list with the new badge.
    assert "alert-error" not in body
    assert _status(_TENANT_A, "rotate-cert", version) == "published"


def test_catalog_row_button_gates_refresh_on_success() -> None:
    """The catalog row button dispatches runbooks-refresh only on success (M1).

    Asserts the rendered ``_list.html`` markup: the button targets the per-row
    alert slot and its ``htmx:after-request`` handler is gated on
    ``$event.detail.successful`` AND the response carrying no ``alert-error``
    (the typed-400 returns a 200 body with the alert, so ``successful`` alone is
    insufficient). This is the static counterpart to the behavioural typed-400
    test -- it pins the client-side wiring the reviewer required.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks", headers={"HX-Request": "false"})
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The row action targets the per-row alert slot (so a typed-400 lands inline).
    assert 'hx-target="#runbook-row-alert-' in body
    assert 'id="runbook-row-alert-' in body
    # The dispatch is gated: success AND no alert-error in the response body.
    assert "$event.detail.successful" in body
    assert "alert-error" in body  # appears in the gate expression
    assert "$dispatch('runbooks-refresh')" in body


def test_detail_draft_shows_edit_affordance() -> None:
    """A draft detail exposes an in-place Edit link, not just Publish (#2117 D2).

    Editing a draft mutates it in place (the engine's ``update_or_fork`` forks
    only a published version), so an author can fix a draft without publishing
    first. The Edit link routes to the shared ``/ui/runbooks/<slug>/edit`` editor.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert")  # publish defaults False -> draft
    session_id, jwks = _admin_session()

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/rotate-cert")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/ui/runbooks/rotate-cert/edit"' in body
    # The draft still shows Publish alongside Edit.
    assert "Publish" in body


# ---------------------------------------------------------------------------
# Role-lift failure on the detail read surface (#2114)
#
# The detail page recomputes the admin verdict per request by re-verifying the
# session's stored access token (``_resolve_role``). A *terminal* session
# expiry (token aged out + refresh failed) must NOT silently degrade a genuine
# ``tenant_admin`` onto the opacity-restricted banner -- it must propagate the
# ``session_expired`` 401 so the app-level handler redirects to re-auth. A
# transient / malformed-token failure still fails soft (restricted view).
# ---------------------------------------------------------------------------

_RB_ROUTES = "meho_backplane.ui.routes.runbooks.routes"


def _build_app_with_session_expired_handler() -> FastAPI:
    """`_build_app` plus the app-level ``session_expired`` handler main wires."""
    app = _build_app()
    app.add_exception_handler(StarletteHTTPException, ui_session_expired_exception_handler)
    return app


@pytest.mark.asyncio
async def test_resolve_role_reraises_session_expired() -> None:
    """A terminal ``session_expired`` propagates (not swallowed to ``None``)."""
    from meho_backplane.ui.routes.runbooks.routes import _resolve_role

    ctx = UISessionContext(session_id=uuid.uuid4(), operator_sub=_OPERATOR_SUB, tenant_id=_TENANT_A)
    expired = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=SESSION_EXPIRED_DETAIL)
    with (
        patch(f"{_RB_ROUTES}.load_fresh_session", AsyncMock(return_value=object())),
        patch(
            f"{_RB_ROUTES}.verify_access_token_with_refresh",
            AsyncMock(side_effect=expired),
        ),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _resolve_role(ctx)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == SESSION_EXPIRED_DETAIL


@pytest.mark.asyncio
async def test_resolve_role_soft_fails_on_non_session_expired_401() -> None:
    """A non-expiry verification failure still fails soft to ``None`` (operator)."""
    from meho_backplane.ui.routes.runbooks.routes import _resolve_role

    ctx = UISessionContext(session_id=uuid.uuid4(), operator_sub=_OPERATOR_SUB, tenant_id=_TENANT_A)
    bad_signature = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="signature_verification_failed"
    )
    with (
        patch(f"{_RB_ROUTES}.load_fresh_session", AsyncMock(return_value=object())),
        patch(
            f"{_RB_ROUTES}.verify_access_token_with_refresh",
            AsyncMock(side_effect=bad_signature),
        ),
    ):
        assert await _resolve_role(ctx) is None


def test_detail_session_expired_redirects_admin_to_reauth() -> None:
    """An admin whose session token expired is redirected to re-login, not the banner.

    Regression for #2114: the fail-soft role lift used to drop a genuine
    ``tenant_admin`` onto the opacity-restricted banner (``restricted=True``)
    when the token re-verify raised ``session_expired``. It must instead
    propagate to the app-level handler's 302 -> ``/ui/auth/login``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", publish=True)
    session_id, _jwks = _admin_session()

    client = TestClient(_build_app_with_session_expired_handler(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    expired = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=SESSION_EXPIRED_DETAIL)
    with (
        patch(f"{_RB_ROUTES}.load_fresh_session", AsyncMock(return_value=object())),
        patch(
            f"{_RB_ROUTES}.verify_access_token_with_refresh",
            AsyncMock(side_effect=expired),
        ),
    ):
        response = client.get("/ui/runbooks/rotate-cert", headers={"accept": "text/html"})

    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")
    assert "Step details are restricted" not in response.text
