# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vrops.api_file.get`` typed read.

The read gets one documentation page or API description file from a VCF
Operations or Aria Operations appliance with one ``GET`` on the target's
own login. HTTP is mocked with ``respx``, like
``test_connectors_vcf_operations_typed_reads.py``. All file contents are
made up here; the large files are built by helpers, not stored in the repo.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import msgspec
import pytest
import respx

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_operations import VcfOperationsConnector
from meho_backplane.connectors.vcf_operations import typed_ops_api_file as api_file
from meho_backplane.connectors.vcf_operations.typed_ops import (
    VROPS_TYPED_OPS,
    VROPS_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vcf_operations.typed_ops_api_file import (
    API_FILE_GROUP_KEY,
    PART_MAX_BYTES,
    VROPS_API_FILE_GET_OP,
    VropsApiFileError,
    split_text_parts,
)
from meho_backplane.connectors.vcf_operations.typed_ops_api_file_secrets import (
    find_secret_shape,
)
from meho_backplane.connectors.vrops8 import Vrops8Connector
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.redaction import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings
from tests.test_vmware_operation_contracts import OperationContract, check_operation

_HOST = "vrops-api-file.test.invalid"
_BASE = f"https://{_HOST}"
_PAGE_PATH = "/suite-api/doc/swagger-ui.html"
_FILE_PATH = "/suite-api/doc/openapi/v3/public-api.json"
_TENANT = UUID("00000000-0000-0000-0000-00000000b6b6")

_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_VERSION_PATH = "/suite-api/api/versions/current"
_TOKEN = "ops-token-abc"

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Swagger UI</title>
<script src="./swagger-ui-bundle.js"></script></head>
<body><div id="swagger-ui"></div>
<script>window.onload = function() { window.ui = SwaggerUIBundle({
  configUrl: "./swagger-configs", dom_id: "#swagger-ui" }); };</script>
</body>
</html>
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
    name: str = "ops-api-file"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "vrops/ops-api-file"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    auth_source: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="op-api-file",
        name="API File Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-test", "password": "stub-password"}


def _connector(
    cls: type[VcfOperationsConnector] = VcfOperationsConnector,
    credentials_loader: Any = _stub_loader,
) -> VcfOperationsConnector:
    return cls(credentials_loader=credentials_loader)


def _mock_session_and_version(mock: respx.MockRouter, *, version_status: int = 200) -> None:
    mock.post(_ACQUIRE_PATH).respond(
        200, json={"token": _TOKEN, "validity": 1, "expiresAt": "later", "roles": []}
    )
    if version_status == 200:
        mock.get(_VERSION_PATH).respond(
            200, json={"releaseName": "9.1.1.24000000", "buildNumber": 24000000}
        )
    else:
        mock.get(_VERSION_PATH).respond(version_status)


async def _call(connector: VcfOperationsConnector, path: str) -> dict[str, Any]:
    return await connector.api_file_get(_operator(), _Target(), {"path": path})


def _joined(answer: dict[str, Any]) -> bytes:
    parts = sorted(answer["text_parts"], key=lambda part: part["index"])
    return "".join(part["text"] for part in parts).encode("utf-8")


def _redact_like_the_dispatcher(answer: dict[str, Any]) -> Any:
    """Run the stored-result redaction with the labels the dispatcher uses."""
    return apply_connector_boundary_redaction(
        answer,
        connector_id=f"{VcfOperationsConnector.impl_id}-{VcfOperationsConnector.version}",
        tenant=str(_TENANT),
        op=VROPS_API_FILE_GET_OP.op_id,
    )


def _json_text_size(text: str) -> int:
    """Size of *text* as a JSON string, without the quotes."""
    return len(msgspec.json.encode(text)) - 2


# ---------------------------------------------------------------------------
# Accepted path
# ---------------------------------------------------------------------------


