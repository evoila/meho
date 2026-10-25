# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vcfa.vra8.api_file.get`` typed read.

The read gets one documentation page or API description file from an Aria
Automation 8 appliance with one ``GET`` and the connector's login. HTTP is
mocked with ``respx``, like ``test_connectors_vra8_typed_reads.py``. All
file contents are made up here; the large files are built by helpers, not
stored in the repo. The dispatch path (zero-catalog dispatch and the 401
re-login) is proven in ``test_connectors_vra8_typed_reads.py``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.api_file_secrets import REDACTED, ApiFileFormatError
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vra8 import VRA8_TYPED_OPS, Vra8Connector, api_file
from meho_backplane.connectors.vra8.api_file import (
    OP_ID,
    PART_MAX_BYTES,
    Vra8ApiFileError,
    split_text_parts,
)
from meho_backplane.connectors.vra8.typed_ops import VRA8_TYPED_WHEN_TO_USE_BY_GROUP
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.redaction import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

_HOST = "vra-api-file.test.invalid"
_BASE = f"https://{_HOST}"
_SWAGGER_PATH = "/iaas/api/swagger"
_TENANT = UUID("00000000-0000-0000-0000-00000000b6b6")

_SWAGGER = json.dumps(
    {
        "swagger": "2.0",
        "info": {"title": "Example IaaS API", "version": "2021-07-15"},
        "paths": {"/iaas/api/projects": {"get": {"summary": "List projects"}}},
    },
    separators=(",", ":"),
)

API_FILE_OP = next(op for op in VRA8_TYPED_OPS if op.op_id == OP_ID)


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
    name: str = "vra-api-file"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vcfa/vra-api-file"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    extras: dict[str, Any] = field(default_factory=dict)


def _operator() -> Operator:
    return Operator(
        sub="op-vra-file",
        name="VRA File Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-test", "password": "stub-password"}


def _connector(credentials_loader: Any = _stub_loader) -> Vra8Connector:
    return Vra8Connector(credentials_loader=credentials_loader)


def _mock_login_and_version(
    mock: respx.MockRouter, *, vco_status: int = 200, iaas_status: int = 200
) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "refresh-abc"})
    mock.post("/iaas/api/login").respond(200, json={"tokenType": "Bearer", "token": "bearer-abc"})
    if vco_status == 200:
        mock.get("/vco/api/about").respond(
            200,
            json={
                "version": "8.18.0.00000",
                "build-number": "22222222",
                "build-date": "2024-01-01",
                "api-version": "8.18",
            },
        )
    else:
        mock.get("/vco/api/about").respond(vco_status)
    if iaas_status == 200:
        mock.get("/iaas/api/about").respond(
            200, json={"latestApiVersion": "2021-07-15", "supportedApis": []}
        )
    else:
        mock.get("/iaas/api/about").respond(iaas_status)


async def _call(connector: Vra8Connector, path: str) -> dict[str, Any]:
    return await connector.api_file_get(_operator(), _Target(), {"path": path})


def _joined(answer: dict[str, Any]) -> bytes:
    parts = sorted(answer["text_parts"], key=lambda part: part["index"])
    return "".join(part["text"] for part in parts).encode("utf-8")


def _settings(*, max_record_bytes: int = 16 * 1024 * 1024, max_spill_rows: int = 10_000) -> Any:
    """The two stored-result limits the read checks, as the settings object."""
    return SimpleNamespace(
        result_handle_max_record_bytes=max_record_bytes,
        result_handle_max_spill_rows=max_spill_rows,
    )


def _redact_like_the_dispatcher(answer: dict[str, Any]) -> Any:
    """Run the stored-result redaction with the labels the dispatcher uses."""
    return apply_connector_boundary_redaction(
        answer,
        connector_id=f"{Vra8Connector.impl_id}-{Vra8Connector.version}",
        tenant=str(_TENANT),
        op=OP_ID,
    )


# ---------------------------------------------------------------------------
# Accepted path
# ---------------------------------------------------------------------------


