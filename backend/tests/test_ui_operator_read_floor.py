# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console read pages have the same role floor as their REST twins.

Each console page in :data:`_OPERATOR_FLOOR_PAGES` reads data whose REST
twin needs the ``operator`` role. The tests check, with real signed
access tokens (no role helper):

* the REST twin needs ``operator`` (read from its ``require_role``
  dependency), and the console page declares ``require_ui_operator``;
* a walk over every console ``GET`` route, plus the listed ``POST`` reads,
  with a ``read_only`` session: each one answers ``403``, except the pages
  in :data:`_READ_ONLY_PAGES`, whose REST twins are open to ``read_only``;
* each listed page answers ``403 operator_required`` to ``read_only``;
* an ``operator`` and a ``tenant_admin`` session pass the check and see
  the pages.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.middleware import require_ui_operator
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _ISSUER
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._route_tree_helpers import iter_routes

_TENANT = uuid.UUID("44444444-4444-4444-4444-444444444444")
_SUB = "op-floor-check"

#: ``(console method, console path, REST method, REST path)``. The REST twin
#: serves the same data and needs the ``operator`` role.
_OPERATOR_FLOOR_PAGES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/ui/first-start", "POST", "/api/v1/targets/{name}/probe"),
    ("GET", "/ui/event-sources", "GET", "/api/v1/event-sources"),
    ("GET", "/ui/conventions", "GET", "/api/v1/conventions"),
    ("GET", "/ui/conventions/{slug}", "GET", "/api/v1/conventions/{slug}"),
    ("GET", "/ui/conventions/{slug}/history", "GET", "/api/v1/conventions/{slug}/history"),
    ("GET", "/ui/kb", "GET", "/api/v1/kb"),
    ("POST", "/ui/kb/search", "GET", "/api/v1/kb"),
    ("GET", "/ui/kb/{slug}", "GET", "/api/v1/kb/{slug}"),
    ("GET", "/ui/kb/{slug}/preview", "GET", "/api/v1/kb/{slug}"),
    ("GET", "/ui/agents/runs", "GET", "/api/v1/agents/runs"),
    ("GET", "/ui/agents/runs/{handle}", "GET", "/api/v1/agents/runs/{handle}"),
    ("GET", "/ui/retrieval", "POST", "/api/v1/retrieve"),
    ("POST", "/ui/retrieval/diagnostics", "POST", "/api/v1/retrieve"),
    ("POST", "/ui/retrieval/usage", "GET", "/api/v1/retrieve/usage"),
    ("POST", "/ui/retrieval/eval", "POST", "/api/v1/retrieve/eval"),
    ("POST", "/ui/retrieval/retire-checklist", "POST", "/api/v1/retrieve/retire-checklist"),
    ("GET", "/ui/broadcast", "GET", "/api/v1/broadcast/recent"),
    ("GET", "/ui/broadcast/feed", "GET", "/api/v1/broadcast/recent"),
    ("GET", "/ui/broadcast/history", "GET", "/api/v1/broadcast/recent"),
    ("GET", "/ui/broadcast/stream", "GET", "/api/v1/feed"),
    ("GET", "/ui/broadcast/event/{audit_id}", "GET", "/api/v1/audit/show/{audit_id}"),
    ("GET", "/ui/agents", "GET", "/api/v1/agents"),
    ("GET", "/ui/agents/{name}", "GET", "/api/v1/agents/{name}"),
    ("GET", "/ui/agents/principals", "GET", "/api/v1/agent-principals"),
    ("GET", "/ui/checks", "GET", "/api/v1/checks/dashboards"),
    ("GET", "/ui/checks/{dashboard_id}", "GET", "/api/v1/checks/dashboards/{dashboard_id}"),
    ("GET", "/ui/connectors", "GET", "/api/v1/targets"),
    ("GET", "/ui/connectors/{name}", "GET", "/api/v1/targets/{name}"),
    ("GET", "/ui/connectors/registry", "GET", "/api/v1/connectors"),
    (
        "GET",
        "/ui/connectors/registry/{connector_id}/review",
        "GET",
        "/api/v1/connectors/{connector_id}/review",
    ),
    ("GET", "/ui/corpus", "GET", "/api/v1/doc_collections"),
    ("GET", "/ui/corpus/collections", "GET", "/api/v1/doc_collections"),
    ("GET", "/ui/operations", "GET", "/api/v1/operations/groups"),
    ("GET", "/ui/operations/search", "GET", "/api/v1/operations/search"),
    ("GET", "/ui/pairing", "GET", "/api/v1/addons/pairings"),
    ("GET", "/ui/runbooks", "GET", "/api/v1/runbooks/templates"),
    ("GET", "/ui/runbooks/{slug}", "GET", "/api/v1/runbooks/templates/{slug}"),
    ("GET", "/ui/runbooks/runs", "GET", "/api/v1/runbooks/runs"),
    ("GET", "/ui/runners", "GET", "/api/v1/runner-principals"),
    ("GET", "/ui/scheduler", "GET", "/api/v1/scheduler/triggers"),
    ("GET", "/ui/sensors", "GET", "/api/v1/sensors"),
    ("GET", "/ui/topology", "GET", "/api/v1/topology/edges"),
    ("GET", "/ui/topology/timeline", "GET", "/api/v1/topology/timeline"),
    ("GET", "/ui/topology/history/{name}", "GET", "/api/v1/topology/history/{name}"),
    ("GET", "/ui/topology/diff", "GET", "/api/v1/topology/diff"),
)

