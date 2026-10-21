# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the HCX connector and its ``hcx.api_file.get`` read.

What is covered, with a respx-mocked HCX Manager and no ingested catalog:

* the login (``POST /hybridity/api/sessions`` -> ``x-hm-authorization``)
  and the token on the read;
* an accepted path: status, content type, size, SHA-256, version, parts;
* refused paths: no HTTP request goes out;
* a 404 reaches the answer; a redirect is not followed, and a relative
  ``Location`` is resolved against the path that was read;
* a 401 makes a new login and the read then succeeds, through ``dispatch()``;
* a one-line minified JSON file of a few MB and a file of more than
  10,000 lines: the joined parts give the exact file (same SHA-256);
* a file too big for one result handle stops with an error naming its size,
  also when the body is compressed and only the decoded file is too big;
  a streamed gzip or deflate body that would decode to many MiB is
  refused while the decoded bytes are still near the limit; an encoding
  the read cannot decode in bounded steps is refused;
* a gzip body of several members gives the whole file, under the same
  limit; a compressed stream that ends early, or bytes after a deflate
  stream, stop with an error and never pass as the complete file;
* the whole stored record is measured: a file that passes the check is read
  back from the result store with every part, through ``dispatch()``;
* a page the secret-redaction rules would change stops with an error, also
  when the secret's label ends one part and its value starts the next;
* content that is not text is refused with its content type;
* no password or token in the result; registration-shape invariants.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tracemalloc
import uuid
import zlib
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
    HcxApiFileTooLargeError,
    check_path,
    split_parts,
)
from meho_backplane.connectors.hcx.connector import parse_manager_version
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
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
        "/hybridity/docs/page;x",
        "/hybridity/api/cloudConfigs;swagger.json",
        "/hybridity/api/cloudConfigs%3Bswagger.json",
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


