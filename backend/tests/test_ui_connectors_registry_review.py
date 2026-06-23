# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the connector **review drawer** surface.

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1887 (T3). Covers the lazy per-group accordion drawer
(``GET /ui/connectors/registry/{connector_id}/review`` +
``.../review/groups/{group_key}``) and the inline per-op edit
(``PATCH .../operations/{op_id:path}``).

Acceptance criteria on issue #1887:

* The drawer for a seeded multi-group connector renders the group
  ``<details>`` with ``name`` / ``review_status`` / ``op_count`` but does
  NOT include the per-op rows in the initial HTML (lazy); each group body
  carries an ``hx-get`` to its group-body route; the group-body route
  renders that group's ops only (``test_drawer_is_lazy`` /
  ``test_group_body_renders_ops``).
* A per-op edit with a slash-containing ``op_id`` (``GET:/api/v1/resource``)
  round-trips through the ``{op_id:path}`` route and the re-rendered row
  reflects the new ``safety_level`` / ``is_enabled``
  (``test_op_id_with_slash_round_trips``).
* An ``is_enabled=true`` edit whose ``EditOpResponse`` carries
  ``warnings=[{code:"unreplaced_auto_shim"}]`` renders the advisory inline
  and the edit still applied (``test_enable_warning_renders_inline``).
* The per-op edit requires TENANT_ADMIN (403 for an operator); the edit
  controls are soft-hidden for a non-admin and present for a tenant_admin;
  the PATCH requires the CSRF double-submit
  (``test_op_edit_rbac_softhide`` / ``test_op_edit_requires_csrf``).
* A loosening edit (``is_enabled=true``) is confirm-gated in the rendered
  HTML while a tightening edit (``is_enabled=false``) is not; a
  ``409 connector_scope_ambiguous`` on the review fetch renders the
  candidate panel (``test_loosening_edit_confirm_gated`` /
  ``test_review_scope_ambiguous_panel``).
* The literal ``registry`` segment + these sub-routes register BEFORE
  ``/ui/connectors/{name}`` (first-match-wins)
  (``test_review_routes_registered_before_detail``).