async def test_reads_a_swagger_file_with_size_hash_version_and_parts() -> None:
    connector = _connector()
    body = _SWAGGER.encode("utf-8")
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        route = mock.get(_SWAGGER_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert answer["path"] == _SWAGGER_PATH
    assert answer["status"] == 200
    assert answer["content_type"] == "application/json"
    assert answer["redirect_path"] is None
    assert answer["size"] == len(body)
    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert answer["parts"] == 1
    assert answer["text_parts"] == [{"index": 0, "text": _SWAGGER}]
    assert answer["version"] == "8.18.0.00000"
    assert answer["build"] == "22222222"
    assert answer["iaas_api_version"] == "2021-07-15"
    assert answer["version_error"] is None
    # The file GET carried the connector's login.
    assert route.calls.last.request.headers["Authorization"] == "Bearer bearer-abc"


async def test_landing_page_html_comes_back_as_text() -> None:
    connector = _connector()
    page = '<html><body><a href="/iaas/api/swagger">IaaS</a></body></html>'
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get("/automation-ui/api-docs").respond(
            200, text=page, headers={"Content-Type": "text/html; charset=utf-8"}
        )
        answer = await _call(connector, "/automation-ui/api-docs")
    await connector.aclose()

    assert answer["status"] == 200
    assert answer["text_parts"] == [{"index": 0, "text": page}]


@pytest.mark.parametrize(
    "path",
    [
        "/iaas/api/swagger?apiVersion=2021-07-15",
        "/deployment/api/swagger/swagger/v2/api-docs?group=2020-08-25",
    ],
)
async def test_query_reaches_the_appliance_as_written(path: str) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        route = mock.get(url__regex=r".*\?(apiVersion|group)=20\d\d-\d\d-\d\d$").respond(
            200, content=_SWAGGER.encode(), headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, path)
    await connector.aclose()

    assert route.call_count == 1
    assert route.calls.last.request.url.raw_path == path.encode()
    assert answer["status"] == 200


# ---------------------------------------------------------------------------
# Refused paths: no request goes out
# ---------------------------------------------------------------------------

_REFUSED_PATHS = [
    "/vco/api/about",
    "/iaas/api/about",
    "/iaas/api/projects",
    "/csp/gateway/am/api/login",
    "/iaas/api/swagger/../projects",
    "/iaas/api/swagger/..",
    "/iaas/api//swagger",
    "/iaas/api/swagger/%2e%2e/projects",
    "/iaas/api/swagger%2fx",
    "/iaas/api/swagger\\x",
    "https://other.example/iaas/api/swagger",
    "//other.example/iaas/api/swagger",
    "/iaas@other.example/api/swagger",
    "/Iaas/api/swagger",
    "/iaas/api/swagger?foo=1",
    "/iaas/api/swagger?apiVersion=2021-07-15&foo=1",
    "/iaas/api/swagger#frag",
    " /iaas/api/swagger",
    "/automation/api-docs\n",
    "/x/swagger.yml",
    "/automation",
    "",
]


@pytest.mark.parametrize("path", _REFUSED_PATHS)
async def test_refused_path_sends_no_request(path: str) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        catch_all = mock.route().respond(200, content=b"{}")
        with pytest.raises(Vra8ApiFileError, match="No request was sent"):
            await _call(connector, path)
        assert mock.calls.call_count == 0
        assert catch_all.call_count == 0
    await connector.aclose()


@pytest.mark.parametrize("path", [p for p in _REFUSED_PATHS if "\n" not in p])
def test_refused_path_fails_parameter_validation(path: str) -> None:
    """The dispatcher checks the same rule before the handler runs."""
    errors = validate_params(API_FILE_OP.parameter_schema, {"path": path})
    assert errors


@pytest.mark.parametrize(
    "path",
    [
        "/automation/api-docs",
        "/automation/api-docs/",
        "/automation-ui/api-docs",
        "/automation-ui/api-docs/index.html",
        "/iaas/api/swagger",
        "/iaas/api/swagger?apiVersion=2021-07-15",
        "/deployment/api/swagger/swagger/v2/api-docs?group=2020-08-25",
        "/vco/api/docs",
        "/vco/api/docs/",
        "/vco/api/docs/swagger.json",
        "/cmx/v2/api-docs",
        "/x/swagger-resources/configuration/ui",
        "/x/swagger-config",
        "/x/openapi.yaml",
    ],
)
def test_accepted_paths_pass_parameter_validation(path: str) -> None:
    schema = API_FILE_OP.parameter_schema
    assert validate_params(schema, {"path": path}) == []
    assert api_file.check_api_file_path(path) == path


# ---------------------------------------------------------------------------
# Status, redirect and version cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_error_status_comes_back_in_the_answer(status: int) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            status, text="denied", headers={"Content-Type": "text/html"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert answer["status"] == status
    assert answer["size"] is None
    assert answer["sha256"] is None
    assert answer["parts"] == 0
    assert answer["text_parts"] == []
    assert answer["build"] == "22222222"


async def test_401_on_the_file_is_raised_so_the_dispatcher_logs_in_again() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(401)
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert excinfo.value.response.status_code == 401


async def test_redirect_is_not_followed_and_its_path_comes_back() -> None:
    connector = _connector()
    next_path = "/vco/api/docs/index.html"
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/vco/api/docs").respond(302, headers={"Location": next_path})
        followed = mock.get(next_path).respond(200, content=b"<x/>")
        answer = await _call(connector, "/vco/api/docs")
    await connector.aclose()

    assert followed.call_count == 0
    assert answer["status"] == 302
    assert answer["redirect_path"] == next_path
    assert answer["parts"] == 0


async def test_relative_redirect_resolves_against_the_file_folder() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get("/vco/api/docs/").respond(301, headers={"Location": "swagger.json"})
        answer = await _call(connector, "/vco/api/docs/")
    await connector.aclose()

    assert answer["redirect_path"] == "/vco/api/docs/swagger.json"


async def test_redirect_to_another_host_names_the_host_without_the_query() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get("/vco/api/docs").respond(
            302, headers={"Location": "https://other.test.invalid/docs/index.html?page=2#top"}
        )
        answer = await _call(connector, "/vco/api/docs")
    await connector.aclose()

    assert answer["status"] == 302
    assert answer["redirect_path"] == "https://other.test.invalid/docs/index.html"
    assert answer["parts"] == 0


@pytest.mark.parametrize(
    ("vco_status", "iaas_status", "failed_path"),
    [(500, 200, "/vco/api/about"), (403, 200, "/vco/api/about"), (200, 500, "/iaas/api/about")],
)
async def test_failed_version_read_still_returns_the_file(
    vco_status: int, iaas_status: int, failed_path: str
) -> None:
    connector = _connector()
    body = _SWAGGER.encode()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock, vco_status=vco_status, iaas_status=iaas_status)
        mock.get(_SWAGGER_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert failed_path in answer["version_error"]
    if failed_path == "/vco/api/about":
        assert answer["version"] is None
        assert answer["build"] is None
        assert answer["iaas_api_version"] == "2021-07-15"
    else:
        assert answer["build"] == "22222222"
        assert answer["iaas_api_version"] is None


async def test_about_without_a_build_number_says_so() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "r"})
        mock.post("/iaas/api/login").respond(200, json={"token": "bearer-abc"})
        mock.get("/vco/api/about").respond(200, json={"version": "8.18.0.00000"})
        mock.get("/iaas/api/about").respond(200, json={"latestApiVersion": "2021-07-15"})
        mock.get(_SWAGGER_PATH).respond(
            200, content=b"{}", headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert answer["version"] == "8.18.0.00000"
    assert answer["build"] is None
    assert "no build-number" in answer["version_error"]


async def _no_credentials(_target: Any, _operator: Operator) -> dict[str, str]:
    raise VaultCredentialsReadError("no credentials stored for target 'vra-api-file'")


async def test_missing_credentials_sends_no_request() -> None:
    """The file GET needs the login, so a credential failure stops the read."""
    connector = _connector(credentials_loader=_no_credentials)
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        catch_all = mock.route().respond(200, content=b"{}")
        with pytest.raises(VaultCredentialsReadError):
            await _call(connector, _SWAGGER_PATH)
        assert catch_all.call_count == 0
    await connector.aclose()


# ---------------------------------------------------------------------------
# Large files: joining the parts gives the checked copy
# ---------------------------------------------------------------------------


def _one_line_json(size: int) -> bytes:
    """A made-up one-line JSON file of about *size* bytes, with multi-byte text."""
    item = '{"name":"entry","note":"café ✓"}'
    count = size // len(item.encode("utf-8"))
    return ('{"items":[' + ",".join([item] * count) + "]}").encode("utf-8")


def _many_line_yaml(lines: int) -> bytes:
    rows = "\n".join(f"  - name: line-{n}\n    value: {n}" for n in range(lines))
    return f"openapi: 3.0.0\nitems:\n{rows}\n".encode()


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (_one_line_json(3 * 1024 * 1024), "application/json"),
        (_many_line_yaml(12_000), "application/yaml"),
    ],
    ids=["one-line-3mb", "12000-lines"],
)
async def test_joined_parts_have_the_copy_sha256(body: bytes, content_type: str) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": content_type})
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert answer["size"] == len(_joined(answer))
    assert answer["secrets_replaced"] == 0
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
    body = _one_line_json(200 * 1024)
    # The file fits the read cap, but not once it is stored as JSON text
    # (its quotes are escaped, and the record needs room).
    cap = len(body) + 2_000
    monkeypatch.setattr(api_file, "get_settings", lambda: _settings(max_record_bytes=cap))
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        with pytest.raises(Vra8ApiFileError, match=f"is {len(body)} bytes"):
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()


