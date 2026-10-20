# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX connector and its ``hcx.api_file.get`` read.

What is covered, with a respx-mocked HCX Manager and no ingested catalog:

* the login (``POST /hybridity/api/sessions`` -> ``x-hm-authorization``)
  and the token on the read;
* an accepted path: status, content type, size, SHA-256, version, parts;
* refused paths: no HTTP request goes out;
* a 404 reaches the answer; a redirect is not followed;
* a 401 makes a new login and the read then succeeds, through ``dispatch()``;
* a one-line minified JSON file of a few MB and a file of more than
  10,000 lines: the joined parts give the exact file (same SHA-256);
* a file too big for one result handle stops with an error naming its size;
* content that is not text is refused with its content type;
* no password or token in the result; registration-shape invariants.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.hcx import (
    HCX_CONNECTOR_ID,
    HCX_IMPL_ID,
    HCX_PRODUCT,
    HCX_TYPED_OPS,
    HCX_VERSION,
    HcxConnector,
    register_hcx_typed_operations,
)
from meho_backplane.connectors.hcx.api_file import (
    PART_BYTES,
    HcxApiFilePathError,
    check_path,
    split_parts,
)
from meho_backplane.connectors.hcx.connector import parse_manager_version
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_HOST = "hcx-typed.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSIONS = "/hybridity/api/sessions"
_APPLIANCES = "/hybridity/api/hcspAppliances"
_DOCS = "/hybridity/docs"
_TOKEN = "typed-hm-token-1"
_PASSWORD = "typed-password-value"
_LOGIN_HEADERS = {"x-hm-authorization": _TOKEN}
_APPLIANCES_PAYLOAD: dict[str, Any] = {
    "items": [{"systemName": "hcx-mgr", "systemType": "HCX-Manager", "version": "9.1.0.0.12345678"}]
}
_HTML = "<html><head><title>HCX API</title></head><body>docs</body></html>"


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


