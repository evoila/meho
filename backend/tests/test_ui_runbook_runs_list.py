# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the runbooks UI run surface (list + start modal).

Initiative #1837 (G10.11 Runbook runs UI), Task #1884 (T1). Acceptance
criteria on issue #1884:

* ``GET /ui/runbooks/runs`` is role-scoped: a seeded OPERATOR with one own
  run and another operator's run sees ONLY their own row (the foreign
  ``run_id`` string is absent from the HTML); a seeded TENANT_ADMIN sees
  both. Verifies the ``caller_is_admin`` branch is wired, not just buttons
  hidden.
* ``POST /ui/runbooks/runs`` with a valid published ``template_slug``
  returns 204 with ``HX-Redirect: /ui/runbooks/runs/{run_id}`` and a
  ``RunbookRun`` row lands with ``assigned_to == operator.sub``; a template
  body referencing an unsupplied ``${run.params.X}`` renders the modal
  ``alert-error`` (HTTP 200 fragment, not 500) carrying the missing-params
  message.
* ``POST /ui/runbooks/runs`` with a missing/invalid ``X-CSRF-Token`` is
  rejected 403 by ``CSRFMiddleware`` (``x-csrf-rejection-reason`` header
  present); the start-modal fragment render refreshes the ``meho_csrf``
  cookie so the immediately-following submit's header matches the cookie
  (no ``value_mismatch``).

Suite shape mirrors :mod:`backend.tests.test_ui_runbooks_list` (the read
surface) + :mod:`backend.tests.test_ui_runbooks_lifecycle` (the
CSRF-bearing write surface): a minimal FastAPI app wired with the BFF
middlewares + the UI surface router, SQLite-backed seeding via the real
:class:`~meho_backplane.runbooks.service.RunbookTemplateService`, a pre-set
session cookie, and the ``Secure``-cookie reconstruction the lifecycle
suite documents (the http TestClient does not auto-store a ``Secure``
cookie, so the value is read off the ``Set-Cookie`` header and replayed).
The admin path mints a real ``tenant_admin`` JWT so the soft role lift
resolves admin; the operator path relies on the soft-fail lift (the
plaintext access token is not a valid JWT -> "no admin" -> own-runs scope).
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

#: The operator the session belongs to (the JWT ``sub`` for the admin path,
#: and the session ``operator_sub`` for the operator path).
_OPERATOR_SUB = "op-self"
#: A second operator whose run must be invisible to ``_OPERATOR_SUB`` but
#: visible to a tenant_admin.
_OTHER_SUB = "op-other"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (read-surface baseline)."""
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
    """Minimal FastAPI app wired for runbook-runs UI tests."""
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
    step_body: str = "Run the **drain** command.",
) -> RunbookTemplateBody:
    """One manual step + confirm verify; no ``${run.params.X}`` references."""
    return RunbookTemplateBody(
        title=title,
        description="Procedure for draining a node.",
        target_kind="k8s-node",
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


def _params_body() -> RunbookTemplateBody:
    """A template whose step body references ``${run.params.namespace}``.

    Starting a run of this template WITHOUT supplying ``namespace`` raises
    :class:`~meho_backplane.runbooks.run_service.MissingParamsError`, which
    the start handler maps to the inline modal alert.
    """
    return RunbookTemplateBody(
        title="Scale deployment",
        description="Scale a deployment in a namespace.",
        target_kind="k8s-deployment",
        steps=[
            ManualStep(
                id="scale",
                title="Scale the deployment",
                body="Scale in namespace ${run.params.namespace}.",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Scaled?"),
            ),
        ],
    )


def _seed_template(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: RunbookTemplateBody,
    publish: bool = True,
) -> int:
    """Create a draft template (publishing it by default) and return its version."""

    async def _do() -> int:
        service = RunbookTemplateService()
        resp = await service.create_draft(
            tenant_id,
            _OPERATOR_SUB,
            DraftTemplateRequest(slug=slug, body=body),
        )
        if publish:
            from meho_backplane.runbooks.schemas import PublishTemplateRequest

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
    state: str = "in_progress",
    target: str = "host-1",
) -> uuid.UUID:
    """Insert one ``runbook_runs`` row and return its ``run_id``."""
    run_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            session.add(
                RunbookRun(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    template_slug=slug,
                    template_version=version,
                    assigned_to=assigned_to,
                    target=target,
                    params={},
                    state=state,
                    started_by=assigned_to,
                    started_at=datetime.now(UTC),
                )
            )
            await session.commit()

    asyncio.run(_do())
    return run_id


def _run_rows(tenant_id: uuid.UUID) -> list[RunbookRun]:
    """Read back every ``runbook_runs`` row for *tenant_id* (assertion helper)."""

    async def _do() -> list[RunbookRun]:
        from sqlalchemy import select

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (
                (await session.execute(select(RunbookRun).where(RunbookRun.tenant_id == tenant_id)))
                .scalars()
                .all()
            )
            return list(rows)

    return asyncio.run(_do())


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
        keypair = _make_rsa_keypair("ui-runbook-runs-test-kid")
    return keypair, _public_jwks(keypair)


def _admin_session(tenant_id: uuid.UUID = _TENANT_A) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real tenant_admin JWT for _OPERATOR_SUB."""
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OPERATOR_SUB,
        tenant_id=str(tenant_id),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(tenant_id=tenant_id, access_token=access_token)
    return session_id, jwks


