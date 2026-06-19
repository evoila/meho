# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Operations launcher UI surface.

Initiative #1835 (G10.9 Operations console), Task #1879 (G10.9-T1). The
acceptance criteria on issue #1879 are:

* ``GET /ui/operations`` lists connectors by their ``<impl_id>-<version>``
  id; the bare product slug (``vault``) is NOT emitted as a selectable
  picker value.
* ``GET /ui/operations/search?connector_id=<id>&q=<term>`` returns ranked
  hits carrying ``safety_level`` + ``requires_approval`` badges; an unknown
  ``connector_id`` renders the 404 / ``next_step`` ingest hint, not an
  empty 200.
* ``GET /ui/operations/descriptor/{id}`` as a plain operator renders the
  drawer WITHOUT any ``llm_instructions`` content; as tenant_admin the
  ``llm_instructions`` block IS present.
* ``build_operations_router`` is included before ``build_stubs_router()``;
  ``/ui/operations/search`` resolves to the search handler, not a
  ``{param}`` route.

Suite shape mirrors :mod:`backend.tests.test_ui_connectors_view`: a minimal
FastAPI app with the UI session + CSRF middlewares, a ``web_session`` row
carrying a real Keycloak-minted access token (so the operator lift + role
probe re-verify the token and pick up the right :class:`TenantRole`), and
seeded ``operation_group`` / ``endpoint_descriptor`` rows.
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
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
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
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP_OPERATOR = "op-operator"
_OP_ADMIN = "op-admin"