class _HcxTarget:
    """Target satisfying both ``HcxTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = HCX_PRODUCT
        self.fingerprint = type("_FP", (), {"version": HCX_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
        self.name = "hcx-typed"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/hcx-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-hcx",
        name="HCX Reads Operator",
        email=None,
        raw_jwt="op.reads.hcx.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "hcx-typed-svc", "password": _PASSWORD}


async def _register_and_resolve(_stub_embedding: AsyncMock) -> HcxConnector:
    await register_hcx_typed_operations()
    instance = get_or_create_connector_instance(HcxConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    return instance


def _mock_login_and_version(mock: respx.MockRouter) -> None:
    mock.post(_SESSIONS).respond(200, headers=_LOGIN_HEADERS)
    mock.post(_APPLIANCES).respond(200, json=_APPLIANCES_PAYLOAD)


async def _dispatch(path: str) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=HCX_CONNECTOR_ID,
        op_id="hcx.api_file.get",
        target=_HcxTarget(),
        params={"path": path},
    )


# ---------------------------------------------------------------------------
# Login + an accepted path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_path_returns_status_size_sha_version_and_parts(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        route = mock.get(_DOCS).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_DOCS)

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert route.calls[0].request.headers["x-hm-authorization"] == _TOKEN
    body = result.result
    assert body["path"] == _DOCS
    assert body["status"] == 200
    assert body["content_type"] == "text/html"
    assert body["size"] == len(_HTML.encode())
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] == "9.1.0.0.12345678"
    assert body["build"] == "12345678"
    assert body["version_error"] is None
    assert body["redirect_to"] is None
    assert body["parts"] == 1
    assert body["items"] == [{"index": 0, "text": _HTML}]


@pytest.mark.asyncio
async def test_query_string_with_allowed_keys_is_sent(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        route = mock.get("/hybridity/v2/api-docs").respond(200, json={"swagger": "2.0"})
        result = await _dispatch("/hybridity/v2/api-docs?group=hybridity&apiVersion=1")

    assert result.status == "ok", result.error
    sent = route.calls[0].request.url
    assert sent.params["group"] == "hybridity"
    assert sent.params["apiVersion"] == "1"
    assert result.result["path"] == "/hybridity/v2/api-docs?group=hybridity&apiVersion=1"


# ---------------------------------------------------------------------------
# Refused paths: no HTTP request goes out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../hybridity/docs",
        "/hybridity/docs/../api/sessions",
        "//other-host.test.invalid/hybridity/docs",
        "https://other-host.test.invalid/hybridity/docs",
        "other-host.test.invalid/hybridity/docs",
        "/hybridity/docs%2f..%2fapi",
        "/hybridity/docs/%2e%2e/api",
        "/hybridity/docs\\x",
        "/hybridity/docs#frag",
        "/hybridity/docs?foo=1",
        "/hybridity/docs?group=a&token=b",
        "/hybridity/api/sessions",
        "/hybridity/api/cloudConfigs",
        "/hybridity/docsx",
        "/hybridity/api/data.json",
        "",
    ],
)
@pytest.mark.asyncio
async def test_refused_path_sends_no_http_request(
    _stub_embedding: AsyncMock, session: AsyncSession, path: str
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.route().respond(200, text="never")
        result = await _dispatch(path)
        assert not mock.calls, f"path {path!r} reached the appliance"

    assert result.status == "error"
    assert result.error


def test_check_path_accepts_the_documented_shapes() -> None:
    assert check_path("/hybridity/docs") == ("/hybridity/docs", "")
    assert check_path("/hybridity/docs/swagger-ui.html") == ("/hybridity/docs/swagger-ui.html", "")
    assert check_path("/hybridity/v2/api-docs?group=x") == ("/hybridity/v2/api-docs", "group=x")
    assert check_path("/hybridity/swagger-resources") == ("/hybridity/swagger-resources", "")
    assert check_path("/hybridity/swagger-config") == ("/hybridity/swagger-config", "")
    assert check_path("/docs/spec/openapi.yaml") == ("/docs/spec/openapi.yaml", "")
    with pytest.raises(HcxApiFilePathError):
        check_path("/hybridity/docs?group=a&group=b&other=c")


# ---------------------------------------------------------------------------
# Status, redirect, 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_status_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/missing.json").respond(404, text="not found")
        result = await _dispatch("/hybridity/docs/missing.json")

    assert result.status == "ok", result.error
    assert result.result["status"] == 404
    assert result.result["items"] == [{"index": 0, "text": "not found"}]


@pytest.mark.asyncio
async def test_redirect_is_reported_and_not_followed(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        first = mock.get(_DOCS).respond(301, headers={"Location": f"{_BASE_URL}{_DOCS}/"})
        second = mock.get(f"{_DOCS}/").respond(200, text=_HTML)
        result = await _dispatch(_DOCS)

    assert result.status == "ok", result.error
    assert first.call_count == 1
    assert second.call_count == 0
    assert result.result["status"] == 301
    assert result.result["redirect_to"] == f"{_DOCS}/"


@pytest.mark.asyncio
async def test_401_makes_a_new_login_and_the_read_then_succeeds(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_SESSIONS)
        login.side_effect = [
            httpx.Response(200, headers={"x-hm-authorization": "typed-hm-token-1"}),
            httpx.Response(200, headers={"x-hm-authorization": "typed-hm-token-2"}),
        ]
        mock.post(_APPLIANCES).respond(200, json=_APPLIANCES_PAYLOAD)
        docs = mock.get(_DOCS)
        docs.side_effect = [httpx.Response(401), httpx.Response(200, text=_HTML)]
        result = await _dispatch(_DOCS)

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert docs.call_count == 2
    assert docs.calls[0].request.headers["x-hm-authorization"] == "typed-hm-token-1"
    assert docs.calls[1].request.headers["x-hm-authorization"] == "typed-hm-token-2"
    assert result.result["status"] == 200


@pytest.mark.asyncio
async def test_login_401_surfaces_connector_auth_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSIONS).respond(401, json={"error": "invalid_credentials"})
        docs = mock.get(_DOCS).respond(200, text=_HTML)
        result = await _dispatch(_DOCS)

    assert result.status == "error"
    assert docs.call_count == 0
    assert "401" in (result.error or "")


# ---------------------------------------------------------------------------
# Big files: the joined parts give the exact file
# ---------------------------------------------------------------------------


def _minified_json_of_a_few_mb() -> bytes:
    rows = [
        {"path": f"/hybridity/api/resource-{i}", "summary": "made-up summary " * 8, "n": i}
        for i in range(12_000)
    ]
    data = json.dumps({"paths": rows}, separators=(",", ":")).encode()
    assert b"\n" not in data and len(data) > 2 * 1024 * 1024
    return data


def _text_with_more_than_10000_lines() -> bytes:
    return "".join(f"line {i}: Ünïcödé text for a long file\n" for i in range(12_345)).encode()


@pytest.mark.parametrize(
    ("content_type", "data"),
    [
        ("application/json", _minified_json_of_a_few_mb()),
        ("text/plain; charset=utf-8", _text_with_more_than_10000_lines()),
    ],
)
@pytest.mark.asyncio
async def test_joined_parts_give_the_exact_file(
    _stub_embedding: AsyncMock, session: AsyncSession, content_type: str, data: bytes
) -> None:
    instance = await _register_and_resolve(_stub_embedding)
    target = _HcxTarget()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/big.json").respond(
            200, content=data, headers={"Content-Type": content_type}
        )
        answer = await instance.api_file_get(
            _make_operator(), target, {"path": "/hybridity/docs/big.json"}
        )

    assert answer["size"] == len(data)
    assert answer["parts"] == len(answer["items"]) > 1
    assert [item["index"] for item in answer["items"]] == list(range(answer["parts"]))
    assert all(len(item["text"].encode()) <= PART_BYTES for item in answer["items"])
    joined = "".join(item["text"] for item in answer["items"]).encode()
    assert joined == data
    assert hashlib.sha256(joined).hexdigest() == answer["sha256"]


@pytest.mark.asyncio
async def test_big_file_dispatches_behind_a_result_handle_with_scalars_inline(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """With the production reducer, the parts go behind a handle and the scalars stay inline."""
    data = _minified_json_of_a_few_mb()
    await _register_and_resolve(_stub_embedding)
    set_default_reducer(JsonFluxReducer())
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/big.json").respond(
            200, content=data, headers={"Content-Type": "application/json"}
        )
        result = await _dispatch("/hybridity/docs/big.json")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["sha256"] == hashlib.sha256(data).hexdigest()
    assert summary["size"] == len(data)
    assert summary["parts"] == summary["row_count"]
    assert summary["version"] == "9.1.0.0.12345678"


def test_split_parts_keeps_characters_whole_and_round_trips() -> None:
    data = ("é" * 40_000 + "a" + "€" * 30_000).encode()
    parts = split_parts(data)
    assert b"".join(parts) == data
    assert all(len(part) <= PART_BYTES for part in parts)
    for part in parts:
        part.decode("utf-8")
    assert split_parts(b"") == []


# ---------------------------------------------------------------------------
# Too big, not text, version failure
# ---------------------------------------------------------------------------


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    """Serve *data* in two chunks with no Content-Length, like a chunked answer."""
    yield data[:3000]
    yield data[3000:]


@pytest.mark.asyncio
async def test_file_too_big_for_one_result_handle_stops_with_its_size(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "2048")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    data = b"x" * 5000
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/declared.json").respond(200, content=data)
        mock.get("/hybridity/docs/chunked.json").respond(200, content=_chunks(data))
        declared = await _dispatch("/hybridity/docs/declared.json")
        chunked = await _dispatch("/hybridity/docs/chunked.json")

    for result in (declared, chunked):
        assert result.status == "error"
        assert "does not fit in one result handle" in result.extras["exception_message"]
    assert "5000" in declared.extras["exception_message"]
    assert "2048" in chunked.extras["exception_message"]


@pytest.mark.asyncio
async def test_parts_that_do_not_fit_as_rows_stop_with_the_size(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body fits under the byte cap, but the rows do not (escaped quotes grow it)."""
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "1000")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    data = b'"' * 990
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/quotes.json").respond(200, content=data)
        result = await _dispatch("/hybridity/docs/quotes.json")

    assert result.status == "error"
    assert "file of 990 bytes" in result.extras["exception_message"]


