# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A write by a platform admin outside their role groups says so in every audit row (#7004).

A platform admin may work in a tenant where they have no role group. There
they act as ``tenant_admin``. Every audit row of a write by such a person
carries ``tenant_role_source: platform_admin`` in its payload: the row of the
request itself and every row a service writes during the same request.

The tests drive the real app (``meho_backplane.main:app``) with a made-up
platform admin login. The login has a role group in tenant A only and
works in tenant C, where it has no group.

* Console: a session row for tenant C, then the real session, CSRF and audit
  middlewares in their real order.
* REST: the same login's token with the ``Meho-Tenant`` header set to tenant C.

Five writes run on both ways in: the settings file apply, a convention
create, a target create, a person block and an operation policy edit. Each
runs with ``AUDIT_WRAPPER_ROWS_RETIRED`` off and on. The same login in tenant
A (where it has a role group) and a login that is no platform admin leave no
note.

One test reads the sentence in the user guide and runs one console write and
one REST write, so the sentence and the code stay equal.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import warnings
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.connectors.registry import deregister_connector_v2, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    AuditEventClass,
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.revalidation import reset_read_revalidation_cache_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._rest_write_helpers import run_writes_at_once
from tests._vault_fakes import install_fake_vault
from tests.test_ui_connectors_forms import _PRODUCT as _TARGET_PRODUCT
from tests.test_ui_connectors_forms import _FakeConnector

from ._ui_role_helpers import allow_ui_operator_role

_TENANT_A = uuid.UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa")
_TENANT_C = uuid.UUID("cccccccc-3333-3333-3333-cccccccccccc")

_SUB = "person-platform-admin"
_PLAIN_SUB = "person-plain-admin"
_ENTRY_A_ADMINS = f"{_TENANT_A}:team-a:Admins"

_NOTE = "tenant_role_source"
_NOTE_VALUE = "platform_admin"

_GUIDE = Path(__file__).resolve().parents[2] / "docs-site" / "guides" / "several-tenants.md"

# The operation the policy edit changes (a made-up connector with one op).
_CONNECTOR_PRODUCT = "harbor"
_CONNECTOR_VERSION = "2.x"
_CONNECTOR_IMPL = "harbor-rest"
_CONNECTOR_ID = f"{_CONNECTOR_IMPL}-{_CONNECTOR_VERSION}"
_OP_ID = "GET:/api/v1/resource"

_BLOCKED = "person-to-block"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    allow_ui_operator_role(monkeypatch)
    install_fake_vault(monkeypatch)
    saved_overrides = dict(app.dependency_overrides)
    _reset()
    register_connector_v2(
        product=_TARGET_PRODUCT, version="1.0", impl_id=_TARGET_PRODUCT, cls=_FakeConnector
    )
    yield
    deregister_connector_v2(product=_TARGET_PRODUCT, version="1.0", impl_id=_TARGET_PRODUCT)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved_overrides)
    _reset()


def _reset() -> None:
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_read_revalidation_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_block_cache()
    reset_engine_for_testing()


def _audit_mode(monkeypatch: pytest.MonkeyPatch, *, retired: bool) -> None:
    """``AUDIT_WRAPPER_ROWS_RETIRED`` off or on (on needs a usage retention)."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true" if retired else "false")
    get_settings.cache_clear()


@pytest.fixture
def key() -> Iterator[Any]:
    """An RSA keypair whose public half the mocked JWKS endpoint serves."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rsa_key = make_rsa_keypair("platform-admin-role-source-kid")
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, public_jwks(rsa_key))
        yield rsa_key


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _seed_world() -> None:
    """Tenant A and tenant C, and a made-up connector with one operation in each."""

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for tenant_id, slug in ((_TENANT_A, "team-a"), (_TENANT_C, "team-c")):
                session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        async with get_sessionmaker()() as session, session.begin():
            for tenant_id in (_TENANT_A, _TENANT_C):
                group_id = uuid.uuid4()
                session.add(
                    OperationGroup(
                        id=group_id,
                        tenant_id=tenant_id,
                        product=_CONNECTOR_PRODUCT,
                        version=_CONNECTOR_VERSION,
                        impl_id=_CONNECTOR_IMPL,
                        group_key="resources",
                        name="Resources",
                        when_to_use="Use for resources.",
                        review_status="staged",
                    )
                )
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=_CONNECTOR_PRODUCT,
                        version=_CONNECTOR_VERSION,
                        impl_id=_CONNECTOR_IMPL,
                        op_id=_OP_ID,
                        source_kind="ingested",
                        method="GET",
                        path="/api/v1/resource",
                        summary="GET /api/v1/resource",
                        group_id=group_id,
                        tags=["test"],
                        parameter_schema={"type": "object"},
                        safety_level="safe",
                        requires_approval=False,
                        is_enabled=False,
                    )
                )

    _run(_do())