#: The per-op agent prompt text the operator-render must never carry. Used
#: as the substring grep both ways in the RBAC drawer tests.
_LLM_PROMPT_TEXT = "SECRET-AGENT-PROMPT-do-not-leak"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the UI suites)."""
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
    clear_registry()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    clear_registry()
    reset_dispatcher_caches()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the operations UI tests."""
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
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_group_and_op(
    *,
    tenant_id: uuid.UUID | None,
    product: str,
    version: str,
    impl_id: str,
    group_key: str,
    group_name: str,
    op_id: str,
    summary: str = "test op",
    safety_level: str = "safe",
    requires_approval: bool = False,
    llm_instructions: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one enabled ``operation_group`` + one ``endpoint_descriptor`` row."""
    group_id = uuid.uuid4()
    desc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    group_key=group_key,
                    name=group_name,
                    when_to_use="when you need the test op",
                    review_status="enabled",
                ),
            )
            session.add(
                EndpointDescriptor(
                    id=desc_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    op_id=op_id,
                    source_kind="typed",
                    method="GET",
                    path="/v1/secret",
                    summary=summary,
                    description="reads a secret value",
                    group_id=group_id,
                    parameter_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                    llm_instructions=llm_instructions,
                    safety_level=safety_level,
                    requires_approval=requires_approval,
                    is_enabled=True,
                ),
            )

    asyncio.run(_do())
    return group_id, desc_id


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
) -> uuid.UUID:
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
    # Unique kid per call so a JWKS cached in a prior test (keyed by the
    # constant JWKS URL) cannot shadow this token's signing key -- the
    # ``jws_signature_mismatch`` failure mode when every test reuses one kid.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair(f"ui-operations-test-kid-{uuid.uuid4().hex[:8]}")
    return keypair, _public_jwks(keypair)


def _client_with_role(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient + respx mock for the role-gated operations routes.

    The operator lift + role probe re-validate the BFF session's access
    token through the JWT chain; the chain needs the JWKS endpoint mocked.
    The caller enters ``mock`` as a context manager.
    """
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
    # Return the mock UN-started: the caller enters it via ``with mock:``,
    # which starts AND stops it exactly once. Starting here as well would
    # leave the global httpx patch active after the test's ``with`` block
    # exits (start-twice / stop-once), so the NEXT test's JWKS fetch would
    # be intercepted by this test's stale routes -> ``invalid_token`` 401.
    mock = respx.mock(assert_all_called=False)
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


def _client_with_role_and_csrf(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + respx mock + a minted CSRF token for a POST.

    Mirrors :func:`_client_with_role` but also sets the ``meho_csrf``
    double-submit cookie and returns the matching token so a state-changing
    ``POST /ui/operations/preview`` can echo it via ``X-CSRF-Token``. A
    request omitting the header gets a 403 from the CSRF middleware.
    """
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
    mock = respx.mock(assert_all_called=False)
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF echo + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Picker: connector_id shape footgun
# ---------------------------------------------------------------------------


def test_operations_ui_picker_lists_connector_id_not_product_slug() -> None:
    """``GET /ui/operations`` picker emits ``<impl_id>-<version>``, not the slug."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Vault: connector_id is ``vault-1.x``; the bare slug ``vault`` is the
    # footgun value an operator reaches for and must NOT be selectable.
    _seed_group_and_op(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="secrets",
        group_name="Secrets",
        op_id="vault.kv.read",
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/operations")
    assert response.status_code == 200
    body = response.text
    # The full connector_id is a selectable option value.
    assert 'value="vault-1.x"' in body
    # The bare product slug must NOT appear as a selectable option value.
    assert 'value="vault"' not in body


# ---------------------------------------------------------------------------
# Search: ranked hits + unknown-connector / not-ingested hint
# ---------------------------------------------------------------------------


def test_operations_ui_search_returns_ranked_hits_with_badges() -> None:
    """Search returns hits carrying safety_level + requires_approval badges."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_group_and_op(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="secrets",
        group_name="Secrets",
        op_id="vault.kv.read",
        summary="read a secret value",
        safety_level="caution",
        requires_approval=True,
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(
            "/ui/operations/search", params={"connector_id": "vault-1.x", "q": "secret"}
        )
    assert response.status_code == 200
    body = response.text
    assert "vault.kv.read" in body
    # safety_level badge text + the requires_approval badge.
    assert "caution" in body
    assert "approval" in body
    # The drawer link target must be a descriptor UUID, not the op_id.
    assert "/ui/operations/descriptor/" in body


def test_operations_ui_search_unknown_connector_renders_hint_not_empty_200() -> None:
    """An unknown ``connector_id`` renders the 404-class hint, not an empty 200."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        # ``no-such-connector-9.0`` parses to a triple with no rows + no
        # registered class -> UnknownConnectorError -> the unknown-connector
        # panel (NOT a silent empty results region).
        response = client.get(
            "/ui/operations/search",
            params={"connector_id": "no-such-connector-9.0", "q": "anything"},
        )
    assert response.status_code == 200
    body = response.text
    assert 'data-ops-error="unknown_connector"' in body


# ---------------------------------------------------------------------------
# Drawer RBAC: the same-template-both-roles trap
# ---------------------------------------------------------------------------


def test_operations_ui_drawer_operator_has_no_llm_instructions() -> None:
    """A plain operator's drawer render carries NO ``llm_instructions`` text."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, desc_id = _seed_group_and_op(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="secrets",
        group_name="Secrets",
        op_id="vault.kv.read",
        llm_instructions={"system": _LLM_PROMPT_TEXT},
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/operations/descriptor/{desc_id}")
    assert response.status_code == 200
    body = response.text
    # Operator-safe fields render.
    assert "vault.kv.read" in body
    # The per-op agent prompt must be ABSENT from the operator render.
    assert _LLM_PROMPT_TEXT not in body
    assert 'data-llm-instructions="true"' not in body


def test_operations_ui_drawer_tenant_admin_has_llm_instructions() -> None:
    """A tenant_admin's drawer render DOES carry the ``llm_instructions`` block."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, desc_id = _seed_group_and_op(
        tenant_id=None,
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="secrets",
        group_name="Secrets",
        op_id="vault.kv.read",
        llm_instructions={"system": _LLM_PROMPT_TEXT},
    )
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_ADMIN, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        response = client.get(f"/ui/operations/descriptor/{desc_id}")
    assert response.status_code == 200
    body = response.text
    assert _LLM_PROMPT_TEXT in body
    assert 'data-llm-instructions="true"' in body


# ---------------------------------------------------------------------------
# Route ordering: literal ``search`` resolves to the search handler
# ---------------------------------------------------------------------------


def test_operations_ui_route_ordering_search_not_param() -> None:
    """``/ui/operations/search`` resolves to the search handler, not a {param}.

    The only ``{param}`` route is ``/ui/operations/descriptor/{id}``; the
    literal ``search`` segment must resolve to ``operations_search``. With
    no connector_id the search handler renders the "select a connector"
    prompt -- a 200, not a descriptor-id 422/404.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get("/ui/operations/search")
    assert response.status_code == 200
    # The search partial's empty-connector branch, not a descriptor 404/422.
    assert "Select a connector" in response.text


# ---------------------------------------------------------------------------
# Preview panel (Task #1880, T2): the read-only would-be-request action
# ---------------------------------------------------------------------------


class _RecordingHttpConnector(HttpConnector):
    """Connector whose transport records calls instead of sending.

    The preview path must resolve the literal request WITHOUT touching the
    HTTP transport; a non-empty ``calls`` list after a preview is the "it
    dispatched" failure signal. Mirrors the sibling
    ``test_operations_request_preview`` recording connector.
    """

    product = "gh"
    version = "3"
    impl_id = "gh-rest"
    supported_version_range = ">=3,<4"
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"verb": method, "path": path, "params": params, "json": json})
        return {"sent": True}

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"verb": "POST", "path": path, "json": json})
        return {"sent": True}

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


def _register_recording_gh_connector() -> _RecordingHttpConnector:
    """Register :class:`_RecordingHttpConnector` under ``gh-rest-3``."""
    register_connector_v2(product="gh", version="3", impl_id="gh-rest", cls=_RecordingHttpConnector)
    connector = get_or_create_connector_instance(_RecordingHttpConnector)
    assert isinstance(connector, _RecordingHttpConnector)
    return connector


def _seed_ingested_op(
    *,
    tenant_id: uuid.UUID | None,
    op_id: str = "POST:/repos/{owner}/{repo}/issues",
    method: str = "POST",
    path: str = "/repos/{owner}/{repo}/issues",
    parameter_schema: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Seed one enabled ``source_kind='ingested'`` gh-rest descriptor row.

    Returns the descriptor id (so the drawer + preview form can be exercised
    end to end through the BFF). The default schema models a gh-rest
    issue-create -- ``owner`` / ``repo`` path params + a ``body`` container.
    """
    if parameter_schema is None:
        parameter_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "x-meho-param-loc": "path"},
                "repo": {"type": "string", "x-meho-param-loc": "path"},
                "body": {"type": "object", "x-meho-param-loc": "body"},
            },
        }
    desc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                EndpointDescriptor(
                    id=desc_id,
                    tenant_id=tenant_id,
                    product="gh",
                    version="3",
                    impl_id="gh-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    summary="Create issue.",
                    description="Ingested write test op.",
                    parameter_schema=parameter_schema,
                    llm_instructions=None,
                    safety_level="caution",
                    requires_approval=False,
                    is_enabled=True,
                )
            )

    asyncio.run(_do())
    return desc_id


