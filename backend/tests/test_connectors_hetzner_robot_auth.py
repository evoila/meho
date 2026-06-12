# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`HetznerRobotConnector` (G3.7-T7 #846).

Exercises HTTP Basic auth, no-retry-on-401 (IP-block protection), per-target
credential isolation, the ``_post_form`` helper, the auth_model boundary gate,
and fingerprint / probe shapes against mocked Robot Webservice endpoints.

Auth: no session token — HTTP Basic sent on every request with the Webservice-
user credentials loaded from Vault (injectable for tests).  A 401 response
raises ``auth_failed`` immediately with an instructional message — no retry,
no second attempt.  Three consecutive 401s from one IP trigger a 10-minute
IP block on Hetzner Robot; this connector protects every operator on the
shared egress IP by failing on the first.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.hetzner_robot import (
    HetznerRobotConnector,
    HetznerRobotTargetLike,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel


def _make_operator(raw_jwt: str = "") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_hetzner_robot_registry() -> Iterator[None]:
    """Re-register HetznerRobotConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that calls
    :func:`clear_registry` between tests.  Re-register before every test in
    this module and clear after — same pattern established by
    :mod:`tests.test_connectors_harbor_auth`.
    """
    clear_registry()
    register_connector_v2(
        product=HetznerRobotConnector.product,
        version=HetznerRobotConnector.version,
        impl_id=HetznerRobotConnector.impl_id,
        cls=HetznerRobotConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies HetznerRobotTargetLike Protocol structurally.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    # Tenant-unique cache key components (#1642/#1672).
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="robot-a",
    host="robot.your-server.de",
    port=443,
    secret_ref="hetzner/robot-a",
)
_TARGET_B = _StubTarget(
    name="robot-b",
    host="robot.your-server.de",
    port=None,
    secret_ref="hetzner/robot-b",
)


async def _stub_loader(_target: HetznerRobotTargetLike) -> dict[str, str]:
    return {"username": "webservice-user", "password": "stub-password"}


def _make_connector() -> HetznerRobotConnector:
    return HetznerRobotConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_hetzner_robot_connector_subclasses_http_connector() -> None:
    assert issubclass(HetznerRobotConnector, HttpConnector)
    assert HetznerRobotConnector.product == "hetzner-robot"
    assert HetznerRobotConnector.version == "2026.04"
    assert HetznerRobotConnector.impl_id == "hetzner-rest"
    assert HetznerRobotConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("hetzner-robot", "2026.04", "hetzner-rest")
    assert key in registry
    assert registry[key] is HetznerRobotConnector


def test_default_credentials_loader_raises_until_g214_lands() -> None:
    import asyncio

    from meho_backplane.connectors.hetzner_robot.session import load_credentials_from_vault

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"Goal #214"):
            await load_credentials_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# HTTP Basic auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_basic_auth_header() -> None:
    """auth_headers() produces Authorization: Basic with Webservice-user credentials."""
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == "webservice-user"
    assert password == "stub-password"
    await connector.aclose()


# ---------------------------------------------------------------------------
# 401 → auth_failed immediately, exactly ONE request issued (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_raises_auth_failed_with_instructional_message() -> None:
    """A 401 from the Robot API raises RuntimeError with 'auth_failed' immediately."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        route = mock.get("/server").respond(401)
        with pytest.raises(RuntimeError, match="auth_failed") as exc_info:
            await connector._get_robot_json(_TARGET_A, "/server")

    # Exactly one request issued — no retry on 401.
    assert route.call_count == 1
    assert "robot-a" in str(exc_info.value)
    assert "Webservice" in str(exc_info.value) or "credentials" in str(exc_info.value).lower()
    await connector.aclose()


@pytest.mark.asyncio
async def test_401_issues_exactly_one_request_no_retry() -> None:
    """401 must not trigger tenacity retries — assert call count is exactly 1."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        route = mock.get("/server").respond(401)
        with pytest.raises(RuntimeError, match="auth_failed"):
            await connector._get_robot_json(_TARGET_A, "/server")

    assert route.call_count == 1, (
        f"expected exactly 1 request on 401 (no retry), got {route.call_count}"
    )
    await connector.aclose()