# ---------------------------------------------------------------------------
# Who writes, and where
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Login:
    """A made-up login and the tenant it works in."""

    sub: str
    platform_admin: bool
    tenant_id: uuid.UUID
    expect_note: bool


#: The platform admin in tenant C, where it has no role group.
OUTSIDE = Login(_SUB, True, _TENANT_C, expect_note=True)
#: The same platform admin in tenant A, where it has a role group.
INSIDE = Login(_SUB, True, _TENANT_A, expect_note=False)
#: A tenant admin through a role group who is no platform admin.
PLAIN = Login(_PLAIN_SUB, False, _TENANT_A, expect_note=False)


def _token(key: Any, login: Login) -> str:
    return mint_token(
        key,
        sub=login.sub,
        platform_admin=True if login.platform_admin else None,
        extra_claims={"meho_tenants": [_ENTRY_A_ADMINS]},
    )


class Rest:
    """The REST way in: the token with ``Meho-Tenant``."""

    name = "rest"

    def __init__(self, key: Any, login: Login) -> None:
        self.login = login
        self.client = TestClient(run_writes_at_once(app))
        self.headers = {
            "Authorization": f"Bearer {_token(key, login)}",
            "Meho-Tenant": str(login.tenant_id),
        }

    def settings(self, content: bytes) -> Any:
        return self.client.post(
            "/api/v1/settings/import",
            content=content,
            headers={**self.headers, "Content-Type": "application/yaml"},
        )

    def convention(self, slug: str) -> Any:
        return self.client.post(
            "/api/v1/conventions",
            json={
                "slug": slug,
                "title": "Confirm first",
                "body": "Ask first.",
                "kind": "reference",
            },
            headers=self.headers,
        )

    def target(self, name: str) -> Any:
        return self.client.post(
            "/api/v1/targets",
            json={"name": name, "product": _TARGET_PRODUCT, "host": "new.example.test"},
            headers=self.headers,
        )

    def block(self, sub: str) -> Any:
        return self.client.post(
            "/api/v1/principals/blocked",
            json={"sub": sub, "reason": "left the team"},
            headers=self.headers,
        )

    def policy(self) -> Any:
        return self.client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}/operations/{_OP_ID}",
            json={"safety_level": "dangerous"},
            headers=self.headers,
        )