@pytest.mark.parametrize(
    ("read_path", "location", "expected"),
    [
        ("/hybridity/docs/index.html", "swagger.json", "/hybridity/docs/swagger.json"),
        ("/hybridity/v2/api-docs", "?group=hybridity", "/hybridity/v2/api-docs?group=hybridity"),
        ("/hybridity/docs/index.html", "../v2/api-docs", "/hybridity/v2/api-docs"),
        ("/hybridity/docs/index.html", "/hybridity/docs/", "/hybridity/docs/"),
        (
            "/hybridity/docs/index.html",
            "https://other-host.test.invalid/docs/x?y=1#f",
            "https://other-host.test.invalid/docs/x",
        ),
    ],
)
@pytest.mark.asyncio
async def test_relative_redirect_is_resolved_against_the_read_path_and_not_followed(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    read_path: str,
    location: str,
    expected: str,
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        first = mock.get(read_path).respond(302, headers={"Location": location})
        others = mock.route().respond(200, text="never")
        result = await _dispatch(read_path)

    assert result.status == "ok", result.error
    assert first.call_count == 1
    assert others.call_count == 0
    assert result.result["status"] == 302
    assert result.result["redirect_to"] == expected
    if expected.startswith("/"):
        check_path(expected)


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
async def test_compressed_body_is_measured_after_decoding(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small gzip body that decodes to a file over the limit stops at the limit."""
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "4096")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    small = b"x" * 1000
    bomb = b"x" * 1_000_000
    assert len(gzip.compress(bomb)) < 2048
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/small.json").respond(
            200,
            content=gzip.compress(small),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        mock.get("/hybridity/docs/bomb.json").respond(
            200,
            content=gzip.compress(bomb),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        fits = await _dispatch("/hybridity/docs/small.json")
        too_big = await _dispatch("/hybridity/docs/bomb.json")

    assert fits.status == "ok", fits.error
    assert fits.result["size"] == len(small)
    assert fits.result["sha256"] == hashlib.sha256(small).hexdigest()
    assert fits.result["items"] == [{"index": 0, "text": small.decode()}]
    assert too_big.status == "error"
    assert "more than 4096 bytes" in too_big.extras["exception_message"]


class _ChunkedStream(httpx.AsyncByteStream):
    """A wire body that arrives in fixed chunks, as a server would send it."""

    def __init__(self, data: bytes, chunk: int) -> None:
        self._data = data
        self._chunk = chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._data), self._chunk):
            yield self._data[start : start + self._chunk]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip.compress), ("deflate", zlib.compress)],
)
async def test_streamed_compressed_body_is_refused_before_it_grows_past_the_limit(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    compress: Any,
) -> None:
    """Decoding stops at the limit: the decoded bytes never fill memory first.

    A 32 MiB file of one byte compresses to about 32 KiB. The old read
    let httpx decode each wire chunk whole, so one chunk could expand to
    many MiB before anything was counted. The peak memory of the whole
    dispatch now stays far below the decoded size.
    """
    limit = 4096
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", str(limit))
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    bomb_size = 32 * 1024 * 1024
    wire = compress(b"x" * bomb_size)
    assert len(wire) < 64 * 1024
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/bomb.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(wire, 4096),
                headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
            )
        )
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            result = await _dispatch("/hybridity/docs/bomb.json")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert result.status == "error"
    assert f"more than {limit} bytes" in result.extras["exception_message"]
    assert peak < bomb_size // 8, f"peak {peak} bytes while decoding a {bomb_size} byte file"


@pytest.mark.asyncio
async def test_content_encoding_the_read_cannot_bound_is_refused(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        route = mock.get("/hybridity/docs/br.json").respond(
            200, content=b"\x0b\x00\x80", headers={"Content-Encoding": "br"}
        )
        result = await _dispatch("/hybridity/docs/br.json")

    assert result.status == "error"
    assert "content encoding 'br'" in result.extras["exception_message"]
    assert route.calls.last.request.headers["accept-encoding"] == "gzip, deflate"


@pytest.mark.asyncio
async def test_gzip_body_of_several_members_gives_the_whole_file_under_the_limit(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gzip body may hold several members in a row; the read decodes them all.

    The old decoder stopped after the first member and reported that
    part's size and SHA-256 as the complete file. The limit still counts
    every member: two members that together pass it stop the read.
    """
    limit = 4096
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", str(limit))
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    first, second = b'{"a": "' + b"a" * 500 + b'", ', b'"b": "' + b"b" * 500 + b'"}'
    whole = first + second
    joined = gzip.compress(first) + gzip.compress(second)
    over = gzip.compress(b"x" * 3000) + gzip.compress(b"y" * 3000)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/joined.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(joined, 7),
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        )
        mock.get("/hybridity/docs/over.json").respond(
            200, content=over, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"}
        )
        result = await _dispatch("/hybridity/docs/joined.json")
        too_big = await _dispatch("/hybridity/docs/over.json")

    assert result.status == "ok", result.error
    assert result.result["size"] == len(whole)
    assert result.result["sha256"] == hashlib.sha256(whole).hexdigest()
    assert "".join(item["text"] for item in result.result["items"]) == whole.decode()
    assert too_big.status == "error"
    assert f"more than {limit} bytes" in too_big.extras["exception_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "encoding", "wire"),
    [
        ("gzip cut short", "gzip", gzip.compress(b'{"cut": "' + b"c" * 3000 + b'"}')[:-12]),
        ("deflate cut short", "deflate", zlib.compress(b'{"cut": "' + b"c" * 3000 + b'"}')[:-4]),
        (
            "second gzip member cut short",
            "gzip",
            gzip.compress(b'{"one": 1}') + gzip.compress(b'{"two": 2}')[:-12],
        ),
        ("bytes after the deflate stream", "deflate", zlib.compress(b'{"ok": 1}') + b"tail"),
    ],
)
async def test_unfinished_or_overrun_compressed_stream_stops_with_an_error(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    name: str,
    encoding: str,
    wire: bytes,
) -> None:
    """A compressed body that does not decode to one complete stream is refused.

    The old decoder returned the bytes it had and reported their size and
    SHA-256 as the complete file.
    """
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/broken.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(wire, 5),
                headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
            )
        )
        result = await _dispatch("/hybridity/docs/broken.json")

    assert result.status == "error", name
    message = result.extras["exception_message"]
    assert "compressed stream finished" in message or "after the end of the deflate" in message


