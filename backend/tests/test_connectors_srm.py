# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Site Recovery Manager connector: login, ``srm.about``, ``srm.api_file.get``.

What is covered, with a respx-mocked appliance and no ingested catalog:

* the login (``POST /api/rest/srm/v2/session`` with HTTP Basic ->
  ``session_id``) and the ``x-dr-session`` header on later calls;
* ``srm.about`` through ``dispatch()``: the server summary, certificates
  reduced to a short view;
* two targets on one address: an ``srm`` target and a target of another
  product resolve to two connector classes, and each uses its own login;
* a ``401`` on one SRM target makes a new SRM login for that target only;
  the session of a second SRM target on the same address with its own
  credential and of the other product's target stay as they were;
* two SRM targets that share one credential share one session: parallel
  calls and a renewal on one target do not end the other target's
  session; calls on both targets at once after expiry all work with one
  renewal, even when ``401`` answers arrive late; different credentials
  and different tenants keep their own;
* ``srm.api_file.get`` for both files: status, size, SHA-256, version,
  parts; an unknown file name sends no HTTP request; a 404 and a redirect
  reach the answer; a failed version read still returns the file; a
  secret-named value, an ``Authorization`` example and a secret-named
  redirect query value come back replaced and counted; the replaced copy
  still parses and every value keeps its type; a ``401`` on the version
  read ends only that credential's session; a file that does not parse
  stops; a file too big stops;
* no password or session id in a result; registration shape; fingerprint.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.api_file_secrets import REDACTED
from meho_backplane.connectors._shared.dr_rest import parse_server_version
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.srm import (
    SRM_CONNECTOR_ID,
    SRM_IMPL_ID,
    SRM_PRODUCT,
    SRM_TYPED_OPS,
    SRM_VERSION,
    SrmConnector,
    register_srm_typed_operations,
)
from meho_backplane.connectors.srm.api_file import API_FILES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings
from tests._spec_shelf import require_shelf_spec

_HOST = "srm-typed.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSION = "/api/rest/srm/v2/session"
_INFO = "/api/rest/srm/v2/server/info"
_SRM_FILE = "/api/rest/srm/v2/open-api.yaml"
_CONFIGURE_FILE = "/api/rest/configure/v2/open-api.yaml"
_PASSWORD = "typed-password-value"
_HCX_SESSIONS = "/hybridity/api/sessions"
_HCX_APPLIANCES = "/hybridity/api/hcspAppliances"
_HCX_DOCS = "/hybridity/docs"

_PEM = "-----BEGIN CERTIFICATE-----\nMIIBmadeupcertificatetext\n-----END CERTIFICATE-----"
_CERT = {
    "serial_number": "0a1b2c",
    "not_after": 1893456000000,
    "not_before": 1700000000000,
    "subject_dn": "CN=<srm-name>",
    "issuer_dn": "CN=<ca-name>",
    "thumbprint": "AA:BB:CC",
    "certificate": _PEM,
    "public_key": "made-up-public-key",
    "signature": "made-up-signature",
}
_INFO_PAYLOAD: dict[str, Any] = {
    "id": "srm-id-0001",
    "url": "https://<srm-name>:443",
    "name": "<srm-name>",
    "server_status": "OK",
    "version": "9.1.1.0",
    "build_number": "11111111",
    "vc_guid": "vc-guid-0001",
    "vc_name": "<vcenter-name>",
    "extension_key": "com.vmware.vcDr",
    "certificates": [_CERT],
    "certificate_warnings": [],
    "_meta": {"errors": []},
}
_YAML = "openapi: 3.0.1\ninfo:\n  title: made-up API\n  version: 2.0.0\npaths: {}\n"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=SRM_PRODUCT, version=SRM_VERSION, impl_id=SRM_IMPL_ID, cls=SrmConnector
    )
    register_connector_v2(
        product=HCX_PRODUCT, version=HCX_VERSION, impl_id=HCX_IMPL_ID, cls=HcxConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
_OTHER_SECRET = "targets/op-reads/srm-typed-second"


class _Target:
    """Target satisfying the connector Protocols and the resolver shape."""

    def __init__(
        self,
        *,
        product: str = SRM_PRODUCT,
        version: str = SRM_VERSION,
        secret_ref: str = "targets/op-reads/srm-typed",
        tenant_id: UUID = _TENANT_ID,
        port: int | None = 443,
    ) -> None:
        self.product = product
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = tenant_id
        self.name = f"{product}-typed-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = port
        self.secret_ref = secret_ref
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-srm",
        name="SRM Reads Operator",
        email=None,
        raw_jwt="op.reads.srm.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "srm-typed-svc", "password": _PASSWORD}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> SrmConnector:
    await register_srm_typed_operations()
    instance = get_or_create_connector_instance(SrmConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, SrmConnector)
    return instance


def _login(session_id: str = "srm-session-1") -> httpx.Response:
    return httpx.Response(200, json={"session_id": session_id})


def _mock_login_and_info(mock: respx.MockRouter) -> None:
    mock.post(_SESSION).mock(return_value=_login())
    mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)