async def test_reads_the_swagger_page_with_size_hash_version_and_parts() -> None:
    connector = _connector()
    body = _PAGE.encode("utf-8")
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        route = mock.get(_PAGE_PATH).respond(
            200, content=body, headers={"Content-Type": "text/html;charset=UTF-8"}
        )
        answer = await _call(connector, _PAGE_PATH)
    await connector.aclose()

    assert answer["path"] == _PAGE_PATH
    assert answer["status"] == 200
    assert answer["content_type"] == "text/html;charset=UTF-8"
    assert answer["redirect_path"] is None
    assert answer["size"] == len(body)
    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert answer["parts"] == 1
    assert answer["text_parts"] == [{"index": 0, "text": _PAGE}]
    assert answer["version"] == "9.1.1.24000000"
    assert answer["build"] == "24000000"
    assert answer["version_error"] is None
    assert route.calls.last.request.headers["Authorization"] == f"OpsToken {_TOKEN}"


async def test_the_8x_connector_sends_its_own_token_scheme() -> None:
    connector = _connector(Vrops8Connector)
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        route = mock.get(_FILE_PATH).respond(200, json={"openapi": "3.0.1"})
        answer = await _call(connector, _FILE_PATH)
    await connector.aclose()

    assert answer["status"] == 200
    assert answer["parts"] == 1
    assert route.calls.last.request.headers["Authorization"] == f"vRealizeOpsToken {_TOKEN}"


async def test_query_reaches_the_appliance_as_written() -> None:
    connector = _connector()
    path = "/suite-api/doc/v2/api-docs?group=public&apiVersion=1.0"
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        route = mock.get(url__regex=r".*/suite-api/doc/v2/api-docs\?group=public&apiVersion=1\.0$")
        route.respond(200, json={"swagger": "2.0"})
        answer = await _call(connector, path)
    await connector.aclose()

    assert route.call_count == 1
    assert route.calls.last.request.url.raw_path == path.encode()
    assert answer["status"] == 200


# ---------------------------------------------------------------------------
# Refused paths: no request goes out
# ---------------------------------------------------------------------------

_REFUSED_PATHS = [
    "/suite-api/api/versions/current",
    "/suite-api/api/alerts",
    "/suite-api/doc/../api/alerts",
    "/suite-api/doc/..",
    "/suite-api/doc//swagger-ui.html",
    "/suite-api/doc/%2e%2e/api",
    "/suite-api/doc/a%2fb",
    "/suite-api/doc\\swagger-ui.html",
    "https://other.example/suite-api/doc/",
    "//other.example/suite-api/doc/",
    "/suite-api/doc@other.example/",
    "/suite-api",
    "/suite-api/docs/",
    "/api-docs-data/x",
    "/x/swagger.json.bak",
    "/v2/api-docsx",
    "/suite-api/doc/?foo=1",
    "/suite-api/doc/?group=a&x=1",
    "/suite-api/doc/?group=a%20b",
    "/suite-api/doc/?group=",
    "/suite-api/doc/#frag",
    " /suite-api/doc/",
]


@pytest.mark.parametrize("path", _REFUSED_PATHS)
async def test_refused_path_sends_no_request(path: str) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        catch_all = mock.route().respond(200, content=b"<x/>")
        with pytest.raises(VropsApiFileError, match="No request was sent"):
            await _call(connector, path)
        assert mock.calls.call_count == 0
        assert catch_all.call_count == 0
    await connector.aclose()


@pytest.mark.parametrize("path", _REFUSED_PATHS)
def test_refused_path_fails_parameter_validation(path: str) -> None:
    """The dispatcher checks the same rule before the handler runs."""
    errors = validate_params(VROPS_API_FILE_GET_OP.parameter_schema, {"path": path})
    assert errors


@pytest.mark.parametrize(
    "path",
    [
        "/suite-api/",
        "/suite-api/doc/",
        "/suite-api/doc/swagger-ui.html",
        "/suite-api/doc/swagger-configs",
        "/suite-api/doc/v2/api-docs",
        "/suite-api/doc/openapi/v3/internal-api.json",
        "/suite-api/doc/swagger-ui-bundle.js",
        "/v3/api-docs",
        "/v2/api-docs?group=public",
        "/swagger-resources/",
        "/swagger-resources/configuration/ui",
        "/x/swagger-config",
        "/openapi.yaml",
        "/x/y/swagger.json",
        "/suite-api/doc/v2/api-docs?group=internal&apiVersion=1.0",
        "/api-docs",
        "/api-docs/",
    ],
)
def test_accepted_paths_pass_parameter_validation(path: str) -> None:
    schema = VROPS_API_FILE_GET_OP.parameter_schema
    assert validate_params(schema, {"path": path}) == []
    assert api_file.check_api_file_path(path) == path


