# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the runbooks UI read surface.

Initiative #1381 (G10.6 Runbooks UI), Task #1382 (T1). Acceptance
criteria on issue #1382:

* ``GET /ui/runbooks`` returns 200 for an authenticated operator, lists
  the tenant's templates with status badges; cross-tenant templates never
  appear.
* ``status`` + ``target_kind`` filters work via the HTMX partial
  (``HX-Request`` -> ``_list.html`` fragment; direct nav -> full page).
* ``GET /ui/runbooks/<slug>`` renders steps (both ``manual`` and
  ``operation_call``) and both verify-gate kinds; step bodies are
  server-rendered Markdown.
* Opacity-floor: an operator with no completed run on ``(slug, version)``
  gets the graceful restricted-detail state; a ``tenant_admin`` (or
  post-completion operator) sees full steps.
* Unauthenticated request to either route redirects per the chassis
  convention; runbooks tile + sidebar link render on the dashboard.

Suite shape mirrors :mod:`backend.tests.test_ui_kb_search` (the KB read
surface precedent #870): a minimal FastAPI app wired with the BFF
middlewares + the UI surface router, SQLite-backed seeding via the real
:class:`~meho_backplane.runbooks.service.RunbookTemplateService`, and a
pre-set session cookie. The opacity-floor admin path mints a real JWT via
``tests._oidc_jwt_helpers`` so the role lift resolves ``tenant_admin``;
the operator path relies on the soft-fail role lift (the seeded
plaintext access token is not a valid JWT -> the lift returns "no admin"
-> the post-completion predicate gates the steps).
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
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import RunbookRun, Tenant
from meho_backplane.runbooks.schemas import (
    ConfirmVerify,
    DraftTemplateRequest,
    ManualStep,
    OperationCallStep,
    OperationCallVerify,
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
from meho_backplane.ui.csrf import CSRFMiddleware
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
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_OPERATOR_SUB = "op-42"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (KB-suite baseline)."""
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
    """Minimal FastAPI app wired for runbooks UI tests (KB-suite shape)."""
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


def _manual_body(
    *,
    title: str = "Drain node",
    target_kind: str | None = "k8s-node",
    step_body: str = "Run the **drain** command.",
) -> RunbookTemplateBody:
    """One manual step + confirm verify."""
    return RunbookTemplateBody(
        title=title,
        description="Procedure for draining a node.",
        target_kind=target_kind,
        steps=[
            ManualStep(
                id="drain",
                title="Drain the node",
                body=step_body,
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Node drained?"),
            ),
        ],
    )


def _mixed_body() -> RunbookTemplateBody:
    """One manual + one operation_call step; both verify kinds present."""
    return RunbookTemplateBody(
        title="Rotate certificate",
        description="Rotate the cluster's serving certificate.",
        target_kind="vmware-vcenter",
        steps=[
            ManualStep(
                id="prep",
                title="Prepare the change window",
                body="Announce the maintenance window.",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Window announced?"),
            ),
            OperationCallStep(
                id="rotate-cert",
                title="Rotate the serving cert",
                body="Dispatch the rotation operation.",
                type="operation_call",
                op_id="vmware.composite.cert.rotate",
                params={"target": "${run.target}"},
                verify=OperationCallVerify(
                    type="operation_call",
                    op_id="vmware.cert.status",
                    params={"target": "${run.target}"},
                    expect={"valid": True},
                ),
            ),
        ],
    )


def _seed_template(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: RunbookTemplateBody,
    publish: bool = False,
) -> int:
    """Create a draft template (optionally publishing it) and return its version."""

    async def _do() -> int:
        service = RunbookTemplateService()
        resp = await service.create_draft(
            tenant_id,
            _OPERATOR_SUB,
            DraftTemplateRequest(slug=slug, body=body),
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
    assigned_to: str,
    state: str = "completed",
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
                    assigned_to=assigned_to,
                    target="host-1",
                    params={},
                    state=state,
                    started_by=assigned_to,
                    started_at=datetime.now(UTC),
                )
            )
            await session.commit()

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = _OPERATOR_SUB,
    access_token: str = "access-token-plaintext",
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


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-runbooks-test-kid")
    return keypair, _public_jwks(keypair)


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_runbooks_index_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/runbooks`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/runbooks")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_runbooks_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/runbooks/<slug>`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/runbooks/some-template")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/runbooks -- catalog
# ---------------------------------------------------------------------------


def test_runbooks_index_lists_templates_with_status_badges() -> None:
    """``GET /ui/runbooks`` lists the tenant's templates with status badges."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    _seed_template(
        tenant_id=_TENANT_A,
        slug="rotate-cert",
        body=_mixed_body(),
        publish=True,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Runbooks" in body
    assert "drain-node" in body
    assert "rotate-cert" in body
    # Status badges for both lifecycle states present.
    assert "draft" in body
    assert "published" in body
    # Sidebar runbooks link points to /ui/runbooks.
    assert 'href="/ui/runbooks"' in body


def test_runbooks_index_empty_state() -> None:
    """A tenant with no templates renders the empty-state copy."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks")

    assert response.status_code == 200, response.text
    assert "No runbook templates yet" in response.text


def test_runbooks_index_htmx_returns_fragment_only() -> None:
    """``GET /ui/runbooks`` with ``HX-Request: true`` returns the fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks", headers={"HX-Request": "true"})

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="runbooks-list"' in body
    assert "drain-node" in body
    # Full-page chrome absent from the fragment.
    assert "<!doctype html>" not in body.lower()


# ---------------------------------------------------------------------------
# GET /ui/runbooks/list -- HTMX filter partial
# ---------------------------------------------------------------------------


def test_runbooks_list_partial_returns_fragment() -> None:
    """``GET /ui/runbooks/list`` returns the ``_list.html`` fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/list")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="runbooks-list"' in body
    assert "drain-node" in body
    assert "<!doctype html>" not in body.lower()


def test_runbooks_list_filters_by_status() -> None:
    """``status=published`` returns only published templates."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="draft-only", body=_manual_body())
    _seed_template(
        tenant_id=_TENANT_A,
        slug="published-one",
        body=_mixed_body(),
        publish=True,
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/list", params={"status": "published"})

    assert response.status_code == 200, response.text
    body = response.text
    assert "published-one" in body
    assert "draft-only" not in body


def test_runbooks_list_filters_by_target_kind() -> None:
    """``target_kind`` narrows the list to matching templates."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(
        tenant_id=_TENANT_A,
        slug="k8s-drain",
        body=_manual_body(target_kind="k8s-node"),
    )
    _seed_template(
        tenant_id=_TENANT_A,
        slug="vcenter-cert",
        body=_manual_body(target_kind="vmware-vcenter"),
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/list", params={"target_kind": "k8s-node"})

    assert response.status_code == 200, response.text
    body = response.text
    assert "k8s-drain" in body
    assert "vcenter-cert" not in body


def test_runbooks_list_invalid_status_returns_422() -> None:
    """An out-of-vocab ``status`` value trips a 422 at the query boundary."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/list", params={"status": "bogus"})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /ui/runbooks/<slug> -- detail (admin / post-completion sees full steps)
# ---------------------------------------------------------------------------


def test_runbooks_detail_admin_renders_all_step_kinds() -> None:
    """A tenant_admin sees both step kinds + both verify kinds, Markdown rendered."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(
        tenant_id=_TENANT_A,
        slug="rotate-cert",
        body=_mixed_body(),
        publish=True,
    )

    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=access_token)

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
    # Both step kinds rendered.
    assert "operation_call" in body
    assert "manual" in body
    # Step ids + op_id present.
    assert "rotate-cert" in body
    assert "vmware.composite.cert.rotate" in body
    # operation_call verify op_id + expect present.
    assert "vmware.cert.status" in body
    # confirm verify prompt present.
    assert "Window announced?" in body
    # Step body Markdown rendered server-side (manual step has no markup,
    # but the operation_call body is plain; assert the title renders).
    assert "Rotate the serving cert" in body
    # Not the restricted state.
    assert "Step details are restricted" not in body


def test_runbooks_detail_renders_markdown_step_body() -> None:
    """The step body Markdown is rendered to HTML server-side."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(
        tenant_id=_TENANT_A,
        slug="drain-node",
        body=_manual_body(step_body="Run the **drain** command and `kubectl cordon`."),
        publish=True,
    )

    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=access_token)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/drain-node")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Markdown bold + inline code rendered to HTML.
    assert "<strong>drain</strong>" in body
    assert "<code>kubectl cordon</code>" in body


