# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the connector-ingest modal + async job-poll.

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1886 (T2). Covers the ``GET``/``POST /ui/connectors/registry/ingest``
modal + submit and the ``GET /ui/connectors/registry/ingest/jobs/{job_id}``
async job-poll surface that layers onto the T1 registry list.

Acceptance criteria on issue #1886:

* A dry-run submit renders the 200 sync-shape (parse counts) and the job
  registry gains NO job row -- nothing written
  (``test_dry_run_writes_nothing``).
* A real async submit renders a 202-shape carrying the ``job_id`` + a poll
  fragment whose root carries ``hx-trigger="every``; flipping the seeded
  job terminal re-renders WITHOUT ``hx-trigger`` (poll stops) -- the
  "stop returning the polling element" contract
  (``test_async_submit_seeds_poll_then_stops_on_terminal``).
* A poll for an unknown job_id renders the "job lost -- re-check the
  registry list" panel with NO ``hx-trigger`` (no infinite spinner) --
  the process-local-jobs footgun guard
  (``test_poll_unknown_job_renders_job_lost_no_poll``).
* A ``degraded`` job renders the counts AND the
  ``ingested_not_dispatchable`` reason
  (``test_degraded_job_shows_counts_and_reason``); a
  ``catalog_entry_not_found`` 422 renders an inline panel listing
  ``available_entries[]`` (``test_catalog_entry_not_found_panel``); a
  ``503 LlmClientUnavailable`` renders an actionable panel, not a 500
  (``test_llm_unavailable_renders_panel``).
* The ingest submit + the poll route require TENANT_ADMIN (403 for an
  operator) (``test_ingest_routes_require_tenant_admin``) and the submit
  requires CSRF double-submit (``test_ingest_submit_requires_csrf``).
* A body that sets BOTH ``catalog_entry`` and a quadruple field is
  rejected with a friendly inline error, not a 500
  (``test_both_shapes_rejected_with_friendly_error``).
* The new literal ``/ui/connectors/registry/ingest*`` routes register
  before ``/ui/connectors/{name}`` AND before the
  ``/ui/connectors/registry/{connector_id}`` param routes
  (``test_ingest_routes_registered_before_detail_and_param``).

Harness shape mirrors :mod:`backend.tests.test_ui_connectors_registry_list`
(a real Keycloak-minted access token so ``resolve_operator_or_403``
re-verifies the role). The ingest pipeline is never actually run: the
in-process ``ingest_endpoint`` is patched (``IngestionPipelineService``
seam) so the dry-run / async / error paths are driven deterministically,
and the async job-poll fragments read jobs seeded directly into the
process-local ``IngestJobRegistry``.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.operations.ingest import (
    IngestJobHandle,
    get_job_registry,
    reset_job_registry_for_tests,
)
from meho_backplane.operations.ingest.api_schemas import IngestionResultModel, IngestResponse
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
from meho_backplane.ui.routes.connectors import build_router as build_connectors_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"

#: The in-process ingest BFF imports ``ingest_endpoint`` into its own
#: module namespace, so patches target THAT binding (not the api module).
_INGEST_ENDPOINT = "meho_backplane.ui.routes.connectors.ingest_modal.ingest_endpoint"
_CATALOG_ENDPOINT = "meho_backplane.ui.routes.connectors.ingest_modal.catalog_endpoint"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the registry suite)."""
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
    reset_job_registry_for_tests()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    reset_job_registry_for_tests()


def _build_app() -> FastAPI:
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


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        from meho_backplane.db.engine import get_sessionmaker

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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-connectors-ingest-test-kid")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + csrf token for the role-gated routes."""
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id,
        access_token=access_token,
        operator_sub=operator_sub,
    )
    clear_jwks_cache()
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _form_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


def _ingestion_model(*, inserted: int = 7) -> IngestionResultModel:
    return IngestionResultModel(
        connector_id="vmware-rest-9.0",
        inserted_count=inserted,
        updated_count=0,
        skipped_count=1,
        connector_registered=False,
        operations_grouped=False,
    )


def _sync_ingest_response(inserted: int = 7) -> JSONResponse:
    """The 200 sync ``IngestResponse`` body ``ingest_endpoint`` returns for dry-run."""
    body = IngestResponse(ingestion=_ingestion_model(inserted=inserted), grouping=None)
    return JSONResponse(content=body.model_dump(mode="json"), status_code=200)