class Console:
    """The console way in: a session row, then the session, CSRF and audit middlewares."""

    name = "console"

    def __init__(self, key: Any, login: Login) -> None:
        self.login = login
        session_id = self._seed_session(_token(key, login), login)
        self.client = TestClient(app, follow_redirects=False)
        self.client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        csrf = mint_csrf_token(str(session_id))
        self.client.cookies.set(CSRF_COOKIE_NAME, csrf)
        self.headers = {"X-CSRF-Token": csrf, "HX-Request": "true"}

    @staticmethod
    def _seed_session(access_token: str, login: Login) -> uuid.UUID:
        async def _do() -> uuid.UUID:
            async with get_sessionmaker()() as session, session.begin():
                decrypted = await create_session(
                    session,
                    operator_sub=login.sub,
                    tenant_id=login.tenant_id,
                    access_token=access_token,
                    refresh_token="refresh-token-plaintext",
                    lifetime=timedelta(hours=1),
                    tenant_chosen=True,
                    can_switch_tenant=True,
                )
                return uuid.UUID(str(decrypted.id))

        return _run(_do())

    def settings(self, content: bytes) -> Any:
        preview = self.client.post(
            "/ui/settings/preview",
            files={"file": ("meho-settings.yaml", content, "application/yaml")},
            headers=self.headers,
        )
        assert preview.status_code == 200, preview.text
        match = re.search(r'name="file_b64" value="([^"]*)"', preview.text)
        assert match is not None, preview.text
        return self.client.post(
            "/ui/settings/apply",
            files={"file_b64": (None, match.group(1))},
            headers=self.headers,
        )

    def convention(self, slug: str) -> Any:
        return self.client.post(
            "/ui/conventions/create",
            data={
                "slug": slug,
                "title": "Confirm first",
                "body": "Ask first.",
                "kind": "reference",
                "priority": "10",
            },
            headers=self.headers,
        )

    def target(self, name: str) -> Any:
        return self.client.post(
            "/ui/connectors/create",
            data={
                "name": name,
                "product": _TARGET_PRODUCT,
                "host": "new.example.test",
                "auth_model": "shared_service_account",
            },
            headers=self.headers,
        )

    def block(self, sub: str) -> Any:
        return self.client.post(
            "/ui/blocked-people",
            data={"sub": sub, "reason": "left the team"},
            headers=self.headers,
        )

    def switch(self, tenant_id: uuid.UUID) -> Any:
        return self.client.post(
            "/ui/tenant/switch",
            data={"tenant": str(tenant_id)},
            headers=self.headers,
        )

    def policy(self) -> Any:
        return self.client.patch(
            f"/ui/connectors/registry/{_CONNECTOR_ID}/operations/{_OP_ID}",
            data={"safety_level": "dangerous"},
            headers=self.headers,
        )


_WAYS: dict[str, type[Rest] | type[Console]] = {"console": Console, "rest": Rest}

#: A file that adds one convention and leaves every policy as it is.
_FILE_FOR: Callable[[uuid.UUID], bytes] = lambda tenant_id: yaml.safe_dump(  # noqa: E731
    {
        "apiVersion": "meho.evoila.com/v1",
        "kind": "Settings",
        "spec": {
            "tenants": [
                {
                    "id": str(tenant_id),
                    "conventions": [
                        {
                            "slug": "rule-001",
                            "title": "Rule",
                            "body": "Nights only.",
                            "kind": "reference",
                        }
                    ],
                }
            ]
        },
    },
    sort_keys=False,
).encode()


def _do_write(way: Rest | Console, write: str) -> Any:
    if write == "settings":
        return way.settings(_FILE_FOR(way.login.tenant_id))
    if write == "convention":
        return way.convention("confirm-first")
    if write == "target":
        return way.target("new-target")
    if write == "block":
        return way.block(_BLOCKED)
    assert write == "policy"
    return way.policy()


#: The rows each write must leave, by ``op_id``: the extra row a service writes.
_SERVICE_ROW: dict[str, Callable[[AuditLog], bool]] = {
    "block": lambda row: (row.payload or {}).get("op_id") == "principal.block",
    "policy": lambda row: row.event_class == AuditEventClass.CONFIG_CHANGE,
}

_OK = {200, 201, 204, 303}


def _all_rows() -> list[AuditLog]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(AuditLog))).scalars().all())

    return _run(_do())


def _write_and_read_rows(
    key: Any, login: Login, way_name: str, write: str
) -> tuple[list[AuditLog], Any]:
    """Do one write as *login* and return the audit rows it left, with the answer."""
    _seed_world()
    way = _WAYS[way_name](key, login)
    before = {row.id for row in _all_rows()}
    response = _do_write(way, write)
    assert response.status_code in _OK, (response.status_code, response.text)
    rows = [row for row in _all_rows() if row.id not in before]
    assert rows, "the write left no audit row"
    return rows, response


def _noted(row: AuditLog) -> bool:
    return (row.payload or {}).get(_NOTE) == _NOTE_VALUE