class _FakeValkey:
    """In-memory stand-in for the result store's Valkey client."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: bytes, ex: int | None = None) -> None:
        self.store[name] = value

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


def _largest_file_that_passes_the_check(limit: int) -> bytes:
    """The largest ``x``-file (in steps of 16 bytes) whose stored record fits *limit*."""
    import meho_backplane.connectors.hcx.api_file as api_file

    answer = {
        "path": "/hybridity/docs/fit.json",
        "status": 200,
        "content_type": "application/json",
        "size": 0,
        "sha256": "0" * 64,
        "version": "9.1.0.0.12345678",
        "build": "12345678",
        "version_error": None,
        "redirect_to": None,
        "parts": 0,
    }
    size = limit
    while size > 0:
        data = b"x" * size
        items = [{"index": i, "text": p.decode()} for i, p in enumerate(split_parts(data))]
        answer.update(size=size, sha256=hashlib.sha256(data).hexdigest(), parts=len(items))
        try:
            api_file._assert_fits_result_handle(
                answer, items, operator_sub="op-reads-hcx", max_bytes=limit
            )
        except HcxApiFileTooLargeError:
            size -= 16
            continue
        return data
    raise AssertionError("no file size passes the check")


@pytest.mark.asyncio
async def test_every_part_of_a_file_that_passes_the_check_is_read_back_from_storage(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size check counts the whole stored record, so storage keeps every part."""
    limit = 131_072
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", str(limit))
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    data = _largest_file_that_passes_the_check(limit)
    assert len(data) > PART_BYTES
    store = ResultHandleStore(_FakeValkey(), read_max_record_bytes=limit)  # type: ignore[arg-type]
    set_default_reducer(JsonFluxReducer(store=store))
    target = _HcxTarget()
    operator = _make_operator()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/fit.json").respond(
            200, content=data, headers={"Content-Type": "application/json"}
        )
        mock.get("/hybridity/docs/over.json").respond(
            200, content=data + b"x" * 200, headers={"Content-Type": "application/json"}
        )
        result = await dispatch(
            operator=operator,
            connector_id=HCX_CONNECTOR_ID,
            op_id="hcx.api_file.get",
            target=target,
            params={"path": "/hybridity/docs/fit.json"},
        )
        over = await _dispatch("/hybridity/docs/over.json")

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["parts"] == result.result["row_count"]
    assert operator.tenant_id is not None
    stored = await store.fetch_rows(
        tenant_id=operator.tenant_id, operator_sub=operator.sub, handle_id=result.handle.handle_id
    )
    assert stored is not None
    assert stored.stored_rows == stored.total_rows == result.result["parts"]
    joined = "".join(row["text"] for row in sorted(stored.rows, key=lambda r: r["index"]))
    assert joined.encode() == data
    assert hashlib.sha256(joined.encode()).hexdigest() == result.result["sha256"]

    assert over.status == "error"
    message = over.extras["exception_message"]
    assert f"file of {len(data) + 200} bytes" in message
    assert "as a stored result record" in message


@pytest.mark.asyncio
async def test_page_the_redaction_rules_would_change_stops_with_an_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A documentation example that looks like a secret cannot come back as an exact file."""
    await _register_and_resolve(_stub_embedding)
    set_default_reducer(JsonFluxReducer(store=ResultHandleStore(_FakeValkey())))  # type: ignore[arg-type]
    page = (
        "<pre>GET /hybridity/api/sessions HTTP/1.1\n"
        "Authorization: Bearer example-token-value-0123456789\n</pre>"
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/auth.html").respond(
            200, text=page, headers={"Content-Type": "text/html"}
        )
        result = await _dispatch("/hybridity/docs/auth.html")

    assert result.status == "error"
    message = result.extras["exception_message"]
    assert "strip-authorization-header" in message
    assert "1 place(s)" in message
    assert "exact file cannot be returned" in message
    assert "example-token-value" not in json.dumps(result.result) + json.dumps(
        result.extras, default=str
    )


@pytest.mark.asyncio
async def test_secret_that_crosses_a_part_boundary_stops_with_an_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """``password=`` ends the first part and the value starts the second.

    Neither part alone matches the rule, so a check per part would let the
    secret through. The whole text is checked first, so the read stops.
    """
    await _register_and_resolve(_stub_embedding)
    set_default_reducer(JsonFluxReducer(store=ResultHandleStore(_FakeValkey())))  # type: ignore[arg-type]
    label = b" password="
    page = b"x" * (PART_BYTES - len(label)) + label + b"example-secret-value-0123456789 tail\n"
    parts = [part.decode() for part in split_parts(page)]
    assert parts[0].endswith("password=")
    assert parts[1].startswith("example-secret-value")
    for index, text in enumerate(parts):
        alone = apply_connector_boundary_redaction(
            {"index": index, "text": text}, connector_id=HCX_CONNECTOR_ID, tenant=None, op=None
        )
        assert alone.redacted == alone.raw, "a part alone must not match; the gap is real"

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login_and_version(mock)
        mock.get("/hybridity/docs/split.html").respond(
            200, content=page, headers={"Content-Type": "text/html"}
        )
        result = await _dispatch("/hybridity/docs/split.html")

    assert result.status == "error"
    message = result.extras["exception_message"]
    assert "exact file cannot be returned" in message
    assert "1 place(s)" in message
    assert "example-secret-value" not in json.dumps(result.result) + json.dumps(
        result.extras, default=str
    )


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