async def _dispatch(op_id: str, params: dict[str, Any], target: _Target | None = None) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=SRM_CONNECTOR_ID,
        op_id=op_id,
        target=target or _Target(),
        params=params,
    )


# ---------------------------------------------------------------------------
# Login and srm.about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_logs_in_with_basic_and_returns_the_server_summary(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION).mock(return_value=_login())
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        result = await _dispatch("srm.about", {})

    assert result.status == "ok", result.error
    assert login.call_count == 1
    assert login.calls[0].request.headers["authorization"].startswith("Basic ")
    assert info.calls[0].request.headers["x-dr-session"] == "srm-session-1"
    body = result.result
    assert body["version"] == "9.1.1.0"
    assert body["build_number"] == "11111111"
    assert body["server_status"] == "OK"
    assert body["vc_name"] == "<vcenter-name>"
    assert body["certificates"] == [
        {
            "subject_dn": "CN=<srm-name>",
            "issuer_dn": "CN=<ca-name>",
            "serial_number": "0a1b2c",
            "not_before": 1700000000000,
            "not_after": 1893456000000,
            "thumbprint": "AA:BB:CC",
        }
    ]
    assert body["certificate_warnings"] == []
    assert "_meta" not in body
    assert _PEM not in json.dumps(body)


@pytest.mark.asyncio
async def test_login_401_surfaces_connector_auth_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).respond(401, json={"error_code": "made-up"})
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        result = await _dispatch("srm.about", {})

    assert result.status == "error"
    assert info.call_count == 0
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_login_without_session_id_is_an_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).respond(200, json={})
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        result = await _dispatch("srm.about", {})

    assert result.status == "error"
    assert info.call_count == 0
    assert "no session_id" in result.extras["exception_message"]


@pytest.mark.asyncio
async def test_vendor_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(_INFO).respond(403, json={"error_code": "made-up", "message": "no access"})
        result = await _dispatch("srm.about", {})

    assert result.status == "error"
    assert "403" in (result.error or "") + json.dumps(result.extras, default=str)


# ---------------------------------------------------------------------------
# Two targets on one address
# ---------------------------------------------------------------------------


def _hcx_instance() -> HcxConnector:
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, HcxConnector)
    return instance


async def _dispatch_hcx(target: _Target) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id="hcx.api_file.get",
        target=target,
        params={"path": _HCX_DOCS},
    )


def _mock_hcx(mock: respx.MockRouter) -> tuple[respx.Route, respx.Route]:
    login = mock.post(_HCX_SESSIONS).respond(200, headers={"x-hm-authorization": "hcx-token-1"})
    mock.post(_HCX_APPLIANCES).respond(200, json={"items": [{"version": "9.1.0"}]})
    docs = mock.get(_HCX_DOCS).respond(
        200, text="<html>docs</html>", headers={"Content-Type": "text/html"}
    )
    return login, docs