async def test_file_over_the_byte_cap_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_file, "get_settings", lambda: _settings(max_record_bytes=10_000))
    body = _many_line_yaml(2_000)
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": "text/plain"})
        with pytest.raises(Vra8ApiFileError, match=f"{len(body)} bytes"):
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()


async def test_gzip_body_is_measured_after_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small gzip file is read; a small gzip body that decodes past the cap is refused."""
    monkeypatch.setattr(api_file, "get_settings", lambda: _settings(max_record_bytes=10_000))
    small = _SWAGGER.encode()
    large = b"x" * 1_000_000
    assert len(gzip.compress(large)) < 10_000
    gzip_json = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/vco/api/docs/swagger.json").respond(
            200, content=gzip.compress(small), headers=gzip_json
        )
        mock.get(_SWAGGER_PATH).respond(200, content=gzip.compress(large), headers=gzip_json)
        fits = await _call(connector, "/vco/api/docs/swagger.json")
        with pytest.raises(Vra8ApiFileError, match="more than 10000 bytes"):
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    assert fits["status"] == 200
    assert _joined(fits) == small
    assert fits["sha256"] == hashlib.sha256(small).hexdigest()


async def test_file_get_is_kept_in_the_flight_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        api_file.flight_recorder_capture,
        "record_vendor_call",
        lambda _span, **kwargs: recorded.append(kwargs),
    )
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, text=_SWAGGER, headers={"Content-Type": "application/json"}
        )
        await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    file_calls = [call for call in recorded if call["response"].url.path == _SWAGGER_PATH]
    assert [(call["method"], call["response"].status_code) for call in file_calls] == [("GET", 200)]


_SECRET_FILE = '{"swagger":"2.0","x-example":{"password":"made-up-recorder-secret"}}'


def _record_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Catch what the read hands the flight recorder for the file GET."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        api_file.flight_recorder_capture,
        "record_vendor_call",
        lambda _span, **kwargs: recorded.append(kwargs),
    )
    return recorded


@pytest.mark.parametrize(
    ("status", "body", "content_type"),
    [
        (200, _SECRET_FILE, "application/json"),
        (404, _SECRET_FILE, "application/json"),
        (500, "<html><body>password: not parsed</body>", "text/plain"),
    ],
)
async def test_flight_recorder_gets_only_the_checked_body(
    monkeypatch: pytest.MonkeyPatch, status: int, body: str, content_type: str
) -> None:
    """The recorder gets the checked copy of the body, or no body when it cannot be checked."""
    recorded = _record_calls(monkeypatch)
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(status, text=body, headers={"Content-Type": content_type})
        await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    (call,) = [call for call in recorded if call["response"].url.path == _SWAGGER_PATH]
    if content_type == "application/json":
        assert REDACTED.encode() in call["response_body"]
    else:
        assert call["response_body"] == b""
    assert b"made-up-recorder-secret" not in call["response_body"]
    assert b"not parsed" not in call["response_body"]


async def test_flight_recorder_gets_no_body_when_the_file_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _record_calls(monkeypatch)
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200,
            text='{"password": "made-up-recorder-secret"',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(ApiFileFormatError):
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    (call,) = [call for call in recorded if call["response"].url.path == _SWAGGER_PATH]
    assert call["response_body"] == b""


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (_SECRET_FILE, "application/json", REDACTED.encode()),
        ("password=made-up-recorder-secret", "text/plain", b""),
        ("\xff\xfe", "application/octet-stream", b""),
    ],
)
async def test_401_body_is_checked_before_it_is_raised(
    monkeypatch: pytest.MonkeyPatch, body: str, content_type: str, expected: bytes
) -> None:
    """A 401 is raised with the checked body, or none; the status stays for the login retry."""
    recorded = _record_calls(monkeypatch)
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(401, text=body, headers={"Content-Type": content_type})
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    raised = excinfo.value.response
    assert raised.status_code == 401
    if expected:
        assert expected in raised.content
    else:
        assert raised.content == b""
    assert b"made-up-recorder-secret" not in raised.content
    (call,) = [call for call in recorded if call["response"].url.path == _SWAGGER_PATH]
    assert call["response_body"] == raised.content


async def test_file_with_more_parts_than_the_row_limit_stops_with_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store keeps at most ``result_handle_max_spill_rows`` parts; three do not fit in two."""
    monkeypatch.setattr(api_file, "get_settings", lambda: _settings(max_spill_rows=2))
    body = _one_line_json(150 * 1024)
    assert len(split_text_parts(body)) == 3
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        with pytest.raises(Vra8ApiFileError) as excinfo:
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert f"is {len(body)} bytes in 3 parts" in message
    assert "at most 2 parts" in message
    assert "not returned" in message