def _csrf_kwargs(token: str) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check."""
    return {
        "headers": {CSRF_HEADER_NAME: token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


#: Pull the ``meho_csrf`` value out of a raw ``Set-Cookie`` header. The cookie
#: is ``Secure``, so the plain-``http`` TestClient does not auto-store it -- the
#: real browser cookie/header round-trip is reconstructed by reading the
#: Set-Cookie the server emitted and presenting it back verbatim.
_CSRF_SETCOOKIE_RE = re.compile(rf"{CSRF_COOKIE_NAME}=([^;]+)")

#: Pull the ``X-CSRF-Token`` the start form echoes via ``hx-headers`` out of the
#: rendered modal fragment. This is the *header* half of the double-submit pair.
_FORM_HX_HEADERS_RE = re.compile(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'')


def _extract_csrf_cookie(response: Any) -> str:
    """Return the ``meho_csrf`` value the response set, or fail the test."""
    set_cookie = response.headers.get("set-cookie", "")
    match = _CSRF_SETCOOKIE_RE.search(set_cookie)
    assert match, f"no {CSRF_COOKIE_NAME} cookie set; got Set-Cookie={set_cookie!r}"
    return match.group(1)


def _extract_form_hx_token(body: str) -> str:
    """Return the ``X-CSRF-Token`` the start form echoes via hx-headers."""
    match = _FORM_HX_HEADERS_RE.search(body)
    assert match, "no start-form hx-headers X-CSRF-Token found in the modal fragment"
    return match.group(1)


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_runs_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/runbooks/runs`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/runbooks/runs")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_runs_start_modal_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/runbooks/runs/start`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/runbooks/runs/start")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/runbooks/runs -- role-scoped list (the headline acceptance criterion)
# ---------------------------------------------------------------------------


def test_runs_operator_sees_only_own_run() -> None:
    """An OPERATOR sees ONLY their own run; the foreign run_id is absent.

    Verifies the ``caller_is_admin=False`` branch is wired at the service
    level -- not merely that a control is hidden. The plaintext access token
    makes the role lift soft-fail to operator-level, so the service forces
    ``assignee=operator_sub`` and the other operator's run never appears.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    own_run = _seed_run(
        tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OPERATOR_SUB
    )
    foreign_run = _seed_run(
        tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OTHER_SUB
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs")

    assert response.status_code == 200, response.text
    body = response.text
    assert str(own_run) in body
    # The headline assertion: the foreign run_id string is absent from the HTML.
    assert str(foreign_run) not in body


def test_runs_admin_sees_all_runs() -> None:
    """A TENANT_ADMIN sees BOTH their own and another operator's run."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    own_run = _seed_run(
        tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OPERATOR_SUB
    )
    foreign_run = _seed_run(
        tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OTHER_SUB
    )

    session_id, jwks = _admin_session()
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert str(own_run) in body
    # The admin (caller_is_admin=True) honours the unfiltered view -> sees both.
    assert str(foreign_run) in body
    # The admin-only assignee filter control renders.
    assert 'name="assignee"' in body


def test_runs_operator_no_assignee_filter_control() -> None:
    """An OPERATOR does not get the admin-only assignee filter control."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs")

    assert response.status_code == 200, response.text
    assert 'name="assignee"' not in response.text


