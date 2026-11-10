# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's settings page (#4048): landing, preview, apply, history, export.

The harness is the one of :mod:`tests.test_ui_connectors_import`: a minimal app
with the UI session and CSRF middlewares and a ``web_session`` row holding a real
minted token, so the role gates re-verify it. The API twin of every behaviour is
in :mod:`tests.test_api_v1_settings`; here the question is that the page shows
the same thing and goes through the same gates.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import InstanceSettingsRevision, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.settings import diff_rows
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import AUDIENCE as _DEFAULT_AUDIENCE
from tests._oidc_jwt_helpers import ISSUER as _DEFAULT_ISSUER
from tests._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from tests._oidc_jwt_helpers import mint_token as _mint_token
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from tests._oidc_jwt_helpers import public_jwks as _public_jwks

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ADMIN = "op-admin"
_PRODUCT = "fakeprod"

#: The fixture the CLI's render test reads too, so both give the same rows.
_CLI_TESTDATA = (
    Path(__file__).resolve().parents[2] / "cli" / "internal" / "cmd" / "settings" / "testdata"
)


def _file(*targets: dict[str, Any]) -> bytes:
    return yaml.safe_dump(
        {
            "apiVersion": "meho.evoila.com/v1",
            "kind": "Settings",
            "spec": {"tenants": [{"id": str(_TENANT), "targets": list(targets)}]},
        }
    ).encode()


_ONE_TARGET = _file({"name": "alpha", "product": _PRODUCT, "host": "a.example.test"})


class _FakeConnector(Connector):
    product = _PRODUCT
    version = "1.0"
    impl_id = _PRODUCT
    supported_version_range = None

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def execute(  # pragma: no cover
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        raise NotImplementedError


def _reset() -> None:
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    _reset()
    clear_registry()
    register_connector_v2(product=_PRODUCT, version="1.0", impl_id=_PRODUCT, cls=_FakeConnector)
    yield
    clear_registry()
    _reset()


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


async def _in_session(*rows: Any) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add_all(rows)


def _seed_tenant() -> None:
    asyncio.run(_in_session(Tenant(id=_TENANT, slug="a", name="Tenant a")))


def _seed_target(name: str = "existing") -> None:
    now = datetime.now(UTC)
    asyncio.run(
        _in_session(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name=name,
                aliases=[],
                product=_PRODUCT,
                host="old.example.test",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                created_at=now,
                updated_at=now,
            )
        )
    )


def _client(role: TenantRole = TenantRole.TENANT_ADMIN) -> tuple[TestClient, respx.MockRouter, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-settings-test-kid")
    token = _mint_token(keypair, sub=_ADMIN, tenant_id=str(_TENANT), tenant_role=role.value)

    async def _session() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            row = await create_session(
                session,
                operator_sub=_ADMIN,
                tenant_id=_TENANT,
                access_token=token,
                refresh_token="refresh",
                lifetime=timedelta(hours=1),
            )
            return row.id

    session_id = asyncio.run(_session())
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, _public_jwks(keypair))
    client = TestClient(_build_app(), follow_redirects=False, base_url="https://testserver")
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return client, mock, csrf


def _headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "HX-Request": "true"}


def _preview(client: TestClient, csrf: str, content: bytes) -> Any:
    return client.post(
        "/ui/settings/import/preview",
        files={"file": ("meho-settings.yaml", content, "application/yaml")},
        headers=_headers(csrf),
    )


def _apply(client: TestClient, csrf: str, content: bytes) -> Any:
    return client.post(
        "/ui/settings/import/apply",
        files={"file_b64": (None, base64.b64encode(content).decode())},
        headers=_headers(csrf),
    )


def _revisions() -> list[InstanceSettingsRevision]:
    async def _do() -> list[InstanceSettingsRevision]:
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(InstanceSettingsRevision))).scalars())

    return asyncio.run(_do())


def _target_names() -> set[str]:
    async def _do() -> set[str]:
        async with get_sessionmaker()() as session:
            return set((await session.execute(select(TargetORM.name))).scalars())

    return asyncio.run(_do())


