# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""phpIPAM connector: the four operations, the login and the read-back, on a fake phpIPAM.

The handlers run against :class:`tests._phpipam_fake.FakePhpipam` through
respx, so every vendor call is the real HTTP request the connector sends.
Documentation addresses and made-up names only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.phpipam import PhpipamConnector, PhpipamCredentials
from meho_backplane.connectors.phpipam._common import PhpipamRefusedError
from meho_backplane.connectors.phpipam._errors import PhpipamAddressTakenError, PhpipamApiError
from meho_backplane.connectors.phpipam.session import (
    PhpipamConfigError,
    parse_credentials,
    resolve_target_config,
)
from meho_backplane.settings import get_settings

from ._phpipam_fake import APP_ID, BASE_URL, HOST, FakePhpipam

_TENANT = UUID("00000000-0000-0000-0000-000000005070")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Target:
    def __init__(self, **extras: Any) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "ipam-test"
        self.host = HOST
        self.port = 443
        self.secret_ref = "tenants/test/ipam-test"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {"app_id": APP_ID, **extras}


def _operator() -> Operator:
    return Operator(
        sub="op-ipam",
        name="IPAM operator",
        email=None,
        raw_jwt="op.ipam.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _fake() -> FakePhpipam:
    fake = FakePhpipam()
    fake.add_subnet(7, "192.0.2.0/24")
    fake.add_address(7, "192.0.2.1", "gw.example.test")
    fake.add_address(7, "192.0.2.2", "dns.example.test")
    return fake


def _connector(fake: FakePhpipam) -> PhpipamConnector:
    async def _loader(target: Any, operator: Operator) -> PhpipamCredentials:
        if fake.app_token is not None:
            return PhpipamCredentials(app_token=fake.app_token)
        return PhpipamCredentials(username=fake.username, password=fake.password)

    return PhpipamConnector(credentials_loader=_loader)


async def _run(fake: FakePhpipam, handler: str, params: dict[str, Any], **extras: Any) -> Any:
    conn = _connector(fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        return await getattr(conn, handler)(_operator(), _Target(**extras), params)


# ---------------------------------------------------------------------------
# Target settings and secret
# ---------------------------------------------------------------------------


def test_target_config_needs_app_id_and_a_clean_base_path() -> None:
    assert resolve_target_config(_Target(base_path="/phpipam/")).base_path == "/phpipam"
    with pytest.raises(PhpipamConfigError, match="app_id"):
        resolve_target_config(_Target(app_id="bad id"))
    with pytest.raises(PhpipamConfigError, match="base_path"):
        resolve_target_config(_Target(base_path="/../admin"))


def test_secret_shapes_and_error_names_fields_only() -> None:
    assert parse_credentials({"token": " abc\n"}, target_name="t").app_token == "abc"
    user = parse_credentials({"username": "meho", "password": "pw-planted"}, target_name="t")
    assert user.mode == "user_token"
    assert "pw-planted" not in repr(user)
    with pytest.raises(PhpipamConfigError) as info:
        parse_credentials({"user": "x", "pass": "pw-planted"}, target_name="t")
    assert "pw-planted" not in str(info.value)
    assert "['pass', 'user']" in str(info.value)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_subnet_list_skips_folders_and_reads_usage() -> None:
    fake = _fake()
    fake.add_subnet(8, "198.51.100.0/24", description="second net")
    fake.add_subnet(9, "0.0.0.0/0", isFolder="1", subnet="", mask="")
    result = await _run(fake, "subnet_list", {})
    assert result["total"] == 2
    assert result["usage_rows"] == 2
    first = result["subnets"][0]
    assert first["id"] == 7
    assert first["cidr"] == "192.0.2.0/24"
    assert first["used"] == 2
    assert first["max_hosts"] == 254
    assert first["used_percent"] == round(200 / 254, 2)


async def test_subnet_list_by_cidr_uses_the_cidr_call_and_404_is_empty() -> None:
    fake = _fake()
    result = await _run(fake, "subnet_list", {"cidr": "192.0.2.0/24"})
    assert [row["id"] for row in result["subnets"]] == [7]
    assert fake.calls_of("GET", f"/api/{APP_ID}/subnets/cidr/192.0.2.0/24/")
    empty = await _run(_fake(), "subnet_list", {"cidr": "203.0.113.0/24"})
    assert empty["subnets"] == []
    with pytest.raises(PhpipamRefusedError, match="CIDR"):
        await _run(_fake(), "subnet_list", {"cidr": "192.0.2.7/24"})


async def test_free_addresses_skips_used_network_and_broadcast() -> None:
    fake = _fake()
    result = await _run(fake, "subnet_free_addresses", {"subnet_id": 7})
    assert result["returned"] == 20
    assert result["addresses"][0] == {"ip": "192.0.2.3"}
    assert result["free_count"] == 254 - 2
    assert result["truncated"] is True


async def test_free_addresses_small_ipv6_subnet() -> None:
    fake = FakePhpipam()
    fake.add_subnet(11, "2001:db8::/126")
    fake.add_address(11, "2001:db8::1", "a.example.test")
    result = await _run(fake, "subnet_free_addresses", {"subnet_id": 11})
    assert [row["ip"] for row in result["addresses"]] == ["2001:db8::2", "2001:db8::3"]
    assert result["free_count"] == 2


async def test_free_addresses_refuses_a_subnet_with_child_subnets() -> None:
    fake = _fake()
    fake.add_subnet(12, "192.0.2.0/25")
    fake.children[7] = [12]
    with pytest.raises(PhpipamRefusedError, match="child subnets"):
        await _run(fake, "subnet_free_addresses", {"subnet_id": 7})


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


async def test_reserve_writes_once_and_reads_back() -> None:
    fake = _fake()
    params = {
        "subnet_id": 7,
        "ip": "192.0.2.10",
        "hostname": "web-01.example.test",
        "description": "web server",
        "note": "example build",
    }
    result = await _run(fake, "address_reserve", params)
    assert result["status"] == "reserved"
    assert result["verification"] == "verified"
    assert result["changed"] is True
    assert result["address_id"] is not None
    posts = fake.calls_of("POST", f"/api/{APP_ID}/addresses/")
    assert posts == [
        (
            "POST",
            f"/api/{APP_ID}/addresses/",
            {
                "subnetId": "7",
                "ip": "192.0.2.10",
                "hostname": "web-01.example.test",
                "description": "web server",
                "note": "example build",
            },
        )
    ]


async def test_reserve_twice_changes_nothing_the_second_time() -> None:
    fake = _fake()
    params = {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01.example.test"}
    first = await _run(fake, "address_reserve", params)
    second = await _run(fake, "address_reserve", {**params, "hostname": "WEB-01.example.test."})
    assert first["status"] == "reserved"
    assert second["status"] == "nothing_to_do"
    assert second["changed"] is False
    assert len(fake.calls_of("POST")) == 1


async def test_reserve_first_free_twice_reserves_one_address() -> None:
    fake = _fake()
    params = {"subnet_id": 7, "first_free": True, "hostname": "db-01.example.test"}
    first = await _run(fake, "address_reserve", params)
    second = await _run(fake, "address_reserve", params)
    assert first["ip"] == "192.0.2.3"
    assert first["verification"] == "verified"
    assert second["status"] == "nothing_to_do"
    assert second["ip"] == "192.0.2.3"
    assert len(fake.calls_of("POST")) == 1


async def test_reserve_of_a_taken_address_fails_and_names_the_holder() -> None:
    fake = _fake()
    with pytest.raises(PhpipamAddressTakenError) as info:
        await _run(
            fake, "address_reserve", {"subnet_id": 7, "ip": "192.0.2.1", "hostname": "web-02"}
        )
    assert "gw.example.test" not in str(info.value)
    assert "another host name" in str(info.value)
    assert info.value.upstream_message == "gw.example.test"
    assert info.value.held_by == "gw.example.test"
    assert fake.calls_of("POST") == []


async def test_reserve_race_409_reads_the_holder() -> None:
    fake = _fake()
    original = fake._subnet_address

    def _address_hidden_once(subnet_id: int, ip: str) -> httpx.Response:
        # The first read says "free"; someone else books it before the POST.
        fake._subnet_address = original  # type: ignore[method-assign]
        fake.add_address(7, "192.0.2.10", "other.example.test")
        return httpx.Response(404, json={"code": 404, "success": False, "message": "none"})

    fake._subnet_address = _address_hidden_once  # type: ignore[method-assign]
    with pytest.raises(PhpipamAddressTakenError) as info:
        await _run(fake, "address_reserve", {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "w"})
    assert info.value.upstream_message == "other.example.test"


async def test_address_that_is_not_the_first_of_its_subnet_is_found() -> None:
    # 192.0.2.1 is the first address row of subnet 7, so 192.0.2.10 is not.
    # The fake answers GET /addresses/{ip}/{subnet}/ only for the first row,
    # like phpIPAM 1.8.2, so this checks that MEHO reads the address another way.
    fake = _fake()
    params = {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01.example.test"}
    first = await _run(fake, "address_reserve", params)
    second = await _run(fake, "address_reserve", params)
    assert first["status"] == "reserved"
    assert first["verification"] == "verified"
    assert second["status"] == "nothing_to_do"
    assert len(fake.calls_of("POST")) == 1
    reads = fake.calls_of("GET", f"/api/{APP_ID}/subnets/7/addresses/192.0.2.10/")
    assert len(reads) == 3

    released = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.10"})
    again = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.10"})
    assert released["status"] == "released"
    assert released["released_hostname"] == "web-01.example.test"
    assert released["verification"] == "verified"
    assert again["status"] == "nothing_to_do"
    assert fake.calls_of("DELETE") == [("DELETE", f"/api/{APP_ID}/addresses/192.0.2.10/7/", None)]
    assert fake.calls_of("GET", f"/api/{APP_ID}/addresses/192.0.2.10/") == []


async def test_reserve_readback_mismatch_is_not_verified() -> None:
    fake = _fake()
    fake.readback_hostname = "somebody-else.example.test"
    result = await _run(
        fake, "address_reserve", {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01"}
    )
    assert result["status"] == "reserved"
    assert result["verification"] == "not_verified"


async def test_reserve_lost_answer_is_unknown_with_no_second_call() -> None:
    fake = _fake()
    fake.lose_write = True
    result = await _run(
        fake, "address_reserve", {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01"}
    )
    assert result["status"] == "unknown"
    assert result["verification"] == "unknown"
    assert result["changed"] is None
    assert "did not send it again" in result["guidance"]
    assert len(fake.calls_of("POST")) == 1
    # The POST was the last call: no read-back, no second write.
    assert fake.calls[-1][0] == "POST"


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"subnet_id": 7, "hostname": "web-01"}, "exactly one"),
        ({"subnet_id": 7, "ip": "192.0.2.10", "first_free": True, "hostname": "w"}, "exactly one"),
        ({"subnet_id": 7, "ip": "198.51.100.5", "hostname": "web-01"}, "not in subnet"),
        ({"subnet_id": 7, "ip": "192.0.2.255", "hostname": "web-01"}, "broadcast"),
        ({"subnet_id": 7, "ip": "192.0.2.10", "hostname": "bad host"}, "host name"),
    ],
)
async def test_reserve_refuses_bad_input_before_any_write(
    params: dict[str, Any], match: str
) -> None:
    fake = _fake()
    with pytest.raises(PhpipamRefusedError, match=match):
        await _run(fake, "address_reserve", params)
    assert fake.calls_of("POST") == []


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


async def test_release_deletes_without_remove_dns_and_reads_back() -> None:
    fake = _fake()
    result = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.2"})
    assert result["status"] == "released"
    assert result["released_hostname"] == "dns.example.test"
    assert result["verification"] == "verified"
    deletes = fake.calls_of("DELETE")
    assert deletes == [("DELETE", f"/api/{APP_ID}/addresses/192.0.2.2/7/", None)]


async def test_release_twice_changes_nothing_the_second_time() -> None:
    fake = _fake()
    first = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.2"})
    second = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.2"})
    assert first["status"] == "released"
    assert second["status"] == "nothing_to_do"
    assert second["changed"] is False
    assert len(fake.calls_of("DELETE")) == 1


async def test_release_lost_answer_is_unknown_with_no_second_call() -> None:
    fake = _fake()
    fake.lose_write = True
    result = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.2"})
    assert result["status"] == "unknown"
    assert result["verification"] == "unknown"
    assert len(fake.calls_of("DELETE")) == 1
    assert fake.calls[-1][0] == "DELETE"


async def test_release_readback_still_present_is_not_verified() -> None:
    fake = _fake()
    fake.readback_hostname = "kept"
    result = await _run(fake, "address_release", {"subnet_id": 7, "ip": "192.0.2.2"})
    assert result["verification"] == "not_verified"


def _user_fake() -> FakePhpipam:
    fake = _fake()
    fake.app_token = None
    fake.username = "meho-api"
    fake.password = "pw-fake-0001"
    return fake


@pytest.mark.parametrize("user_login", [False, True])
@pytest.mark.parametrize(
    ("handler", "params", "status", "reserved_after"),
    [
        (
            "address_reserve",
            {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01.example.test"},
            "reserved",
            True,
        ),
        ("address_release", {"subnet_id": 7, "ip": "192.0.2.2"}, "released", False),
    ],
)
async def test_refused_readback_keeps_the_write_result_and_forgets_the_secret(
    handler: str, params: dict[str, Any], status: str, reserved_after: bool, user_login: bool
) -> None:
    fake = _user_fake() if user_login else _fake()
    # An app code is refused once; a user token is refused, logged in again, refused again.
    fake.refuse_tokens_after_write = 2 if user_login else 1
    conn = _connector(fake)
    target = _Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        result = await getattr(conn, handler)(_operator(), target, params)
    assert result["status"] == status
    assert result["changed"] is True
    assert result["verification"] == "unknown"
    assert "refused the token" in result["guidance"]
    assert ((7, params["ip"]) in fake.addresses) is reserved_after
    assert len(fake.calls_of("POST", f"/api/{APP_ID}/addresses/") + fake.calls_of("DELETE")) == 1
    assert conn._credentials == {}
    assert conn._tokens == {}


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def test_user_login_runs_once_and_sends_the_token() -> None:
    fake = _user_fake()
    conn = _connector(fake)
    target = _Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.route().mock(side_effect=fake.handler)
        await conn.subnet_list(_operator(), target, {})
        await conn.subnet_list(_operator(), target, {})
    assert len(fake.calls_of("POST", f"/api/{APP_ID}/user/")) == 1
    sent = [call.request.headers.get("phpipam-token") for call in route.calls]
    assert fake.user_token in sent


async def test_refused_user_token_logs_in_again_once() -> None:
    fake = _user_fake()
    conn = _connector(fake)
    target = _Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        await conn.subnet_list(_operator(), target, {})
        fake.refuse_tokens = 1
        result = await conn.subnet_list(_operator(), target, {})
    assert result["total"] == 1
    assert len(fake.calls_of("POST", f"/api/{APP_ID}/user/")) == 2


async def test_second_refusal_is_a_connector_auth_error() -> None:
    fake = _user_fake()
    fake.refuse_tokens = 2
    with pytest.raises(ConnectorAuthError) as info:
        await _run(fake, "subnet_list", {})
    assert info.value.status_code == 401
    assert info.value.stage == "after_relogin"


async def test_failed_login_forgets_the_secret_so_a_corrected_one_works() -> None:
    fake = _user_fake()
    stored = {"password": "pw-wrong-0001"}
    loads = 0

    async def _loader(target: Any, operator: Operator) -> PhpipamCredentials:
        nonlocal loads
        loads += 1
        return PhpipamCredentials(username=fake.username, password=stored["password"])

    conn = PhpipamConnector(credentials_loader=_loader)
    target = _Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        with pytest.raises(PhpipamApiError) as info:
            await conn.subnet_list(_operator(), target, {})
        stored["password"] = fake.password
        result = await conn.subnet_list(_operator(), target, {})
    assert info.value.http_status == 500
    assert "secret" in (info.value.remediation or "")
    assert result["total"] == 1
    assert loads == 2
    assert len(fake.calls_of("POST", f"/api/{APP_ID}/user/")) == 2


async def test_refused_app_token_is_not_retried() -> None:
    fake = _fake()
    fake.refuse_tokens = 1
    with pytest.raises(ConnectorAuthError):
        await _run(fake, "subnet_list", {})
    assert len(fake.calls_of("GET", f"/api/{APP_ID}/subnets/")) == 1


async def test_base_path_is_put_in_front_of_every_call() -> None:
    fake = _fake()
    conn = _connector(fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.get(f"/phpipam/api/{APP_ID}/subnets/").respond(
            200, json={"code": 200, "success": True, "data": []}
        )
        result = await conn.subnet_list(_operator(), _Target(base_path="/phpipam"), {})
    assert route.called
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# Fingerprint and trace
# ---------------------------------------------------------------------------


async def test_fingerprint_is_reachable_on_a_phpipam_answer() -> None:
    fake = _fake()
    conn = _connector(fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        result = await conn.fingerprint(_Target())
        wrong = await conn.fingerprint(_Target(app_id="other"))
    assert result.reachable is True
    assert result.product == "phpipam"
    assert result.extras["api_code"] == 401
    assert wrong.extras["api_message"] == "Invalid application id"


def test_trace_body_masks_tokens_and_passwords() -> None:
    conn = PhpipamConnector()
    response = httpx.Response(
        200,
        json={"code": 200, "data": {"token": "tok-planted", "users": [{"password": "pw"}]}},
    )
    traced = conn.trace_response_body(response)
    assert "tok-planted" not in str(traced)
    assert traced["data"]["users"][0]["password"] == "<redacted>"
