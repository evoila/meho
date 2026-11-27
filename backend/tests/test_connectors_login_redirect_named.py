# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector's own login that ends on a redirect MEHO did not follow is never a login.

The phpIPAM user login and the Proxmox ticket login are named logins: they
follow a redirect on the same origin, as a read does. A redirect to another
origin ends the login before the login code reads the body, so a token in
that body is never used. With the switch ``REDIRECT_ON_CHANGE_RESULT`` on,
the error is MEHO's fixed login sentence. Names and addresses are made up;
every answer comes from ``respx``.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.connectors._shared.login_redirect import SessionLoginRedirectError
from meho_backplane.connectors.phpipam import PhpipamConnector, PhpipamCredentials
from meho_backplane.connectors.phpipam._errors import PhpipamApiError
from meho_backplane.connectors.proxmox import ProxmoxConnector
from meho_backplane.settings import get_settings
from tests._phpipam_fake import APP_ID, BASE_URL
from tests.test_connectors_phpipam import _operator
from tests.test_connectors_phpipam import _Target as _IpamTarget

_TOKEN_IN_BODY = "token-in-redirect-body-5d"
_LOGIN_PATH = f"/api/{APP_ID}/user/"


@pytest.fixture
def answer_switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")

    def set_to(on: bool) -> None:
        monkeypatch.setenv("REDIRECT_ON_CHANGE_RESULT", "true" if on else "false")
        get_settings.cache_clear()

    yield set_to
    get_settings.cache_clear()


def _ipam() -> PhpipamConnector:
    async def loader(target: Any, operator: Any) -> PhpipamCredentials:
        return PhpipamCredentials(username="made-up", password="made-up-pw")

    return PhpipamConnector(credentials_loader=loader)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_phpipam_login_redirect_to_another_host_is_never_a_login(
    on: bool, status: int, answer_switch: Any
) -> None:
    answer_switch(on)
    conn = _ipam()
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(f"{BASE_URL}{_LOGIN_PATH}").respond(
            status,
            headers={"Location": "https://other.test.invalid/user/"},
            json={"code": 200, "success": True, "data": {"token": _TOKEN_IN_BODY}},
        )
        other = mock.route(host="other.test.invalid").respond(200, json={})
        operation = mock.get(url__startswith=f"{BASE_URL}/api/{APP_ID}/subnets").respond(
            200, json={"code": 200, "success": True, "data": []}
        )
        with pytest.raises((PhpipamApiError, SessionLoginRedirectError)) as refused:
            await conn.subnet_list(_operator(), _IpamTarget(), {})
    await conn.aclose()

    assert (login.call_count, other.call_count, operation.call_count) == (1, 0, 0)
    assert _TOKEN_IN_BODY not in str(refused.value)
    if on:
        assert type(refused.value) is SessionLoginRedirectError
    else:
        assert type(refused.value) is PhpipamApiError
        assert refused.value.http_status == status
        assert refused.value.upstream_message is None


def _proxmox() -> ProxmoxConnector:
    return ProxmoxConnector()


def _pve_target() -> Any:
    return SimpleNamespace(
        name="pve-test",
        host="pve.test.invalid",
        port=8006,
        id="33333333-3333-3333-3333-333333333333",
        tenant_id="00000000-0000-0000-0000-000000000000",
        secret_ref="targets/pve-test",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )


_PVE = "https://pve.test.invalid:8006"
_TICKET = "/api2/json/access/ticket"
_CREDS = SimpleNamespace(mode="ticket", username="made-up@pve", password="made-up-pw")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_the_proxmox_ticket_login_follows_a_same_origin_redirect(
    status: int, answer_switch: Any
) -> None:
    answer_switch(True)
    conn = _proxmox()
    async with respx.mock(base_url=_PVE, assert_all_called=False) as mock:
        mock.post(_TICKET).respond(status, headers={"Location": f"{_TICKET}/"})
        mock.route(path=f"{_TICKET}/").respond(
            200, json={"data": {"ticket": "t-1", "CSRFPreventionToken": "c-1"}}
        )
        session = await conn._ticket_session(_pve_target(), _operator(), _CREDS)  # type: ignore[arg-type]
    await conn.aclose()
    assert session == {"ticket": "t-1", "csrf": "c-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_the_proxmox_ticket_login_never_follows_another_origin(
    on: bool, status: int, answer_switch: Any
) -> None:
    answer_switch(on)
    conn = _proxmox()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{_PVE}{_TICKET}").respond(
            status, headers={"Location": "https://other.test.invalid/ticket"}
        )
        other = mock.route(host="other.test.invalid").respond(200, json={})
        with pytest.raises((httpx.HTTPStatusError, SessionLoginRedirectError)) as refused:
            await conn._ticket_session(_pve_target(), _operator(), _CREDS)  # type: ignore[arg-type]
    await conn.aclose()

    assert other.call_count == 0
    if on:
        assert str(refused.value) == (
            f"The login of this connector got HTTP {status}, a redirect. MEHO did not "
            "follow it, so the login did not finish."
        )
    else:
        assert type(refused.value) is httpx.HTTPStatusError
