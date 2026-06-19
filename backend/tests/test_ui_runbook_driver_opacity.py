# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the runbooks UI run *driver* (T2 #1893).

Initiative #1837 (G10.11 Runbook runs UI), Task #1893. The driver is the
step-by-step execution surface (``/ui/runbooks/runs/{run_id}`` + the
Advance / Abort / Reassign POSTs). Acceptance criteria on issue #1893:

* **Opacity (the #1 risk).** (a) ``RunbookRunService.get_current_step``
  returns a single-step ``CurrentStepResponse`` whose
  ``current_step.id == position`` step and exposes NO other step (no
  full-step-list attribute; the other steps' titles are absent from its
  serialization). (b) ``GET /ui/runbooks/runs/{run_id}`` for an
  in-progress run renders ONLY the current step — the current step's
  title/body are present and every OTHER template step's title/body text
  is ABSENT from the page HTML. Guards the skip-ahead leak #1198 closed
  end-to-end.
* **Assignee gate (service-enforced, fail-closed).** ``POST
  .../next`` by a non-assignee TENANT_ADMIN renders an inline
  "reassigned away / not the assignee" message at HTTP 200 (no 500). A
  ``confirm`` step answered ``no`` renders the failed-step dead-end
  banner with Advance hidden and Abort shown (driven by
  ``PreviousStepFailedError``).
* **Abort reason.** ``POST .../abort`` with an empty reason is handled
  (client-guard + server-side ``min_length=1`` 422 surfaced as an inline
  alert, not a 500); a valid reason flips the run to ``abandoned`` and
  lands an abort audit row carrying the reason.
* **Reassign RBAC.** ``POST .../reassign`` by an OPERATOR is 403 at
  ``require_ui_admin`` (before the service is touched); by a
  TENANT_ADMIN it flips ``assigned_to`` and the prior assignee's
  subsequent ``next`` POST renders the "reassigned away" message.

Suite shape mirrors :mod:`backend.tests.test_ui_runbook_runs_list` (T1)
and :mod:`backend.tests.test_ui_runbooks_lifecycle`: a minimal FastAPI app
with the BFF middlewares + the UI router, SQLite-backed seeding, a pre-set
session cookie, and the ``Secure``-cookie / JWT-role-lift reconstruction
those suites document. Runs are seeded through the real
:meth:`RunbookRunService.start_run` so the ``runbook_run_step_states`` rows
exist (the driver's ``get_current_step`` needs them); the admin / operator
paths mint real role JWTs so ``require_ui_admin`` (reassign) and the soft
role probe (Reassign control) resolve.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, RunbookRun, Tenant
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.runs_schemas import (
    CurrentStepResponse,
    StartRunRequest,
)
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
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

#: The session's operator (the assignee for runs started under it, and the
#: JWT ``sub`` for the role-lift paths).
_OPERATOR_SUB = "op-self"
#: A second operator — the run's assignee in the non-assignee tests (so the
#: session operator is NOT the assignee).
_OTHER_SUB = "op-other"

#: Distinctive, collision-free step titles/bodies so the opacity assertion is
#: unambiguous: step 2's strings must NOT appear when the run is on step 1.
_STEP1_TITLE = "Drain the primary node"
_STEP1_BODY = "Cordon then drain the **primary** node now."
_STEP2_TITLE = "Verify cluster quorum restored"
_STEP2_BODY = "Confirm the raft quorum is healthy on the **secondary**."

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
# Builders / seeding
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal FastAPI app wired for runbook-driver UI tests."""
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


def _two_step_body() -> RunbookTemplateBody:
    """Two manual+confirm steps with distinctive, collision-free titles/bodies."""
    return RunbookTemplateBody(
        title="Drain procedure",
        description="Two-step drain + quorum check.",
        target_kind="k8s-node",
        steps=[
            ManualStep(
                id="drain-primary",
                title=_STEP1_TITLE,
                body=_STEP1_BODY,
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Primary drained?"),
            ),
            ManualStep(
                id="verify-quorum",
                title=_STEP2_TITLE,
                body=_STEP2_BODY,
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Quorum restored?"),
            ),
        ],
    )


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert one ``tenant`` row so FK constraints resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_template(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: RunbookTemplateBody,
) -> int:
    """Create+publish a template via the real service; return its version."""

    async def _do() -> int:
        service = RunbookTemplateService()
        resp = await service.create_draft(
            tenant_id, _OPERATOR_SUB, DraftTemplateRequest(slug=slug, body=body)
        )
        await service.publish(tenant_id, PublishTemplateRequest(slug=slug, version=resp.version))
        return resp.version

    return asyncio.run(_do())


def _start_run(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    assigned_to: str = _OPERATOR_SUB,
    target: str = "host-1",
) -> uuid.UUID:
    """Start a real run (creating step-state rows) and return its run_id.

    Going through :meth:`RunbookRunService.start_run` (rather than a raw row
    insert) gives the run the ``runbook_run_step_states`` rows the driver's
    ``get_current_step`` reads — the first step ``in_progress``, the rest
    ``pending``. ``assigned_to`` is the started operator; pass ``_OTHER_SUB``
    to seed a run the session operator is NOT the assignee of.
    """

    async def _do() -> uuid.UUID:
        resp = await RunbookRunService().start_run(
            tenant_id, assigned_to, StartRunRequest(template_slug=slug, target=target)
        )
        return resp.run_id

    return asyncio.run(_do())


def _run_row(run_id: uuid.UUID) -> RunbookRun:
    """Read back a single ``runbook_runs`` row (assertion helper)."""

    async def _do() -> RunbookRun:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = (
                await session.execute(select(RunbookRun).where(RunbookRun.run_id == run_id))
            ).scalar_one()
            return row

    return asyncio.run(_do())


def _abort_audit_rows(tenant_id: uuid.UUID) -> list[AuditLog]:
    """Read back every ``runbook.abort`` audit row for *tenant_id*."""

    async def _do() -> list[AuditLog]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.tenant_id == tenant_id,
                            AuditLog.path == "runbook.abort",
                        )
                    )
                )
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
    """Create a ``web_session`` row carrying *access_token*; return its UUID."""

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
        keypair = _make_rsa_keypair("ui-runbook-driver-test-kid")
    return keypair, _public_jwks(keypair)


def _role_session(
    role: TenantRole,
    *,
    operator_sub: str = _OPERATOR_SUB,
    tenant_id: uuid.UUID = _TENANT_A,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed a session whose access token is a real JWT carrying *role*.

    Returns the session id and the JWKS the role lift / ``require_ui_admin``
    gate must reach. A ``TENANT_ADMIN`` token passes the admin gate; an
    ``OPERATOR`` token decodes cleanly but fails the role rank check -> 403.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id, operator_sub=operator_sub, access_token=access_token
    )
    return session_id, jwks


def _csrf_kwargs(token: str) -> dict[str, Any]:
    """Header + cookie kwargs that satisfy the double-submit CSRF check."""
    return {
        "headers": {CSRF_HEADER_NAME: token},
        "cookies": {CSRF_COOKIE_NAME: token},
    }


_CSRF_SETCOOKIE_RE = re.compile(rf"{CSRF_COOKIE_NAME}=([^;]+)")
_FORM_HX_HEADERS_RE = re.compile(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'')


def _extract_csrf_cookie(response: Any) -> str:
    """Return the ``meho_csrf`` value the response set, or fail the test."""
    set_cookie = response.headers.get("set-cookie", "")
    match = _CSRF_SETCOOKIE_RE.search(set_cookie)
    assert match, f"no {CSRF_COOKIE_NAME} cookie set; got Set-Cookie={set_cookie!r}"
    return match.group(1)


def _extract_form_hx_token(body: str) -> str:
    """Return an ``X-CSRF-Token`` echoed via hx-headers in the rendered page."""
    match = _FORM_HX_HEADERS_RE.search(body)
    assert match, "no hx-headers X-CSRF-Token found in the driver render"
    return match.group(1)


# ===========================================================================
# (0) Service-level opacity: get_current_step returns ONE step, no list
# ===========================================================================


def test_get_current_step_returns_single_step_no_list() -> None:
    """``get_current_step`` returns the current ``StepBody`` and exposes no other step.

    Acceptance (a): the returned ``CurrentStepResponse`` carries exactly one
    ``current_step`` whose ``id`` is the position-1 step; it has NO
    full-step-list attribute, and the OTHER step's title/body are absent from
    its serialization.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain")

    async def _get() -> CurrentStepResponse | Any:
        return await RunbookRunService().get_current_step(_TENANT_A, _OPERATOR_SUB, run_id)

    result = asyncio.run(_get())

    assert isinstance(result, CurrentStepResponse)
    # The current step is step 1 of 2.
    assert result.position.n == 1
    assert result.position.total == 2
    assert result.current_step.id == "drain-primary"
    assert result.current_step.title == _STEP1_TITLE

    # No full-step-list attribute anywhere on the response.
    assert not hasattr(result, "steps")
    assert not hasattr(result.current_step, "steps")

    # The OTHER step's title/body text is absent from the serialized response.
    dumped = result.model_dump_json()
    assert _STEP2_TITLE not in dumped
    assert "secondary" not in dumped  # the distinctive token from step 2's body
    # Sanity: the current step's content IS present.
    assert _STEP1_TITLE in dumped


def test_get_current_step_terminal_completed_returns_no_step() -> None:
    """A completed run yields the completed terminal shape (no step body)."""
    from meho_backplane.runbooks.runs_schemas import (
        ConfirmVerifyResponse,
        NextStepRequest,
        RunCompletedResponse,
    )

    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="solo", body=_one_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="solo")

    async def _drive_to_completion() -> Any:
        service = RunbookRunService()
        await service.next_step(
            _TENANT_A,
            _OPERATOR_SUB,
            run_id,
            NextStepRequest(
                last_verified=True,
                verify_response=ConfirmVerifyResponse(type="confirm", answer="yes"),
            ),
        )
        return await service.get_current_step(_TENANT_A, _OPERATOR_SUB, run_id)

    result = asyncio.run(_drive_to_completion())
    assert isinstance(result, RunCompletedResponse)


def _one_step_body() -> RunbookTemplateBody:
    """A single manual+confirm step (so one ``next`` completes the run)."""
    return RunbookTemplateBody(
        title="Solo procedure",
        description="single step",
        target_kind="k8s-node",
        steps=[
            ManualStep(
                id="only",
                title="The only step",
                body="Do it.",
                type="manual",
                verify=ConfirmVerify(type="confirm", prompt="Done?"),
            ),
        ],
    )


# ===========================================================================
# (1) Page-level opacity: GET renders ONLY the current step
# ===========================================================================


def test_driver_page_renders_only_current_step() -> None:
    """``GET /ui/runbooks/runs/{run_id}`` renders the current step, hides the rest.

    Acceptance (b): the current step's title/body appear; every OTHER
    template step's title/body text is absent from the page HTML.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.text
    # Current (step 1) content present.
    assert _STEP1_TITLE in body
    assert "primary" in body
    assert "step 1 of 2" in body
    # The OPACITY assertion: step 2's title + distinctive body token absent.
    assert _STEP2_TITLE not in body
    assert "secondary" not in body
    assert "verify-quorum" not in body  # step 2's id


def test_driver_page_unknown_run_returns_404() -> None:
    """A run id that does not resolve renders the 404 page (no leak)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{uuid.uuid4()}")

    assert response.status_code == 404, response.text
    assert "No such run" in response.text


def test_driver_page_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/runbooks/runs/{run_id}`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(f"/ui/runbooks/runs/{uuid.uuid4()}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_driver_page_assignee_sees_advance_control() -> None:
    """The assignee's page renders the Advance control (and the abort modal)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert f'hx-post="/ui/runbooks/runs/{run_id}/next"' in body
    assert "Advance" in body


def test_driver_page_non_assignee_hides_advance_control() -> None:
    """A viewer who is NOT the assignee does not get the Advance control.

    The run is assigned to ``_OTHER_SUB``; the session operator can view it
    (admin scope below would be needed to *find* it via the list, but the
    driver shows any run the tenant can address) but Advance is withheld
    because they are not the assignee.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OTHER_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.text
    # Advance control absent (no next POST form); the current step still shows.
    assert f'hx-post="/ui/runbooks/runs/{run_id}/next"' not in body
    assert _STEP1_TITLE in body


def test_driver_page_admin_sees_reassign_control() -> None:
    """A TENANT_ADMIN's driver page renders the Reassign control."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{run_id}")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "Reassign" in body
    assert f'hx-post="/ui/runbooks/runs/{run_id}/reassign"' in body


def test_driver_page_operator_hides_reassign_control() -> None:
    """An OPERATOR's driver page does NOT render the Reassign control."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    # Plaintext token -> soft role lift fails to operator -> no reassign.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{run_id}")

    assert response.status_code == 200, response.text
    assert f'hx-post="/ui/runbooks/runs/{run_id}/reassign"' not in response.text


def test_driver_page_refreshes_csrf_cookie_matching_form_header() -> None:
    """The page render sets a ``meho_csrf`` cookie matching a control's hx-header token."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/runbooks/runs/{run_id}")

    assert response.status_code == 200, response.text
    cookie_token = _extract_csrf_cookie(response)
    form_token = _extract_form_hx_token(response.text)
    assert cookie_token == form_token


# ===========================================================================
# (2) Advance — assignee gate + confirm-no dead-end
# ===========================================================================


def test_next_non_assignee_admin_renders_inline_not_500() -> None:
    """A non-assignee TENANT_ADMIN's ``next`` POST is an inline 200 alert, not a 500.

    The run is assigned to ``_OTHER_SUB``; the admin session operator
    (``_OPERATOR_SUB``) is NOT the assignee. The service raises
    ``NotRunAssigneeError`` (fail-closed, even for an admin); the driver
    surfaces it as the "reassigned away" inline message at HTTP 200.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OTHER_SUB)

    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    token = mint_csrf_token(str(session_id))
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/next",
            data={"verify_type": "confirm", "answer": "yes"},
            **_csrf_kwargs(token),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    assert "reassigned away" in body
    # The run was NOT advanced (still in_progress, still assigned to _OTHER_SUB).
    row = _run_row(run_id)
    assert row.state == "in_progress"
    assert row.assigned_to == _OTHER_SUB


def test_next_confirm_no_renders_failed_dead_end_banner() -> None:
    """Answering ``no`` on a confirm step renders the dead-end banner; Advance hidden.

    ``PreviousStepFailedError`` fires (the step flips to ``failed``); the
    driver renders the dead-end banner explaining the only forward move is
    Abort, with the Advance control withheld and the Abort control shown.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/next",
            data={"verify_type": "confirm", "answer": "no"},
            **_csrf_kwargs(token),
        )

    assert response.status_code == 200, response.text
    body = response.text
    # Dead-end banner present; Abort still available; Advance withheld.
    assert "failed its verify" in body
    assert "Abort" in body
    assert f'hx-post="/ui/runbooks/runs/{run_id}/next"' not in body
    # Run stays in_progress (the step is failed, but the run is not terminal).
    assert _run_row(run_id).state == "in_progress"


def test_next_confirm_yes_advances_to_step_two() -> None:
    """Answering ``yes`` on step 1 advances and renders step 2 (and only step 2)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/next",
            data={"verify_type": "confirm", "answer": "yes"},
            **_csrf_kwargs(token),
        )

    assert response.status_code == 200, response.text
    body = response.text
    # Now on step 2; step 1's content is gone (opacity holds on advance too).
    assert _STEP2_TITLE in body
    assert "step 2 of 2" in body
    assert _STEP1_TITLE not in body


# ===========================================================================
# (3) Abort — empty reason handled; valid reason flips state + audits
# ===========================================================================


def test_abort_empty_reason_handled_not_500() -> None:
    """A tampered empty ``reason`` is handled inline (no 500), and no abort lands."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/abort",
            data={"reason": "   "},  # whitespace-only -> empty after strip
            **_csrf_kwargs(token),
        )

    assert response.status_code == 200, response.text
    assert "reason is required" in response.text
    # The run was NOT aborted, and no abort audit row landed.
    assert _run_row(run_id).state == "in_progress"
    assert _abort_audit_rows(_TENANT_A) == []


def test_abort_valid_reason_flips_state_and_audits() -> None:
    """A valid reason flips the run to ``abandoned`` and lands an abort audit row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    reason = "node lost; escalating to on-call"
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    token = mint_csrf_token(str(session_id))
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/abort",
            data={"reason": reason},
            **_csrf_kwargs(token),
        )

    assert response.status_code == 200, response.text
    assert "Run abandoned" in response.text
    # Row flipped to abandoned.
    assert _run_row(run_id).state == "abandoned"
    # An abort audit row landed carrying the reason.
    audits = _abort_audit_rows(_TENANT_A)
    assert len(audits) == 1
    assert audits[0].payload.get("reason") == reason


def test_abort_missing_csrf_rejected_403() -> None:
    """An abort POST with no CSRF token is 403 (and no abort lands)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/abort",
            data={"reason": "x"},
        )

    assert response.status_code == 403, response.text
    assert _run_row(run_id).state == "in_progress"


# ===========================================================================
# (4) Reassign — RBAC gate + prior-assignee "reassigned away"
# ===========================================================================


def test_reassign_operator_forbidden_403_before_service() -> None:
    """An OPERATOR's reassign POST is 403 at ``require_ui_admin`` (service untouched).

    The forged POST carries a valid CSRF token (so it clears the CSRF gate)
    but a real OPERATOR-role JWT, so the hard ``require_ui_admin`` dependency
    403s it before the handler body / service runs. The run's assignee is
    unchanged.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id, jwks = _role_session(TenantRole.OPERATOR)
    token = mint_csrf_token(str(session_id))
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/reassign",
            data={"new_assignee": _OTHER_SUB},
            **_csrf_kwargs(token),
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    # Assignee unchanged — the service was never reached.
    assert _run_row(run_id).assigned_to == _OPERATOR_SUB


def test_reassign_admin_flips_assignee_and_prior_loses_advance() -> None:
    """A TENANT_ADMIN reassign flips ``assigned_to``; the prior assignee loses Advance.

    The admin (``_OPERATOR_SUB``) reassigns their own run to ``_OTHER_SUB``;
    the row's ``assigned_to`` flips, and the prior assignee's subsequent
    ``next`` POST renders the "reassigned away" message (the service returns
    ``NotRunAssigneeError`` for them).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    # Started by (assigned to) the admin operator so the admin is the prior assignee.
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    token = mint_csrf_token(str(session_id))
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        reassign = client.post(
            f"/ui/runbooks/runs/{run_id}/reassign",
            data={"new_assignee": _OTHER_SUB},
            **_csrf_kwargs(token),
        )
        assert reassign.status_code == 200, reassign.text
        # Assignee flipped on the row.
        assert _run_row(run_id).assigned_to == _OTHER_SUB

        # The prior assignee (the admin operator, now NOT the assignee) tries to
        # advance -> "reassigned away" inline message.
        nxt = client.post(
            f"/ui/runbooks/runs/{run_id}/next",
            data={"verify_type": "confirm", "answer": "yes"},
            **_csrf_kwargs(token),
        )
    finally:
        mock.stop()

    assert nxt.status_code == 200, nxt.text
    assert "reassigned away" in nxt.text
    # Still not advanced.
    assert _run_row(run_id).state == "in_progress"


def test_reassign_admin_empty_assignee_handled_not_500() -> None:
    """A blank ``new_assignee`` from an admin is handled inline (no 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_template(tenant_id=_TENANT_A, slug="drain", body=_two_step_body())
    run_id = _start_run(tenant_id=_TENANT_A, slug="drain", assigned_to=_OPERATOR_SUB)

    session_id, jwks = _role_session(TenantRole.TENANT_ADMIN)
    token = mint_csrf_token(str(session_id))
    mock = respx.mock(assert_all_called=False)
    mock.start()
    try:
        _mock_discovery_and_jwks(mock, jwks)
        client = _authenticated_client(session_id)
        response = client.post(
            f"/ui/runbooks/runs/{run_id}/reassign",
            data={"new_assignee": "   "},
            **_csrf_kwargs(token),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert "new assignee is required" in response.text
    assert _run_row(run_id).assigned_to == _OPERATOR_SUB


# ===========================================================================
# (5) Route-ordering grep-proof (build the router, assert the index)
# ===========================================================================


def test_route_ordering_start_before_run_id_before_slug() -> None:
    """The driver routes register before ``{slug}`` and after T1's ``runs/start``.

    Grep-proof acceptance criterion: ``/ui/runbooks/runs/start`` (T1) is
    registered BEFORE ``/ui/runbooks/runs/{run_id}`` (else ``start`` binds as
    a ``run_id``), and the driver's ``{run_id}`` GET + ``next``/``abort``/
    ``reassign`` POSTs are all BEFORE the ``/ui/runbooks/{slug}`` catch-all
    (else ``runs`` binds as a ``slug``). Also asserts the reassign route
    declares the ``require_ui_admin`` dependency.
    """
    from meho_backplane.ui.auth.middleware import require_ui_admin
    from meho_backplane.ui.routes.runbooks.routes import build_runbooks_router

    router = build_runbooks_router()

    def _index(path: str, method: str) -> int:
        for i, route in enumerate(router.routes):
            if getattr(route, "path", None) == path and method in (
                getattr(route, "methods", None) or set()
            ):
                return i
        raise AssertionError(f"route {method} {path} not found")

    start_i = _index("/ui/runbooks/runs/start", "GET")
    run_get_i = _index("/ui/runbooks/runs/{run_id}", "GET")
    next_i = _index("/ui/runbooks/runs/{run_id}/next", "POST")
    abort_i = _index("/ui/runbooks/runs/{run_id}/abort", "POST")
    reassign_i = _index("/ui/runbooks/runs/{run_id}/reassign", "POST")
    slug_i = _index("/ui/runbooks/{slug}", "GET")

    # start (literal) before {run_id} (param).
    assert start_i < run_get_i
    # all driver routes before the {slug} catch-all.
    assert run_get_i < slug_i
    assert next_i < slug_i
    assert abort_i < slug_i
    assert reassign_i < slug_i

    # The reassign route declares the require_ui_admin hard dependency.
    reassign_route = next(
        route
        for route in router.routes
        if getattr(route, "path", None) == "/ui/runbooks/runs/{run_id}/reassign"
        and "POST" in (getattr(route, "methods", None) or set())
    )
    dep_calls = [dep.call for dep in reassign_route.dependant.dependencies]
    assert require_ui_admin in dep_calls, "reassign route must Depend on require_ui_admin"