# ---------------------------------------------------------------------------
# Status, redirect, version and login cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_error_status_comes_back_in_the_answer(status: int) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(status, text="denied", headers={"Content-Type": "text/html"})
        answer = await _call(connector, _FILE_PATH)
    await connector.aclose()

    assert answer["status"] == status
    assert answer["size"] is None
    assert answer["sha256"] is None
    assert answer["parts"] == 0
    assert answer["text_parts"] == []
    assert answer["build"] == "24000000"


async def test_redirect_is_not_followed_and_its_path_comes_back() -> None:
    connector = _connector()
    next_path = "/suite-api/doc/swagger-ui/index.html"
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_PAGE_PATH).respond(302, headers={"Location": next_path})
        followed = mock.get(next_path).respond(200, content=b"<html/>")
        answer = await _call(connector, _PAGE_PATH)
    await connector.aclose()

    assert followed.call_count == 0
    assert answer["status"] == 302
    assert answer["redirect_path"] == next_path
    assert answer["parts"] == 0


async def test_relative_redirect_resolves_against_the_page_folder() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get("/suite-api/").respond(301, headers={"Location": "doc/swagger-ui.html"})
        answer = await _call(connector, "/suite-api/")
    await connector.aclose()

    assert answer["redirect_path"] == _PAGE_PATH


@pytest.mark.parametrize("version_status", [500, 403])
async def test_failed_version_read_still_returns_the_file(version_status: int) -> None:
    connector = _connector()
    body = _PAGE.encode()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock, version_status=version_status)
        mock.get(_PAGE_PATH).respond(200, content=body, headers={"Content-Type": "text/html"})
        answer = await _call(connector, _PAGE_PATH)
    await connector.aclose()

    assert answer["sha256"] == hashlib.sha256(body).hexdigest()
    assert answer["version"] is None
    assert answer["build"] is None
    assert _VERSION_PATH in answer["version_error"]


async def test_version_answer_without_a_build_says_so() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _TOKEN})
        mock.get(_VERSION_PATH).respond(200, json={"releaseName": "9.0.0"})
        mock.get(_PAGE_PATH).respond(200, content=_PAGE.encode())
        answer = await _call(connector, _PAGE_PATH)
    await connector.aclose()

    assert answer["version"] == "9.0.0"
    assert answer["build"] is None
    assert "buildNumber" in answer["version_error"]


async def _no_credentials(_target: Any, _operator: Operator) -> dict[str, str]:
    raise VaultCredentialsReadError("no credentials stored for target 'ops-api-file'")


async def test_without_credentials_the_file_request_never_goes_out() -> None:
    """The file GET carries the target's login, so a login failure stops the read."""
    connector = _connector(credentials_loader=_no_credentials)
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        file_route = mock.get(_PAGE_PATH).respond(200, content=_PAGE.encode())
        with pytest.raises(VaultCredentialsReadError):
            await _call(connector, _PAGE_PATH)
        assert file_route.call_count == 0
    await connector.aclose()


# ---------------------------------------------------------------------------
# Large files: joining the parts gives the exact file
# ---------------------------------------------------------------------------


def _one_line_json(size: int) -> bytes:
    """A made-up one-line (minified) JSON API file of about *size* bytes."""
    item = {"name": "entry", "note": "café ✓", "type": "string"}
    count = size // len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    document = {"openapi": "3.0.1", "items": [item] * count}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _many_line_json(lines: int) -> bytes:
    rows = ",\n".join(f'    {{"n": {n}, "value": "value {n}"}}' for n in range(lines))
    return f'{{\n  "openapi": "3.0.1",\n  "items": [\n{rows}\n  ]\n}}\n'.encode()