def _seed_gh_target(*, tenant_id: uuid.UUID, name: str = "gh-prod") -> None:
    """Seed a gh target row ``resolve_target`` can find by name."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TargetORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product="gh",
                    version="3",
                    host="api.github.com",
                    port=443,
                    fqdn=None,
                    secret_ref=None,
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={},
                    notes=None,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    asyncio.run(_do())


def _count_audit_rows() -> int:
    """Return the total ``AuditLog`` row count (the no-dispatch assertion)."""

    async def _do() -> int:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(select(func.count()).select_from(AuditLog))
            return int(result.scalar_one())

    return asyncio.run(_do())


def test_operations_ui_preview_renders_literal_request_no_audit_row() -> None:
    """A valid ingested-op preview renders method + path + redacted body, no audit row."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    _seed_gh_target(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    audit_before = _count_audit_rows()
    with mock:
        response = client.post(
            "/ui/operations/preview",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": "gh-prod",
                "params": '{"owner": "evoila", "repo": "meho", "body": {"title": "diagnose me"}}',
            },
        )
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-preview-status="ok"' in body
    # The literal request line: method + the substituted path.
    assert "POST" in body
    assert "/repos/evoila/meho/issues" in body
    # The redacted-body block + the masked note are present.
    assert "data-preview-body" in body
    assert "diagnose me" in body
    assert "redacted" in body
    # The preview wrote no audit row (it never dispatched).
    assert _count_audit_rows() == audit_before