def test_runbooks_detail_admin_missing_slug_404() -> None:
    """A tenant_admin probing a missing slug gets 404."""
    _seed_tenant(_TENANT_A, "tenant-a")

    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=access_token)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/does-not-exist")
    finally:
        mock.stop()

    assert response.status_code == 404


def test_runbooks_detail_post_completion_operator_sees_steps() -> None:
    """An operator with a completed run sees the full steps."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(
        tenant_id=_TENANT_A,
        slug="drain-node",
        body=_manual_body(),
        publish=True,
    )
    # Completed run unlocks the post-completion read for this operator.
    _seed_run(
        tenant_id=_TENANT_A,
        slug="drain-node",
        version=version,
        assigned_to=_OPERATOR_SUB,
        state="completed",
    )

    # Plaintext access token -> role lift soft-fails -> treated as operator.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/drain-node")

    assert response.status_code == 200, response.text
    body = response.text
    assert "Step details are restricted" not in body
    assert "Drain the node" in body
    assert "Node drained?" in body


# ---------------------------------------------------------------------------
# Opacity floor -- operator without a completed run sees the restricted state
# ---------------------------------------------------------------------------


def test_runbooks_detail_operator_without_run_restricted() -> None:
    """An operator with no completed run gets the graceful restricted state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(
        tenant_id=_TENANT_A,
        slug="drain-node",
        body=_manual_body(),
        publish=True,
    )

    # No run seeded -> can_show_template_post_completion is False; the
    # plaintext token makes the role lift soft-fail to operator-level.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/drain-node")

    # Page renders (not a raw 403/error) with the restricted notice and the
    # summary, but withholds the steps.
    assert response.status_code == 200, response.text
    body = response.text
    assert "Step details are restricted" in body
    # The catalog-level summary (slug) still renders.
    assert "drain-node" in body
    # The step internals are withheld.
    assert "Node drained?" not in body