def _rendered_rows(html: str) -> list[list[str]]:
    rows = []
    for row in re.findall(r"<tr data-settings-row.*?</tr>", html, re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        rows.append([unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in cells])
    return rows


# --- The first login -------------------------------------------------------


def test_first_login_lands_on_settings_with_two_choices() -> None:
    client, mock, _csrf = _client()
    try:
        landing = client.get("/ui/")
        assert landing.status_code == 303
        assert landing.headers["location"] == "/ui/settings"

        page = client.get("/ui/settings")
        assert page.status_code == 200
        assert "data-settings-first-run" in page.text
        assert "Fill in the form" in page.text
        assert "Import a settings file" in page.text
        assert 'href="/ui/connectors"' in page.text
    finally:
        mock.stop()


def test_once_a_target_exists_login_lands_on_the_dashboard() -> None:
    _seed_tenant()
    _seed_target()
    client, mock, _csrf = _client()
    try:
        assert client.get("/ui/").status_code == 200
        assert "data-settings-first-run" not in client.get("/ui/settings").text
    finally:
        mock.stop()


def test_once_a_revision_exists_login_lands_on_the_dashboard() -> None:
    _seed_tenant()
    client, mock, csrf = _client()
    try:
        assert _apply(client, csrf, _ONE_TARGET).status_code == 200
        assert len(_revisions()) == 1
        assert client.get("/ui/").status_code == 200
    finally:
        mock.stop()


def test_a_non_admin_is_not_sent_to_a_page_they_cannot_open() -> None:
    client, mock, csrf = _client(TenantRole.OPERATOR)
    try:
        assert client.get("/ui/").status_code == 200
        assert client.get("/ui/settings").status_code == 403
        assert client.get("/ui/settings/export").status_code == 403
        assert _preview(client, csrf, _ONE_TARGET).status_code == 403
        assert _apply(client, csrf, _ONE_TARGET).status_code == 403
    finally:
        mock.stop()
    assert _target_names() == set()


# --- Preview and apply -----------------------------------------------------


def test_preview_shows_the_same_diff_as_the_api_and_writes_nothing() -> None:
    _seed_tenant()
    _seed_target()
    content = _file(
        {"name": "alpha", "product": _PRODUCT, "host": "a.example.test"},
    )
    client, mock, csrf = _client()
    try:
        response = _preview(client, csrf, content)
    finally:
        mock.stop()
    assert response.status_code == 200

    rendered = _rendered_rows(response.text)
    assert ["Targets", "added", str(_TENANT), "alpha", ""] in rendered
    assert ["Targets", "not in file", str(_TENANT), "existing", ""] in rendered
    assert "an import never deletes" in response.text
    assert 'data-action="settings-apply"' in response.text
    assert _target_names() == {"existing"}
    assert _revisions() == []


def test_mistakes_are_the_api_sentences_and_there_is_no_apply_button() -> None:
    _seed_tenant()
    bad = _file({"name": "alpha", "product": _PRODUCT})
    client, mock, csrf = _client()
    try:
        response = _preview(client, csrf, bad)
    finally:
        mock.stop()
    assert "`spec.tenants[0].targets[0].host` is required." in unescape(response.text)
    assert 'data-action="settings-apply"' not in response.text


def test_apply_keeps_the_file_bytes_and_updates_the_history() -> None:
    _seed_tenant()
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, _ONE_TARGET)
        carried = re.search(r'name="file_b64" value="([^"]+)"', preview.text)
        assert carried is not None
        assert base64.b64decode(carried.group(1)) == _ONE_TARGET

        applied = _apply(client, csrf, _ONE_TARGET)
        page = client.get("/ui/settings")
    finally:
        mock.stop()

    assert "data-settings-result" in applied.text
    assert 'hx-swap-oob="true"' in applied.text
    assert _target_names() == {"alpha"}
    revisions = _revisions()
    assert len(revisions) == 1
    assert revisions[0].file_sha256 == hashlib.sha256(_ONE_TARGET).hexdigest()
    assert revisions[0].actor_sub == _ADMIN
    assert f'data-settings-revision="{revisions[0].id}"' in page.text


def test_a_file_that_changes_nothing_offers_no_apply() -> None:
    _seed_tenant()
    client, mock, csrf = _client()
    try:
        _apply(client, csrf, _ONE_TARGET)
        response = _preview(client, csrf, _ONE_TARGET)
    finally:
        mock.stop()
    assert "data-settings-nothing" in response.text
    assert 'data-action="settings-apply"' not in response.text


def test_no_file_chosen_is_one_sentence_the_page_can_show() -> None:
    client, mock, csrf = _client()
    try:
        response = client.post(
            "/ui/settings/import/preview",
            files={"other": (None, "x")},
            headers=_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "Choose a settings file first." in response.text


def test_the_import_needs_a_csrf_token() -> None:
    client, mock, _csrf = _client()
    try:
        response = client.post(
            "/ui/settings/import/apply",
            files={"file_b64": (None, base64.b64encode(_ONE_TARGET).decode())},
        )
    finally:
        mock.stop()
    assert response.status_code == 403
    assert _target_names() == set()


def test_a_large_file_fits_through_preview_and_apply() -> None:
    """Past the 1 MiB multipart default and the 256 KiB url-encoded CSRF cap."""
    _seed_tenant()
    filler = "x" * (1536 * 1024)
    content = _file(
        {"name": "alpha", "product": _PRODUCT, "host": "a.example.test", "notes": filler[:60000]}
    )
    content += b"\n# " + filler.encode()
    client, mock, csrf = _client()
    try:
        assert 'data-action="settings-apply"' in _preview(client, csrf, content).text
        assert "data-settings-result" in _apply(client, csrf, content).text
    finally:
        mock.stop()
    assert _target_names() == {"alpha"}


# --- Export ----------------------------------------------------------------


def test_export_downloads_the_file() -> None:
    _seed_tenant()
    _seed_target()
    client, mock, _csrf = _client()
    try:
        response = client.get("/ui/settings/export")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="meho-settings.yaml"'
    assert yaml.safe_load(response.text)["spec"]["tenants"][0]["targets"][0]["name"] == "existing"


def test_a_refused_export_shows_the_sentences() -> None:
    _seed_tenant()
    now = datetime.now(UTC)
    asyncio.run(
        _in_session(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name="leaky",
                aliases=[],
                product=_PRODUCT,
                host="h.example.test",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"password": "hunter2"},
                created_at=now,
                updated_at=now,
            )
        )
    )
    client, mock, _csrf = _client()
    try:
        response = client.get("/ui/settings/export")
    finally:
        mock.stop()
    assert response.status_code == 409
    assert "data-settings-export-refused" in response.text
    assert "hunter2" not in response.text


# --- One reading of a diff, shared with the CLI ------------------------------


def test_rows_match_the_fixture_the_cli_reads() -> None:
    diff = json.loads((_CLI_TESTDATA / "preview_diff.json").read_text())
    rows = json.loads((_CLI_TESTDATA / "preview_rows.json").read_text())
    assert diff_rows(diff) == rows