def test_runs_empty_state() -> None:
    """A caller with no runs renders the empty-state copy."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs")

    assert response.status_code == 200, response.text
    assert "No runbook runs yet" in response.text


def test_runs_htmx_returns_fragment_only() -> None:
    """``GET /ui/runbooks/runs`` with ``HX-Request: true`` returns the fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    _seed_run(tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs", headers={"HX-Request": "true"})

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="runbook-runs-list"' in body
    # Full-page chrome absent from the fragment.
    assert "<!doctype html>" not in body.lower()


def test_runs_filters_by_state() -> None:
    """``status=completed`` returns only completed runs."""
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    in_progress_run = _seed_run(
        tenant_id=_TENANT_A,
        slug="drain-node",
        version=version,
        assigned_to=_OPERATOR_SUB,
        state="in_progress",
    )
    completed_run = _seed_run(
        tenant_id=_TENANT_A,
        slug="drain-node",
        version=version,
        assigned_to=_OPERATOR_SUB,
        state="completed",
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs", params={"status": "completed"})

    assert response.status_code == 200, response.text
    body = response.text
    assert str(completed_run) in body
    assert str(in_progress_run) not in body


def test_runs_invalid_state_returns_422() -> None:
    """An out-of-vocab ``status`` value trips a 422 at the query boundary."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs", params={"status": "bogus"})

    assert response.status_code == 422


def test_runs_operator_forged_assignee_filter_ignored() -> None:
    """An OPERATOR passing ``?assignee=<other>`` still sees only their own runs.

    The service pins ``assignee=caller_sub`` for a non-admin regardless of the
    query string -- the filter is a narrow surface for the caller's own view,
    never an escape into another operator's.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    version = _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    own_run = _seed_run(
        tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OPERATOR_SUB
    )
    foreign_run = _seed_run(
        tenant_id=_TENANT_A, slug="drain-node", version=version, assigned_to=_OTHER_SUB
    )

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs", params={"assignee": _OTHER_SUB})

    assert response.status_code == 200, response.text
    body = response.text
    assert str(own_run) in body
    assert str(foreign_run) not in body


# ---------------------------------------------------------------------------
# GET /ui/runbooks/runs/start -- start modal fragment
# ---------------------------------------------------------------------------


def test_start_modal_renders_with_published_picker() -> None:
    """The start modal renders the form + a datalist of published slugs."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())
    # A draft must not appear in the published picker.
    _seed_template(tenant_id=_TENANT_A, slug="draft-only", body=_manual_body(), publish=False)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs/start")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="runbook-run-start-modal"' in body
    assert 'hx-post="/ui/runbooks/runs"' in body
    assert 'name="template_slug"' in body
    # Published slug offered; draft slug withheld from the picker.
    assert '<option value="drain-node">' in body
    assert '<option value="draft-only">' not in body


def test_start_modal_refreshes_csrf_cookie_matching_form_header() -> None:
    """The modal render sets a ``meho_csrf`` cookie matching the form's header token.

    The double-submit pair must line up so the immediately-following submit is
    not 403'd with ``value_mismatch`` (#1693): the cookie the modal sets and
    the ``hx-headers`` token the form echoes are the SAME value.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs/start")

    assert response.status_code == 200, response.text
    cookie_token = _extract_csrf_cookie(response)
    form_token = _extract_form_hx_token(response.text)
    assert cookie_token == form_token


# ---------------------------------------------------------------------------
# POST /ui/runbooks/runs -- start handler
# ---------------------------------------------------------------------------


def test_start_run_success_redirects_and_lands_row() -> None:
    """A valid start returns 204 + HX-Redirect and lands a row assigned to the operator."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={"template_slug": "drain-node", "target": "host-9"},
            **_csrf_kwargs(token),
        )

    assert response.status_code == 204, response.text
    redirect = response.headers.get("HX-Redirect", "")
    assert redirect.startswith("/ui/runbooks/runs/")

    rows = _run_rows(_TENANT_A)
    assert len(rows) == 1
    run = rows[0]
    # The run is assigned to the session operator (not any other principal).
    assert run.assigned_to == _OPERATOR_SUB
    assert run.target == "host-9"
    assert run.template_slug == "drain-node"
    # The redirect points at the run that just landed.
    assert redirect == f"/ui/runbooks/runs/{run.run_id}"


def test_start_run_missing_params_renders_alert_not_500() -> None:
    """A template needing an unsupplied ``${run.params.X}`` renders the modal alert.

    HTTP 200 fragment (not 500), carrying the missing-params message that
    names the key the operator must supply, and no run row lands.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="scale-deploy", body=_params_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={"template_slug": "scale-deploy", "target": "host-9"},
            **_csrf_kwargs(token),
        )

    assert response.status_code == 200, response.text
    body = response.text
    assert "alert-error" in body
    # The missing-params message names the unsupplied key.
    assert "namespace" in body
    # The modal re-renders (so the operator can fix + resubmit).
    assert 'id="runbook-run-start-modal"' in body
    # No run row landed.
    assert _run_rows(_TENANT_A) == []