# ---------------------------------------------------------------------------
# _post_form sends application/x-www-form-urlencoded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_form_sends_form_encoded_content_type() -> None:
    """_post_form sends data as application/x-www-form-urlencoded, not JSON."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        route = mock.post("/server/reset").respond(200, json={"result": "ok"})
        await connector._post_form(
            _TARGET_A,
            "/server/reset",
            data={"server_ip": "1.2.3.4", "type": "sw"},
        )

    assert route.call_count == 1
    request = route.calls[0].request
    content_type = request.headers.get("content-type", "")
    assert "application/x-www-form-urlencoded" in content_type, (
        f"Expected form-encoded Content-Type, got {content_type!r}"
    )
    await connector.aclose()


@pytest.mark.asyncio
async def test_post_form_encodes_dict_as_form_data() -> None:
    """_post_form body contains key=value pairs in form-encoded format."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        route = mock.post("/server/reset").respond(200, json={"result": "ok"})
        await connector._post_form(
            _TARGET_A,
            "/server/reset",
            data={"server_ip": "1.2.3.4", "type": "sw"},
        )

    request = route.calls[0].request
    body = request.content.decode()
    assert "server_ip=1.2.3.4" in body
    assert "type=sw" in body
    await connector.aclose()


@pytest.mark.asyncio
async def test_post_form_401_raises_auth_failed_immediately() -> None:
    """_post_form also raises auth_failed on 401 without retry."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        route = mock.post("/server/reset").respond(401)
        with pytest.raises(RuntimeError, match="auth_failed"):
            await connector._post_form(_TARGET_A, "/server/reset", data={"server_ip": "1.2.3.4"})

    assert route.call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_credentials_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: HetznerRobotTargetLike) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "webservice-user", "password": "stub-password"}

    connector = HetznerRobotConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2
    assert call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-target client isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_credentials_separate() -> None:
    """Two targets get two distinct credential cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: HetznerRobotTargetLike) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = HetznerRobotConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    username_a, _ = _decode_basic_auth(h_a["Authorization"])
    username_b, _ = _decode_basic_auth(h_b["Authorization"])
    assert username_a == "svc-robot-a"
    assert username_b == "svc-robot-b"
    assert call_log == ["robot-a", "robot-b"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_get_distinct_credentials() -> None:
    """Same-named targets in DIFFERENT tenants never share cached credentials.

    Regression guard for #1642/#1672: the credential cache used to key on
    ``target.name`` alone, so two same-named targets in different tenants
    collapsed onto one entry. The cache keys on the tenant-unique
    ``(tenant_id, id)`` tuple instead.
    """
    tenant_one = _StubTarget(
        name="robot-shared",
        host="robot.your-server.de",
        port=443,
        secret_ref="hetzner/robot-shared",
        id=UUID(int=0x1),
        tenant_id=UUID(int=0x100),
    )
    tenant_two = _StubTarget(
        name="robot-shared",
        host="robot.your-server.de",
        port=443,
        secret_ref="hetzner/robot-shared",
        id=UUID(int=0x2),
        tenant_id=UUID(int=0x200),
    )
    call_log: list[str] = []

    async def _tracking_loader(target: HetznerRobotTargetLike) -> dict[str, str]:
        call_log.append(str(target.tenant_id))
        return {"username": f"svc-{target.tenant_id}", "password": "pass"}

    connector = HetznerRobotConnector(credentials_loader=_tracking_loader)
    h_one = await connector.auth_headers(tenant_one, operator=_make_operator())
    h_two = await connector.auth_headers(tenant_two, operator=_make_operator())

    user_one, _ = _decode_basic_auth(h_one["Authorization"])
    user_two, _ = _decode_basic_auth(h_two["Authorization"])
    # Each tenant loaded its own credential — no cross-tenant cache hit.
    assert user_one == f"svc-{tenant_one.tenant_id}"
    assert user_two == f"svc-{tenant_two.tenant_id}"
    assert len(call_log) == 2
    assert set(connector._creds_cache) == {
        target_cache_key(tenant_one),
        target_cache_key(tenant_two),
    }

    # Same-tenant re-fetch is a cache HIT — loader not re-invoked.
    await connector.auth_headers(tenant_one, operator=_make_operator())
    assert len(call_log) == 2
    await connector.aclose()


# ---------------------------------------------------------------------------
# Vault loader missing key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: HetznerRobotTargetLike) -> dict[str, str]:
        return {"username": "webservice-user"}  # type: ignore[return-value]

    connector = HetznerRobotConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "robot-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: HetznerRobotTargetLike) -> dict[str, str]:
        return {"password": "stub-password"}  # type: ignore[return-value]

    connector = HetznerRobotConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "robot-a" in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="robot-per-user",
        host="robot.your-server.de",
        port=443,
        secret_ref="hetzner/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "robot-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="robot-pre-g03",
        host="robot.your-server.de",
        port=443,
        secret_ref="hetzner/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="robot-enum",
        host="robot.your-server.de",
        port=443,
        secret_ref="hetzner/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against mocked GET /server returns account_id + server_count."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        mock.get("/server").respond(
            200,
            json=[
                {"server": {"server_number": 123456, "server_ip": "1.2.3.4", "dc": "FSN1-DC1"}},
                {"server": {"server_number": 789012, "server_ip": "5.6.7.8", "dc": "NBG1-DC3"}},
            ],
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "hetzner"
    assert fp.product == "robot-webservice"
    assert fp.reachable is True
    assert fp.probe_method == "GET /server"
    assert fp.extras["server_count"] == 2
    assert fp.extras["account_id"] == "123456"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_empty_server_list_returns_none_account_id() -> None:
    """An account with no servers yields account_id=None and server_count=0."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        mock.get("/server").respond(200, json=[])
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is True
    assert fp.extras["server_count"] == 0
    assert fp.extras["account_id"] is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_structured_error() -> None:
    """Transport failure returns reachable=False with extras['error']."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        mock.get("/server").respond(401)
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "hetzner"
    assert fp.product == "robot-webservice"
    assert fp.reachable is False
    assert fp.probe_method == "GET /server"
    error = fp.extras["error"]
    assert "auth_failed" in error or "401" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_dict_wrapper_shape_also_parsed() -> None:
    """The ``{"servers": [...]}`` wrapper shape is normalised correctly."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        mock.get("/server").respond(
            200,
            json={
                "servers": [
                    {"server": {"server_number": 111111, "server_ip": "9.9.9.9", "dc": "HEL1-DC2"}}
                ]
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is True
    assert fp.extras["server_count"] == 1
    assert fp.extras["account_id"] == "111111"
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_true_on_authenticated_get() -> None:
    """probe() returns ok=True when GET /server succeeds."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        mock.get("/server").respond(200, json=[])
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_on_401() -> None:
    """probe() returns ok=False with auth_failed reason on 401 (not retried)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        route = mock.get("/server").respond(401)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "auth_failed" in result.reason
    # Must not have retried — exactly one request
    assert route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_on_transport_error() -> None:
    """probe() returns ok=False + reason on non-auth transport failure."""
    connector = _make_connector()

    async with respx.mock(base_url="https://robot.your-server.de") as mock:
        mock.get("/server").respond(503)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_credential_cache_and_pool() -> None:
    """aclose() clears the in-memory credential cache and tears down the httpx pool."""
    connector = _make_connector()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert target_cache_key(_TARGET_A) in connector._creds_cache
    await connector.aclose()
    assert connector._creds_cache == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_credentials_is_a_noop() -> None:
    """A fresh connector with no credentials established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._creds_cache == {}