def _assert_rows(rows: list[AuditLog], login: Login, write: str) -> None:
    for row in rows:
        assert row.operator_sub == login.sub, row.payload
        assert row.tenant_id == login.tenant_id, (row.path, row.payload)
    if login.expect_note:
        silent = [(row.path, (row.payload or {}).get("op_id")) for row in rows if not _noted(row)]
        assert silent == []
    else:
        noted = [(row.path, (row.payload or {}).get("op_id")) for row in rows if _noted(row)]
        assert noted == []
    wanted = _SERVICE_ROW.get(write)
    if wanted is not None:
        assert any(wanted(row) for row in rows), [(row.path, row.event_class) for row in rows]


_WRITES = ["settings", "convention", "target", "block", "policy"]
_WAY_NAMES = ["console", "rest"]


# ---------------------------------------------------------------------------
# A platform admin outside their role groups: the note, in both audit modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", [False, True], ids=["wrapper-rows", "wrapper-rows-retired"])
@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize("way", _WAY_NAMES)
def test_every_row_of_the_write_carries_the_note(
    monkeypatch: pytest.MonkeyPatch, key: Any, way: str, write: str, retired: bool
) -> None:
    _audit_mode(monkeypatch, retired=retired)
    rows, _ = _write_and_read_rows(key, OUTSIDE, way, write)
    _assert_rows(rows, OUTSIDE, write)


# ---------------------------------------------------------------------------
# Where there is a role group, or no platform admin: no note
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", [False, True], ids=["wrapper-rows", "wrapper-rows-retired"])
@pytest.mark.parametrize("login", [INSIDE, PLAIN], ids=["platform-admin-in-own-tenant", "plain"])
@pytest.mark.parametrize("write", _WRITES)
@pytest.mark.parametrize("way", _WAY_NAMES)
def test_no_note_with_a_role_group_or_without_platform_admin(
    monkeypatch: pytest.MonkeyPatch, key: Any, way: str, write: str, login: Login, retired: bool
) -> None:
    _audit_mode(monkeypatch, retired=retired)
    rows, _ = _write_and_read_rows(key, login, way, write)
    _assert_rows(rows, login, write)


# ---------------------------------------------------------------------------
# A console tenant switch: the note follows the tenant switched to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "to_tenant", "expect_note"),
    [(OUTSIDE, _TENANT_A, False), (INSIDE, _TENANT_C, True)],
    ids=["c-to-a-no-note", "a-to-c-note"],
)
def test_the_switch_row_has_the_note_only_without_a_role_group(
    key: Any, start: Login, to_tenant: uuid.UUID, expect_note: bool
) -> None:
    _seed_world()
    console = Console(key, start)
    before = {row.id for row in _all_rows()}
    response = console.switch(to_tenant)
    assert response.status_code == 303, response.text
    rows = [
        row
        for row in _all_rows()
        if row.id not in before and (row.payload or {}).get("op_id") == "meho.ui.tenant.switch"
    ]
    assert len(rows) == 1, [(row.path, row.payload) for row in _all_rows()]
    row = rows[0]
    assert row.tenant_id == to_tenant
    if expect_note:
        assert (row.payload or {}).get(_NOTE) == _NOTE_VALUE
    else:
        assert _NOTE not in (row.payload or {})


# ---------------------------------------------------------------------------
# The user guide sentence
# ---------------------------------------------------------------------------


def test_the_guide_sentence_is_true_for_the_console_and_for_rest(
    monkeypatch: pytest.MonkeyPatch, key: Any
) -> None:
    text = " ".join(_GUIDE.read_text(encoding="utf-8").split())
    sentence = (
        "Every audit row of a request where the role came from the platform admin group "
        "carries `tenant_role_source: platform_admin`"
    )
    assert sentence in text, "the guide sentence changed: change this test with it"

    _seed_world()
    for way_name in _WAY_NAMES:
        way = _WAYS[way_name](key, OUTSIDE)
        before = {row.id for row in _all_rows()}
        response = way.convention(f"guide-{way_name}")
        assert response.status_code in _OK, response.text
        rows = [row for row in _all_rows() if row.id not in before]
        assert rows
        assert all(_noted(row) for row in rows)