def test_start_run_unknown_template_renders_alert_not_500() -> None:
    """Starting a run on a non-existent slug renders the modal alert (200), not a 500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={"template_slug": "does-not-exist", "target": "host-9"},
            **_csrf_kwargs(token),
        )

    assert response.status_code == 200, response.text
    assert "alert-error" in response.text
    assert _run_rows(_TENANT_A) == []


def test_start_run_invalid_params_json_returns_422() -> None:
    """A non-object / unparseable ``params`` value trips a 422 at the handler."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={
                "template_slug": "drain-node",
                "target": "host-9",
                "params": "[1, 2, 3]",
            },
            **_csrf_kwargs(token),
        )

    assert response.status_code == 422, response.text
    assert _run_rows(_TENANT_A) == []


def test_start_run_passes_params_and_work_ref() -> None:
    """A well-formed params object + work_ref are persisted on the run row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="scale-deploy", body=_params_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={
                "template_slug": "scale-deploy",
                "target": "host-9",
                "params": '{"namespace": "prod"}',
                "work_ref": "gh:evoila/meho#9",
            },
            **_csrf_kwargs(token),
        )

    assert response.status_code == 204, response.text
    rows = _run_rows(_TENANT_A)
    assert len(rows) == 1
    run = rows[0]
    assert run.params == {"namespace": "prod"}
    assert run.work_ref == "gh:evoila/meho#9"


# ---------------------------------------------------------------------------
# CSRF enforcement on POST /ui/runbooks/runs
# ---------------------------------------------------------------------------


def test_start_run_missing_csrf_token_rejected_403() -> None:
    """A start POST with no CSRF token is 403 with the rejection-reason header."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={"template_slug": "drain-node", "target": "host-9"},
        )

    assert response.status_code == 403, response.text
    assert "x-csrf-rejection-reason" in response.headers
    # No run row landed on a rejected request.
    assert _run_rows(_TENANT_A) == []


def test_start_run_mismatched_csrf_token_rejected_403() -> None:
    """A start POST whose header token != cookie token is 403 (value_mismatch)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    good = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            "/ui/runbooks/runs",
            data={"template_slug": "drain-node", "target": "host-9"},
            headers={CSRF_HEADER_NAME: good},
            cookies={CSRF_COOKIE_NAME: "not-the-same-token"},
        )

    assert response.status_code == 403, response.text
    assert response.headers.get("x-csrf-rejection-reason") == "value_mismatch"
    assert _run_rows(_TENANT_A) == []


def test_start_run_modal_token_satisfies_immediate_submit() -> None:
    """The modal-render token (cookie + form header) satisfies the very next submit.

    End-to-end double-submit: render the modal, replay the ``Secure`` cookie it
    set + the header token the form echoed (they match by construction), and the
    start POST succeeds (204) rather than being 403'd with ``value_mismatch``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain-node", body=_manual_body())

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        modal = client.get("/ui/runbooks/runs/start")
        cookie_token = _extract_csrf_cookie(modal)
        form_token = _extract_form_hx_token(modal.text)
        assert cookie_token == form_token
        response = client.post(
            "/ui/runbooks/runs",
            data={"template_slug": "drain-node", "target": "host-9"},
            headers={CSRF_HEADER_NAME: form_token},
            cookies={CSRF_COOKIE_NAME: cookie_token},
        )

    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect", "").startswith("/ui/runbooks/runs/")


# ---------------------------------------------------------------------------
# Sub-tab nav present on both runbooks views
# ---------------------------------------------------------------------------


def test_runs_page_renders_tab_nav() -> None:
    """The runs page renders the Templates/Runs sub-tab nav with Runs active."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/runbooks/runs")

    assert response.status_code == 200, response.text
    body = response.text
    # Both tab links present; the runs page keeps the runbooks sidebar entry.
    assert 'href="/ui/runbooks"' in body
    assert 'href="/ui/runbooks/runs"' in body