@pytest.mark.parametrize(
    "body",
    [_one_line_json(3 * 1024 * 1024), _many_line_json(12_000)],
    ids=["one-line-3mb", "12000-lines"],
)
async def test_joined_parts_have_the_file_sha256(body: bytes) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(
            200, content=body, headers={"Content-Type": "application/json"}
        )
        answer = await _call(connector, _FILE_PATH)
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


@pytest.mark.parametrize(
    "text",
    [
        "\x0b" * (3 * PART_MAX_BYTES),
        '"' * 200_000 + "é" * 3_000,
        ("a" * 5_000 + "\x01" * 5_000 + "\U0001f600" * 3_000 + '\\"' * 2_000) * 7,
        "x" * (5 * PART_MAX_BYTES),
    ],
    ids=["vertical-tabs", "quotes", "mixed-escapes", "plain"],
)
def test_split_bounds_each_part_by_its_json_size(text: str) -> None:
    """A part full of escaped characters still fits one result_query answer."""
    data = text.encode("utf-8")
    parts = split_text_parts(data)
    assert "".join(parts).encode("utf-8") == data
    assert all(_json_text_size(part) <= PART_MAX_BYTES for part in parts)
    assert all(len(part.encode("utf-8")) <= PART_MAX_BYTES for part in parts)
    # The parts are as full as the bound allows: every part but the last
    # is within one escape of the bound.
    assert all(_json_text_size(part) > PART_MAX_BYTES - 6 for part in parts[:-1])


def _settings_with(**caps: int) -> Any:
    values = {
        "result_handle_max_record_bytes": 16 * 1024 * 1024,
        "result_handle_max_spill_rows": 10_000,
    }
    values.update(caps)
    return lambda: SimpleNamespace(**values)


async def test_file_too_big_for_one_result_stops_with_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _many_line_json(4_000)
    # The file fits the read cap, but not once it is stored as JSON text
    # (quotes and line ends are escaped, and the record needs room).
    cap = len(body) + 2_000
    monkeypatch.setattr(
        api_file, "get_settings", _settings_with(result_handle_max_record_bytes=cap)
    )
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body)
        with pytest.raises(VropsApiFileError, match=f"is {len(body)} bytes"):
            await _call(connector, _FILE_PATH)
    await connector.aclose()


async def test_file_with_more_parts_than_one_result_keeps_stops_with_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _one_line_json(300 * 1024)
    monkeypatch.setattr(api_file, "get_settings", _settings_with(result_handle_max_spill_rows=2))
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body)
        with pytest.raises(VropsApiFileError, match=f"is {len(body)} bytes .* at most 2 parts"):
            await _call(connector, _FILE_PATH)
    await connector.aclose()


async def test_file_over_the_byte_cap_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api_file, "get_settings", _settings_with(result_handle_max_record_bytes=10_000)
    )
    body = _many_line_json(2_000)
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body)
        with pytest.raises(VropsApiFileError, match=f"{len(body)} bytes"):
            await _call(connector, _FILE_PATH)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Text only
# ---------------------------------------------------------------------------


async def test_non_text_content_is_refused_with_its_content_type() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get("/suite-api/doc/favicon.png").respond(
            200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
        )
        with pytest.raises(VropsApiFileError, match="image/png"):
            await _call(connector, "/suite-api/doc/favicon.png")
    await connector.aclose()


async def test_non_utf8_text_is_refused() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(
            200, content=b'{"a": "\xff\xfe"}', headers={"Content-Type": "application/json"}
        )
        with pytest.raises(VropsApiFileError, match="not UTF-8"):
            await _call(connector, _FILE_PATH)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Redaction keeps the file whole
# ---------------------------------------------------------------------------

_FILE_WITH_A_LABELLED_SECRET = (
    '{"openapi":"3.0.1","info":{"description":"Use password: made-up-pass-value-0123 to log in"}}'
)