async def test_two_parts_fit_the_row_limit_of_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_file, "get_settings", lambda: _settings(max_spill_rows=2))
    body = _one_line_json(100 * 1024)
    assert len(split_text_parts(body)) == 2
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()
    assert answer["parts"] == 2
    assert hashlib.sha256(_joined(answer)).hexdigest() == answer["sha256"]


# ---------------------------------------------------------------------------
# Text only
# ---------------------------------------------------------------------------


async def test_non_text_content_is_refused_with_its_content_type() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=b"\x00\x01", headers={"Content-Type": "application/octet-stream"}
        )
        with pytest.raises(Vra8ApiFileError, match="application/octet-stream"):
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()


async def test_non_utf8_text_is_refused() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=b'{"a":"\xff\xfe"}', headers={"Content-Type": "application/json"}
        )
        with pytest.raises(Vra8ApiFileError, match="not UTF-8"):
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Secret values are replaced before the text is returned
# ---------------------------------------------------------------------------

# Built from two strings so the repository's private-key scan does not see a key.
_PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
_PEM_BLOCK = f"{_PEM_HEADER}\nMIIEmadeupmadeupmadeup\n-----END RSA PRIVATE KEY-----\n"

_HTML_HEAD = "<!DOCTYPE html>\n<html><head><title>API docs</title></head>\n<body>\n"
_HTML_TAIL = "\n</body></html>\n"
_SCRIPT_WITH_PASSWORD = (
    '<script>\nvar login = {"user": "u",\n  "password":\n    "made-up-pass-value-0123"};\n</script>'
)
_XML_WITH_PASSWORD = (
    '<?xml version="1.0"?>\n<api>\n  <user>u</user>\n'
    "  <password>made-up-pass-value-0123</password>\n</api>\n"
)
_HTML_WITH_PASSWORD_ATTRIBUTE = '<input name="login" password="mu-01">'

