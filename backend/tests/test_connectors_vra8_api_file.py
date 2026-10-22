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
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vra8 import VRA8_TYPED_OPS, Vra8Connector, api_file
from meho_backplane.connectors.vra8.api_file import (
    OP_ID,
    PART_MAX_BYTES,
    SECRET_SHAPES,
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
    }
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
    page = "<html><body><a href='/iaas/api/swagger'>IaaS</a></body></html>"
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
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": "text/plain"})
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
        mock.get(_SWAGGER_PATH).respond(200, content=b"{}")
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
# Large files: joining the parts gives the exact file
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
    "body",
    [_one_line_json(3 * 1024 * 1024), _many_line_yaml(12_000)],
    ids=["one-line-3mb", "12000-lines"],
)
async def test_joined_parts_have_the_file_sha256(body: bytes) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": "text/plain"})
        answer = await _call(connector, _SWAGGER_PATH)
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
    body = _many_line_yaml(4_000)
    # The file fits the read cap, but not once it is stored as JSON text
    # (line ends are escaped, and the record needs room).
    cap = len(body) + 2_000
    monkeypatch.setattr(api_file, "get_settings", lambda: _settings(max_record_bytes=cap))
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": "text/plain"})
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
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": "text/plain"})
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
        mock.get(_SWAGGER_PATH).respond(200, content=body, headers={"Content-Type": "text/plain"})
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


_SWAGGER_WITH_PASSWORD = _SWAGGER.replace(
    '"swagger": "2.0"', '"swagger": "2.0", "x-note": "password=made-up-pass-value-0123"'
)


async def test_file_whose_text_redaction_would_change_is_refused() -> None:
    body = _SWAGGER_WITH_PASSWORD.encode()
    assert _redact_like_the_dispatcher({"text": _SWAGGER_WITH_PASSWORD}).manifest
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
    assert _SWAGGER_PATH in message
    assert "strip-api-key" in message
    assert "text_parts.0.text" in message
    assert "not returned" in message
    assert "made-up-pass-value" not in message


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


async def test_secret_split_across_the_part_cut_is_refused() -> None:
    body = _json_with_a_secret_across_the_part_cut()
    parts = split_text_parts(body)
    assert parts[0].endswith("pass") and parts[1].startswith("word=")
    # The per-part check alone misses the split label; the whole text trips it.
    per_part = _redact_like_the_dispatcher(
        {"text_parts": [{"index": i, "text": text} for i, text in enumerate(parts)]}
    )
    assert per_part.manifest == () and per_part.redacted == per_part.raw
    assert _redact_like_the_dispatcher({"text": body.decode()}).manifest

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
    assert "strip-api-key" in message
    assert "file_text" in message
    assert "not returned" in message
    assert "made-up-pass-value" not in message


# ---------------------------------------------------------------------------
# Secret shapes the stored-result redaction misses
# ---------------------------------------------------------------------------

# Built from two strings so the repository's private-key scan does not see a key.
_PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
_PEM_BLOCK = f"{_PEM_HEADER}\nMIIEmadeupmadeupmadeup\n-----END RSA PRIVATE KEY-----\n"


def _swagger_with(field: str, value: str) -> str:
    return _SWAGGER.replace('"swagger": "2.0"', f'"swagger": "2.0", "{field}": "{value}"')


_SWAGGER_YAML = "swagger: '2.0'\ninfo:\n  title: Example IaaS API\n  version: 2021-07-15\n"


def _swagger_yaml_with(line: str) -> str:
    return _SWAGGER_YAML + line + "\npaths: {}\n"