async def test_file_whose_text_redaction_would_change_is_refused() -> None:
    body = _FILE_WITH_A_LABELLED_SECRET.encode()
    assert _redact_like_the_dispatcher({"text": _FILE_WITH_A_LABELLED_SECRET}).manifest
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body)
        with pytest.raises(VropsApiFileError) as excinfo:
            await _call(connector, _FILE_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert _FILE_PATH in message
    assert "strip-api-key" in message
    assert "text_parts.0.text" in message
    assert "not returned" in message
    assert "made-up-pass-value" not in message


def _json_with_a_secret_across_the_part_cut(tail: str) -> bytes:
    """Valid one-line JSON whose secret label straddles the first part cut.

    The padding is sized by JSON text, the measure the split uses, so the
    first part ends exactly with ``pass`` and the second starts with
    ``word`` and the rest of *tail*.
    """
    head = '{"pad":"'
    joint = '","note":"'
    padding = "a" * (PART_MAX_BYTES - _json_text_size(head + joint) - len("pass"))
    return (head + padding + joint + "pass" + tail).encode()


async def test_secret_split_across_the_part_cut_is_refused() -> None:
    body = _json_with_a_secret_across_the_part_cut('word=made-up-pass-value-0123"}')
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
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body)
        with pytest.raises(VropsApiFileError) as excinfo:
            await _call(connector, _FILE_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert "strip-api-key" in message
    assert "file_text" in message
    assert "not returned" in message
    assert "made-up-pass-value" not in message


# ---------------------------------------------------------------------------
# Secret shapes the stored-result redaction does not see
# ---------------------------------------------------------------------------

# Built in pieces so that the test file itself holds no key header.
_PEM_HEADER = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"

_SECRET_SHAPED_FILES = {
    "quoted-json-password": (
        '{"openapi":"3.0.1","x-login":{"password": "made-up-pass-value-0123"}}',
        "a quoted password or token field",
    ),
    "quoted-json-token": (
        '{"openapi":"3.0.1","x-session":{"token":"made-up-token-value-0123"}}',
        "a quoted password or token field",
    ),
    "quoted-json-prefixed-label": (
        '{"openapi":"3.0.1","x-login":{"x-auth-token": "made-up-token-value-0123"}}',
        "a quoted password or token field",
    ),
    "single-quoted-yaml": (
        "openapi: 3.0.1\nx-login:\n  'password': 'made-up-pass-value-0123'\n",
        "a quoted password or token field",
    ),
    "quoted-authorization": (
        '{"openapi":"3.0.1","x-headers":{"Authorization": "Basic bWFkZS11cDpwYXNz"}}',
        "a quoted Authorization field",
    ),
    "pem-private-key": (
        "openapi: 3.0.1\nx-key: |\n  " + _PEM_HEADER + "\n  MADEUPMADEUPMADEUP\n",
        "a PEM private key",
    ),
}


@pytest.mark.parametrize(
    ("text", "shape"), list(_SECRET_SHAPED_FILES.values()), ids=list(_SECRET_SHAPED_FILES)
)
async def test_file_with_a_secret_shape_is_refused(text: str, shape: str) -> None:
    # The stored-result redaction leaves these forms as they are; the
    # read's own check must refuse them.
    redaction = _redact_like_the_dispatcher({"text": text})
    assert redaction.manifest == () and redaction.redacted == redaction.raw

    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=text.encode())
        with pytest.raises(VropsApiFileError) as excinfo:
            await _call(connector, _FILE_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert _FILE_PATH in message
    assert shape in message
    assert "not returned" in message
    assert "made-up-" not in message and "MADEUP" not in message and "bWFkZS" not in message


@pytest.mark.parametrize(
    "text",
    [
        '{"properties":{"password":{"type":"string","format":"password"}}}',
        '{"password": "string", "token": "string", "max_tokens": 12345678901}',
        '{"type": "apiKey", "name": "Authorization", "in": "header"}',
        '{"passwordPolicy": {"minimum": "eight-or-more"}}',
    ],
    ids=["schema-property", "short-stubs", "security-scheme", "label-prefix-of-a-name"],
)
def test_ordinary_api_description_text_is_not_a_secret_shape(text: str) -> None:
    assert find_secret_shape(text) is None


async def test_quoted_secret_split_across_the_part_cut_is_refused() -> None:
    body = _json_with_a_secret_across_the_part_cut('word": "made-up-pass-value-0123"}')
    parts = split_text_parts(body)
    assert parts[0].endswith('"pass') and parts[1].startswith('word": ')
    # Neither part alone shows the shape; the whole text does.
    assert all(find_secret_shape(part) is None for part in parts)
    assert find_secret_shape(body.decode()) is not None
    assert _redact_like_the_dispatcher({"text": body.decode()}).manifest == ()

    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body)
        with pytest.raises(VropsApiFileError) as excinfo:
            await _call(connector, _FILE_PATH)
    await connector.aclose()

    message = str(excinfo.value)
    assert "a quoted password or token field" in message
    assert "not returned" in message
    assert "made-up-pass-value" not in message


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