def test_operations_ui_preview_invalid_params_renders_in_envelope_error() -> None:
    """Params failing the schema render the in-envelope status=error inline (HTTP 200)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(
        tenant_id=None,
        parameter_schema={
            "type": "object",
            "properties": {"owner": {"type": "string", "x-meho-param-loc": "path"}},
            "required": ["owner"],
            "additionalProperties": False,
        },
    )
    _seed_gh_target(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/preview",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": "gh-prod",
                "params": '{"unexpected": "field"}',
            },
        )
    # An operator-input fault is INSIDE the envelope -- HTTP 200, not 4xx.
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-preview-status="error"' in body
    assert "invalid_params" in body


def test_operations_ui_preview_missing_target_name_is_inline_400() -> None:
    """A blank target surfaces the meta-tool's missing-target-name 400 inline.

    The route forwards the whitespace-stripped target verbatim, so a blank /
    whitespace-only field strips to the empty string the meta-tool rejects
    with the ValueError the REST route maps to a hard 400 (the
    ``{"target": {}}``-without-name contract). The BFF renders it as an
    inline form error instead of tearing the operator out of the drawer.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/preview",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                # Whitespace-only target -> strips to "" -> the meta-tool's
                # missing-target-name ValueError -> the REST route's hard 400.
                "target": "   ",
                "params": "{}",
            },
        )
    assert response.status_code == 400, response.text
    body = response.text
    assert 'data-preview-status="form_error"' in body
    assert "name" in body


def test_operations_ui_preview_malformed_params_json_is_inline_400() -> None:
    """A malformed ``params`` JSON renders an inline 400 form error, not a 422/500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    _seed_gh_target(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/preview",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": "gh-prod",
                "params": "{not valid json",
            },
        )
    assert response.status_code == 400, response.text
    assert 'data-preview-status="form_error"' in response.text


def test_operations_ui_preview_without_csrf_token_is_403() -> None:
    """A preview POST omitting the CSRF header is rejected by the middleware (403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    _seed_gh_target(tenant_id=_TENANT_A)
    client, mock, _csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/preview",
            # No X-CSRF-Token header -> the double-submit pair is incomplete.
            headers={"HX-Request": "true"},
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": "gh-prod",
                "params": "{}",
            },
        )
    assert response.status_code == 403, response.text


def test_operations_ui_preview_operator_role_can_preview() -> None:
    """RBAC: a plain operator session can preview (no tenant_admin required)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    _seed_gh_target(tenant_id=_TENANT_A)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/preview",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": "gh-prod",
                "params": '{"owner": "evoila", "repo": "meho", "body": {"title": "ok"}}',
            },
        )
    # Operator (not tenant_admin) renders the ok preview -- no 403.
    assert response.status_code == 200, response.text
    assert 'data-preview-status="ok"' in response.text


def test_operations_ui_drawer_renders_preview_form_for_ingested_op() -> None:
    """The detail drawer carries the preview form for an HTTP-ingested op."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    desc_id = _seed_ingested_op(tenant_id=None)
    client, mock = _client_with_role(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.get(f"/ui/operations/descriptor/{desc_id}")
    assert response.status_code == 200, response.text
    body = response.text
    # The preview form posts to the preview route and seeds the connector_id.
    assert 'hx-post="/ui/operations/preview"' in body
    assert 'value="gh-rest-3"' in body
    assert 'id="operations-preview-region"' in body
    # The drawer GET re-set the CSRF cookie so the form's double-submit lines up.
    assert CSRF_COOKIE_NAME in response.cookies