@pytest.mark.asyncio
async def test_two_targets_on_one_address_resolve_to_their_own_connector_and_login(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    await register_hcx_typed_operations()
    _hcx_instance()
    srm_target = _Target()
    other_target = _Target(product=HCX_PRODUCT, version=HCX_VERSION)
    assert srm_target.host == other_target.host

    assert resolve_connector(srm_target) is SrmConnector
    assert resolve_connector(other_target) is HcxConnector

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        srm_login = mock.post(_SESSION).mock(return_value=_login("srm-session-own"))
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        hcx_login, hcx_docs = _mock_hcx(mock)
        srm_result = await _dispatch("srm.about", {}, srm_target)
        hcx_result = await _dispatch_hcx(other_target)

    assert srm_result.status == "ok", srm_result.error
    assert hcx_result.status == "ok", hcx_result.error
    assert srm_login.call_count == 1
    assert hcx_login.call_count == 1
    srm_headers = info.calls[0].request.headers
    assert srm_headers["x-dr-session"] == "srm-session-own"
    assert "x-hm-authorization" not in srm_headers
    hcx_docs_headers = hcx_docs.calls[0].request.headers
    assert hcx_docs_headers["x-hm-authorization"] == "hcx-token-1"
    assert "x-dr-session" not in hcx_docs_headers


@pytest.mark.asyncio
async def test_401_on_one_srm_target_logs_in_again_and_leaves_other_sessions_alone(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    await register_hcx_typed_operations()
    _hcx_instance()
    first = _Target()
    second = _Target(secret_ref=_OTHER_SECRET)
    other = _Target(product=HCX_PRODUCT, version=HCX_VERSION)
    rejected: set[str] = set()

    def _info(request: httpx.Request) -> httpx.Response:
        session_id = request.headers["x-dr-session"]
        if session_id in rejected:
            return httpx.Response(401)
        return httpx.Response(200, json=_INFO_PAYLOAD)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        srm_login = mock.post(_SESSION)
        srm_login.side_effect = [_login("srm-a-1"), _login("srm-b-1"), _login("srm-a-2")]
        info = mock.get(_INFO).mock(side_effect=_info)
        hcx_login, _ = _mock_hcx(mock)

        assert (await _dispatch("srm.about", {}, first)).status == "ok"
        assert (await _dispatch("srm.about", {}, second)).status == "ok"
        assert (await _dispatch_hcx(other)).status == "ok"
        rejected.add("srm-a-1")
        retried = await _dispatch("srm.about", {}, first)
        again_second = await _dispatch("srm.about", {}, second)
        again_other = await _dispatch_hcx(other)

    assert retried.status == "ok", retried.error
    assert again_second.status == "ok", again_second.error
    assert again_other.status == "ok", again_other.error
    assert srm_login.call_count == 3
    assert hcx_login.call_count == 1
    sent = [call.request.headers["x-dr-session"] for call in info.calls]
    assert sent == ["srm-a-1", "srm-b-1", "srm-a-1", "srm-a-2", "srm-b-1"]


class _OneSessionPerUser:
    """Appliance that keeps one active session per user: a login ends the one before."""

    def __init__(self) -> None:
        self.logins = 0
        self.active: dict[str, str] = {}
        self.rejected = 0

    def login(self, request: httpx.Request) -> httpx.Response:
        self.logins += 1
        user = request.headers["authorization"]
        session_id = f"srm-session-{self.logins}"
        self.active[user] = session_id
        return _login(session_id)

    def info(self, request: httpx.Request) -> httpx.Response:
        if request.headers["x-dr-session"] in self.active.values():
            return httpx.Response(200, json=_INFO_PAYLOAD)
        self.rejected += 1
        return httpx.Response(401)

    def expire_all(self) -> None:
        self.active.clear()


@pytest.mark.asyncio
async def test_two_targets_with_one_credential_share_one_session_and_one_renewal(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """Parallel calls and a renewal on one target do not end the other target's session."""
    await _register_and_resolve(_stub_embedding)
    first = _Target()
    second = _Target()
    appliance = _OneSessionPerUser()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(side_effect=appliance.login)
        info = mock.get(_INFO).mock(side_effect=appliance.info)

        warm = await asyncio.gather(
            *(_dispatch("srm.about", {}, target) for target in (first, second) * 3)
        )
        appliance.expire_all()
        renewed = await _dispatch("srm.about", {}, first)
        after = await asyncio.gather(
            *(_dispatch("srm.about", {}, target) for target in (second, first) * 3)
        )

    for outcome in (*warm, renewed, *after):
        assert outcome.status == "ok", outcome.error
    assert appliance.logins == 2
    assert appliance.rejected == 1
    sent = {call.request.headers["x-dr-session"] for call in info.calls}
    assert sent == {"srm-session-1", "srm-session-2"}


@pytest.mark.asyncio
async def test_late_401s_after_a_renewal_do_not_end_the_new_shared_session(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """Calls on both targets at once after expiry: every call works, one renewal.

    The appliance waits a little before it checks the session, so many calls
    send the expired session, and their ``401`` answers arrive after the
    first renewal. Such a late ``401`` must keep the new session.
    """
    await _register_and_resolve(_stub_embedding)
    first = _Target()
    second = _Target()
    appliance = _OneSessionPerUser()

    async def _slow_info(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return appliance.info(request)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(side_effect=appliance.login)
        mock.get(_INFO).mock(side_effect=_slow_info)

        warm = await asyncio.gather(
            *(_dispatch("srm.about", {}, target) for target in (first, second))
        )
        appliance.expire_all()
        after = await asyncio.gather(
            *(_dispatch("srm.about", {}, target) for target in (first, second) * 10)
        )

    for outcome in (*warm, *after):
        assert outcome.status == "ok", outcome.error
    assert appliance.logins == 2
    # More than one call got a 401, so late 401s did happen.
    assert appliance.rejected > 1


@pytest.mark.asyncio
async def test_unset_port_and_explicit_default_port_share_one_session_and_one_renewal(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A target with no port and one with port 443 reach the same login address."""
    await _register_and_resolve(_stub_embedding)
    unset = _Target(port=None)
    explicit = _Target(port=443)
    appliance = _OneSessionPerUser()

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(side_effect=appliance.login)
        info = mock.get(_INFO).mock(side_effect=appliance.info)

        warm = await asyncio.gather(
            *(_dispatch("srm.about", {}, target) for target in (unset, explicit) * 3)
        )
        appliance.expire_all()
        renewed = await _dispatch("srm.about", {}, explicit)
        after = await asyncio.gather(
            *(_dispatch("srm.about", {}, target) for target in (unset, explicit) * 3)
        )

    for outcome in (*warm, renewed, *after):
        assert outcome.status == "ok", outcome.error
    assert appliance.logins == 2
    assert appliance.rejected == 1
    sent = {call.request.headers["x-dr-session"] for call in info.calls}
    assert sent == {"srm-session-1", "srm-session-2"}


@pytest.mark.asyncio
async def test_two_targets_with_different_credentials_keep_their_own_sessions(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    first = _Target()
    second = _Target(secret_ref=_OTHER_SECRET)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("srm-a-1"), _login("srm-b-1")]
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        for target in (first, second, first, second):
            assert (await _dispatch("srm.about", {}, target)).status == "ok"

    assert login.call_count == 2
    sent = [call.request.headers["x-dr-session"] for call in info.calls]
    assert sent == ["srm-a-1", "srm-b-1", "srm-a-1", "srm-b-1"]


@pytest.mark.asyncio
async def test_two_tenants_with_the_same_secret_reference_keep_their_own_sessions(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    in_tenant = _Target()
    in_other_tenant = _Target(tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000000d9"))

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("srm-t1-1"), _login("srm-t2-1")]
        info = mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        for target in (in_tenant, in_other_tenant, in_tenant, in_other_tenant):
            assert (await _dispatch("srm.about", {}, target)).status == "ok"

    assert login.call_count == 2
    sent = [call.request.headers["x-dr-session"] for call in info.calls]
    assert sent == ["srm-t1-1", "srm-t2-1", "srm-t1-1", "srm-t2-1"]


# ---------------------------------------------------------------------------
# srm.api_file.get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "path"), [("srm", _SRM_FILE), ("configure", _CONFIGURE_FILE)])
async def test_api_file_returns_status_size_sha_version_and_parts(
    _stub_embedding: AsyncMock, session: AsyncSession, name: str, path: str
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        route = mock.get(path).respond(
            200, text=_YAML, headers={"Content-Type": "application/yaml"}
        )
        result = await _dispatch("srm.api_file.get", {"file": name})

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert route.calls[0].request.headers["x-dr-session"] == "srm-session-1"
    body = result.result
    assert body["file"] == name
    assert body["path"] == path == API_FILES[name]
    assert body["status"] == 200
    assert body["content_type"] == "application/yaml"
    assert body["size"] == len(_YAML.encode())
    assert body["sha256"] == hashlib.sha256(_YAML.encode()).hexdigest()
    assert body["version"] == "9.1.1.0"
    assert body["build"] == "11111111"
    assert body["version_error"] is None
    assert body["redirect_to"] is None
    assert body["parts"] == 1
    assert body["items"] == [{"index": 0, "text": _YAML}]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["other", "../srm", "/api/rest/srm/v2/open-api.yaml", ""])
async def test_unknown_file_name_sends_no_http_request(
    _stub_embedding: AsyncMock, session: AsyncSession, name: str
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        result = await _dispatch("srm.api_file.get", {"file": name})
        sent = len(mock.calls)

    assert result.status == "error"
    assert sent == 0


@pytest.mark.asyncio
async def test_404_and_redirect_reach_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(404, text="not here", headers={"Content-Type": "text/plain"})
        moved = mock.get(_CONFIGURE_FILE).respond(
            302, headers={"Location": "/ui/login?next=x"}, text=""
        )
        missing = await _dispatch("srm.api_file.get", {"file": "srm"})
        redirect = await _dispatch("srm.api_file.get", {"file": "configure"})

    assert missing.status == "ok", missing.error
    assert missing.result["status"] == 404
    # The body is checked as YAML (the path ends in .yaml) and written back.
    assert missing.result["items"] == [{"index": 0, "text": "not here\n"}]
    assert redirect.status == "ok", redirect.error
    assert redirect.result["status"] == 302
    assert redirect.result["redirect_to"] == "/ui/login?next=x"
    assert moved.call_count == 1


@pytest.mark.asyncio
async def test_401_on_the_file_makes_a_new_login_and_the_read_then_succeeds(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("srm-session-1"), _login("srm-session-2")]
        mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        route = mock.get(_SRM_FILE)
        route.side_effect = [httpx.Response(401), httpx.Response(200, text=_YAML)]
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert route.calls[0].request.headers["x-dr-session"] == "srm-session-1"
    assert route.calls[1].request.headers["x-dr-session"] == "srm-session-2"


@pytest.mark.asyncio
async def test_version_read_failure_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).mock(return_value=_login())
        mock.get(_INFO).respond(500, text="boom")
        mock.get(_SRM_FILE).respond(200, text=_YAML)
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "ok", result.error
    assert result.result["sha256"] == hashlib.sha256(_YAML.encode()).hexdigest()
    assert result.result["version"] is None
    assert "500" in result.result["version_error"]


@pytest.mark.asyncio
async def test_secret_values_and_authorization_examples_come_back_replaced_and_counted(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A password value and an ``Authorization`` example are replaced; size and hash fit."""
    await _register_and_resolve(_stub_embedding)
    text = (
        "openapi: 3.0.1\ninfo:\n  description: |-\n"
        "    Authorization: Basic bWFkZS11cDpleGFtcGxlLXZhbHVl\n"
        "components:\n  examples:\n    login:\n      value:\n"
        "        password: made-up-example-password\n"
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, text=text, headers={"Content-Type": "application/yaml"})
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "ok", result.error
    body = result.result
    joined = "".join(item["text"] for item in body["items"])
    assert "[REDACTED:authorization_header]" in joined
    assert "[REDACTED:param_name]" in joined
    assert body["secrets_replaced"] == 2
    assert body["size"] == len(joined.encode())
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert "bWFkZS11cDpleGFtcGxlLXZhbHVl" not in rendered
    assert "made-up-example-password" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "text", "load"),
    [
        pytest.param(
            "application/yaml",
            "info:\n"
            "  note: Bearer made-up-token-value-0123456789\n"
            '  description: "line one\\nAuthorization: Basic bWFkZS11cDpleGFtcGxl"\n'
            "  count: 3\n"
            "  flag: true\n"
            "responses:\n"
            "  200:\n"
            "    description: ok\n",
            yaml.safe_load,
            id="yaml",
        ),
        pytest.param(
            "application/json",
            json.dumps(
                {
                    "info": {
                        "note": "Bearer made-up-token-value-0123456789",
                        "description": "line one\nAuthorization: Basic bWFkZS11cDpleGFtcGxl",
                        "count": 3,
                        "flag": True,
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            ),
            json.loads,
            id="json",
        ),
    ],
)
async def test_replaced_copy_still_parses_and_every_value_keeps_its_type(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    content_type: str,
    text: str,
    load: Any,
) -> None:
    """The rules run on the parsed values, so a placeholder never changes the file's shape.

    A plain YAML scalar that becomes ``[REDACTED:bearer_token]`` would read
    as a list if the rules ran on the written text; a quoted value with a
    line break would lose its quote. Here the copy is parsed again and
    compared value by value.
    """
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, text=text, headers={"Content-Type": content_type})
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "ok", result.error
    body = result.result
    joined = "".join(item["text"] for item in body["items"])
    parsed = load(joined)
    assert parsed["info"] == {
        "note": "[REDACTED:bearer_token]",
        "description": "line one\n[REDACTED:authorization_header]",
        "count": 3,
        "flag": True,
    }
    (status_key,) = parsed["responses"]
    assert status_key == (200 if content_type == "application/yaml" else "200")
    assert body["secrets_replaced"] == 2
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()


@pytest.mark.asyncio
async def test_401_on_the_version_read_ends_only_that_credentials_session(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The file comes back, but ``server/info`` rejects the session.

    The version read never raises, so the dispatcher's own ``401`` retry
    does not run. The connector drops the rejected session itself: the
    next read of that target logs in again, and the other target, with its
    own credential, keeps its session.
    """
    await _register_and_resolve(_stub_embedding)
    first = _Target()
    second = _Target(secret_ref=_OTHER_SECRET)

    def _info(request: httpx.Request) -> httpx.Response:
        if request.headers["x-dr-session"] == "srm-a-1":
            return httpx.Response(401)
        return httpx.Response(200, json=_INFO_PAYLOAD)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("srm-a-1"), _login("srm-b-1"), _login("srm-a-2")]
        mock.get(_INFO).mock(side_effect=_info)
        file_route = mock.get(_SRM_FILE).respond(
            200, text=_YAML, headers={"Content-Type": "application/yaml"}
        )
        rejected = await _dispatch("srm.api_file.get", {"file": "srm"}, first)
        other = await _dispatch("srm.api_file.get", {"file": "srm"}, second)
        retried = await _dispatch("srm.api_file.get", {"file": "srm"}, first)
        other_again = await _dispatch("srm.api_file.get", {"file": "srm"}, second)

    assert rejected.status == "ok", rejected.error
    assert rejected.result["version"] is None
    assert "401" in rejected.result["version_error"]
    for outcome in (other, retried, other_again):
        assert outcome.status == "ok", outcome.error
        assert outcome.result["version"] == "9.1.1.0"
    assert login.call_count == 3
    sent = [call.request.headers["x-dr-session"] for call in file_route.calls]
    assert sent == ["srm-a-1", "srm-b-1", "srm-a-2", "srm-b-1"]


@pytest.mark.asyncio
async def test_secret_named_query_value_in_a_redirect_is_replaced_and_counted(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(
            302, headers={"Location": "/ui/login?licenseKey=made-up-licence&next=x"}, text=""
        )
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "ok", result.error
    assert "made-up-licence" not in result.result["redirect_to"]
    assert result.result["redirect_to"].startswith("/ui/login?licenseKey=")
    assert result.result["secrets_replaced"] == 1


@pytest.mark.asyncio
async def test_file_that_does_not_parse_stops_with_an_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    text = "openapi: [3.0.1\npassword: made-up-example-password\n"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, text=text, headers={"Content-Type": "application/yaml"})
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "error"
    assert "not well-formed YAML" in result.extras["exception_message"]
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert "made-up-example-password" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "spec_file", "expected_replaced"),
    [("srm", "recovery-srm-v2.yaml", 6), ("configure", "appliance-configure-v2.yaml", 10)],
)
async def test_pinned_vendor_file_comes_back_through_the_read(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    name: str,
    spec_file: str,
    expected_replaced: int,
) -> None:
    """The pinned 9.1 vendor files come back, with their example secrets replaced.

    Runs only when the spec shelf is present. The SRM file has
    ``Authorization: Basic ...`` examples in its description text; the read
    must return them replaced instead of stopping.
    """
    spec_path = require_shelf_spec("vcf-protection-and-recovery-9.1", spec_file)
    content = spec_path.read_bytes()
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(API_FILES[name]).respond(
            200, content=content, headers={"Content-Type": "application/yaml"}
        )
        result = await _dispatch("srm.api_file.get", {"file": name})

    assert result.status == "ok", result.error
    body = result.result
    assert body["secrets_replaced"] == expected_replaced
    assert [item["index"] for item in body["items"]] == list(range(body["parts"]))
    joined = "".join(item["text"] for item in body["items"]).encode()
    assert body["size"] == len(joined)
    assert body["sha256"] == hashlib.sha256(joined).hexdigest()


@pytest.mark.asyncio
async def test_file_too_big_for_one_result_handle_stops_with_its_size(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "2048")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, content=b"x" * 5000)
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "error"
    assert "does not fit in one result handle" in result.extras["exception_message"]
    assert "5000" in result.extras["exception_message"]


@pytest.mark.parametrize(
    ("payload", "version", "build"),
    [
        (_INFO_PAYLOAD, "9.1.1.0", "11111111"),
        ({"version": "8.8.0", "build_number": 2222}, "8.8.0", "2222"),
        ({"version": "9.0.0"}, "9.0.0", None),
        ({}, None, None),
        ([], None, None),
    ],
)
def test_parse_server_version_shapes(payload: Any, version: str | None, build: str | None) -> None:
    read = parse_server_version(payload)
    assert (read.version, read.build) == (version, build)
    assert (read.error is None) == (version is not None)


# ---------------------------------------------------------------------------
# Secrets, registration shape, fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_results_hold_no_password_or_session_id(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, text=_YAML)
        about = await _dispatch("srm.about", {})
        api_file = await _dispatch("srm.api_file.get", {"file": "srm"})

    for result in (about, api_file):
        assert result.status == "ok", result.error
        rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
        assert _PASSWORD not in rendered
        assert "srm-session-1" not in rendered


@pytest.mark.asyncio
async def test_registered_ops_are_typed_safe_and_documented(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_srm_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == SRM_PRODUCT,
                    EndpointDescriptor.impl_id == SRM_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in SRM_TYPED_OPS}
    assert {r.op_id for r in rows} == {"srm.about", "srm.api_file.get"}
    for row in rows:
        assert row.source_kind == "typed"
        assert row.handler_ref is not None
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert row.parameter_schema["additionalProperties"] is False
        assert len(row.parameter_schema["examples"]) == 1
        assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
        assert "\n" not in row.summary and len(row.description) <= 300
    by_id = {row.op_id: row for row in rows}
    file_schema = by_id["srm.api_file.get"].parameter_schema["properties"]["file"]
    assert file_schema["enum"] == list(API_FILES)


def test_package_registers_versioned_connector_and_wildcard() -> None:
    import meho_backplane.connectors.srm  # noqa: F401  (import-time registration)

    clear_registry()
    register_connector_v2(
        product=SRM_PRODUCT, version=SRM_VERSION, impl_id=SRM_IMPL_ID, cls=SrmConnector
    )
    register_connector_v2(product=SRM_PRODUCT, version="", impl_id="", cls=SrmConnector)
    registry = all_connectors_v2()
    assert registry[(SRM_PRODUCT, SRM_VERSION, SRM_IMPL_ID)] is SrmConnector
    assert registry[(SRM_PRODUCT, "", "")] is SrmConnector
    assert SrmConnector.supported_version_range == ">=8.8,<10.0"
    assert SRM_CONNECTOR_ID == "srm-rest-9.1"


@pytest.mark.parametrize("version", ["8.8", "9.0", "9.1"])
def test_each_covered_version_resolves_to_the_connector(version: str) -> None:
    assert resolve_connector(_Target(version=version)) is SrmConnector


@pytest.mark.asyncio
async def test_fingerprint_reports_version_and_unreachable_shape() -> None:
    connector = SrmConnector(session_loader=_session_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        fp = await connector.fingerprint(_Target(), _make_operator())
    assert (fp.vendor, fp.product, fp.reachable) == ("vmware", "srm", True)
    assert (fp.version, fp.build) == ("9.1.1.0", "11111111")
    await connector.aclose()

    connector = SrmConnector(session_loader=_session_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION).respond(401)
        fp = await connector.fingerprint(_Target(), _make_operator())
        probe = await connector.probe(_Target())
    assert fp.reachable is False
    assert "401" in str(fp.extras["error"])
    assert probe.ok is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_rejects_other_auth_models() -> None:
    connector = SrmConnector(session_loader=_session_loader)
    target = _Target()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError, match="per_user"):
        await connector.auth_headers(target, _make_operator())


# ---------------------------------------------------------------------------
# The flight recorder and the 401 body: only the checked copy leaves the read
# ---------------------------------------------------------------------------

_RECORDER_SECRET = "made-up-recorder-secret"
_SECRET_YAML = (
    "openapi: 3.0.1\ninfo:\n  title: made-up API\n  version: 2.0.0\n"
    f"  x-example:\n    password: {_RECORDER_SECRET}\npaths: {{}}\n"
)


def _recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Catch what the read hands the flight recorder."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        flight_recorder_capture,
        "record_vendor_call",
        lambda _span, **kwargs: recorded.append(kwargs),
    )
    return recorded


def _file_calls(recorded: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    return [call for call in recorded if call["response"].url.path == path]


async def _audit_payloads(op_id: str) -> list[str]:
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [json.dumps(row.payload, default=str) for row in rows]


@pytest.mark.asyncio
async def test_flight_recorder_gets_the_checked_file_not_the_password(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, text=_SECRET_YAML)
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "ok", result.error
    (call,) = _file_calls(recorded, _SRM_FILE)
    assert REDACTED.encode() in call["response_body"]
    assert _RECORDER_SECRET.encode() not in call["response_body"]


@pytest.mark.asyncio
async def test_flight_recorder_gets_no_body_when_the_file_is_refused(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, text="info: {password: " + _RECORDER_SECRET)
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "error"
    (call,) = _file_calls(recorded, _SRM_FILE)
    assert call["response_body"] == b""


@pytest.mark.asyncio
async def test_flight_recorder_gets_no_body_when_the_stored_record_does_not_fit(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file fits the download limit, but its stored record does not: no body is recorded."""
    recorded = _recorded_calls(monkeypatch)
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "1000")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    data = b'"' + b'\\"' * 494 + b'"'
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_info(mock)
        mock.get(_SRM_FILE).respond(200, content=data)
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "error"
    assert "does not fit in one result handle" in result.extras["exception_message"]
    (call,) = _file_calls(recorded, _SRM_FILE)
    assert call["response"].status_code == 200
    assert call["response_body"] == b""


@pytest.mark.asyncio
async def test_401_body_is_checked_before_it_is_raised_and_the_retry_still_runs(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSION)
        login.side_effect = [_login("srm-session-1"), _login("srm-session-2")]
        mock.get(_INFO).respond(200, json=_INFO_PAYLOAD)
        route = mock.get(_SRM_FILE).respond(401, text=_SECRET_YAML)
        result = await _dispatch("srm.api_file.get", {"file": "srm"})

    assert result.status == "error"
    assert "401" in (result.error or "")
    assert login.call_count == 2
    assert route.call_count == 2
    rendered = (
        (result.error or "") + json.dumps(result.result) + json.dumps(result.extras, default=str)
    )
    assert _RECORDER_SECRET not in rendered
    calls = _file_calls(recorded, _SRM_FILE)
    assert len(calls) == 2
    for call in calls:
        assert REDACTED.encode() in call["response_body"]
        assert _RECORDER_SECRET.encode() not in call["response_body"]
    payloads = await _audit_payloads("srm.api_file.get")
    assert payloads
    assert all(_RECORDER_SECRET not in payload for payload in payloads)