@pytest.mark.parametrize(
    ("body", "content_type", "limit"),
    [
        (_one_line_json(1024 * 1024), "application/json", 2),
        # Every vertical tab is six bytes as JSON text. The parts are bounded
        # by that size, so each row still fits the result_query byte budget.
        (("\x0b" * (3 * PART_MAX_BYTES)).encode(), "text/plain", 1),
    ],
    ids=["one-line-json", "vertical-tabs"],
)
async def test_large_answer_is_read_back_part_by_part_through_result_query(
    monkeypatch: pytest.MonkeyPatch, body: bytes, content_type: str, limit: int
) -> None:
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_FILE_PATH).respond(200, content=body, headers={"Content-Type": content_type})
        answer = await _call(connector, _FILE_PATH)
    await connector.aclose()
    assert all(_json_text_size(part["text"]) <= PART_MAX_BYTES for part in answer["text_parts"])

    redaction = _redact_like_the_dispatcher(answer)
    assert redaction.manifest == ()
    assert redaction.redacted == redaction.raw
    context = {
        "op_id": VROPS_API_FILE_GET_OP.op_id,
        "operator_sub": _operator().sub,
        "tenant_id": str(_TENANT),
    }
    summary, handle = await JsonFluxReducer(store=store).reduce(redaction.redacted, None, context)

    assert handle is not None
    assert handle.total_rows == answer["parts"]
    assert summary["sha256"] == answer["sha256"]
    assert summary["parts"] == answer["parts"]
    assert summary["build"] == "24000000"
    texts: list[str] = []
    for offset in range(0, answer["parts"], limit):
        page = await read_result_window(_operator(), handle.handle_id, offset=offset, limit=limit)
        texts.extend(row["text"] for row in page["rows"])
    assert len(texts) == answer["parts"]
    assert hashlib.sha256("".join(texts).encode("utf-8")).hexdigest() == answer["sha256"]


# ---------------------------------------------------------------------------
# Registration and contract
# ---------------------------------------------------------------------------


def test_op_is_a_registered_typed_read_on_both_vrops_catalogs() -> None:
    op = VROPS_API_FILE_GET_OP
    assert op in VROPS_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert callable(getattr(VcfOperationsConnector, op.handler_attr))
    assert callable(getattr(Vrops8Connector, op.handler_attr))
    group_text = VROPS_TYPED_WHEN_TO_USE_BY_GROUP[API_FILE_GROUP_KEY]
    assert op.op_id in group_text
    assert "safe" not in group_text.lower()


def test_examples_validate_and_use_placeholders() -> None:
    schema = VROPS_API_FILE_GET_OP.parameter_schema
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
        assert "FILE" in example["path"]


def test_op_passes_every_operation_contract_rule() -> None:
    op = VROPS_API_FILE_GET_OP
    contract = OperationContract(
        connector="vrops-rest",
        op_id=op.op_id,
        summary=op.summary,
        description=op.description,
        parameter_schema=op.parameter_schema,
        response_schema=op.response_schema,
        llm_instructions=op.llm_instructions,
        safety_level=op.safety_level,
        requires_approval=op.requires_approval,
        group_text=VROPS_TYPED_WHEN_TO_USE_BY_GROUP[API_FILE_GROUP_KEY],
    )
    assert check_operation(contract) == {}


async def test_answer_never_carries_the_session_token_or_password() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        _mock_session_and_version(mock)
        mock.get(_PAGE_PATH).respond(200, content=_PAGE.encode())
        answer = await _call(connector, _PAGE_PATH)
    await connector.aclose()

    text = json.dumps(answer)
    assert _TOKEN not in text
    assert "stub-password" not in text