def test_runbooks_detail_operator_in_progress_run_restricted() -> None:
    """An in-progress run does NOT unlock the steps (opacity floor stays)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(
        tenant_id=_TENANT_A,
        slug="drain-node",
        body=_manual_body(),
        publish=True,
    )
    _seed_run(
        tenant_id=_TENANT_A,
        slug="drain-node",
        version=version,
        assigned_to=_OPERATOR_SUB,
        state="in_progress",
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/drain-node")

    assert response.status_code == 200, response.text
    assert "Step details are restricted" in response.text


def test_runbooks_detail_operator_missing_slug_restricted_not_404() -> None:
    """An operator probing a missing slug gets the restricted state, not 404.

    Anti-enumeration: an operator must not learn a slug exists (or not) via
    a status-code differential -- the missing-slug path collapses to the
    same restricted page the no-run path produces.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/never-existed")

    assert response.status_code == 200, response.text
    assert "Step details are restricted" in response.text


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_cross_tenant_template_not_in_catalog() -> None:
    """Tenant B's template never appears in Tenant A's catalog."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_template(tenant_id=_TENANT_A, slug="tenant-a-rb", body=_manual_body())
    _seed_template(tenant_id=_TENANT_B, slug="tenant-b-rb", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks")

    assert response.status_code == 200, response.text
    body = response.text
    assert "tenant-a-rb" in body
    assert "tenant-b-rb" not in body


def test_cross_tenant_detail_admin_404() -> None:
    """A tenant_admin reading another tenant's slug gets 404 (tenant-scoped)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_template(
        tenant_id=_TENANT_B,
        slug="secret-rb",
        body=_manual_body(),
        publish=True,
    )

    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=access_token)

    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/secret-rb")
    finally:
        mock.stop()

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Dashboard tile + sidebar
# ---------------------------------------------------------------------------


def test_dashboard_renders_runbooks_tile_and_sidebar_link() -> None:
    """The dashboard renders the Runbooks tile + the sidebar link."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/")

    assert response.status_code == 200, response.text
    body = response.text
    # Tile + sidebar both link to /ui/runbooks.
    assert 'href="/ui/runbooks"' in body
    assert "Runbooks" in body