#: Console ``GET`` pages a ``read_only`` session may open, each with the REST
#: twin whose floor it shares (``None``: the page shows only the caller's own
#: session, tenant or the landing page). Every other console ``GET`` page
#: answers ``403`` to a ``read_only`` session.
_READ_ONLY_PAGES: dict[str, tuple[str, str] | None] = {
    "/ui/": None,
    "/ui/account": None,
    "/ui/tenant": None,
    "/ui/approvals": ("GET", "/api/v1/approvals"),
    "/ui/approvals/badge": ("GET", "/api/v1/approvals"),
    "/ui/approvals/list": ("GET", "/api/v1/approvals"),
    "/ui/approvals/in-order": ("GET", "/api/v1/approvals"),
    "/ui/approvals/{request_id}": ("GET", "/api/v1/approvals/{request_id}"),
    "/ui/automation": ("GET", "/api/v1/automation"),
    "/ui/memory": ("GET", "/api/v1/memory"),
    "/ui/memory/create": ("GET", "/api/v1/memory"),
    "/ui/memory/tags": ("GET", "/api/v1/memory"),
    "/ui/memory/{scope}/{slug}": ("GET", "/api/v1/memory/{scope}/{slug}"),
    "/ui/memory/{scope}/{slug}/edit": ("GET", "/api/v1/memory/{scope}/{slug}"),
    "/ui/memory/{scope}/{slug}/promote": ("GET", "/api/v1/memory/{scope}/{slug}"),
}

#: The sign-in flow runs before there is a session; it is not a read page.
_SIGN_IN_PREFIX = "/ui/auth/"

#: GET pages an operator session must see with ``200`` (no path parameter, no
#: endless stream).
_OPERATOR_SEES_200: tuple[str, ...] = (
    "/ui/event-sources",
    "/ui/conventions",
    "/ui/kb",
    "/ui/agents/runs",
    "/ui/retrieval",
    "/ui/broadcast",
    "/ui/broadcast/feed",
    "/ui/broadcast/history",
    "/ui/agents",
    "/ui/checks",
    "/ui/connectors",
    "/ui/operations",
    "/ui/runbooks",
    "/ui/runners",
    "/ui/scheduler",
    "/ui/sensors",
    "/ui/topology",
)


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis + BFF env vars the console routes read."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    _reset()
    yield
    _reset()


def _reset() -> None:
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


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


def _calls(dependant: Dependant) -> Iterator[Any]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _has_operator_gate(route: APIRoute) -> bool:
    return any(call is require_ui_operator for call in _calls(route.dependant))


def _rest_floors(route: APIRoute) -> list[TenantRole]:
    """The ``min_role`` of every ``require_role`` dependency on a REST route."""
    floors: list[TenantRole] = []
    for call in _calls(route.dependant):
        if not inspect.isfunction(call):
            continue
        min_role = inspect.getclosurevars(call).nonlocals.get("min_role")
        if isinstance(min_role, TenantRole):
            floors.append(min_role)
    return floors