_SWAGGER_YAML = "swagger: '2.0'\ninfo:\n  title: Example IaaS API\n  version: 2021-07-15\n"


def _swagger_with(field: str, value: str) -> str:
    return _SWAGGER.replace('"swagger":"2.0"', f'"swagger":"2.0","{field}":"{value}"')


def _swagger_yaml_with(line: str) -> str:
    return _SWAGGER_YAML + line + "\npaths: {}\n"


async def _read(path: str, body: bytes, content_type: str) -> dict[str, Any]:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(path).respond(200, content=body, headers={"Content-Type": content_type})
        answer = await _call(connector, path)
    await connector.aclose()
    return answer


def _assert_cleaned(answer: dict[str, Any], secrets: tuple[str, ...], count: int) -> None:
    """The answer holds the checked copy, counts the values, and pins the copy."""
    assert answer["secrets_replaced"] == count
    copy = _joined(answer)
    assert b"[REDACTED:" in copy
    assert answer["size"] == len(copy)
    assert answer["sha256"] == hashlib.sha256(copy).hexdigest()
    dumped = json.dumps(answer)
    for secret in secrets:
        assert secret not in dumped
    # The stored-result redaction leaves the answer as it is, so the
    # dispatcher stores exactly this text.
    redaction = _redact_like_the_dispatcher(answer)
    assert redaction.manifest == () and redaction.redacted == redaction.raw