@pytest.mark.asyncio
async def test_non_text_content_is_refused_naming_the_content_type(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/logo.openapi.json").respond(
            200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"}
        )
        mock.get("/hybridity/docs/bad.json").respond(
            200, content=b"\xff\xfe\x00bad", headers={"Content-Type": "text/plain"}
        )
        png = await _dispatch("/hybridity/docs/logo.openapi.json")
        bad = await _dispatch("/hybridity/docs/bad.json")

    assert png.status == "error" and "image/png" in png.extras["exception_message"]
    assert bad.status == "error" and "text/plain" in bad.extras["exception_message"]


@pytest.mark.asyncio
async def test_version_read_failure_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSIONS).respond(200, headers=_LOGIN_HEADERS)
        mock.post(_APPLIANCES).respond(500, text="boom")
        mock.get(_DOCS).respond(200, text=_HTML)
        result = await _dispatch(_DOCS)

    assert result.status == "ok", result.error
    assert result.result["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert result.result["version"] is None
    assert result.result["build"] is None
    assert "500" in result.result["version_error"]


@pytest.mark.parametrize(
    ("payload", "version", "build"),
    [
        ({"items": [{"version": "4.11.0.0.98765432"}]}, "4.11.0.0.98765432", "98765432"),
        ({"data": {"items": [{"version": "9.0.0", "buildNumber": 11}]}}, "9.0.0", "11"),
        ([{"version": "9.1.0"}], "9.1.0", None),
        ({"items": []}, None, None),
    ],
)
def test_parse_manager_version_shapes(payload: Any, version: str | None, build: str | None) -> None:
    read = parse_manager_version(payload)
    assert (read.version, read.build) == (version, build)
    assert (read.error is None) == (version is not None)


# ---------------------------------------------------------------------------
# Secrets, registration shape, fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_holds_no_password_or_token(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get(_DOCS).respond(200, text=_HTML)
        result = await _dispatch(_DOCS)

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert _PASSWORD not in rendered
    assert _TOKEN not in rendered


@pytest.mark.asyncio
async def test_registered_op_is_typed_safe_and_documented(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_hcx_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == HCX_PRODUCT,
                    EndpointDescriptor.impl_id == HCX_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in HCX_TYPED_OPS} == {"hcx.api_file.get"}
    row = rows[0]
    assert row.source_kind == "typed"
    assert row.handler_ref is not None
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert "read-only" in row.tags
    assert row.parameter_schema["additionalProperties"] is False
    assert row.parameter_schema["examples"] == [{"path": "<path>"}]
    assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
    assert "\n" not in row.summary and len(row.description) <= 300


def test_package_registers_versioned_connector_and_wildcard() -> None:
    import meho_backplane.connectors.hcx  # noqa: F401  (import-time registration)

    clear_registry()
    register_connector_v2(
        product=HCX_PRODUCT, version=HCX_VERSION, impl_id=HCX_IMPL_ID, cls=HcxConnector
    )
    register_connector_v2(product=HCX_PRODUCT, version="", impl_id="", cls=HcxConnector)
    registry = all_connectors_v2()
    assert registry[(HCX_PRODUCT, HCX_VERSION, HCX_IMPL_ID)] is HcxConnector
    assert registry[(HCX_PRODUCT, "", "")] is HcxConnector
    assert HcxConnector.supported_version_range == ">=4.7,<10.0"


@pytest.mark.asyncio
async def test_fingerprint_reports_version_and_unreachable_shape() -> None:
    connector = HcxConnector(session_loader=_session_loader)
    target = _HcxTarget()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        fp = await connector.fingerprint(target, _make_operator())
    assert (fp.vendor, fp.product, fp.reachable) == ("vmware", "hcx", True)
    assert (fp.version, fp.build) == ("9.1.0.0.12345678", "12345678")

    connector = HcxConnector(session_loader=_session_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSIONS).respond(401)
        fp = await connector.fingerprint(_HcxTarget(), _make_operator())
        probe = await connector.probe(_HcxTarget())
    assert fp.reachable is False
    assert "401" in str(fp.extras["error"])
    assert probe.ok is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_rejects_other_auth_models() -> None:
    connector = HcxConnector(session_loader=_session_loader)
    target = _HcxTarget()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError, match="per_user"):
        await connector.auth_headers(target, _make_operator())
