# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vcenter.sso_admin.api_file.get`` typed read.

The read gets one SSO Admin API description file (a WSDL or an XSD file)
from a vCenter with one ``GET`` and no login. HTTP is mocked with
``respx``, like ``test_connectors_vmware_rest_fingerprint.py``. All file
contents are made up here; the large files are built by helpers, not
stored in the repo.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultUnreachableError
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRest80Connector, VmwareRestConnector
from meho_backplane.connectors.vmware_rest import typed_ops_sso_admin_api_file as api_file
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_sso_admin_api_file import (
    PART_MAX_BYTES,
    SSO_ADMIN_API_FILE_GROUP_KEY,
    VCENTER_SSO_ADMIN_API_FILE_GET_OP,
    SsoAdminApiFileError,
    find_imports,
    split_text_parts,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.redaction import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

_HOST = "vc-sso.test.invalid"
_BASE = f"https://{_HOST}"
_WSDL_PATH = "/sso-adminserver/sdk/example.test/admin.wsdl"
_TENANT = UUID("00000000-0000-0000-0000-00000000a5a5")

_WSDL = """<?xml version="1.0" encoding="UTF-8"?>
<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:example:admin">
  <wsdl:import namespace="urn:example:admin:messages" location="admin-messages.wsdl"/>
  <wsdl:types>
    <xsd:schema targetNamespace="urn:example:admin">
      <xsd:import namespace="urn:example:admin:types" schemaLocation="admin-types.xsd"/>
      <xsd:include schemaLocation="admin-faults.xsd"/>
    </xsd:schema>
  </wsdl:types>
</wsdl:definitions>
"""


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _Target:
    name: str = "vc-sso"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vsphere/vc-sso"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="op-sso-file",
        name="SSO File Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-test", "password": "stub-password"}


def _connector(session_loader: Any = _stub_loader) -> VmwareRestConnector:
    connector = VmwareRestConnector(session_loader=session_loader)

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]
    return connector


def _mock_session_and_version(mock: respx.MockRouter, *, version_status: int = 200) -> None:
    mock.post("/api/session").respond(
        200,
        json="session-token-abc",
        headers={"Set-Cookie": "vmware-api-session-id=cookie-abc; Path=/"},
    )
    if version_status == 200:
        mock.get("/api/appliance/system/version").respond(
            200, json={"version": "8.0.3.00000", "build": "11111111", "product": "vCenter"}
        )
    else:
        mock.get("/api/appliance/system/version").respond(version_status)


async def _call(connector: VmwareRestConnector, path: str) -> dict[str, Any]:
    return await connector.sso_admin_api_file_get(_operator(), _Target(), {"path": path})


def _joined(answer: dict[str, Any]) -> bytes:
    parts = sorted(answer["text_parts"], key=lambda part: part["index"])
    return "".join(part["text"] for part in parts).encode("utf-8")


def _redact_like_the_dispatcher(answer: dict[str, Any]) -> Any:
    """Run the stored-result redaction with the labels the dispatcher uses."""
    return apply_connector_boundary_redaction(
        answer,
        connector_id=f"{VmwareRestConnector.impl_id}-{VmwareRestConnector.version}",
        tenant=str(_TENANT),
        op=VCENTER_SSO_ADMIN_API_FILE_GET_OP.op_id,
    )


# ---------------------------------------------------------------------------
# Accepted path
# ---------------------------------------------------------------------------


async def test_reads_a_wsdl_with_size_hash_imports_version_and_parts() -> None:
    connector = _connector()
    body = _WSDL.encode("utf-8")
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert answer["path"] == _WSDL_PATH
    assert answer["status"] == 200
    assert answer["content_type"] == "text/xml"
    assert answer["redirect_path"] is None
    assert answer["size"] == len(body)
    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert answer["imports"] == ["admin-messages.wsdl", "admin-types.xsd", "admin-faults.xsd"]
    assert answer["parts"] == 1
    assert answer["text_parts"] == [{"index": 0, "text": _WSDL}]
    assert answer["version"] == "8.0.3.00000"
    assert answer["build"] == "11111111"
    assert answer["version_error"] is None


async def test_query_wsdl_reaches_the_vcenter_as_written() -> None:
    connector = _connector()
    path = "/sso-adminserver/sdk/example.test?wsdl"
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        route = mock.get(url__regex=r".*/sso-adminserver/sdk/example\.test\?wsdl$").respond(
            200, content=_WSDL.encode(), headers={"Content-Type": "text/xml; charset=utf-8"}
        )
        answer = await _call(connector, path)
    await connector.aclose()

    assert route.call_count == 1
    assert route.calls.last.request.url.raw_path == path.encode()
    assert answer["status"] == 200