async def test_labelled_pair_in_a_description_is_replaced_and_counted() -> None:
    """A ``password=...`` label inside a string: MEHO's general rules replace it in the copy."""
    body = _swagger_with("x-note", "password=made-up-pass-value-0123")
    assert _redact_like_the_dispatcher({"text": body}).manifest
    answer = await _read(_SWAGGER_PATH, body.encode(), "application/json")
    _assert_cleaned(answer, ("made-up-pass-value",), 1)
    assert json.loads(_joined(answer))["swagger"] == "2.0"


def _json_with_a_secret_across_the_part_cut() -> bytes:
    """Valid one-line JSON whose ``password=...`` label straddles the first part cut.

    All characters are ASCII, so the cut falls exactly at ``PART_MAX_BYTES``:
    ``pass`` ends part 0 and ``word=...`` starts part 1.
    """
    head = '{"pad":"'
    joint = '","note":"'
    before = head + "a" * (PART_MAX_BYTES - 4 - len(head) - len(joint)) + joint
    assert len(before) == PART_MAX_BYTES - 4
    return (before + 'password=made-up-pass-value-0123"}').encode()


async def test_labelled_pair_split_across_the_part_cut_is_replaced() -> None:
    body = _json_with_a_secret_across_the_part_cut()
    parts = split_text_parts(body)
    assert parts[0].endswith("pass") and parts[1].startswith("word=")
    answer = await _read(_SWAGGER_PATH, body, "application/json")
    _assert_cleaned(answer, ("made-up-pass-value",), 1)


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    [
        (
            "/automation/api-docs",
            _HTML_HEAD + _SCRIPT_WITH_PASSWORD + _HTML_TAIL,
            "text/html; charset=utf-8",
        ),
        (f"{_SWAGGER_PATH}/api-docs", _XML_WITH_PASSWORD, "application/xml"),
        (
            "/automation-ui/api-docs",
            _HTML_HEAD + _HTML_WITH_PASSWORD_ATTRIBUTE + _HTML_TAIL,
            "text/html; charset=utf-8",
        ),
    ],
    ids=["html-script-value-on-the-next-line", "xml-element", "html-attribute"],
)
async def test_password_in_an_html_or_xml_file_is_replaced(
    path: str, body: str, content_type: str
) -> None:
    answer = await _read(path, body.encode(), content_type)
    _assert_cleaned(answer, ("made-up-pass-value", "mu-01"), 1)


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (_swagger_with("password", "made-up-pass-value-0123"), "application/json"),
        (_swagger_with("token", "made-up-token-value-0123"), "application/json"),
        (_swagger_with("refreshToken", "made-up-token-value-0123"), "application/json"),
        (_swagger_with("password", "it's-made-up-value"), "application/json"),
        (_swagger_with("pwd", "m"), "application/json"),
        (_swagger_with("licenseKey", "MADE-UP-0000-0000"), "application/json"),
        (_swagger_with("pass\\u0077ord", "made-up"), "application/json"),
        (_swagger_yaml_with("licenseKey: MADE-UP-0000-0000"), "application/yaml"),
        (_swagger_yaml_with("example: {password: mu-01}"), "application/yaml"),
        (_swagger_yaml_with("password:\n  made-up"), "application/yaml"),
        (_swagger_yaml_with('password: "made\n  up"'), "application/yaml"),
        (
            _swagger_yaml_with("x:\n  password: | # note\n    made up\n    and more"),
            "application/yaml",
        ),
        (_swagger_yaml_with("password: made up # note"), "application/yaml"),
        (_swagger_yaml_with("password: # note\n  madeup"), "application/yaml"),
        (_swagger_yaml_with("password: made\n  up"), "application/yaml"),
        (_swagger_yaml_with("password: null madeup"), "application/yaml"),
        (_swagger_yaml_with("password: # note\n  # another note\n  madeup"), "application/yaml"),
        (_swagger_yaml_with("password: made,up"), "application/yaml"),
        (
            '<?xml version="1.0"?>\n<api>\n  <password>made<!-- note -->up</password>\n</api>\n',
            "application/xml",
        ),
        (
            '<?xml version="1.0"?>\n<api>\n  <password>ma<![CDATA[de]]>up</password>\n</api>\n',
            "application/xml",
        ),
        (
            '<?xml version="1.0"?>\n<api>\n  <password>made<b>up</b></password>\n</api>\n',
            "application/xml",
        ),
        (_PEM_BLOCK, "application/x-yaml"),
    ],
    ids=[
        "json-password",
        "json-token",
        "json-refresh-token",
        "json-password-with-apostrophe",
        "json-one-character-pwd",
        "json-license-key",
        "json-key-written-with-an-escape",
        "yaml-license-key",
        "yaml-flow-mapping",
        "yaml-value-on-the-next-line",
        "yaml-quoted-value-over-two-lines",
        "yaml-block-scalar-with-a-comment",
        "yaml-value-with-a-space-and-a-comment",
        "yaml-comment-after-the-colon",
        "yaml-bare-value-over-two-lines",
        "yaml-value-after-null",
        "yaml-value-after-two-comment-lines",
        "yaml-value-with-a-comma",
        "xml-text-split-by-a-comment",
        "xml-text-split-by-a-cdata-section",
        "xml-element-with-a-child-element",
        "private-key",
    ],
)
async def test_secret_value_in_any_layout_is_replaced_whole(body: str, content_type: str) -> None:
    """Every layout the code reviews found: the file is parsed, so the whole value goes."""
    answer = await _read(_SWAGGER_PATH, body.encode(), content_type)
    _assert_cleaned(answer, (), 1)
    copy = _joined(answer).decode()
    for leftover in ("made", "MADE", "madeup", "mu-01", "MIIE", "'m'", '"m"', "up</", ",up"):
        assert leftover not in copy


