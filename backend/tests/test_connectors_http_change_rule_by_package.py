# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which connectors get the change rule: the ones of MEHO, and packages that name their logins.

A connector that comes with MEHO gets the change rule for every request that
is neither a read nor a named login. A connector package gets it only when it
declares ``names_its_logins``. A package without the mark keeps the rule of a
read for every request, so a login it sends outside ``auth_headers`` still
follows a redirect on the same origin. Names and addresses are made up; every
answer comes from ``respx``.
"""

from __future__ import annotations

import types
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane import connector_packages
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.argocd import ArgoCdConnector
from meho_backplane.connectors.proxmox import ProxmoxConnector
from meho_backplane.settings import get_settings

_BASE = "https://pkg.example.test"
#: A made-up connector folder that a made-up package installed.
_FOLDER = "mehotestpkgfolder"


def _operator() -> Operator:
    return Operator(
        sub="op-package-rule",
        name=None,
        email=None,
        raw_jwt="made-up.jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _target() -> Any:
    return types.SimpleNamespace(
        name="pkg",
        host="pkg.example.test",
        port=443,
        id="22222222-2222-2222-2222-222222222222",
        tenant_id="00000000-0000-0000-0000-000000000000",
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )


class _Base(HttpConnector):
    product = "pkg-test"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {"Authorization": "Bearer made-up"}

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def own_login(self, target: Any) -> httpx.Response:
        """A login the package sends itself, outside ``auth_headers`` and unnamed."""
        client = await self._http_client(target)
        return await client.post("/api/session", json={"user": "made-up"})


class _PackageConn(_Base):
    """A connector of a package that does not name its logins."""


class _MarkedPackageConn(_Base):
    """A connector of a package that names its logins."""

    names_its_logins = True


_PackageConn.__module__ = f"meho_backplane.connectors.{_FOLDER}.connector"
_MarkedPackageConn.__module__ = f"meho_backplane.connectors.{_FOLDER}.connector"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("REDIRECT_ON_CHANGE_RESULT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _installed_package() -> Iterator[None]:
    """Record the made-up folder as installed by a package, as the start check does."""
    saved = dict(connector_packages._installed_folders)
    connector_packages.record_installed_connector_packages(
        {**saved, _FOLDER: "meho-connectors-made-up"}
    )
    yield
    connector_packages.record_installed_connector_packages(saved)


def test_a_connector_of_meho_gets_the_change_rule() -> None:
    assert ArgoCdConnector.uses_change_rule() is True
    assert ProxmoxConnector.uses_change_rule() is True


def test_a_package_gets_the_change_rule_only_with_its_mark() -> None:
    assert _PackageConn.uses_change_rule() is False
    assert _MarkedPackageConn.uses_change_rule() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cls", "expected"), [(_PackageConn, False), (_MarkedPackageConn, True), (_Base, True)]
)
async def test_the_pooled_client_carries_the_rule_of_its_connector(
    cls: type[_Base], expected: bool
) -> None:
    conn = cls()
    client = await conn._http_client(_target())
    assert client.change_rule is expected  # type: ignore[attr-defined]
    await conn.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_an_unnamed_login_of_a_package_without_the_mark_follows_a_same_origin_redirect(
    status: int,
) -> None:
    # The rule of a read, for every request of the package.
    conn = _PackageConn()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(status, headers={"Location": "/api/v2/session"})
        again = mock.route(path="/api/v2/session").respond(200, json={"token": "made-up"})
        resp = await conn.own_login(_target())
    await conn.aclose()
    assert resp.status_code == 200
    assert again.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_an_unnamed_login_of_a_package_with_the_mark_gets_the_change_rule(
    status: int,
) -> None:
    # With the mark, a request that is not named as a login is a change.
    conn = _MarkedPackageConn()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(status, headers={"Location": "/api/v2/session"})
        again = mock.route(path="/api/v2/session").respond(200, json={"token": "made-up"})
        resp = await conn.own_login(_target())
    await conn.aclose()
    assert resp.status_code == status
    assert again.call_count == 0


@pytest.mark.asyncio
async def test_a_change_of_a_package_without_the_mark_follows_a_same_origin_303() -> None:
    conn = _PackageConn()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.put("/items/7").respond(303, headers={"Location": "/jobs/42"})
        job = mock.get("/jobs/42").respond(200, json={"ok": True})
        await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()
    assert job.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [_PackageConn, _MarkedPackageConn, _Base])
async def test_no_connector_sends_a_change_to_another_host(cls: type[_Base]) -> None:
    conn = cls()
    with respx.mock(assert_all_called=False) as mock:
        mock.put(f"{_BASE}/items/7").respond(307, headers={"Location": "https://other.test/x"})
        other = mock.route(host="other.test").respond(200, json={})
        with pytest.raises(httpx.HTTPStatusError) as refused:
            await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()
    assert refused.value.response.status_code == 307
    assert other.call_count == 0


class _WriteInAuthHeaders(_Base):
    """``auth_headers`` sends a write that is not a login, before it answers."""

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        client = await self._http_client(target)
        resp = await client.put("/api/token-note", json={"seen": True})
        return {"X-Note": str(resp.status_code)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["https://other.test/api/token-note", "http://pkg.example.test/api/token-note"],
    ids=["other-host", "other-scheme"],
)
async def test_a_write_inside_auth_headers_never_goes_to_another_origin(location: str) -> None:
    conn = _WriteInAuthHeaders()
    with respx.mock(assert_all_called=False) as mock:
        mock.put(f"{_BASE}/api/token-note").respond(307, headers={"Location": location})
        elsewhere = mock.route(url__startswith=location.rsplit("/", 2)[0]).respond(200)
        headers = await conn.auth_headers(_target(), _operator())
    await conn.aclose()
    assert headers == {"X-Note": "307"}
    assert elsewhere.call_count == 0


@pytest.mark.asyncio
async def test_a_write_inside_auth_headers_follows_only_the_same_origin() -> None:
    conn = _WriteInAuthHeaders()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.put("/api/token-note").respond(302, headers={"Location": "/api/token-note/"})
        same = mock.get("/api/token-note/").respond(200)
        headers = await conn.auth_headers(_target(), _operator())
    await conn.aclose()
    assert headers == {"X-Note": "200"}
    assert same.call_count == 1