async def test_the_file_get_sends_no_login_header_and_no_session_cookie() -> None:
    connector = _connector()
    target = _Target()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        file_route = mock.get(_WSDL_PATH).respond(
            200, content=_WSDL.encode(), headers={"Content-Type": "text/xml"}
        )
        # Warm the pooled session first, so a cookie and a token exist.
        await connector._get_json(target, "/api/appliance/system/version", operator=_operator())
        await connector.sso_admin_api_file_get(_operator(), target, {"path": _WSDL_PATH})
    await connector.aclose()

    request = file_route.calls.last.request
    header_names = {name.lower() for name in request.headers}
    assert "cookie" not in header_names
    assert "authorization" not in header_names
    assert "vmware-api-session-id" not in header_names


# ---------------------------------------------------------------------------
# Refused paths: no request goes out
# ---------------------------------------------------------------------------

_REFUSED_PATHS = [
    "/sso-adminserver/../api/session",
    "/sso-adminserver/sdk/..",
    "/sso-adminserver//sdk",
    "/sso-adminserver/%2e%2e/api",
    "/sso-adminserver/sdk%2fx",
    "/sso-adminserver\\sdk",
    "https://other.example/sso-adminserver/sdk",
    "//other.example/sso-adminserver/sdk",
    "/sso-adminserver@other.example/sdk",
    "/api/appliance/system/version",
    "/sso-adminserver",
    "/sdk/vimServiceVersions.xml",
    "/sso-adminserver/sdk?foo",
    "/sso-adminserver/sdk?wsdl&x=1",
    "/sso-adminserver/sdk?xsd=a",
    "/sso-adminserver/sdk#frag",
    " /sso-adminserver/sdk",
]


@pytest.mark.parametrize("path", _REFUSED_PATHS)
async def test_refused_path_sends_no_request(path: str) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        catch_all = mock.route().respond(200, content=b"<x/>")
        with pytest.raises(SsoAdminApiFileError, match="No request was sent"):
            await _call(connector, path)
        assert mock.calls.call_count == 0
        assert catch_all.call_count == 0
    await connector.aclose()


@pytest.mark.parametrize("path", _REFUSED_PATHS)
def test_refused_path_fails_parameter_validation(path: str) -> None:
    """The dispatcher checks the same rule before the handler runs."""
    errors = validate_params(VCENTER_SSO_ADMIN_API_FILE_GET_OP.parameter_schema, {"path": path})
    assert errors


@pytest.mark.parametrize(
    "path",
    [
        "/sso-adminserver/",
        "/sso-adminserver/sdk/example.test",
        "/sso-adminserver/sdk/example.test?wsdl",
        "/sso-adminserver/sdk/example.test?xsd",
        "/sso-adminserver/sdk/example.test?xsd=2",
        "/sso-adminserver/wsdl/admin-types.xsd",
    ],
)
def test_accepted_paths_pass_parameter_validation(path: str) -> None:
    schema = VCENTER_SSO_ADMIN_API_FILE_GET_OP.parameter_schema
    assert validate_params(schema, {"path": path}) == []
    assert api_file.check_api_file_path(path) == path


# ---------------------------------------------------------------------------
# Status, redirect and version cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_error_status_comes_back_in_the_answer(status: int) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(status, text="denied", headers={"Content-Type": "text/html"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert answer["status"] == status
    assert answer["size"] is None
    assert answer["sha256"] is None
    assert answer["parts"] == 0
    assert answer["text_parts"] == []
    assert answer["imports"] == []
    assert answer["build"] == "11111111"


async def test_redirect_is_not_followed_and_its_path_comes_back() -> None:
    connector = _connector()
    next_path = "/sso-adminserver/sdk/example.test/?wsdl"
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(302, headers={"Location": next_path})
        followed = mock.get(url__regex=r".*/sso-adminserver/sdk/example\.test/\?wsdl$").respond(
            200, content=b"<x/>"
        )
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert followed.call_count == 0
    assert answer["status"] == 302
    assert answer["redirect_path"] == next_path
    assert answer["parts"] == 0