@pytest.mark.parametrize(
    ("kind", "snippet", "before_cut"),
    [
        ("xml", "<password>made<!-- note -->up</password>", 20),
        ("yaml", 'password: "made\n  up"', 16),
        ("yaml", "password: # note\n  # another note\n  madeup", 17),
        ("yaml", "password: null madeup", 14),
        ("yaml", "password: made,up", 14),
        ("json", '"pass\\u0077ord": "madeup"', 8),
        ("xml", "<password>made<b>up</b></password>", 16),
    ],
    ids=[
        "xml-cut-inside-the-comment",
        "yaml-cut-at-the-line-break-inside-the-quotes",
        "yaml-cut-in-the-comment-lines",
        "yaml-cut-after-null",
        "yaml-cut-before-the-comma",
        "json-cut-inside-the-escape-of-the-key",
        "xml-cut-inside-the-child-element",
    ],
)
async def test_secret_value_split_across_the_part_cut_is_replaced(
    kind: str, snippet: str, before_cut: int
) -> None:
    """The file is checked whole before the cut, so a part boundary hides nothing."""
    head, joint, tail, content_type = {
        "xml": ('<?xml version="1.0"?>\n<api>\n<!-- ', " -->\n", "\n</api>\n", "application/xml"),
        "yaml": ("swagger: '2.0'\n# ", "\n", "\npaths: {}\n", "application/yaml"),
        "json": (
            '{"swagger": "2.0", "info": {"title": "',
            '"}, ',
            ', "paths": {}}',
            "application/json",
        ),
    }[kind]
    pad = "/" * (PART_MAX_BYTES - before_cut - len(head) - len(joint))
    body = f"{head}{pad}{joint}{snippet}{tail}"
    assert body.index(snippet) == PART_MAX_BYTES - before_cut
    parts = split_text_parts(body.encode())
    assert parts[0].endswith(snippet[:before_cut]) and parts[1].startswith(snippet[before_cut:])

    answer = await _read(_SWAGGER_PATH, body.encode(), content_type)
    _assert_cleaned(answer, (), 1)
    assert "made" not in _joined(answer).decode()