@pytest.mark.parametrize(
    ("body", "shape"),
    [
        (_swagger_with("password", "made-up-pass-value-0123"), "a credential field"),
        (_swagger_with("token", "made-up-token-value-0123"), "a credential field"),
        (_swagger_with("refreshToken", "made-up-token-value-0123"), "a credential field"),
        (_swagger_with("password", "it's-made-up"), "a credential field"),
        (_swagger_with("password", "abc"), "a credential field"),
        (_swagger_with("pwd", "a"), "a credential field"),
        (_swagger_with("licenseKey", "MADE-UP-0000-0000"), "a credential field"),
        (_swagger_yaml_with("licenseKey: MADE-UP-0000-0000"), "a credential field"),
        (_swagger_yaml_with("  password: abc"), "a credential field"),
        (_swagger_yaml_with("- token: 'it''s-made-up'"), "a credential field"),
        (_PEM_BLOCK, "a private key block"),
    ],
    ids=[
        "json-password",
        "json-token",
        "json-refresh-token",
        "json-password-with-apostrophe",
        "json-short-password",
        "json-one-character-pwd",
        "json-license-key",
        "yaml-license-key",
        "yaml-short-password",
        "yaml-list-token-with-apostrophe",
        "private-key",
    ],
)
async def test_secret_shape_the_redaction_misses_is_refused(body: str, shape: str) -> None:
    # The stored-result redaction leaves these shapes as they are; that is why
    # the read looks for them itself.
    redaction = _redact_like_the_dispatcher({"text": body})
    assert redaction.manifest == () and redaction.redacted == redaction.raw

    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_SWAGGER_PATH).respond(
            200, content=body.encode(), headers={"Content-Type": "application/json"}
        )
        with pytest.raises(Vra8ApiFileError) as excinfo:
            await _call(connector, _SWAGGER_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert _SWAGGER_PATH in message
    assert shape in message
    assert "in 1 place(s)" in message
    assert "not returned" in message
    assert "made-up-" not in message and "MIIE" not in message


_YAML_SCHEMA_WITH_PASSWORD = _SWAGGER_YAML + (
    "definitions:\n"
    "  Login:\n"
    "    properties:\n"
    "      password:\n"
    "        type: string\n"
    "        format: password\n"
    "      token: {type: string, example: ''}\n"
    "    required:\n"
    "      - username\n"
    "      - password\n"
    "securityDefinitions:\n"
    "  oauth:\n"
    "    flow: password\n"
    "    tokenUrl: https://example.invalid/token\n"
    "    description: 'Send the token: in the Authorization header'\n"
    "paths: {}\n"
)


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    [
        (
            _SWAGGER_PATH,
            _SWAGGER.replace(
                '"paths"',
                '"definitions": {"Login": {"properties": {"password": {"type": "string"}, '
                '"token": {"type": "string", "example": ""}}, '
                '"required": ["username", "password"]}}, '
                '"securityDefinitions": {"oauth": {"flow": "password", '
                '"tokenUrl": "https://example.invalid/token"}}, '
                '"x-example": {"username": "u", "password": null, "pwd": false}, "paths"',
            ),
            "application/json",
        ),
        (f"{_SWAGGER_PATH}/swagger.yaml", _YAML_SCHEMA_WITH_PASSWORD, "application/yaml"),
    ],
    ids=["json", "yaml"],
)
async def test_schema_fields_named_like_a_secret_are_not_a_secret(
    path: str, body: str, content_type: str
) -> None:
    """A schema names ``password`` as a property, in a required list and as an OAuth flow.

    None of these is a credential value: the property is an object (or a
    mapping on the next lines), the list holds the name only, ``flow`` and
    ``format`` are the keys, ``null`` and ``false`` are no secret, and a
    label inside a description is running text. The file comes back whole.
    """
    assert not any(pattern.search(body) for _, pattern in SECRET_SHAPES)
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_login_and_version(mock)
        mock.get(path).respond(200, content=body.encode(), headers={"Content-Type": content_type})
        answer = await _call(connector, path)
    await connector.aclose()
    assert _joined(answer) == body.encode()


def _json_with_text_across_the_part_cut(text: str, before_cut: int) -> bytes:
    """Valid one-line JSON where *text* starts *before_cut* bytes before the first part cut.

    All characters are ASCII, so the cut falls exactly at ``PART_MAX_BYTES``.
    """
    head = '{"pad":"'
    joint = '","note":"'
    before = head + "a" * (PART_MAX_BYTES - before_cut - len(head) - len(joint)) + joint
    assert len(before) == PART_MAX_BYTES - before_cut
    return (before + text + '"}').encode()


def _yaml_with_text_across_the_part_cut(text: str, before_cut: int) -> bytes:
    """YAML where the line *text* starts *before_cut* bytes before the first part cut."""
    head = "pad: "
    before = head + "a" * (PART_MAX_BYTES - before_cut - len(head) - 1) + "\n"
    assert len(before) == PART_MAX_BYTES - before_cut
    return (before + text + "\n").encode()


@pytest.mark.parametrize(
    ("text", "before_cut", "shape", "yaml"),
    [
        ('", "password": "made-up-pass-value-0123', 8, "a credential field", False),
        ('", "password": "it\'s-made-up', 8, "a credential field", False),
        ('", "password": "it\'s-made-up', 18, "a credential field", False),
        ('", "pwd": "abc', 6, "a credential field", False),
        ('", "licenseKey": "MADE-UP-0000-0000', 10, "a credential field", False),
        ("licenseKey: MADE-UP-0000-0000", 7, "a credential field", True),
        ("  password: abc", 5, "a credential field", True),
        (_PEM_HEADER, 14, "a private key block", False),
    ],
    ids=[
        "json-password",
        "json-password-with-apostrophe-cut-in-the-key",
        "json-password-with-apostrophe-cut-in-the-value",
        "json-short-pwd",
        "json-license-key",
        "yaml-license-key",
        "yaml-short-password",
        "private-key-header",
    ],
)
async def test_secret_shape_split_across_the_part_cut_is_refused(
    text: str, before_cut: int, shape: str, yaml: bool
) -> None:
    body = (
        _yaml_with_text_across_the_part_cut(text, before_cut)
        if yaml
        else _json_with_text_across_the_part_cut(text, before_cut)
    )
    parts = split_text_parts(body)
    assert parts[0].endswith(text[:before_cut]) and parts[1].startswith(text[before_cut:])
    # No part alone holds the shape; the whole text does.
    assert not any(pattern.search(part) for part in parts for _, pattern in SECRET_SHAPES)
    assert any(pattern.search(body.decode()) for _, pattern in SECRET_SHAPES)

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
    assert shape in message
    assert "not returned" in message
    assert "made-up-" not in message


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