async def test_relative_redirect_resolves_against_the_file_folder() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(301, headers={"Location": "other.wsdl"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert answer["redirect_path"] == "/sso-adminserver/sdk/example.test/other.wsdl"


@pytest.mark.parametrize("version_status", [500, 401])
async def test_failed_version_read_still_returns_the_file(version_status: int) -> None:
    connector = _connector()
    body = _WSDL.encode()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock, version_status=version_status)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert answer["version"] is None
    assert answer["build"] is None
    assert "/api/appliance/system/version" in answer["version_error"]


async def _no_credentials(_target: Any, _operator: Operator) -> dict[str, str]:
    raise VaultCredentialsReadError("no credentials stored for target 'vc-sso'")


async def _store_login_fails(_target: Any, _operator: Operator) -> dict[str, str]:
    raise VaultUnreachableError("credential store login failed: connection refused")


@pytest.mark.parametrize(
    ("loader", "error_name"),
    [
        (_no_credentials, "VaultCredentialsReadError"),
        (_store_login_fails, "VaultUnreachableError"),
    ],
    ids=["missing-credentials", "credential-store-login-fails"],
)
async def test_credential_failure_on_the_version_read_still_returns_the_file(
    loader: Any, error_name: str
) -> None:
    connector = _connector(session_loader=loader)
    body = _WSDL.encode()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert answer["status"] == 200
    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert answer["text_parts"] == [{"index": 0, "text": _WSDL}]
    assert answer["version"] is None
    assert answer["build"] is None
    assert error_name in answer["version_error"]
    assert "/api/appliance/system/version" in answer["version_error"]


# ---------------------------------------------------------------------------
# Large files: joining the parts gives the exact file
# ---------------------------------------------------------------------------


def _one_line_xml(size: int) -> bytes:
    """A made-up one-line XML file of about *size* bytes, with multi-byte text."""
    item = '<item name="entry" note="café ✓"/>'
    count = size // len(item.encode("utf-8"))
    return ('<?xml version="1.0"?><root>' + item * count + "</root>").encode("utf-8")


def _many_line_xml(lines: int) -> bytes:
    rows = "\n".join(f'  <line n="{n}">value {n}</line>' for n in range(lines))
    return f'<?xml version="1.0"?>\n<root>\n{rows}\n</root>\n'.encode()


@pytest.mark.parametrize(
    "body",
    [_one_line_xml(3 * 1024 * 1024), _many_line_xml(12_000)],
    ids=["one-line-3mb", "12000-lines"],
)
async def test_joined_parts_have_the_file_sha256(body: bytes) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    assert answer["size"] == len(body)
    assert answer["parts"] == len(answer["text_parts"]) > 1
    assert [part["index"] for part in answer["text_parts"]] == list(range(answer["parts"]))
    assert all(len(part["text"].encode()) <= PART_MAX_BYTES for part in answer["text_parts"])
    assert hashlib.sha256(_joined(answer)).hexdigest() == answer["sha256"]


def test_split_never_cuts_a_multi_byte_character() -> None:
    data = ("a" + "é" * 50_000 + "\U0001f600" * 20_000).encode("utf-8")
    parts = split_text_parts(data)
    assert len(parts) > 2
    assert all(len(part.encode("utf-8")) <= PART_MAX_BYTES for part in parts)
    assert "".join(parts).encode("utf-8") == data
    assert split_text_parts(b"") == []


async def test_file_too_big_for_one_result_stops_with_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _many_line_xml(4_000)
    # The file fits the read cap, but not once it is stored as JSON text
    # (quotes and line ends are escaped, and the record needs room).
    cap = len(body) + 2_000
    monkeypatch.setattr(
        api_file, "get_settings", lambda: SimpleNamespace(result_handle_max_record_bytes=cap)
    )
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        with pytest.raises(SsoAdminApiFileError, match=f"is {len(body)} bytes"):
            await _call(connector, _WSDL_PATH)
    await connector.aclose()


async def test_file_over_the_byte_cap_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api_file, "get_settings", lambda: SimpleNamespace(result_handle_max_record_bytes=10_000)
    )
    body = _many_line_xml(2_000)
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        with pytest.raises(SsoAdminApiFileError, match=f"{len(body)} bytes"):
            await _call(connector, _WSDL_PATH)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Text only
# ---------------------------------------------------------------------------


async def test_non_text_content_is_refused_with_its_content_type() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(
            200, content=b"\x00\x01", headers={"Content-Type": "application/octet-stream"}
        )
        with pytest.raises(SsoAdminApiFileError, match="application/octet-stream"):
            await _call(connector, _WSDL_PATH)
    await connector.aclose()


async def test_non_utf8_text_is_refused() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(
            200, content=b"<a>\xff\xfe</a>", headers={"Content-Type": "text/xml"}
        )
        with pytest.raises(SsoAdminApiFileError, match="not UTF-8"):
            await _call(connector, _WSDL_PATH)
    await connector.aclose()


_WSDL_WITH_TOKEN_TYPE = _WSDL.replace(
    '<xsd:include schemaLocation="admin-faults.xsd"/>',
    '<xsd:include schemaLocation="admin-faults.xsd"/>\n'
    '      <xsd:element name="token" type="token:SecurityToken"/>',
)