async def test_every_secret_value_is_counted() -> None:
    body = _SWAGGER.replace(
        '"paths"',
        '"x-examples":[{"password":\n "made-up-a"},{"token":\n "made-up-b"},{"pwd":null}],"paths"',
    )
    answer = await _read(_SWAGGER_PATH, body.encode(), "application/json")
    _assert_cleaned(answer, ("made-up-a", "made-up-b"), 3)


async def test_secret_in_the_redirect_query_is_replaced_and_counted() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get("/vco/api/docs").respond(
            302, headers={"Location": "/vco/api/docs/index.html?token=made-up-token"}
        )
        answer = await _call(connector, "/vco/api/docs")
    await connector.aclose()

    assert answer["redirect_path"] == f"/vco/api/docs/index.html?token={REDACTED}"
    assert answer["secrets_replaced"] == 1
    assert "made-up-token" not in json.dumps(answer)


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    [
        (_SWAGGER_PATH, '{"swagger": "2.0",', "application/json"),
        (_SWAGGER_PATH, "swagger: [2.0\n", "application/yaml"),
        (_SWAGGER_PATH, "var login = {};", "text/javascript"),
        (_SWAGGER_PATH, _SWAGGER, "text/plain"),
    ],
    ids=["broken-json", "broken-yaml", "javascript", "plain-text-without-a-file-ending"],
)
async def test_file_that_does_not_parse_or_has_no_known_format_is_refused(
    path: str, body: str, content_type: str
) -> None:
    with pytest.raises(ApiFileFormatError):
        await _read(path, body.encode(), content_type)


async def test_plain_text_with_a_known_file_ending_is_read_by_its_ending() -> None:
    answer = await _read("/vco/api/docs/swagger.json", _SWAGGER.encode(), "text/plain")
    assert answer["secrets_replaced"] == 0
    assert _joined(answer) == _SWAGGER.encode()


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
    body = _one_line_json(1024 * 1024)
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    redaction = _redact_like_the_dispatcher(answer)
    assert redaction.manifest == ()
    assert redaction.redacted == redaction.raw
    context = {"op_id": OP_ID, "operator_sub": _operator().sub, "tenant_id": str(_TENANT)}
    summary, handle = await JsonFluxReducer(store=store).reduce(redaction.redacted, None, context)

    assert handle is not None
    assert handle.fetch_more is not None
    assert handle.fetch_more.drill_in.available is True
    assert summary["sha256"] == answer["sha256"]
    assert summary["parts"] == answer["parts"]
    assert summary["build"] == "22222222"
    # The text parts are the one list in the answer, so the handle holds them as
    # rows: the caller pages them with offset / limit, no path needed.
    texts: list[str] = []
    for offset in range(0, answer["parts"], 2):
        page = await read_result_window(_operator(), handle.handle_id, offset=offset, limit=2)
        texts.extend(row["text"] for row in page["rows"])
    assert len(texts) == answer["parts"]
    assert hashlib.sha256("".join(texts).encode("utf-8")).hexdigest() == answer["sha256"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_op_is_a_registered_typed_read_in_its_own_group() -> None:
    op = API_FILE_OP
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags
    assert callable(getattr(Vra8Connector, op.handler_attr))
    group_text = VRA8_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key]
    assert op.op_id in group_text
    assert "safe" not in group_text.lower()
    assert len(op.summary) <= 120 and "\n" not in op.summary
    assert len(op.description) <= 300


def test_examples_validate_and_name_a_documented_landing_page() -> None:
    schema = API_FILE_OP.parameter_schema
    assert schema["examples"]
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
        assert example["path"] in api_file.DOCUMENTATION_PATHS


async def test_answer_never_carries_the_tokens_or_password() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=_SWAGGER.encode(), headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    text = json.dumps(answer)
    assert "bearer-abc" not in text
    assert "refresh-abc" not in text
    assert "stub-password" not in text