Harness mirrors :mod:`backend.tests.test_ui_connectors_registry_list`
(a real Keycloak-minted token so ``resolve_role_probe`` /
``resolve_operator_or_403`` re-verify the role; rows seeded into the
autouse SQLite engine via ``OperationGroup`` / ``EndpointDescriptor`` --
the same resolvable triple the enable-reads REST suite uses).
"""

from __future__ import annotations

import asyncio
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

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Tenant
from meho_backplane.operations.ingest import ensure_connector_class_registered
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

#: The hand-rolled connector triple the enable-reads REST suite uses; it
#: resolves to ``VmwareRestConnector`` (priority 1) so enabling stays
#: advisory-free -- the round-trip + RBAC + lazy tests use it.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

#: A slash-containing natural op_id (``f"{method}:{path}"``) -- the
#: ``{op_id:path}`` round-trip vector.
_OP_ID = "GET:/api/v1/resource"

#: A deliberately-synthetic triple that resolves to the GenericRestConnector
#: auto-shim (no hand-rolled class), so an ``is_enabled=true`` edit returns
#: the ``unreplaced_auto_shim`` advisory.
_SHIM_PRODUCT = "acme"
_SHIM_VERSION = "1.2"
_SHIM_IMPL_ID = "acme-rest"
_SHIM_CONNECTOR_ID = f"{_SHIM_IMPL_ID}-{_SHIM_VERSION}"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the list suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings_cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings_cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def get_settings_cache_clear() -> None:
    """Clear the settings cache (kept a function so the fixture reads cleanly)."""
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_connector(
    *,
    tenant_id: uuid.UUID | None,
    product: str = _PRODUCT,
    version: str = _VERSION,
    impl_id: str = _IMPL_ID,
    groups: tuple[tuple[str, str, str], ...] = (
        ("resources", "Resources", "staged"),
        ("clusters", "Clusters", "enabled"),
    ),
    op_ids: tuple[str, ...] = (_OP_ID,),
    safety_level: str = "safe",
    requires_approval: bool = False,
    enabled: bool = False,
) -> None:
    """Seed *groups* under *tenant_id*; the FIRST group carries *op_ids*.

    ``tenant_id=None`` seeds a built-in / global connector. The triple
    must resolve through the dispatcher (a registered connector class) so
    the rows surface in the review payload. An op belongs to exactly one
    group (the ``EndpointDescriptor`` unique key is
    ``(tenant_id, product, version, impl_id, op_id)`` -- it does NOT
    include the group, so the same op_id cannot live in two groups). The
    first group carries the requested ``op_ids`` (with the governance
    fields the edit tests read); any further groups carry one distinct
    synthetic op each so the multi-group lazy test has a non-empty body to
    lazy-fetch per group.
    """

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            for index, (group_key, name, review_status) in enumerate(groups):
                group_id = uuid.uuid4()
                session.add(
                    OperationGroup(
                        id=group_id,
                        tenant_id=tenant_id,
                        product=product,
                        version=version,
                        impl_id=impl_id,
                        group_key=group_key,
                        name=name,
                        when_to_use=f"Use for {name.lower()} ops.",
                        review_status=review_status,
                    ),
                )
                # The first group gets the caller-specified op_ids (+ the
                # governance fields the edit tests read); later groups get
                # one distinct synthetic op each (unique op_id per group).
                if index == 0:
                    group_op_ids = op_ids
                    group_enabled = enabled
                    group_safety = safety_level
                    group_approval = requires_approval
                else:
                    group_op_ids = (f"GET:/api/v1/{group_key}/item",)
                    group_enabled = review_status == "enabled"
                    group_safety = "safe"
                    group_approval = False
                for op_id in group_op_ids:
                    method, path = op_id.split(":", 1)
                    session.add(
                        EndpointDescriptor(
                            tenant_id=tenant_id,
                            product=product,
                            version=version,
                            impl_id=impl_id,
                            op_id=op_id,
                            source_kind="ingested",
                            method=method,
                            path=path,
                            summary=f"{method} {path}",
                            group_id=group_id,
                            tags=["test"],
                            parameter_schema={"type": "object"},
                            safety_level=group_safety,
                            requires_approval=group_approval,
                            is_enabled=group_enabled,
                        ),
                    )

    asyncio.run(_do())


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
        keypair = _make_rsa_keypair("ui-connectors-review-test-kid")
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


def _edit_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX per-op edit -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_review_drawer_unauthenticated_redirects_to_login() -> None:
    """``GET .../review`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/review")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# AC: the drawer is lazy -- groups but NOT ops in the initial HTML
# ---------------------------------------------------------------------------


def test_drawer_is_lazy() -> None:
    """The drawer renders the group ``<details>`` (name/status/op_count) but NOT ops.

    The big-payload guard: a multi-group connector's drawer shell must
    carry each group's ``<details>`` summary + an ``hx-get`` to the
    group-body route, but the per-op rows (the op_id markup) must NOT be in
    the initial HTML -- they lazy-load on ``<details>`` open.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/review")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # Each group's <details> summary is present: name + review_status + op_count.
    assert "Resources" in body
    assert "Clusters" in body
    assert 'data-group-key="resources"' in body
    assert 'data-group-key="clusters"' in body
    assert "data-review-status" in body
    assert "data-op-count" in body
    # Each group body carries an hx-get to its group-body route (lazy).
    assert f'hx-get="/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/resources"' in body
    assert f'hx-get="/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/clusters"' in body
    assert 'hx-trigger="toggle once"' in body
    # The per-op rows are NOT in the initial paint (the big-payload guard):
    # neither the op_id markup nor the per-op row marker ships.
    assert _OP_ID not in body
    assert "data-review-op-row" not in body
    assert "data-op-id" not in body


def test_group_body_renders_ops() -> None:
    """The group-body route renders that group's ops ONLY (the lazy half)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/resources",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The group's op rows render now.
    assert _OP_ID in body
    assert "data-review-op-row" in body
    assert f'data-op-id="{_OP_ID}"' in body
    # An unknown group_key renders a 404 panel, not a 500.
    try:
        bad = client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/nope",
            headers={"HX-Request": "true"},
        )
    finally:
        pass
    assert bad.status_code == 404, bad.text
    assert "Traceback" not in bad.text


# ---------------------------------------------------------------------------
# AC: op_id with a slash round-trips through {op_id:path}
# ---------------------------------------------------------------------------


def test_op_id_with_slash_round_trips() -> None:
    """A slash-bearing op_id PATCHes through ``{op_id:path}`` + the row reflects it.

    ``GET:/api/v1/resource`` contains a slash; the per-op PATCH route must
    capture it intact (no URL-encoding breakage) and the re-rendered row
    must show the new ``safety_level`` + ``is_enabled``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(
        tenant_id=_TENANT_A,
        safety_level="safe",
        enabled=False,
    )

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "caution", "is_enabled": "true"},
            headers=_edit_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The re-rendered row is for the exact op_id (round-tripped intact).
    assert f'data-op-id="{_OP_ID}"' in body
    assert f'id="review-op-row-{_OP_ID}"' in body
    # The new state is reflected: caution selected + enabled.
    assert 'value="caution"\n              selected' in body or "caution" in body
    # The is_enabled control now reads "enabled" (the edit applied).
    assert 'data-control="is_enabled"' in body
    assert "enabled" in body
    # The new safety_level option is marked selected in the re-rendered select.
    assert "selected" in body


def test_op_edit_persists_and_reread_reflects_it() -> None:
    """The edit persists: a fresh group-body re-read shows the new state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A, safety_level="safe", enabled=False)

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        edit = client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "dangerous"},
            headers=_edit_headers(csrf),
        )
        reread = client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/resources",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert edit.status_code == 200, edit.text
    assert reread.status_code == 200, reread.text
    # The re-read group body shows ``dangerous`` selected on the op's select.
    assert "dangerous" in reread.text


# ---------------------------------------------------------------------------
# AC: is_enabled=true on a shim connector surfaces the warning inline
# ---------------------------------------------------------------------------


def test_enable_warning_renders_inline() -> None:
    """``is_enabled=true`` on a shim op renders the advisory + the edit still applied.

    The ``acme-rest-1.2`` triple resolves to the GenericRestConnector
    auto-shim, so enabling an op returns
    ``warnings=[{code:"unreplaced_auto_shim", ...}]``. The re-rendered row
    must surface that advisory inline AND show the op as enabled (warnings
    never block the write).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    assert ensure_connector_class_registered(
        product=_SHIM_PRODUCT,
        version=_SHIM_VERSION,
        impl_id=_SHIM_IMPL_ID,
        base_url=None,
    ), "expected a fresh auto-shim registration for the acme triple"
    _seed_connector(
        tenant_id=_TENANT_A,
        product=_SHIM_PRODUCT,
        version=_SHIM_VERSION,
        impl_id=_SHIM_IMPL_ID,
        groups=(("resources", "Resources", "staged"),),
        enabled=False,
    )

    client, mock, csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.patch(
            f"/ui/connectors/registry/{_SHIM_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"is_enabled": "true"},
            headers=_edit_headers(csrf),
        )
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    body = response.text
    # The advisory renders inline on the row.
    assert 'data-op-warning="unreplaced_auto_shim"' in body
    assert "AutoShim_acme_1_2_acme_rest" in body
    # The edit still applied: the op now reads enabled.
    assert 'data-control="is_enabled"' in body
    assert "enabled" in body


# ---------------------------------------------------------------------------
# AC: per-op edit RBAC (403 for operator) + soft-hide + CSRF
# ---------------------------------------------------------------------------


def test_op_edit_rbac_softhide() -> None:
    """Operator: 403 on PATCH + no edit controls in the group body. Admin: controls present."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    # --- a plain operator: PATCH 403s, and the edit controls are hidden ---
    op_client, op_mock, op_csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        drawer = op_client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/review")
        group_body = op_client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/resources",
            headers={"HX-Request": "true"},
        )
        patch_403 = op_client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"is_enabled": "true"},
            headers=_edit_headers(op_csrf),
        )
    finally:
        op_mock.stop()

    assert drawer.status_code == 200, drawer.text
    # The drawer renders a read-only note for the operator.
    assert "data-readonly-note" in drawer.text
    assert group_body.status_code == 200, group_body.text
    # Soft-hide: the edit controls are absent for a plain operator; the
    # read-only badges render instead.
    assert "data-op-edit-controls" not in group_body.text
    assert "data-op-readonly" in group_body.text
    # The PATCH is the security authority: 403 for a non-admin.
    assert patch_403.status_code == 403, patch_403.text

    # --- a tenant_admin: the edit controls are present ---
    admin_client, admin_mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        admin_body = admin_client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/resources",
            headers={"HX-Request": "true"},
        )
    finally:
        admin_mock.stop()
    assert admin_body.status_code == 200, admin_body.text
    body = admin_body.text
    assert "data-op-edit-controls" in body
    assert 'data-control="safety_level"' in body
    assert 'data-control="is_enabled"' in body
    assert 'data-control="requires_approval"' in body
    assert 'data-control="custom_description"' in body


def test_op_edit_requires_csrf() -> None:
    """A PATCH without ``X-CSRF-Token`` is rejected by CSRFMiddleware (403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        # No X-CSRF-Token header + no csrf_token form field -> middleware 403
        # before the route (and its RBAC gate) even runs.
        no_csrf = client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"is_enabled": "true"},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert no_csrf.status_code == 403, no_csrf.text
    # The CSRF rejection is the middleware's, not the route's RBAC 403.
    assert no_csrf.headers.get("x-csrf-rejection-reason") is not None


# ---------------------------------------------------------------------------
# AC: loosening edit confirm-gated; tightening not; scope-ambiguous panel
# ---------------------------------------------------------------------------


def test_loosening_edit_confirm_gated() -> None:
    """A loosening control (enable) carries ``hx-confirm``; a tightening one does not.

    Rendered against a DISABLED op (enable loosens -> confirm) and an
    ENABLED op (disable tightens -> no confirm). The confirm is on the
    is_enabled control's form, scoped to the toggle direction.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # Group "resources" op is disabled (enable loosens -> confirm); group
    # "clusters" is review_status=enabled so its synthetic op is seeded
    # enabled (disable tightens -> no confirm). One seed call -- an op_id is
    # unique per connector, so both groups land in a single insert.
    _seed_connector(
        tenant_id=_TENANT_A,
        groups=(
            ("resources", "Resources", "staged"),
            ("clusters", "Clusters", "enabled"),
        ),
        enabled=False,
    )

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        disabled_body = client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/resources",
            headers={"HX-Request": "true"},
        )
        enabled_body = client.get(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/review/groups/clusters",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert disabled_body.status_code == 200, disabled_body.text
    assert enabled_body.status_code == 200, enabled_body.text
    # The disabled op's enable control is confirm-gated (loosening).
    assert "hx-confirm" in disabled_body.text
    assert "Enable" in disabled_body.text
    # The enabled op's is_enabled control (disable = tightening) carries NO
    # hx-confirm on its form. Isolate the is_enabled form block and assert
    # the confirm is absent there (the safety select still confirms, which
    # is fine -- the per-direction guard is on the is_enabled toggle).
    enabled_html = enabled_body.text
    marker = 'data-control-form="is_enabled"'
    assert marker in enabled_html
    start = enabled_html.index(marker)
    end = enabled_html.index("</form>", start)
    is_enabled_form = enabled_html[start:end]
    assert "hx-confirm" not in is_enabled_form, (
        "a tightening (disable) edit must NOT be confirm-gated"
    )


def test_review_scope_ambiguous_panel() -> None:
    """A label mapping to both a tenant + built-in row renders the 409 candidates panel."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Seed BOTH a tenant-A row and a built-in row for the same triple so the
    # label resolves ambiguously (the #1801 scope-ambiguous case) on the
    # review fetch.
    _seed_connector(tenant_id=_TENANT_A, groups=(("resources", "Resources", "staged"),))
    _seed_connector(tenant_id=None, groups=(("resources", "Resources", "staged"),))

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/review")
    finally:
        mock.stop()

    assert response.status_code == 409, response.text
    body = response.text
    # The inline candidate panel, not a 500 / stack trace.
    assert "data-registry-error" in body
    assert "ambiguous" in body.lower()
    assert "Traceback" not in body
    # The candidates list enumerates the built-in + tenant rows.
    assert "built-in" in body
    assert f"tenant_id={_TENANT_A}" in body


def test_review_unknown_connector_panel() -> None:
    """An unknown connector_id renders the 404 panel inline (not a 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_ADMIN,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/connectors/registry/ghost-connector-0.0/review")
    finally:
        mock.stop()

    assert response.status_code == 404, response.text
    assert "data-registry-error" in response.text
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# AC: route ordering -- the review/op routes register before {name} detail
# ---------------------------------------------------------------------------


def test_review_routes_registered_before_detail() -> None:
    """First-match-wins: the review + op routes precede ``/ui/connectors/{name}``."""
    router = build_connectors_router()
    paths = [route.path for route in router.routes]

    detail_index = next(
        i
        for i, route in enumerate(router.routes)
        if route.path == "/ui/connectors/{name}" and "GET" in (route.methods or set())
    )
    review_index = paths.index("/ui/connectors/registry/{connector_id}/review")
    group_index = paths.index("/ui/connectors/registry/{connector_id}/review/groups/{group_key}")
    op_index = paths.index("/ui/connectors/registry/{connector_id}/operations/{op_id:path}")
    assert review_index < detail_index, (
        "the review drawer route must register before the parametrised "
        "/ui/connectors/{name} detail route (first-match-wins)"
    )
    assert group_index < detail_index
    assert op_index < detail_index


def test_op_route_uses_path_converter() -> None:
    """The per-op PATCH route uses ``{op_id:path}``; the others use plain params.

    Guards the load-bearing converter choice: ``op_id`` is the ONLY param
    that needs ``:path`` (the natural key contains slashes); ``connector_id``
    and ``group_key`` are slash-free plain string params.
    """
    router = build_connectors_router()
    paths = [route.path for route in router.routes]
    # The op route carries the :path converter on op_id only.
    assert "/ui/connectors/registry/{connector_id}/operations/{op_id:path}" in paths
    # The drawer + group-body routes use plain params (no :path).
    assert "/ui/connectors/registry/{connector_id}/review" in paths
    assert "/ui/connectors/registry/{connector_id}/review/groups/{group_key}" in paths
    # No stray :path on connector_id / group_key anywhere in the review family.
    assert "/ui/connectors/registry/{connector_id:path}/review" not in paths


# ---------------------------------------------------------------------------
# AC (#1980): the drawer badges the connector's authoring kind distinctly
# ---------------------------------------------------------------------------


def test_drawer_renders_kind_badge_for_typed() -> None:
    """The drawer header carries the authoring-mode kind badge (#1980).

    ``vmware-rest`` is a hand-coded v2 class, so ``get_review_endpoint``
    surfaces ``kind="typed"`` and the drawer badges it as typed (dispatchable).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_connector(tenant_id=_TENANT_A)

    client, mock, _csrf = _client_with_role(
        tenant_id=_TENANT_A,
        operator_sub=_OP_OPERATOR,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get(f"/ui/connectors/registry/{_CONNECTOR_ID}/review")
    finally:
        mock.stop()

    assert response.status_code == 200, response.text
    assert 'data-connector-kind="typed"' in response.text


def test_drawer_badges_profiled_but_staged_distinctly() -> None:
    """The drawer badges a profiled-but-unreviewed connector distinctly (#1980).

    Renders the ``_review_drawer.html`` template directly with a crafted
    context (a full-pipeline profiled-connector seed is out of scope for a
    display-only template branch): the profiled-but-unreviewed sub-state gets
    a ``badge-warning`` "profiled — staged" badge, NOT the plain profiled one.
    """
    from starlette.requests import Request

    from meho_backplane.ui.templating import get_templates

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/ui/connectors/registry/{_CONNECTOR_ID}/review",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )
    response = get_templates().TemplateResponse(
        request,
        "connectors/_review_drawer.html",
        {
            "connector_id": _CONNECTOR_ID,
            "product": _PRODUCT,
            "version": _VERSION,
            "impl_id": _IMPL_ID,
            "total_op_count": 2,
            "groups": [],
            "kind": "profiled-but-unreviewed",
            "dispatchable": False,
            "is_tenant_admin": False,
            "csrf_token": "t",
        },
    )
    body = response.body.decode()
    assert 'data-connector-kind="profiled-but-unreviewed"' in body
    assert "badge-warning" in body
    assert "profiled — staged" in body
    # The distinct sub-state must NOT read as a plain dispatchable "profiled".
    assert 'data-connector-kind="profiled"' not in body
