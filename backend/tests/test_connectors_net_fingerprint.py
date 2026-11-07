# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``net.fingerprint``: identify one address without a login or a target.

The answers of the systems are recorded fixtures (made-up names, builds and
documentation addresses). They are fed in through an ``httpx`` mock
transport, a stub for the TLS check and a stub or a loopback server for the
SSH banner. A check against real appliances is not part of these tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import fingerprint as fp
from meho_backplane.connectors.net.allowlist import (
    PROBE_ALLOWLIST_ENV,
    TENANT_PROBE_ALLOWLIST_ENV,
)
from meho_backplane.connectors.net.fingerprint import (
    net_fingerprint,
    register_net_fingerprint_operation,
)
from meho_backplane.connectors.net.fingerprint_rules import (
    Evidence,
    classify,
    extract_title,
    parse_vsphere_about,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.fingerprint"
_HOST = "192.0.2.10"
_TENANT = UUID("00000000-0000-0000-0000-000000004133")

# ---------------------------------------------------------------------------
# Recorded answers (made-up values)
# ---------------------------------------------------------------------------


def _about_xml(name: str, version: str, build: str, api_type: str, line: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body>"
        '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
        '<rootFolder type="Folder">group-d1</rootFolder>'
        f"<about><name>{name}</name><fullName>{name} {version} build-{build}</fullName>"
        f"<vendor>VMware, Inc.</vendor><version>{version}</version><build>{build}</build>"
        f"<apiType>{api_type}</apiType><productLineId>{line}</productLineId></about>"
        "</returnval></RetrieveServiceContentResponse>"
        "</soapenv:Body></soapenv:Envelope>"
    ).encode()


VCENTER_ABOUT = _about_xml("VMware vCenter Server", "8.0.2", "1111111", "VirtualCenter", "vpx")
ESXI_ABOUT = _about_xml("VMware ESXi", "8.0.1", "2222222", "HostAgent", "embeddedEsx")
ESXI_CERT_SUBJECT = (
    "CN=esx-01.example.test,OU=VMware ESX Server Default Certificate,O=VMware\\, Inc."
)
NSX_CERT_SUBJECT = "CN=nsx-mgr.example.test,OU=NSX,O=VMware Inc."
NSX_LOGIN_PAGE = b"<html><head><title>VMware NSX | Login</title></head><body>x</body></html>"
SDDC_PAGE = b"<html><head><title>SDDC Manager</title></head></html>"
NGINX_PAGE = b"<html><head><title>Welcome to nginx!</title></head></html>"
VCENTER_PAGE = b"<html><head><title> vSphere Client </title></head></html>"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(TENANT_PROBE_ALLOWLIST_ENV, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
async def _registered_op() -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_net_fingerprint_operation(embedding_service=service)
    yield


def _operator(tenant: UUID = _TENANT) -> Operator:
    return Operator(
        sub="op-fingerprint",
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant,
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch(params: dict[str, Any], tenant: UUID = _TENANT) -> OperationResult:
    return await dispatch(
        operator=_operator(tenant),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


Handler = Callable[[httpx.Request], httpx.Response]


def _system(
    monkeypatch: pytest.MonkeyPatch,
    *,
    http: Handler | None = None,
    subject: str | None = None,
    banner: bytes | None = None,
) -> list[httpx.Request]:
    """Stand in for one system: its HTTP answers, its certificate and its SSH banner."""
    seen: list[httpx.Request] = []

    def _route(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if http is None:
            raise httpx.ConnectError("connection refused", request=request)
        return http(request)

    def _client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_route), follow_redirects=False)

    async def _tls(_op: Operator, _t: Any, params: dict[str, Any]) -> dict[str, Any]:
        if subject is None:
            return {"handshake": False, "reason": "refused", "leaf": None}
        leaf = {"subject": subject, "issuer": subject, "san": ["esx-01.example.test"]}
        return {"handshake": True, "reason": None, "leaf": leaf}

    async def _banner(_host: str) -> bytes:
        if banner is None:
            raise ConnectionRefusedError
        return banner

    monkeypatch.setattr(fp, "_make_client", _client)
    monkeypatch.setattr(fp, "net_tls_inspect", _tls)
    monkeypatch.setattr(fp, "_read_banner", _banner)
    return seen


def _routes(root: tuple[int, bytes], sdk: tuple[int, bytes]) -> Handler:
    def _handler(request: httpx.Request) -> httpx.Response:
        status, body = sdk if request.url.path == "/sdk" else root
        return httpx.Response(status, content=body, headers={"server": "test-server"})

    return _handler


async def _run(host: str = _HOST) -> dict[str, Any]:
    return await net_fingerprint(_operator(), None, {"host": host, "timeout_seconds": 2})


# ---------------------------------------------------------------------------
# Allowlist: refused and audited
# ---------------------------------------------------------------------------


async def test_address_outside_allowlist_is_refused_and_audited(
    monkeypatch: pytest.MonkeyPatch, _registered_op: None
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "198.51.100.0/24")
    seen = _system(monkeypatch, http=_routes((200, NGINX_PAGE), (404, b"")))

    result = await _dispatch({"host": _HOST})

    assert result.status == "error"
    assert result.result is None
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["host"] == _HOST
    assert seen == []  # no request left MEHO

    async with get_sessionmaker()() as session:
        rows = list(
            (await session.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars()
        )
    assert len(rows) == 1
    assert rows[0].status_code >= 400
    assert rows[0].tenant_id == _TENANT
    assert "connector_probe_refused" in str(rows[0].payload)


async def test_tenant_list_refuses_address_the_instance_list_allows(
    monkeypatch: pytest.MonkeyPatch, _registered_op: None
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    monkeypatch.setenv(TENANT_PROBE_ALLOWLIST_ENV, f"{_TENANT}=198.51.100.0/24")
    seen = _system(monkeypatch, http=_routes((200, NGINX_PAGE), (404, b"")))

    result = await _dispatch({"host": _HOST})

    assert result.extras["error_code"] == "connector_probe_refused"
    assert seen == []


async def test_address_range_is_refused(
    monkeypatch: pytest.MonkeyPatch, _registered_op: None
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    seen = _system(monkeypatch, http=_routes((200, NGINX_PAGE), (404, b"")))

    result = await _dispatch({"host": "192.0.2.0/24"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_probe_refused"
    assert seen == []


# ---------------------------------------------------------------------------
# Products from recorded answers
# ---------------------------------------------------------------------------


async def test_vcenter_identified_from_about_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    seen = _system(
        monkeypatch,
        http=_routes((200, VCENTER_PAGE), (200, VCENTER_ABOUT)),
        subject="CN=vc-01.example.test,OU=VMware Engineering",
        banner=b"SSH-2.0-OpenSSH_9.0\r\n",
    )

    result = await _run()

    assert result["product"] == "vcenter"
    assert result["vendor"] == "VMware"
    assert result["version"] == "8.0.2"
    assert result["build"] == "1111111"
    assert result["confidence"] == "high"
    assert result["reachable"] is True
    assert result["evidence"]["ssh"]["banner"] == "SSH-2.0-OpenSSH_9.0"
    assert result["evidence"]["http"]["title"] == "vSphere Client"
    sdk = next(r for r in seen if r.url.path == "/sdk")
    assert sdk.method == "POST"
    assert b"RetrieveServiceContent" in sdk.content
    assert "authorization" not in sdk.headers  # no login


async def test_esxi_identified_from_about_answer_and_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    _system(
        monkeypatch,
        http=_routes((200, b"<html></html>"), (200, ESXI_ABOUT)),
        subject=ESXI_CERT_SUBJECT,
    )

    result = await _run()

    assert result["product"] == "esxi"
    assert result["version"] == "8.0.1"
    assert result["confidence"] == "high"
    assert {m["source"] for m in result["matched"]} == {"vsphere_about", "tls"}


async def test_nsx_identified_from_title_and_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    _system(
        monkeypatch,
        http=_routes((200, NSX_LOGIN_PAGE), (404, b"<html>not found</html>")),
        subject=NSX_CERT_SUBJECT,
    )

    result = await _run()

    assert result["product"] == "nsx"
    assert result["version"] is None  # NSX does not state its version without a login
    assert result["confidence"] == "high"
    assert result["evidence"]["vsphere_about"]["reason"] == "not_vsphere"


async def test_sddc_manager_identified_after_same_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/ui/"})
        if request.url.path == "/ui/":
            return httpx.Response(200, content=SDDC_PAGE)
        return httpx.Response(404)

    _system(monkeypatch, http=_handler)

    result = await _run()

    assert result["product"] == "sddc"
    assert result["confidence"] == "medium"
    assert result["evidence"]["http"]["path"] == "/ui/"


async def test_unknown_system_gives_unknown_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    _system(
        monkeypatch,
        http=_routes((200, NGINX_PAGE), (404, NGINX_PAGE)),
        subject="CN=web.example.test,O=Example",
        banner=b"SSH-2.0-OpenSSH_9.6\r\n",
    )

    result = await _run()

    assert result["reachable"] is True
    assert result["product"] == "unknown"
    assert result["confidence"] == "none"
    assert result["vendor"] is None
    assert result["version"] is None
    assert result["matched"] == []


async def test_nothing_answers_gives_unknown_and_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    _system(monkeypatch)

    result = await _run()

    assert result["reachable"] is False
    assert result["product"] == "unknown"
    assert result["confidence"] == "none"
    evidence = result["evidence"]
    assert evidence["http"]["reason"] == "unreachable"
    assert evidence["vsphere_about"]["reason"] == "unreachable"
    assert evidence["tls"]["reason"] == "refused"
    assert evidence["ssh"]["reason"] == "refused"


async def test_redirect_to_other_host_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == _HOST, "a request went to another host"
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "https://198.51.100.7/ui/"})
        return httpx.Response(404)

    _system(monkeypatch, http=_handler)

    result = await _run()

    assert result["evidence"]["http"]["status"] == 302
    assert result["evidence"]["http"]["title"] is None
    assert result["product"] == "unknown"


async def test_endless_same_host_redirects_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    seen = _system(monkeypatch, http=lambda _r: httpx.Response(302, headers={"location": "/"}))

    result = await _run()

    assert result["evidence"]["http"]["reason"] == "too_many_redirects"
    assert len([r for r in seen if r.url.path == "/"]) == fp._MAX_SAME_HOST_REDIRECTS + 1


async def test_http_timeout_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _system(monkeypatch, http=_handler)

    result = await _run()

    assert result["evidence"]["http"]["reason"] == "timeout"
    assert result["evidence"]["vsphere_about"]["reason"] == "timeout"
    assert result["product"] == "unknown"


async def test_large_page_is_read_only_up_to_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    late_title = b"x" * (fp._MAX_BODY_BYTES + 10) + b"<title>VMware NSX</title>"
    _system(monkeypatch, http=_routes((200, late_title), (404, b"")))

    result = await _run()

    assert result["evidence"]["http"]["title"] is None
    assert result["product"] == "unknown"


# ---------------------------------------------------------------------------
# SSH banner over a real loopback socket
# ---------------------------------------------------------------------------


async def _serve(payload: bytes) -> tuple[asyncio.Server, int]:
    async def _on_connect(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.parametrize(
    ("payload", "checked", "banner", "reason"),
    [
        (b"SSH-2.0-OpenSSH_9.6\r\n", True, "SSH-2.0-OpenSSH_9.6", None),
        (b"HTTP/1.1 400 Bad Request\r\n", False, None, "not_ssh"),
        (b"SSH-2.0-no-newline", False, None, "not_ssh"),
    ],
)
async def test_ssh_banner_read(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    checked: bool,
    banner: str | None,
    reason: str | None,
) -> None:
    server, port = await _serve(payload)
    monkeypatch.setattr(fp, "_SSH_PORT", port)
    try:
        result = await fp._check_ssh("127.0.0.1", 2.0)
    finally:
        server.close()
        await server.wait_closed()
    assert result == {"checked": checked, "reason": reason, "banner": banner}


async def test_ssh_banner_closed_port_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    server, port = await _serve(b"")
    server.close()
    await server.wait_closed()
    monkeypatch.setattr(fp, "_SSH_PORT", port)
    result = await fp._check_ssh("127.0.0.1", 2.0)
    assert result["checked"] is False
    assert result["reason"] == "refused"


# ---------------------------------------------------------------------------
# Rules and parsers
# ---------------------------------------------------------------------------


def test_parse_vsphere_about_reads_the_about_block() -> None:
    about = parse_vsphere_about(VCENTER_ABOUT)
    assert about is not None
    assert about["api_type"] == "VirtualCenter"
    assert about["product_line_id"] == "vpx"
    assert about["full_name"] == "VMware vCenter Server 8.0.2 build-1111111"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml",
        b"<html><body>404</body></html>",
        b"<Envelope><about><version>1</version></about></Envelope>",  # no name
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',  # entities are refused
    ],
)
def test_parse_vsphere_about_rejects_other_answers(body: bytes) -> None:
    assert parse_vsphere_about(body) is None


def test_extract_title_edge_cases() -> None:
    assert extract_title("<TITLE>\n  VMware  NSX \n</TITLE>") == "VMware NSX"
    assert extract_title("<title></title>") is None
    assert extract_title("no title here") is None
    assert len(extract_title("<title>" + "a" * 1000 + "</title>") or "") == 256


def test_two_products_with_the_same_score_give_unknown() -> None:
    evidence = Evidence(
        http={"checked": True, "title": "NSX and SDDC Manager"},
    )
    verdict = classify(evidence)
    assert verdict["product"] == "unknown"
    assert verdict["confidence"] == "none"
    assert {m["product"] for m in verdict["matched"]} == {"nsx", "sddc"}


def test_certificate_alone_gives_low_confidence() -> None:
    verdict = classify(Evidence(tls={"checked": True, "subject": ESXI_CERT_SUBJECT}))
    assert verdict["product"] == "esxi"
    assert verdict["confidence"] == "low"
    assert verdict["version"] is None


def test_title_version_is_not_taken_without_about_answer() -> None:
    verdict = classify(Evidence(http={"checked": True, "title": "VMware ESXi"}))
    assert verdict["product"] == "esxi"
    assert verdict["version"] is None


def test_about_of_one_product_does_not_give_its_version_to_another() -> None:
    evidence = Evidence(
        http={"checked": True, "title": "NSX"},
        tls={"checked": True, "subject": "OU=NSX"},
        vsphere_about={"checked": True, "api_type": "Unknown", "version": "1.0"},
    )
    verdict = classify(evidence)
    assert verdict["product"] == "nsx"
    assert verdict["version"] is None


# ---------------------------------------------------------------------------
# Dispatch, schema, registration
# ---------------------------------------------------------------------------


async def test_dispatch_result_matches_schema_and_audit_has_host(
    monkeypatch: pytest.MonkeyPatch, _registered_op: None
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "192.0.2.0/24")
    validator = Draft202012Validator(fp._NET_FINGERPRINT_RESPONSE_SCHEMA)
    Draft202012Validator.check_schema(fp._NET_FINGERPRINT_RESPONSE_SCHEMA)

    _system(monkeypatch, http=_routes((200, VCENTER_PAGE), (200, VCENTER_ABOUT)), subject="CN=x")
    known = await _dispatch({"host": _HOST})
    assert known.status == "ok", known.error
    validator.validate(known.result)

    _system(monkeypatch)
    unknown = await _dispatch({"host": _HOST})
    assert unknown.status == "ok", unknown.error
    assert unknown.result["product"] == "unknown"
    validator.validate(unknown.result)

    async with get_sessionmaker()() as session:
        rows = list(
            (await session.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars()
        )
    assert len(rows) == 2
    assert all(row.raw_payload and row.raw_payload["host"] == _HOST for row in rows)


async def test_registered_as_safe_ungated_typed_op(_registered_op: None) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.impl_id == "net-probe",
                    EndpointDescriptor.op_id == _OP_ID,
                )
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False


def test_classifies_as_read() -> None:
    from meho_backplane.broadcast.events import classify_op

    assert classify_op(_OP_ID) == "read"