def _async_ingest_response(job_id: uuid.UUID) -> JSONResponse:
    """The 202 ``IngestJobHandle`` body ``ingest_endpoint`` returns for a real ingest."""
    handle = IngestJobHandle(
        job_id=job_id,
        status="running",
        poll_url=f"/api/v1/connectors/ingest/jobs/{job_id}",
    )
    return JSONResponse(content=handle.model_dump(mode="json"), status_code=202)


def _seed_job(
    *,
    tenant_id: uuid.UUID | None,
    operator_sub: str,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> uuid.UUID:
    """Seed a ``running`` job into the process-local registry; return its id."""

    async def _do() -> uuid.UUID:
        registry = get_job_registry()
        job = await registry.create(
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            catalog_entry=None,
            product=product,
            version=version,
            impl_id=impl_id,
            spec_uris=["https://vendor.example/openapi.yaml"],
        )
        return job.job_id

    return asyncio.run(_do())


def _job_count() -> int:
    """Number of rows in the process-local registry (the "nothing written" probe)."""

    async def _do() -> int:
        registry = get_job_registry()
        async with registry._lock:
            return len(registry._jobs)

    return asyncio.run(_do())


# ---------------------------------------------------------------------------
# AC: route ordering -- ingest routes precede {name} + {connector_id}
# ---------------------------------------------------------------------------


def test_ingest_routes_registered_before_detail_and_param() -> None:
    """First-match-wins: the literal ingest routes precede the param routes."""
    router = build_connectors_router()

    def _index(path: str, method: str) -> int:
        for i, route in enumerate(router.routes):
            if route.path == path and method in (route.methods or set()):
                return i
        raise AssertionError(f"route not found: {method} {path}")

    ingest_modal_i = _index("/ui/connectors/registry/ingest", "GET")
    ingest_submit_i = _index("/ui/connectors/registry/ingest", "POST")
    ingest_job_i = _index("/ui/connectors/registry/ingest/jobs/{job_id}", "GET")
    detail_i = _index("/ui/connectors/{name}", "GET")
    param_enable_i = _index("/ui/connectors/registry/{connector_id}/enable", "GET")
    param_delete_i = _index("/ui/connectors/registry/{connector_id}", "DELETE")

    for ingest_i in (ingest_modal_i, ingest_submit_i, ingest_job_i):
        assert ingest_i < detail_i, (
            "the literal /ui/connectors/registry/ingest* routes must register before "
            "the /ui/connectors/{name} detail catch-all (first-match-wins)"
        )
        assert ingest_i < param_enable_i, (
            "the literal /registry/ingest must register before /registry/{connector_id}/enable"
        )
        assert ingest_i < param_delete_i, (
            "the literal /registry/ingest must register before the DELETE /registry/{connector_id}"
        )


# ---------------------------------------------------------------------------
# AC: dry-run renders parse counts and writes NO job row
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing() -> None:
    """A dry-run submit renders the 200 sync counts; the job registry stays empty."""
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    ingest_mock = AsyncMock(return_value=_sync_ingest_response(inserted=7))
    try:
        with patch(_INGEST_ENDPOINT, ingest_mock):
            resp = client.post(
                "/ui/connectors/registry/ingest",
                data={
                    "mode": "explicit",
                    "product": "vmware",
                    "version": "9.0",
                    "impl_id": "vmware-rest",
                    "spec_uri": ["https://vendor.example/openapi.yaml"],
                    "dry_run": "true",
                },
                headers=_form_headers(csrf),
            )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    # The dry-run parse-counts render, explicitly "nothing written".
    assert "data-ingest-dry-run" in body
    assert "nothing was written" in body.lower()
    assert "7 would insert" in body
    # The in-process call ran with dry_run=True.
    sent_body = ingest_mock.await_args.kwargs["body"]
    assert sent_body.dry_run is True
    # NOTHING was written: the dry-run path never touches the job registry.
    assert _job_count() == 0


# ---------------------------------------------------------------------------
# AC: real async submit seeds a self-polling fragment that stops when terminal
# ---------------------------------------------------------------------------


def test_async_submit_seeds_poll_then_stops_on_terminal() -> None:
    """A real submit seeds a polling fragment with the job_id; terminal stops the poll."""
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    # Seed a real running job so the follow-up poll reads it, and have the
    # patched endpoint hand back its id in the 202 handle.
    job_id = _seed_job(tenant_id=_TENANT_A, operator_sub=_OP_ADMIN)
    ingest_mock = AsyncMock(return_value=_async_ingest_response(job_id))
    try:
        with patch(_INGEST_ENDPOINT, ingest_mock):
            submit = client.post(
                "/ui/connectors/registry/ingest",
                data={
                    "mode": "explicit",
                    "product": "vmware",
                    "version": "9.0",
                    "impl_id": "vmware-rest",
                    "spec_uri": ["https://vendor.example/openapi.yaml"],
                },
                headers=_form_headers(csrf),
            )
        # While running, the poll fragment self-polls.
        running = client.get(
            f"/ui/connectors/registry/ingest/jobs/{job_id}",
            headers={"HX-Request": "true"},
        )
        # Flip the seeded job terminal, then poll again.
        asyncio.run(
            get_job_registry().fail(job_id, error=RuntimeError("boom")),
        )
        terminal = client.get(
            f"/ui/connectors/registry/ingest/jobs/{job_id}",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    # The submit's 202-shape render seeds the poll fragment with the job id.
    assert submit.status_code == 200, submit.text
    assert str(job_id) in submit.text
    assert 'hx-trigger="every' in submit.text
    assert f'id="ingest-job-{job_id}"' in submit.text

    # The running poll keeps the self-poll directive.
    assert running.status_code == 200, running.text
    assert 'hx-trigger="every' in running.text

    # The terminal poll DROPS the directive -- "stop returning the polling element".
    assert terminal.status_code == 200, terminal.text
    assert "hx-trigger" not in terminal.text
    assert "data-job-failed" in terminal.text


# ---------------------------------------------------------------------------
# AC: poll for an unknown job renders the "job lost" panel, no poll
# ---------------------------------------------------------------------------


def test_poll_unknown_job_renders_job_lost_no_poll() -> None:
    """An unknown job_id renders the job-lost panel with NO hx-trigger (no spinner)."""
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    unknown = uuid.uuid4()
    try:
        resp = client.get(
            f"/ui/connectors/registry/ingest/jobs/{unknown}",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "data-job-lost" in body
    assert "re-check" in body.lower()
    assert "registry" in body.lower()
    # No infinite spinner: the fragment carries no poll directive.
    assert "hx-trigger" not in body


def test_poll_non_uuid_job_id_renders_job_lost() -> None:
    """A non-UUID job_id segment renders the job-lost panel, not a raw 422."""
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.get(
            "/ui/connectors/registry/ingest/jobs/not-a-uuid",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    assert "data-job-lost" in resp.text
    assert "hx-trigger" not in resp.text


# ---------------------------------------------------------------------------
# AC: degraded job shows counts AND the ingested_not_dispatchable reason
# ---------------------------------------------------------------------------


def test_degraded_job_shows_counts_and_reason() -> None:
    """A degraded poll shows the ingestion counts AND the non-dispatchable reason."""
    from meho_backplane.operations.ingest.pipeline import IngestionPipelineResult
    from meho_backplane.operations.ingest.register_ingested import IngestionResult

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    job_id = _seed_job(tenant_id=_TENANT_A, operator_sub=_OP_ADMIN)
    result = IngestionPipelineResult(
        connector_id="vmware-rest-9.0",
        ingestion=IngestionResult(
            inserted_count=5,
            updated_count=0,
            skipped_count=2,
            connector_registered=True,
            operations_grouped=True,
        ),
        grouping=None,
    )
    asyncio.run(
        get_job_registry().degrade(
            job_id,
            result=result,
            error_class="ingested_not_dispatchable",
            error="ingested rows resolve to nothing the dispatcher can route",
        )
    )
    try:
        resp = client.get(
            f"/ui/connectors/registry/ingest/jobs/{job_id}",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "data-job-degraded" in body
    # NOT a bare success: both the counts AND the reason render.
    assert "5 inserted" in body
    assert "ingested_not_dispatchable" in body
    assert "nothing the dispatcher can route" in body
    # Terminal: no self-poll.
    assert "hx-trigger" not in body


def test_succeeded_job_shows_counts_and_registry_link() -> None:
    """A succeeded poll shows the inserted counts + a link back to the registry list."""
    from meho_backplane.operations.ingest.pipeline import IngestionPipelineResult
    from meho_backplane.operations.ingest.register_ingested import IngestionResult

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    job_id = _seed_job(tenant_id=_TENANT_A, operator_sub=_OP_ADMIN)
    result = IngestionPipelineResult(
        connector_id="vmware-rest-9.0",
        ingestion=IngestionResult(
            inserted_count=9,
            updated_count=0,
            skipped_count=0,
            connector_registered=True,
            operations_grouped=True,
        ),
        grouping=None,
    )
    asyncio.run(get_job_registry().complete(job_id, result=result))
    try:
        resp = client.get(
            f"/ui/connectors/registry/ingest/jobs/{job_id}",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "data-job-succeeded" in body
    assert "9 inserted" in body
    assert 'href="/ui/connectors/registry"' in body
    # Terminal: no self-poll.
    assert "hx-trigger" not in body


# ---------------------------------------------------------------------------
# AC: catalog_entry_not_found 422 panel listing available_entries[]
# ---------------------------------------------------------------------------


def test_catalog_entry_not_found_panel() -> None:
    """A catalog_entry_not_found 422 renders an inline panel listing available entries."""
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    not_found = HTTPException(
        status_code=422,
        detail={
            "detail": "catalog_entry_not_found",
            "catalog_entry": "bogus/1.0",
            "available_entries": ["vmware/9.0", "github/v3"],
            "message": "catalog_entry_not_found: 'bogus/1.0' is not in the catalog.",
        },
    )
    ingest_mock = AsyncMock(side_effect=not_found)
    try:
        with patch(_INGEST_ENDPOINT, ingest_mock):
            resp = client.post(
                "/ui/connectors/registry/ingest",
                data={"mode": "catalog", "catalog_entry": "bogus/1.0"},
                headers=_form_headers(csrf),
            )
    finally:
        mock.stop()

    assert resp.status_code == 422, resp.text
    body = resp.text
    # Inline panel, not a 500 / stack trace.
    assert "data-registry-error" in body
    assert "Traceback" not in body
    # The available_entries[] are enumerated for the operator.
    assert "data-available-entries" in body
    assert "vmware/9.0" in body
    assert "github/v3" in body


# ---------------------------------------------------------------------------
# AC: 503 LlmClientUnavailable renders an actionable panel (not a 500)
# ---------------------------------------------------------------------------


def test_llm_unavailable_renders_panel() -> None:
    """A 503 LlmClientUnavailable renders an actionable inline panel, not a 500."""
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    llm_down = HTTPException(status_code=503, detail="LlmClientUnavailable: no key configured")
    ingest_mock = AsyncMock(side_effect=llm_down)
    try:
        with patch(_INGEST_ENDPOINT, ingest_mock):
            resp = client.post(
                "/ui/connectors/registry/ingest",
                data={
                    "mode": "explicit",
                    "product": "vmware",
                    "version": "9.0",
                    "impl_id": "vmware-rest",
                    "spec_uri": ["https://vendor.example/openapi.yaml"],
                },
                headers=_form_headers(csrf),
            )
    finally:
        mock.stop()

    assert resp.status_code == 503, resp.text
    body = resp.text
    assert "data-registry-error" in body
    assert "Traceback" not in body
    assert "unavailable" in body.lower()


# ---------------------------------------------------------------------------
# AC: a body setting BOTH shapes is rejected with a friendly error, not a 500
# ---------------------------------------------------------------------------


def test_both_shapes_rejected_with_friendly_error() -> None:
    """A body mixing catalog_entry + a quadruple field renders a friendly inline error."""
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    # The endpoint must NEVER be reached -- the pre-check rejects first.
    ingest_mock = AsyncMock(side_effect=AssertionError("ingest_endpoint must not be called"))
    try:
        with patch(_INGEST_ENDPOINT, ingest_mock):
            resp = client.post(
                "/ui/connectors/registry/ingest",
                data={
                    "mode": "catalog",
                    "catalog_entry": "vmware/9.0",
                    # Quadruple contamination on the catalog tab.
                    "product": "vmware",
                },
                headers=_form_headers(csrf),
            )
    finally:
        mock.stop()

    assert resp.status_code == 422, resp.text
    body = resp.text
    assert "data-registry-error" in body
    assert "Traceback" not in body
    assert "never both" in body.lower()
    ingest_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC: the ingest submit + poll require TENANT_ADMIN (403 for an operator)
# ---------------------------------------------------------------------------


def test_ingest_routes_require_tenant_admin() -> None:
    """A plain operator is 403'd on the modal, the submit, and the poll route."""
    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    job_id = _seed_job(tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR)
    # The endpoint must never be reached -- the RBAC gate rejects first.
    ingest_mock = AsyncMock(side_effect=AssertionError("must not reach ingest_endpoint"))
    catalog_mock = AsyncMock(side_effect=AssertionError("must not reach catalog_endpoint"))
    try:
        with patch(_INGEST_ENDPOINT, ingest_mock), patch(_CATALOG_ENDPOINT, catalog_mock):
            modal_403 = client.get("/ui/connectors/registry/ingest")
            submit_403 = client.post(
                "/ui/connectors/registry/ingest",
                data={"mode": "catalog", "catalog_entry": "vmware/9.0"},
                headers=_form_headers(csrf),
            )
            poll_403 = client.get(
                f"/ui/connectors/registry/ingest/jobs/{job_id}",
                headers={"HX-Request": "true"},
            )
    finally:
        mock.stop()

    assert modal_403.status_code == 403, modal_403.text
    assert submit_403.status_code == 403, submit_403.text
    assert poll_403.status_code == 403, poll_403.text


# ---------------------------------------------------------------------------
# AC: the submit requires the CSRF double-submit token
# ---------------------------------------------------------------------------


def test_ingest_submit_requires_csrf() -> None:
    """A submit POST without X-CSRF-Token is rejected by CSRFMiddleware (403)."""
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # No X-CSRF-Token header -> middleware 403 before the route (or its
        # RBAC gate) even runs.
        no_csrf = client.post(
            "/ui/connectors/registry/ingest",
            data={"mode": "catalog", "catalog_entry": "vmware/9.0"},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert no_csrf.status_code == 403, no_csrf.text
    assert no_csrf.headers.get("x-csrf-rejection-reason") is not None


# ---------------------------------------------------------------------------
# Modal render: the GET serves the two-mode dialog with a fresh CSRF cookie
# ---------------------------------------------------------------------------


def test_ingest_modal_renders_for_admin() -> None:
    """The admin ingest modal loads with both modes + a re-minted CSRF cookie.

    Uses the real in-process ``catalog_endpoint`` (the packaged catalog
    carries ``vmware/9.0``) so the production modal-GET path -- including
    the real catalog read + projection -- is exercised end to end.
    """
    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        resp = client.get("/ui/connectors/registry/ingest")
    finally:
        mock.stop()

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'class="modal"' in body
    assert 'hx-post="/ui/connectors/registry/ingest"' in body
    # Both modes are present.
    assert 'data-mode-tab="catalog"' in body
    assert 'data-mode-tab="explicit"' in body
    # The catalog dropdown carries a real packaged catalog ref.
    assert "vmware/9.0" in body
    # A dry-run toggle + the spec-uri repeater render.
    assert "data-dry-run-toggle" in body
    assert "data-spec-uris" in body
    # The modal re-set the CSRF cookie so the submit's double-submit lines up.
    assert CSRF_COOKIE_NAME in resp.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Entry-point soft-hide: the "Ingest" button is admin-only on the T1 list
# ---------------------------------------------------------------------------


def test_ingest_entry_point_soft_hidden_from_non_admin() -> None:
    """The registry list shows the Ingest button to an admin, hides it from an operator."""
    admin_client, admin_mock, _ac = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        admin_list = admin_client.get("/ui/connectors/registry")
    finally:
        admin_mock.stop()

    op_client, op_mock, _oc = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        op_list = op_client.get("/ui/connectors/registry")
    finally:
        op_mock.stop()

    assert admin_list.status_code == 200, admin_list.text
    assert 'data-action="open-ingest"' in admin_list.text
    assert 'hx-get="/ui/connectors/registry/ingest"' in admin_list.text

    assert op_list.status_code == 200, op_list.text
    # Soft-hide: the operator never sees the Ingest entry point.
    assert 'data-action="open-ingest"' not in op_list.text