def _api_routes(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    return {
        (method, route.path): route
        for route in iter_routes(app.routes)
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def _concrete_path(route: APIRoute) -> str:
    """Fill each path parameter with a value its type accepts."""
    path = route.path
    for param in route.dependant.path_params:
        value = str(uuid.uuid4()) if param.field_info.annotation is uuid.UUID else "some-slug"
        path = path.replace("{" + param.name + "}", value)
        path = path.replace("{" + param.name + ":path}", value)
    return path


def _seed_session(role: TenantRole) -> tuple[uuid.UUID, dict[str, Any]]:
    """Seed the tenant and a session whose access token is a real JWT with *role*."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = make_rsa_keypair(f"floor-kid-{uuid.uuid4().hex[:8]}")
    token = mint_token(keypair, sub=_SUB, tenant_id=str(_TENANT), tenant_role=role.value)

    async def _do() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            if await session.get(Tenant, _TENANT) is None:
                session.add(Tenant(id=_TENANT, slug="floor", name="Tenant floor"))
        async with get_sessionmaker()() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=_SUB,
                tenant_id=_TENANT,
                access_token=token,
                refresh_token="made-up-refresh-token",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do()), public_jwks(keypair)


def _client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False, raise_server_exceptions=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _request(client: TestClient, method: str, path: str, session_id: uuid.UUID) -> Any:
    if method == "GET":
        return client.get(path)
    token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, token)
    return client.request(method, path, headers={CSRF_HEADER_NAME: token}, data={})


@pytest.mark.parametrize(
    ("ui_method", "ui_path", "rest_method", "rest_path"),
    _OPERATOR_FLOOR_PAGES,
    ids=[f"{m} {p}" for m, p, _, _ in _OPERATOR_FLOOR_PAGES],
)
def test_console_page_and_rest_twin_both_need_operator(
    ui_method: str, ui_path: str, rest_method: str, rest_path: str
) -> None:
    from meho_backplane.main import app as main_app

    routes = _api_routes(main_app)
    rest_route = routes.get((rest_method, rest_path))
    assert rest_route is not None, f"REST twin {rest_method} {rest_path} is not routed"
    assert TenantRole.OPERATOR in _rest_floors(rest_route)

    ui_route = _api_routes(_build_app()).get((ui_method, ui_path))
    assert ui_route is not None, f"console page {ui_method} {ui_path} is not routed"
    assert _has_operator_gate(ui_route)


def test_read_only_pages_share_the_floor_of_their_rest_twin() -> None:
    from meho_backplane.main import app as main_app

    rest_routes = _api_routes(main_app)
    ui_routes = _api_routes(_build_app())
    for ui_path, twin in _READ_ONLY_PAGES.items():
        assert ("GET", ui_path) in ui_routes, f"{ui_path} is not routed"
        if twin is None:
            continue
        rest_route = rest_routes.get(twin)
        assert rest_route is not None, f"REST twin {twin} is not routed"
        floors = _rest_floors(rest_route)
        assert TenantRole.OPERATOR not in floors, ui_path
        assert TenantRole.TENANT_ADMIN not in floors, ui_path


def test_read_only_session_is_refused_on_every_other_console_page() -> None:
    app = _build_app()
    walked = [
        ("GET", route)
        for route in iter_routes(app.routes)
        if isinstance(route, APIRoute)
        and "GET" in route.methods
        and not route.path.startswith(_SIGN_IN_PREFIX)
        and route.path not in _READ_ONLY_PAGES
    ]
    listed_posts = {(m, p) for m, p, _, _ in _OPERATOR_FLOOR_PAGES if m == "POST"}
    routes = _api_routes(app)
    walked += [(m, routes[(m, p)]) for m, p in sorted(listed_posts)]
    walked_keys = {(m, r.path) for m, r in walked}
    assert {(m, p) for m, p, _, _ in _OPERATOR_FLOOR_PAGES} <= walked_keys

    session_id, jwks = _seed_session(TenantRole.READ_ONLY)
    not_refused: dict[str, int] = {}
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, jwks)
        client = _client(session_id)
        for method, route in walked:
            response = _request(client, method, _concrete_path(route), session_id)
            if response.status_code != 403:
                not_refused[f"{method} {route.path}"] = response.status_code

    assert not_refused == {}


def test_read_only_session_gets_operator_required_on_listed_pages() -> None:
    session_id, jwks = _seed_session(TenantRole.READ_ONLY)
    app = _build_app()
    routes = _api_routes(app)
    answers: dict[str, Any] = {}
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, jwks)
        client = _client(session_id)
        for method, path, _, _ in _OPERATOR_FLOOR_PAGES:
            response = _request(client, method, _concrete_path(routes[(method, path)]), session_id)
            answers[f"{method} {path}"] = (response.status_code, response.json())

    expected = (403, {"detail": "operator_required"})
    assert {k: v for k, v in answers.items() if v != expected} == {}


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN])
def test_operator_and_tenant_admin_pass_the_check(role: TenantRole) -> None:
    session_id, jwks = _seed_session(role)
    answers: dict[str, int] = {}
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, jwks)
        client = _client(session_id)
        for path in _OPERATOR_SEES_200:
            answers[path] = client.get(path).status_code
        for ui_method, ui_path, _, _ in _OPERATOR_FLOOR_PAGES:
            if ui_method != "GET" or "{" not in ui_path:
                continue
            route = _api_routes(client.app).get((ui_method, ui_path))  # type: ignore[arg-type]
            assert route is not None
            status = client.get(_concrete_path(route)).status_code
            assert status not in (401, 403), f"{ui_path}: {status}"

    assert answers == dict.fromkeys(_OPERATOR_SEES_200, 200)