async def test_file_whose_text_redaction_would_change_is_refused() -> None:
    body = _WSDL_WITH_TOKEN_TYPE.encode()
    assert _redact_like_the_dispatcher({"text": _WSDL_WITH_TOKEN_TYPE}).manifest
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        with pytest.raises(SsoAdminApiFileError) as excinfo:
            await _call(connector, _WSDL_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert _WSDL_PATH in message
    assert "strip-api-key" in message
    assert "text_parts.0.text" in message
    assert "not returned" in message
    assert "SecurityToken" not in message


def _xml_with_a_secret_across_the_part_cut() -> bytes:
    """Valid one-line XML whose ``password="..."`` label straddles the first part cut.

    All characters are ASCII, so the cut falls exactly at ``PART_MAX_BYTES``:
    ``pass`` ends part 0 and ``word="..."`` starts part 1.
    """
    head = '<?xml version="1.0"?><root><pad n="'
    joint = '"/><entry '
    before = head + "a" * (PART_MAX_BYTES - 4 - len(head) - len(joint)) + joint
    assert len(before) == PART_MAX_BYTES - 4
    return (before + 'password="made-up-pass-value-0123"/></root>').encode()


async def test_secret_split_across_the_part_cut_is_refused() -> None:
    body = _xml_with_a_secret_across_the_part_cut()
    parts = split_text_parts(body)
    assert parts[0].endswith("pass") and parts[1].startswith('word="')
    # The per-part check alone misses the split label; the whole text trips it.
    per_part = _redact_like_the_dispatcher(
        {"text_parts": [{"index": i, "text": text} for i, text in enumerate(parts)]}
    )
    assert per_part.manifest == () and per_part.redacted == per_part.raw
    assert _redact_like_the_dispatcher({"text": body.decode()}).manifest

    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        with pytest.raises(SsoAdminApiFileError) as excinfo:
            await _call(connector, _WSDL_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert "strip-api-key" in message
    assert "file_text" in message
    assert "not returned" in message
    assert "made-up-pass-value" not in message


@pytest.mark.parametrize(
    "body",
    [
        b"this is plain text, not XML",
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "v">]><x>&e;</x>',
    ],
    ids=["not-xml", "declares-entities"],
)
def test_imports_is_empty_for_a_file_that_is_not_plain_xml(body: bytes) -> None:
    assert find_imports(body) == []


# ---------------------------------------------------------------------------
# JSONFlux: a large answer is kept whole behind a result handle
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


async def test_large_answer_is_read_back_part_by_part_through_result_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    body = _one_line_xml(1024 * 1024)
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(200, content=body, headers={"Content-Type": "text/xml"})
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    redaction = _redact_like_the_dispatcher(answer)
    assert redaction.manifest == ()
    assert redaction.redacted == redaction.raw
    context = {
        "op_id": VCENTER_SSO_ADMIN_API_FILE_GET_OP.op_id,
        "operator_sub": _operator().sub,
        "tenant_id": str(_TENANT),
    }
    summary, handle = await JsonFluxReducer(store=store).reduce(redaction.redacted, None, context)

    assert handle is not None
    assert handle.fetch_more is not None
    assert handle.fetch_more.drill_in.available is True
    assert summary["sha256"] == answer["sha256"]
    assert summary["parts"] == answer["parts"]
    assert summary["build"] == "11111111"
    texts: list[str] = []
    for offset in range(0, answer["parts"], 2):
        page = await run_result_query(
            _operator(),
            handle.handle_id,
            ResultQuerySpec(path="/text_parts"),
            offset=offset,
            limit=2,
        )
        texts.extend(row["text"] for row in page["rows"])
    assert hashlib.sha256("".join(texts).encode("utf-8")).hexdigest() == answer["sha256"]
    imports = await run_result_query(
        _operator(), handle.handle_id, ResultQuerySpec(path="/imports"), offset=0, limit=50
    )
    assert imports["total_rows"] == 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_op_is_a_registered_typed_read_on_both_vcenter_catalogs() -> None:
    op = VCENTER_SSO_ADMIN_API_FILE_GET_OP
    assert op in VMWARE_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert callable(getattr(VmwareRestConnector, op.handler_attr))
    assert callable(getattr(VmwareRest80Connector, op.handler_attr))
    group_text = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[SSO_ADMIN_API_FILE_GROUP_KEY]
    assert op.op_id in group_text
    assert "safe" not in group_text.lower()


def test_examples_validate_and_use_placeholders() -> None:
    schema = VCENTER_SSO_ADMIN_API_FILE_GET_OP.parameter_schema
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
        assert "DOMAIN" in example["path"]


async def test_answer_never_carries_the_session_token_or_password() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_WSDL_PATH).respond(
            200, content=_WSDL.encode(), headers={"Content-Type": "text/xml"}
        )
        answer = await _call(connector, _WSDL_PATH)
    await connector.aclose()

    text = json.dumps(answer)
    assert "session-token-abc" not in text
    assert "cookie-abc" not in text
    assert "stub-password" not in text
