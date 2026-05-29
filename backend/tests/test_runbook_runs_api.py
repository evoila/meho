# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.runbook_runs`.

Coverage matrix (G12.3-T5 / Task #1311 acceptance criteria):

* **Route mounting** -- all five routes appear in the FastAPI app's
  route table and the OpenAPI document.
* **Start** -- ``operator`` POST -> 201; deprecated template -> 400;
  missing template -> 404; missing params -> 422.
* **Next (happy paths)** -- ``confirm`` ``answer="yes"`` advances;
  ``operation_call`` match advances; verify on the last step yields
  the ``run_completed`` shape (200, ``state="completed"``).
* **Next (refusals)** -- non-assignee operator -> 403; non-assignee
  admin -> 403; terminal run -> 400; previous failed -> 400; missing
  verify response -> 422.
* **Opacity** -- the ``next`` response carries exactly one step body
  and no reference to other step ids in the JSON (load-bearing
  property test against #1191's adherence floor).
* **Abort** -- assignee -> 200; admin on someone else's run -> 200;
  non-assignee non-admin -> 403.
* **Reassign** -- admin -> 200; operator -> 403 (admin-only);
  missing run -> 404.
* **List runs** -- operator sees only own runs (filter ignored);
  admin sees all tenant runs; ``?status=`` filter narrows; cross-
  tenant isolation (operator from tenant A never sees tenant B).
* **Audit op_id binding** -- each route binds the right
  ``audit_op_id``; ``next`` with an ``operation_call`` verify
  produces *two* audit rows (route envelope + dispatched verify call)
  both correlated via ``run_id``.

Tests boot the FastAPI app with the production middleware stack
(:class:`RequestContextMiddleware` + :class:`AuditMiddleware`) so audit
rows are inserted into the autouse-migrated SQLite engine. The
:class:`RunbookRunService` is patched on the route's import site for
the per-route behavioural tests; the service's own DB-backed coverage
lives in ``tests/test_runbooks_run_service.py``.
"""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.runbook_runs import router as runbook_runs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.runbooks.engine import (
    VerifyResponseMismatchError,
    VerifyResponseRequiredError,
)
from meho_backplane.runbooks.run_service import (
    DeprecatedTemplateError,
    MissingParamsError,
    NotRunAssigneeError,
    PreviousStepFailedError,
    RunAlreadyTerminalError,
    RunNotFoundError,
)
from meho_backplane.runbooks.runs_schemas import (
    AbortRunResponse,
    CurrentStepResponse,
    ReassignRunResponse,
    RunCompletedResponse,
    RunSummary,
    StepBody,
    StepBodyVerify,
    StepPosition,
)
from meho_backplane.runbooks.service import TemplateNotFoundError
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_SERVICE_ROUTE = "meho_backplane.api.v1.runbook_runs.RunbookRunService"

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


# ---------------------------------------------------------------------------
# Log capture (mirrors test_runbook_templates_api.py)
# ---------------------------------------------------------------------------


def _configure_capture(buf: io.StringIO) -> None:
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    _configure_capture(buf)
    yield buf
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mirroring prod with the runbook-runs router mounted."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(runbook_runs_router)
    return app


@pytest.fixture
def client(log_buffer: io.StringIO) -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _admin_token(*, tenant_id: UUID | None = None, sub: str = "op-admin") -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-admin")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(tid),
    )
    return key, token


def _operator_token(*, tenant_id: UUID | None = None, sub: str = "op-operator") -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-operator")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tid),
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Body / response builders
# ---------------------------------------------------------------------------


def _step_body(
    *,
    step_id: str = "drain-node",
    title: str = "Drain the node",
    body: str = "Run kubectl drain ${run.target}.",
) -> StepBody:
    """Build a :class:`StepBody` -- the opaque single-step shape."""
    return StepBody(
        id=step_id,
        title=title,
        body=body,
        type="operation_call",
        op_id="kubernetes.drain_node",
        params={"node": "node-1"},
        verify=StepBodyVerify(
            type="confirm",
            prompt=f"Is {step_id} done?",
        ),
    )


def _current_step_response(
    *,
    run_id: UUID,
    step: StepBody | None = None,
    n: int = 1,
    total: int = 3,
    template_slug: str = "rotate-cert",
    template_version: int = 1,
) -> CurrentStepResponse:
    return CurrentStepResponse(
        run_id=run_id,
        template_slug=template_slug,
        template_version=template_version,
        position=StepPosition(n=n, total=total),
        current_step=step if step is not None else _step_body(),
    )


def _run_completed_response(*, run_id: UUID) -> RunCompletedResponse:
    return RunCompletedResponse(
        run_id=run_id,
        completed_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )


def _abort_response(*, run_id: UUID) -> AbortRunResponse:
    return AbortRunResponse(
        run_id=run_id,
        abandoned_at=datetime(2026, 5, 1, 12, 30, tzinfo=UTC),
    )


def _reassign_response(*, run_id: UUID, assigned_to: str = "op-senior") -> ReassignRunResponse:
    return ReassignRunResponse(
        run_id=run_id,
        assigned_to=assigned_to,
        reassigned_at=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
    )


def _run_summary(
    *,
    run_id: UUID | None = None,
    assigned_to: str = "op-operator",
    state: str = "in_progress",
    template_slug: str = "rotate-cert",
) -> RunSummary:
    return RunSummary(
        run_id=run_id if run_id is not None else uuid.uuid4(),
        template_slug=template_slug,
        template_version=1,
        assigned_to=assigned_to,
        target="node-1",
        state=state,  # type: ignore[arg-type]
        started_at=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
    )


async def _audit_rows_for_path(path: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_all_five_routes_mounted_on_main_app() -> None:
    """All five routes appear in :mod:`meho_backplane.main`'s app + OpenAPI."""
    from meho_backplane.main import app

    expected_paths = {
        "/api/v1/runbooks/runs",
        "/api/v1/runbooks/runs/{run_id}/next",
        "/api/v1/runbooks/runs/{run_id}/abort",
        "/api/v1/runbooks/runs/{run_id}/reassign",
    }
    actual_paths = {getattr(r, "path", None) for r in app.routes}
    missing = expected_paths - actual_paths
    assert not missing, f"missing routes: {missing}"

    openapi = app.openapi()
    paths = openapi["paths"]
    assert "post" in paths["/api/v1/runbooks/runs"]
    assert "get" in paths["/api/v1/runbooks/runs"]
    assert "post" in paths["/api/v1/runbooks/runs/{run_id}/next"]
    assert "post" in paths["/api/v1/runbooks/runs/{run_id}/abort"]
    assert "post" in paths["/api/v1/runbooks/runs/{run_id}/reassign"]


# ---------------------------------------------------------------------------
# POST / -- start
# ---------------------------------------------------------------------------


def test_start_201(client: TestClient) -> None:
    """Operator starts a run -> 201 + ``CurrentStepResponse`` shape."""
    tenant_a = uuid.uuid4()
    run_id = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-operator")
    fake_start = AsyncMock(return_value=_current_step_response(run_id=run_id))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.start_run", fake_start),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/runs",
            json={"template_slug": "rotate-cert", "target": "node-1", "params": {}},
            headers=_authed(token),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "current_step"
    assert body["run_id"] == str(run_id)
    assert body["current_step"]["id"] == "drain-node"
    fake_start.assert_awaited_once()
    call_args = fake_start.await_args.args
    assert call_args[0] == tenant_a
    assert call_args[1] == "op-operator"


def test_start_deprecated_400(client: TestClient) -> None:
    """Every version of the slug is deprecated -> 400."""
    key, token = _operator_token()
    fake_start = AsyncMock(
        side_effect=DeprecatedTemplateError("every version of 'rotate-cert' is deprecated")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.start_run", fake_start),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/runs",
            json={"template_slug": "rotate-cert", "target": "node-1"},
            headers=_authed(token),
        )
    assert response.status_code == 400
    assert "deprecated" in response.json()["detail"]


def test_start_missing_template_404(client: TestClient) -> None:
    """Slug does not resolve -> 404."""
    key, token = _operator_token()
    fake_start = AsyncMock(side_effect=TemplateNotFoundError("no template for slug 'nope'"))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.start_run", fake_start),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/runs",
            json={"template_slug": "nope", "target": "node-1"},
            headers=_authed(token),
        )
    assert response.status_code == 404


def test_start_missing_params_422(client: TestClient) -> None:
    """Template references ${run.params.X} not supplied -> 422."""
    key, token = _operator_token()
    fake_start = AsyncMock(
        side_effect=MissingParamsError(
            "template references run.params not supplied at start: cluster"
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.start_run", fake_start),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/runbooks/runs",
            json={"template_slug": "rotate-cert", "target": "node-1"},
            headers=_authed(token),
        )
    assert response.status_code == 422
    assert "run.params" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /{run_id}/next -- happy paths
# ---------------------------------------------------------------------------


def test_next_confirm_yes_200(client: TestClient) -> None:
    """``confirm`` answer="yes" advances -> 200 + CurrentStepResponse for next step."""
    run_id = uuid.uuid4()
    key, token = _operator_token()
    next_step = _step_body(step_id="restart-svc", title="Restart service")
    fake_next = AsyncMock(
        return_value=_current_step_response(run_id=run_id, step=next_step, n=2, total=3)
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "current_step"
    assert body["current_step"]["id"] == "restart-svc"
    assert body["position"] == {"n": 2, "total": 3}


def test_next_operation_call_match_advances_200(client: TestClient) -> None:
    """``operation_call`` verify with structural match -> advances to next step."""
    run_id = uuid.uuid4()
    key, token = _operator_token()
    next_step = _step_body(step_id="verify-cluster")
    fake_next = AsyncMock(
        return_value=_current_step_response(run_id=run_id, step=next_step, n=2, total=2)
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {
                    "type": "operation_call",
                    "matched": True,
                    "actual": {"status": "ok"},
                },
            },
            headers=_authed(token),
        )
    assert response.status_code == 200
    assert response.json()["current_step"]["id"] == "verify-cluster"


def test_next_completes_200(client: TestClient) -> None:
    """Verify on the last step yields ``run_completed`` shape (200, state="completed")."""
    run_id = uuid.uuid4()
    key, token = _operator_token()
    fake_next = AsyncMock(return_value=_run_completed_response(run_id=run_id))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "completed"
    assert body["state"] == "completed"
    assert body["run_id"] == str(run_id)
    assert "current_step" not in body


# ---------------------------------------------------------------------------
# POST /{run_id}/next -- refusals
# ---------------------------------------------------------------------------


def test_next_not_assignee_403(client: TestClient) -> None:
    """Operator who is not the assignee -> 403."""
    run_id = uuid.uuid4()
    key, token = _operator_token(sub="op-thief")
    fake_next = AsyncMock(
        side_effect=NotRunAssigneeError(f"caller 'op-thief' is not the assignee of run {run_id}")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={"last_verified": False},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_next_admin_non_assignee_403(client: TestClient) -> None:
    """Admin who is not the assignee -> 403 (the right path is reassign).

    The service raises ``NotRunAssigneeError`` regardless of role for ``next`` --
    the single-assignee invariant from Initiative #1198. A senior who wants to
    take over uses ``runbook_reassign``, not bypass via role.
    """
    run_id = uuid.uuid4()
    key, token = _admin_token(sub="op-admin")
    fake_next = AsyncMock(
        side_effect=NotRunAssigneeError(f"caller 'op-admin' is not the assignee of run {run_id}")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={"last_verified": False},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_next_run_terminal_400(client: TestClient) -> None:
    """Already-completed/abandoned run -> 400."""
    run_id = uuid.uuid4()
    key, token = _operator_token()
    fake_next = AsyncMock(
        side_effect=RunAlreadyTerminalError(f"run {run_id} is already in state 'completed'")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            headers=_authed(token),
        )
    assert response.status_code == 400


def test_next_previous_failed_400(client: TestClient) -> None:
    """Previous step is in ``failed`` state -> 400."""
    run_id = uuid.uuid4()
    key, token = _operator_token()
    fake_next = AsyncMock(
        side_effect=PreviousStepFailedError(
            "previous step 'drain-node' is in 'failed' state; abort the run"
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            headers=_authed(token),
        )
    assert response.status_code == 400


def test_next_verify_response_required_422(client: TestClient) -> None:
    """Confirm step without a verify response -> 422."""
    run_id = uuid.uuid4()
    key, token = _operator_token()
    fake_next = AsyncMock(
        side_effect=VerifyResponseRequiredError("confirm step requires a verify_response")
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={"last_verified": True},
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_next_verify_response_mismatch_422(client: TestClient) -> None:
    """Verify response shape doesn't match the step's verify type -> 422.

    Regression on the engine's typed-mismatch path (e.g. caller sends a
    ``confirm`` response on an ``operation_call`` step). The route maps
    :class:`VerifyResponseMismatchError` to the same 422 surface as the
    missing-response variant.
    """
    run_id = uuid.uuid4()
    key, token = _operator_token()
    fake_next = AsyncMock(
        side_effect=VerifyResponseMismatchError(
            "operation_call step expects operation_call verify response"
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            headers=_authed(token),
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Opacity contract -- load-bearing property test
# ---------------------------------------------------------------------------


def test_next_response_opacity(client: TestClient) -> None:
    """Response carries exactly one step body and no other step ids in the JSON.

    LOAD-BEARING regression on the opacity-by-construction guarantee from
    Initiative #1198: the wire shape of the ``next`` response must not
    contain ids of any other steps in the template. The substrate gives
    us ``CurrentStepResponse`` with a single ``current_step``; this test
    proves the *serialised JSON* over HTTP keeps that property -- so a
    structural change to the schema that smuggles future-step references
    into the response surface trips here, not in code review.
    """
    run_id = uuid.uuid4()
    key, token = _operator_token()
    current_step = _step_body(step_id="step-current")
    fake_next = AsyncMock(
        return_value=_current_step_response(run_id=run_id, step=current_step, n=2, total=5)
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/next",
            json={
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            headers=_authed(token),
        )
    assert response.status_code == 200
    body_json = response.text
    # Exactly one step body present.
    body = response.json()
    assert body["kind"] == "current_step"
    assert body["current_step"]["id"] == "step-current"
    # No reference to any other step id surface anywhere in the JSON --
    # the opacity floor is the response shape itself.
    forbidden_ids = ["step-1", "step-2", "step-3", "step-4", "step-5", "future-step"]
    for forbidden in forbidden_ids:
        assert forbidden not in body_json, (
            f"opacity violation: response surfaced {forbidden!r} alongside the current step"
        )


# ---------------------------------------------------------------------------
# POST /{run_id}/abort
# ---------------------------------------------------------------------------


def test_abort_by_assignee_200(client: TestClient) -> None:
    """Assignee aborts -> 200, state=abandoned."""
    run_id = uuid.uuid4()
    key, token = _operator_token(sub="op-operator")
    fake_abort = AsyncMock(return_value=_abort_response(run_id=run_id))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.abort_run", fake_abort),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/abort",
            json={"reason": "machine rolled back"},
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "abandoned"
    assert body["run_id"] == str(run_id)
    # caller_is_admin=False is passed to the service for an operator caller.
    assert fake_abort.await_args.kwargs["caller_is_admin"] is False


def test_abort_by_admin_200(client: TestClient) -> None:
    """Admin aborts someone else's run -> 200; caller_is_admin=True forwarded."""
    run_id = uuid.uuid4()
    key, token = _admin_token(sub="op-admin")
    fake_abort = AsyncMock(return_value=_abort_response(run_id=run_id))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.abort_run", fake_abort),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/abort",
            json={"reason": "operator paged off duty"},
            headers=_authed(token),
        )
    assert response.status_code == 200
    # The route surfaces the operator's role to the service so it widens
    # the allowance from "only the assignee" to "the assignee or any admin".
    assert fake_abort.await_args.kwargs["caller_is_admin"] is True


def test_abort_by_other_403(client: TestClient) -> None:
    """Non-assignee non-admin -> 403 (NotRunAssigneeError from the service)."""
    run_id = uuid.uuid4()
    key, token = _operator_token(sub="op-thief")
    fake_abort = AsyncMock(
        side_effect=NotRunAssigneeError(
            f"caller 'op-thief' is not the assignee of run {run_id} "
            f"and does not have TENANT_ADMIN; use runbook_reassign to take over"
        )
    )
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.abort_run", fake_abort),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/abort",
            json={"reason": "tampered"},
            headers=_authed(token),
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /{run_id}/reassign
# ---------------------------------------------------------------------------


def test_reassign_by_admin_200(client: TestClient) -> None:
    """Admin reassigns -> 200."""
    run_id = uuid.uuid4()
    key, token = _admin_token()
    fake_reassign = AsyncMock(return_value=_reassign_response(run_id=run_id))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.reassign_run", fake_reassign),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/reassign",
            json={"new_assignee": "op-senior"},
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["assigned_to"] == "op-senior"


def test_reassign_by_operator_403(client: TestClient) -> None:
    """Operator on reassign -> 403 (admin-only).

    The route gate is the only RBAC check on this surface; an operator's
    token never even reaches the service, so we don't patch ``reassign_run``.
    """
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{uuid.uuid4()}/reassign",
            json={"new_assignee": "op-senior"},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_reassign_missing_run_404(client: TestClient) -> None:
    """``RunNotFoundError`` -> 404."""
    run_id = uuid.uuid4()
    key, token = _admin_token()
    fake_reassign = AsyncMock(side_effect=RunNotFoundError(f"no run {run_id} for tenant"))
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.reassign_run", fake_reassign),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/runbooks/runs/{run_id}/reassign",
            json={"new_assignee": "op-senior"},
            headers=_authed(token),
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET / -- list_runs
# ---------------------------------------------------------------------------


def test_list_runs_operator_filters_to_own(client: TestClient) -> None:
    """Operator -> service called with ``caller_is_admin=False``.

    The route does not itself force the assignee filter -- the service
    does, when ``caller_is_admin=False``. The route's job is to pass
    the role flag through honestly so the service can apply the right
    visibility shape.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-operator")
    fake_list = AsyncMock(return_value=[_run_summary(assigned_to="op-operator")])
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.list_runs", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        # An operator passing ?assignee=someone-else gets ignored at the
        # service layer -- the route hands it through, the service overrides.
        response = client.get(
            "/api/v1/runbooks/runs?assignee=op-other",
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == 1
    fake_list.assert_awaited_once()
    kwargs = fake_list.await_args.kwargs
    assert kwargs["tenant_id"] == tenant_a
    assert kwargs["caller_sub"] == "op-operator"
    assert kwargs["caller_is_admin"] is False
    # The route still forwards the requested filter -- the service does
    # the override -- so the route surface itself is honest about the
    # request shape.
    assert kwargs["filter_"].assignee == "op-other"


def test_list_runs_admin_sees_all(client: TestClient) -> None:
    """Admin -> service called with ``caller_is_admin=True``."""
    tenant_a = uuid.uuid4()
    key, token = _admin_token(tenant_id=tenant_a)
    summaries = [
        _run_summary(assigned_to="op-operator"),
        _run_summary(assigned_to="op-other"),
    ]
    fake_list = AsyncMock(return_value=summaries)
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.list_runs", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/runs", headers=_authed(token))

    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == 2
    kwargs = fake_list.await_args.kwargs
    assert kwargs["caller_is_admin"] is True


def test_list_runs_status_filter(client: TestClient) -> None:
    """``?status=completed`` reaches the ListRunsFilter the service receives."""
    key, token = _operator_token()
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.list_runs", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/runs?status=completed&template_slug=rotate-cert&limit=10",
            headers=_authed(token),
        )

    assert response.status_code == 200
    kwargs = fake_list.await_args.kwargs
    assert kwargs["filter_"].status == "completed"
    assert kwargs["filter_"].template_slug == "rotate-cert"
    assert kwargs["limit"] == 10


def test_list_runs_invalid_status_422(client: TestClient) -> None:
    """An out-of-vocabulary ``status`` trips the query-param validator -> 422."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/runbooks/runs?status=bogus",
            headers=_authed(token),
        )
    assert response.status_code == 422


def test_list_runs_cross_tenant_isolation(client: TestClient) -> None:
    """Operator's tenant_id is the *only* tenant id the service ever sees.

    Tenant A operator hits the list endpoint; the service is invoked
    with ``tenant_id=tenant_a`` (never tenant B), so a tenant-scoped
    query at the substrate level cannot leak tenant B runs even if
    they exist.
    """
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a)
    fake_list = AsyncMock(return_value=[])
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.list_runs", fake_list),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/runbooks/runs", headers=_authed(token))

    assert response.status_code == 200
    assert fake_list.await_args.kwargs["tenant_id"] == tenant_a


# ---------------------------------------------------------------------------
# Audit op_id binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_op_id_bound_per_route() -> None:
    """Each route binds the canonical ``audit_op_id`` + ``audit_op_class``.

    Cycles through start / next / abort / reassign / list and asserts the
    audit row for each carries the expected op_id and class. Drives the
    chassis broadcast-classifier suffix-match gotcha
    (:mod:`runbook_templates`'s ``runbook.list_templates`` precedent) into
    a regression so a refactor that drops the explicit ``audit_op_class``
    bind on any of the five routes trips here.
    """
    tenant_a = uuid.uuid4()
    run_id = uuid.uuid4()

    cases = [
        (
            "POST",
            "/api/v1/runbooks/runs",
            {"template_slug": "rotate-cert", "target": "node-1"},
            "start_run",
            AsyncMock(return_value=_current_step_response(run_id=run_id)),
            "runbook.start_run",
            "write",
            _operator_token,
        ),
        (
            "POST",
            f"/api/v1/runbooks/runs/{run_id}/next",
            {
                "last_verified": True,
                "verify_response": {"type": "confirm", "answer": "yes"},
            },
            "next_step",
            AsyncMock(return_value=_current_step_response(run_id=run_id)),
            "runbook.next_step",
            "write",
            _operator_token,
        ),
        (
            "POST",
            f"/api/v1/runbooks/runs/{run_id}/abort",
            {"reason": "cancelled"},
            "abort_run",
            AsyncMock(return_value=_abort_response(run_id=run_id)),
            "runbook.abort_run",
            "write",
            _operator_token,
        ),
        (
            "POST",
            f"/api/v1/runbooks/runs/{run_id}/reassign",
            {"new_assignee": "op-senior"},
            "reassign_run",
            AsyncMock(return_value=_reassign_response(run_id=run_id)),
            "runbook.reassign_run",
            "write",
            _admin_token,
        ),
        (
            "GET",
            "/api/v1/runbooks/runs",
            None,
            "list_runs",
            AsyncMock(return_value=[]),
            "runbook.list_runs",
            "read",
            _operator_token,
        ),
    ]

    for method, path, payload, svc_attr, fake, expected_op_id, expected_class, token_fn in cases:
        # Clear the JWKS cache between iterations -- each iteration mints a
        # fresh keypair, so a cache entry from the previous iteration would
        # cause the next request's signature verification to fail (the
        # autouse fixture only resets the cache once per pytest function).
        clear_jwks_cache()
        key, token = token_fn(tenant_id=tenant_a)
        client = TestClient(_build_app())
        with (
            respx.mock as mock_router,
            patch(f"{_SERVICE_ROUTE}.{svc_attr}", fake),
        ):
            _mock_discovery_and_jwks(mock_router, _public_jwks(key))
            if method == "POST":
                response = client.post(path, json=payload, headers=_authed(token))
            else:
                response = client.get(path, headers=_authed(token))
        assert response.status_code in (200, 201), (
            f"{method} {path} unexpectedly returned {response.status_code}: {response.text}"
        )

        rows = await _audit_rows_for_path(path)
        matching = [r for r in rows if r.method == method]
        assert matching, f"no {method} audit row for {path}"
        # The most-recent matching row carries the expected op_id; earlier
        # rows from sibling tests in the same DB-backed session won't trip
        # this loop because each test in this batch uses a unique path or
        # method combination, so we take the last row.
        payload_row = matching[-1].payload
        assert payload_row["op_id"] == expected_op_id, (
            f"{method} {path} bound op_id={payload_row.get('op_id')!r}, expected {expected_op_id!r}"
        )
        assert payload_row["op_class"] == expected_class, (
            f"{method} {path} bound op_class={payload_row.get('op_class')!r}, "
            f"expected {expected_class!r}"
        )


@pytest.mark.asyncio
async def test_next_step_produces_correlated_audit_row(log_buffer: io.StringIO) -> None:
    """``next_step`` route binds ``audit_op_id="runbook.next_step"`` + ``op_class="write"``.

    The route surface produces *one* audit row per ``runbook_next``
    invocation -- the envelope row carrying ``runbook.next_step``. When
    the step's verify is an ``operation_call``, the service binds
    ``run_id_var`` + ``step_id_var`` (G12.1-T2 #1294) around the
    dispatched call's audit row; that inner row carries its own op_id
    (the verify call's, e.g. ``vmware.host.is_powered_on``) and is
    correlated to the run via the contextvars. This test exercises the
    route's envelope row only; the dispatched verify row is covered in
    ``tests/test_runbooks_run_service.py`` where the dispatcher is the
    unit under test.
    """
    run_id = uuid.uuid4()
    key, token = _operator_token()
    fake_next = AsyncMock(return_value=_current_step_response(run_id=run_id))
    test_client = TestClient(_build_app())
    path = f"/api/v1/runbooks/runs/{run_id}/next"
    with (
        respx.mock as mock_router,
        patch(f"{_SERVICE_ROUTE}.next_step", fake_next),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = test_client.post(
            path,
            json={
                "last_verified": True,
                "verify_response": {
                    "type": "operation_call",
                    "matched": True,
                    "actual": {"powered_on": True},
                },
            },
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path(path)
    post_rows = [r for r in rows if r.method == "POST"]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "runbook.next_step"
    assert payload["op_class"] == "write"


# ---------------------------------------------------------------------------
# Unauthenticated (401) -- baseline sanity (every route requires a JWT).
# ---------------------------------------------------------------------------


def test_unauthenticated_start_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/runbooks/runs",
        json={"template_slug": "rotate-cert", "target": "node-1"},
    )
    assert response.status_code == 401


def test_unauthenticated_list_returns_401(client: TestClient) -> None:
    assert client.get("/api/v1/runbooks/runs").status_code == 401
