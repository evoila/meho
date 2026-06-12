# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end acceptance for the runbooks UI surface as a whole (G10.6).

Initiative #1381 (G10.6 Runbooks UI), Task #1385 (T4 close-out). The
per-task suites (:mod:`backend.tests.test_ui_runbooks_list` T1,
:mod:`backend.tests.test_ui_runbooks_editor` T2,
:mod:`backend.tests.test_ui_runbooks_lifecycle` T3) cover each route in
isolation. This module asserts the *surface* -- the read + author +
lifecycle paths exercised together against one app instance, plus the
RBAC boundary and the nav/dashboard discoverability that close out the
initiative.

What the single end-to-end test pins (issue #1385 acceptance criteria):

* operator browse -- catalog lists templates with status badges and the
  ``status`` / ``target_kind`` filters narrow the list;
* operator opacity-floor -- an operator with no completed run against a
  template gets the graceful restricted-detail page (summary + notice),
  **not** a raw 403/500;
* ``tenant_admin`` authoring -- author a draft via the editor POST;
* publish -> deprecate round-trip with observable status transitions
  (draft -> published -> deprecated), read back through the read surface;
* operator blocked (403) from author / publish / deprecate (the write
  surfaces gate on ``require_ui_admin``);
* the runbooks sidebar link + dashboard tile render.

Harness shape mirrors the T1-T3 suites (KB read-surface precedent
#870): a minimal FastAPI app wired with the BFF middlewares + the UI
surface router, SQLite-backed seeding via the real
:class:`~meho_backplane.runbooks.service.RunbookTemplateService`, and a
pre-set session cookie. Admin paths mint a real ``tenant_admin`` JWT via
``tests._oidc_jwt_helpers`` so ``require_ui_admin`` resolves; operator
paths either rely on the soft-fail role lift (plaintext access token ->
"no admin") for the read surface or mint a real ``operator`` JWT (which
decodes cleanly but fails the role-rank check -> 403) for the write
boundary.
"""

from __future__ import annotations

import asyncio
import json
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
# App + seeding helpers (mirror the T1-T3 suites)
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal FastAPI app wired for the runbooks UI surface (KB-suite shape)."""
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
                body="Run the **drain** command.",
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
) -> int:
    """Create a draft template and return its version."""

    async def _do() -> int:
        resp = await RunbookTemplateService().create_draft(
            tenant_id,
            _OPERATOR_SUB,
            DraftTemplateRequest(slug=slug, body=body),
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
        keypair = _make_rsa_keypair("ui-runbooks-acceptance-kid")
    return keypair, _public_jwks(keypair)


def _role_session(role: TenantRole) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real JWT carrying *role*.

    Returns the session id and the JWKS the role lift must reach. A
    ``TENANT_ADMIN`` token passes ``require_ui_admin``; an ``OPERATOR``
    token decodes cleanly but fails the role rank check -> 403.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(_TENANT_A),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=access_token)
    return session_id, jwks


def _csrf_kwargs(session_id: uuid.UUID) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check.

    The editor's CSRF cookie is ``Secure``, so the plain-``http`` TestClient
    does not auto-store it; the browser round-trip is reconstructed by
    presenting the same minted token as both the ``X-CSRF-Token`` header and
    an explicit request cookie (the T2/T3 suites use the same shape).
    """
    token = mint_csrf_token(str(session_id))
    return {
        "headers": {CSRF_HEADER_NAME: token, "X-CSRF-Token": token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


def _status(slug: str, version: int) -> str:
    """Read the persisted status of ``(slug, version)`` for assertions."""

    async def _do() -> str:
        tpl = await RunbookTemplateService().show_template(_TENANT_A, slug, version=version)
        return tpl.status

    return asyncio.run(_do())


def _authored_steps_json() -> str:
    """A two-step editor payload: one manual+confirm, one operation_call.

    The editor form serialises steps as a JSON string with ``params`` /
    ``expect`` carried as nested JSON strings (the Alpine form shape the T2
    suite exercises). Built via ``json.dumps`` so the nested-string escaping
    is correct without hand-written backslashes.
    """
    steps = [
        {
            "id": "prep",
            "title": "Prepare",
            "body": "Announce the window.",
            "type": "manual",
            "op_id": "",
            "params": "{}",
            "verify": {
                "type": "confirm",
                "prompt": "Window announced?",
                "op_id": "",
                "params": "{}",
                "expect": "{}",
            },
        },
        {
            "id": "rotate",
            "title": "Rotate",
            "body": "Dispatch rotation.",
            "type": "operation_call",
            "op_id": "vmware.cert.rotate",
            "params": json.dumps({"target": "${run.target}"}),
            "verify": {
                "type": "operation_call",
                "prompt": "",
                "op_id": "vmware.cert.status",
                "params": json.dumps({"target": "${run.target}"}),
                "expect": json.dumps({"valid": True}),
            },
        },
    ]
    return json.dumps(steps)


# ---------------------------------------------------------------------------
# Cross-cutting end-to-end acceptance
# ---------------------------------------------------------------------------


def test_runbooks_surface_end_to_end() -> None:
    """The runbooks surface as a whole: browse -> opacity floor -> author ->
    publish -> deprecate, plus the operator write-boundary and nav/dashboard
    discoverability, exercised against one app instance.

    Asserted in one flow because the value of this test is the *interaction*
    of the routes (a published-then-deprecated template read back through the
    catalog, an operator who can browse but cannot author) -- properties the
    per-route unit suites cannot observe in isolation.
    """
    _seed_tenant(_TENANT_A, "tenant-a")

    # Two seeded drafts so the catalog + filters have something to show before
    # any authoring happens.
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    _seed_template(tenant_id=_TENANT_A, slug="rotate-cert", body=_mixed_body())

    admin_session, admin_jwks = _role_session(TenantRole.TENANT_ADMIN)
    operator_session, operator_jwks = _role_session(TenantRole.OPERATOR)
    # A plaintext-token operator session: the role lift soft-fails to "no
    # admin", so the read surface degrades to the opacity-floor restricted
    # state rather than 5xx-ing.
    operator_soft_session = _seed_session_sync(tenant_id=_TENANT_A)

    admin_mock = respx.mock(assert_all_called=False)
    admin_mock.start()
    try:
        _mock_discovery_and_jwks(admin_mock, admin_jwks)

        # --- 1. Operator browse: catalog lists templates with badges. ------
        op_client = _authenticated_client(operator_soft_session)
        catalog = op_client.get("/ui/runbooks")
        assert catalog.status_code == 200, catalog.text
        assert "<title>Runbooks" in catalog.text
        assert "drain-node" in catalog.text
        assert "rotate-cert" in catalog.text
        assert "draft" in catalog.text  # status badge
        # The sidebar link is part of the full-page chrome.
        assert 'href="/ui/runbooks"' in catalog.text

        # --- 2. Operator filters: target_kind narrows the catalog. ---------
        filtered = op_client.get("/ui/runbooks/list", params={"target_kind": "k8s-node"})
        assert filtered.status_code == 200, filtered.text
        assert "drain-node" in filtered.text  # target_kind=k8s-node
        assert "rotate-cert" not in filtered.text  # target_kind=vmware-vcenter

        status_filtered = op_client.get("/ui/runbooks/list", params={"status": "draft"})
        assert status_filtered.status_code == 200, status_filtered.text
        assert "drain-node" in status_filtered.text
        # An out-of-vocab status is a clean 422 at the query boundary.
        assert op_client.get("/ui/runbooks/list", params={"status": "bogus"}).status_code == 422

        # --- 3. Operator opacity floor: restricted detail, NOT a raw 403. --
        restricted = op_client.get("/ui/runbooks/drain-node")
        assert restricted.status_code == 200, restricted.text
        assert "Step details are restricted" in restricted.text
        assert "drain-node" in restricted.text  # summary still renders
        assert "Node drained?" not in restricted.text  # step internals withheld

        # --- 4. Admin authors a brand-new draft via the editor POST. -------
        admin_client = _authenticated_client(admin_session)
        author = admin_client.post(
            "/ui/runbooks/new",
            data={
                "slug": "cordon-host",
                "title": "Cordon host",
                "description": "Cordon and drain a host.",
                "target_kind": "k8s-node",
                "steps": _authored_steps_json(),
            },
            **_csrf_kwargs(admin_session),
        )
        assert author.status_code == 204, author.text
        assert author.headers.get("HX-Redirect") == "/ui/runbooks/cordon-host"
        authored_version = _status_version("cordon-host")
        assert _status("cordon-host", authored_version) == "draft"

        # --- 5. Lifecycle round-trip: publish -> deprecate, observable. ----
        publish = admin_client.post(
            "/ui/runbooks/cordon-host/publish",
            data={"version": str(authored_version)},
            **_csrf_kwargs(admin_session),
        )
        assert publish.status_code == 200, publish.text
        assert "published" in publish.text
        assert "Deprecate" in publish.text  # next valid action surfaces
        assert _status("cordon-host", authored_version) == "published"

        deprecate = admin_client.post(
            "/ui/runbooks/cordon-host/deprecate",
            data={"version": str(authored_version)},
            **_csrf_kwargs(admin_session),
        )
        assert deprecate.status_code == 200, deprecate.text
        assert "deprecated" in deprecate.text
        assert _status("cordon-host", authored_version) == "deprecated"

        # The transition is observable through the read surface: an admin
        # detail render shows the deprecated badge.
        admin_detail = admin_client.get("/ui/runbooks/cordon-host")
        assert admin_detail.status_code == 200, admin_detail.text
        assert "deprecated" in admin_detail.text
    finally:
        admin_mock.stop()

    # The role lift caches the JWKS it fetched for the admin token; the
    # operator token below is signed by a different keypair, so the stale
    # admin JWKS would 401 (signature mismatch) instead of reaching the
    # role-rank 403. Clear the cache so the operator token verifies against
    # its own JWKS and the gate fails at the *role* check (the boundary under
    # test), not the signature check.
    clear_jwks_cache()

    # --- 6. Operator write-boundary: author/publish/deprecate all 403. -----
    operator_mock = respx.mock(assert_all_called=False)
    operator_mock.start()
    try:
        _mock_discovery_and_jwks(operator_mock, operator_jwks)
        op_client = _authenticated_client(operator_session)

        author_forbidden = op_client.post(
            "/ui/runbooks/new",
            data={
                "slug": "operator-attempt",
                "title": "Nope",
                "description": "",
                "target_kind": "",
                "steps": _authored_steps_json(),
            },
            **_csrf_kwargs(operator_session),
        )
        assert author_forbidden.status_code == 403, author_forbidden.text

        publish_forbidden = op_client.post(
            "/ui/runbooks/drain-node/publish",
            data={"version": "1"},
            **_csrf_kwargs(operator_session),
        )
        assert publish_forbidden.status_code == 403, publish_forbidden.text
        # The forged publish did not fire — drain-node is still a draft.
        assert _status("drain-node", 1) == "draft"

        deprecate_forbidden = op_client.post(
            "/ui/runbooks/drain-node/deprecate",
            data={"version": "1"},
            **_csrf_kwargs(operator_session),
        )
        assert deprecate_forbidden.status_code == 403, deprecate_forbidden.text

        # The operator author attempt never persisted a row.
        assert not _slug_exists("operator-attempt")
    finally:
        operator_mock.stop()

    # --- 7. Discoverability: dashboard tile + sidebar link render. ---------
    with respx.mock(assert_all_called=False):
        dash_client = _authenticated_client(operator_soft_session)
        dashboard = dash_client.get("/ui/")
    assert dashboard.status_code == 200, dashboard.text
    assert 'href="/ui/runbooks"' in dashboard.text  # tile + sidebar both link
    assert "Runbooks" in dashboard.text


def test_runbooks_post_completion_operator_sees_full_steps() -> None:
    """An operator who completed a run crosses the opacity floor (full steps).

    The complement to the restricted branch in the end-to-end flow: the same
    operator, given a ``completed`` run against ``(slug, version)``, now sees
    the full step internals -- confirming the floor is a gate, not a wall.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    _seed_run(
        tenant_id=_TENANT_A,
        slug="drain-node",
        version=version,
        assigned_to=_OPERATOR_SUB,
        state="completed",
    )

    operator_soft_session = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(operator_soft_session)
        detail = client.get("/ui/runbooks/drain-node")

    assert detail.status_code == 200, detail.text
    assert "Step details are restricted" not in detail.text
    assert "Drain the node" in detail.text
    assert "Node drained?" in detail.text


# ---------------------------------------------------------------------------
# Persistence read-back helpers (used by the end-to-end assertions)
# ---------------------------------------------------------------------------


def _status_version(slug: str) -> int:
    """Return the latest version number for *slug* (for lifecycle targeting)."""

    async def _do() -> int:
        tpl = await RunbookTemplateService().show_template(_TENANT_A, slug)
        return tpl.version

    return asyncio.run(_do())


def _slug_exists(slug: str) -> bool:
    """Return whether *slug* resolves to any version for tenant A."""
    from meho_backplane.runbooks.service import TemplateNotFoundError

    async def _do() -> bool:
        try:
            await RunbookTemplateService().show_template(_TENANT_A, slug)
        except TemplateNotFoundError:
            return False
        return True

    return asyncio.run(_do())
