# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vrli.api_file.get`` read of the Logs connectors (9.0 and 8.x).

What is covered, with a respx-mocked Logs system and no ingested catalog:

* an accepted path: status, content type, size, SHA-256, version, build
  and the parts, with the session token on the read;
* the same read on the 8.x implementation (``vrli-vrli8-8.0``), which
  inherits the handler;
* refused paths: no HTTP request goes out;
* a 404 reaches the answer; a redirect is not followed, and a relative
  ``Location`` is resolved against the path that was read;
* a 401 or a 440 on the read drops the session: the system is asked once
  more with a fresh login, and a second 401 or 440 is an error;
* a one-line minified JSON file of a few MB and a JSON file of more than
  10,000 lines: the joined parts give the checked copy (same SHA-256);
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
* a documentation example the secret-redaction rules match (an
  ``Authorization: Bearer`` example in an HTML page or in a JSON API file's
  description) comes back as the placeholder and is counted, also when
  the secret's label would end one part and its value start the next;
  ``size`` and ``sha256`` describe the joined parts;
* every secret form the code reviews found (a JSON value with an
  apostrophe, ``sessionId``, a long value, a private-key block, an
  unquoted YAML name, a YAML ``|`` or ``>`` block value) comes back as the
  placeholder and is counted in ``secrets_replaced``, also across a part
  boundary and in a redirect query; a file that does not parse is
  refused without its text;
* the body of a 401 or 440 is the checked copy, or withheld when it cannot
  be checked, and the login retry still runs; the flight recorder gets
  only the checked copy, on success and on a 401 or 440;
* content that is not text is refused with its content type;
* the version read fails (HTTP 500, no connection, an answer with no
  version) and the file still comes back with the reason;
* no password or session token in the result; registration-shape
  invariants on both implementations.

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
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.api_file_read import (
    PART_BYTES,
    ApiFileTooLargeError,
    split_parts,
)
from meho_backplane.connectors._shared.api_file_secrets import REDACTED
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vcf_logs import (
    VRLI_CONNECTOR_ID,
    VRLI_IMPL_ID,
    VRLI_PRODUCT,
    VRLI_TYPED_OPS,
    VRLI_VERSION,
    VcfLogsConnector,
    register_vrli_typed_operations,
)
from meho_backplane.connectors.vcf_logs.api_file import (
    DOCS_FOLDERS,
    DOCS_PAGES,
    VrliApiFilePathError,
    check_path,
)
from meho_backplane.connectors.vrli8 import (
    VRLI8_CONNECTOR_ID,
    VRLI8_IMPL_ID,
    VRLI8_VERSION,
    Vrli8Connector,
    register_vrli8_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

_HOST = "logs-docs.test.invalid"
_BASE_URL = f"https://{_HOST}"
_SESSIONS = "/api/v2/sessions"
_VERSION = "/api/v2/version"
_REFERENCE = "/rest-api"
_SUPPORTED = "/rest-api/supported"
_API_DOCS = "/rest-api/api-docs"
_PASSWORD = "docs-password-value"
_SESSION_ID = "docs-session-token-value"
_VERSION_PAYLOAD: dict[str, Any] = {
    "version": "9.0.0.0.21000001",
    "releaseName": "Operations for Logs 9.0",
}
_HTML = "<html><head><title>REST API</title></head><body>docs</body></html>"


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
        product=VRLI_PRODUCT, version=VRLI_VERSION, impl_id=VRLI_IMPL_ID, cls=VcfLogsConnector
    )
    register_connector_v2(
        product=VRLI_PRODUCT, version=VRLI8_VERSION, impl_id=VRLI8_IMPL_ID, cls=Vrli8Connector
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


class _LogsTarget:
    """Target satisfying both ``VcfLogsTargetLike`` and the resolver shape."""

    def __init__(self, version: str = "9.0.0") -> None:
        self.product = VRLI_PRODUCT
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "logs-docs"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/logs-docs"
        self.auth_model = "shared_service_account"
        self.provider = None


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-logs",
        name="Logs Reads Operator",
        email=None,
        raw_jwt="op.reads.logs.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


class _CredsLoader:
    """Stub login credentials; counts how often the connector asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _target: object, _operator: Operator) -> dict[str, str]:
        self.calls += 1
        return {"username": "admin-local", "password": _PASSWORD}


async def _register_and_resolve(
    _stub_embedding: AsyncMock, cls: type[VcfLogsConnector] = VcfLogsConnector
) -> tuple[VcfLogsConnector, _CredsLoader]:
    await register_vrli_typed_operations()
    await register_vrli8_typed_operations()
    instance = get_or_create_connector_instance(cls)
    loader = _CredsLoader()
    instance._credentials = type(instance._credentials)(loader, product_label="vrli")  # type: ignore[attr-defined]
    instance._session_tokens.clear()
    return instance, loader


def _mock_login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(_SESSIONS).respond(200, json={"sessionId": _SESSION_ID, "ttl": 1800})


def _mock_version(mock: respx.MockRouter) -> respx.Route:
    _mock_login(mock)
    return mock.get(_VERSION).respond(200, json=_VERSION_PAYLOAD)


async def _dispatch(
    path: str, *, connector_id: str = VRLI_CONNECTOR_ID, target: _LogsTarget | None = None
) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=connector_id,
        op_id="vrli.api_file.get",
        target=target or _LogsTarget(),
        params={"path": path},
    )


# ---------------------------------------------------------------------------
# An accepted path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_path_returns_status_size_sha_version_and_parts(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        version = _mock_version(mock)
        route = mock.get(_REFERENCE).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert version.call_count == 1
    assert route.calls[0].request.headers["authorization"] == f"Bearer {_SESSION_ID}"
    body = result.result
    assert body["path"] == _REFERENCE
    assert body["status"] == 200
    assert body["content_type"] == "text/html"
    assert body["size"] == len(_HTML.encode())
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] == "9.0.0"
    assert body["build"] == "21000001"
    assert body["version_error"] is None
    assert body["redirect_to"] is None
    assert body["parts"] == 1
    assert body["items"] == [{"index": 0, "text": _HTML}]


@pytest.mark.asyncio
async def test_the_8x_implementation_serves_the_same_read(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """``vrli-vrli8-8.0`` inherits the handler; an 8.18 target reads the same page."""
    await _register_and_resolve(_stub_embedding, Vrli8Connector)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login(mock)
        mock.get(_VERSION).respond(200, json={"version": "8.18.0.0.24000001"})
        route = mock.get(_SUPPORTED).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(
            _SUPPORTED, connector_id=VRLI8_CONNECTOR_ID, target=_LogsTarget(version="8.18.0")
        )

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == f"Bearer {_SESSION_ID}"
    body = result.result
    assert body["path"] == _SUPPORTED
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] == "8.18.0"
    assert body["build"] == "24000001"
    assert body["version_error"] is None


@pytest.mark.asyncio
async def test_query_string_with_allowed_keys_is_sent(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        route = mock.get(_API_DOCS).respond(200, json={"swagger": "2.0"})
        result = await _dispatch(f"{_API_DOCS}?group=public&apiVersion=2")

    assert result.status == "ok", result.error
    sent = route.calls[0].request.url
    assert sent.params["group"] == "public"
    assert sent.params["apiVersion"] == "2"
    assert result.result["path"] == f"{_API_DOCS}?group=public&apiVersion=2"


# ---------------------------------------------------------------------------
# Refused paths: no HTTP request goes out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../rest-api",
        "/rest-api/../api/v2/events/",
        "//other-host.test.invalid/rest-api",
        "https://other-host.test.invalid/rest-api",
        "other-host.test.invalid/rest-api",
        "/rest-api/%2e%2e/version",
        "/rest-api%2f..%2fx",
        "/rest-api/\\x",
        "/rest-api/#frag",
        "/rest-api/page;x",
        "/api/v2/version;swagger.json",
        "/api/v2/version%3Bswagger.json",
        "/rest-api/?foo=1",
        "/rest-api/?group=a&token=b",
        "/rest-apix",
        "/rest-api-old",
        "/api/v2/events/",
        "/api/v2/version",
        "/api/v2/sessions",
        "/api/v2/hosts",
        "/api/data.json",
        "",
    ],
)
@pytest.mark.asyncio
async def test_refused_path_sends_no_http_request(
    _stub_embedding: AsyncMock, session: AsyncSession, path: str
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.route().respond(200, text="never")
        result = await _dispatch(path)
        assert not mock.calls, f"path {path!r} reached the system"

    assert result.status == "error"
    assert result.error


def test_check_path_accepts_the_documented_shapes() -> None:
    for page in DOCS_PAGES:
        assert check_path(page) == (page, "")
    for folder in DOCS_FOLDERS:
        assert check_path(folder) == (folder, "")
        assert check_path(f"{folder}supported") == (f"{folder}supported", "")
        assert check_path(f"{folder}index.html") == (f"{folder}index.html", "")
    assert check_path("/rest-api/api-docs?group=x") == ("/rest-api/api-docs", "group=x")
    assert check_path("/api/v2/api-docs") == ("/api/v2/api-docs", "")
    assert check_path("/swagger-resources") == ("/swagger-resources", "")
    assert check_path("/swagger-config") == ("/swagger-config", "")
    assert check_path("/docs/spec/openapi.yaml") == ("/docs/spec/openapi.yaml", "")
    with pytest.raises(VrliApiFilePathError):
        check_path("/rest-api/?group=a&group=b&other=c")


# ---------------------------------------------------------------------------
# Status, redirect, session expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_status_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/missing.json").respond(404, json={"errorMessage": "not found"})
        result = await _dispatch("/rest-api/missing.json")

    assert result.status == "ok", result.error
    assert result.result["status"] == 404
    assert result.result["items"] == [{"index": 0, "text": '{"errorMessage":"not found"}'}]


@pytest.mark.asyncio
async def test_redirect_is_reported_and_not_followed(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        first = mock.get(_REFERENCE).respond(301, headers={"Location": f"{_BASE_URL}/rest-api/"})
        second = mock.get("/rest-api/").respond(200, text=_HTML)
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    assert first.call_count == 1
    assert second.call_count == 0
    assert result.result["status"] == 301
    assert result.result["redirect_to"] == "/rest-api/"


@pytest.mark.parametrize(
    ("read_path", "location", "expected"),
    [
        ("/rest-api/index.html", "swagger.json", "/rest-api/swagger.json"),
        (_API_DOCS, "?group=public", f"{_API_DOCS}?group=public"),
        ("/rest-api/index.html", "../api/v2/api-docs", "/api/v2/api-docs"),
        (_REFERENCE, "/rest-api/", "/rest-api/"),
        (
            _SUPPORTED,
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
        _mock_version(mock)
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


@pytest.mark.parametrize("status", [401, 440])
@pytest.mark.asyncio
async def test_session_expiry_on_the_read_drops_the_session_and_asks_once_more(
    _stub_embedding: AsyncMock, session: AsyncSession, status: int
) -> None:
    """A 401, or the system's own 440, means the session is gone: log in once more."""
    _, loader = await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = _mock_login(mock)
        mock.get(_VERSION).respond(200, json=_VERSION_PAYLOAD)
        docs = mock.get(_REFERENCE).respond(status, json={"errorMessage": "session expired"})
        result = await _dispatch(_REFERENCE)

    assert result.status == "error"
    assert str(status) in (result.error or "")
    assert "session expired" in (result.extras.get("upstream_message") or "")
    assert docs.call_count == 2
    assert login.call_count == 2
    assert loader.calls == 1


# ---------------------------------------------------------------------------
# Big files: the joined parts give the exact file
# ---------------------------------------------------------------------------


def _minified_json_of_a_few_mb() -> bytes:
    rows = [
        {"path": f"/api/v2/resource-{i}", "summary": "made-up summary " * 8, "n": i}
        for i in range(12_000)
    ]
    data = json.dumps({"paths": rows}, separators=(",", ":")).encode()
    assert b"\n" not in data and len(data) > 2 * 1024 * 1024
    return data


def _json_with_more_than_10000_lines() -> bytes:
    rows = [f"line {i}: Ünïcödé text for a long file" for i in range(12_345)]
    return json.dumps(rows, indent=2, ensure_ascii=False).encode()


@pytest.mark.parametrize(
    ("content_type", "data"),
    [
        ("application/json", _minified_json_of_a_few_mb()),
        ("text/plain; charset=utf-8", _json_with_more_than_10000_lines()),
    ],
)
@pytest.mark.asyncio
async def test_joined_parts_give_the_checked_copy(
    _stub_embedding: AsyncMock, session: AsyncSession, content_type: str, data: bytes
) -> None:
    instance, _ = await _register_and_resolve(_stub_embedding)
    target = _LogsTarget()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/big.json").respond(
            200, content=data, headers={"Content-Type": content_type}
        )
        answer = await instance.api_file_get(
            _make_operator(), target, {"path": "/rest-api/big.json"}
        )

    copy = json.dumps(json.loads(data), ensure_ascii=False, separators=(",", ":")).encode()
    assert answer["size"] == len(copy)
    assert answer["secrets_replaced"] == 0
    assert answer["parts"] == len(answer["items"]) > 1
    assert [item["index"] for item in answer["items"]] == list(range(answer["parts"]))
    assert all(len(item["text"].encode()) <= PART_BYTES for item in answer["items"])
    joined = "".join(item["text"] for item in answer["items"]).encode()
    assert joined == copy
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
        _mock_version(mock)
        mock.get("/rest-api/big.json").respond(
            200, content=data, headers={"Content-Type": "application/json"}
        )
        result = await _dispatch("/rest-api/big.json")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["sha256"] == hashlib.sha256(data).hexdigest()
    assert summary["size"] == len(data)
    assert summary["parts"] == summary["row_count"]
    assert summary["version"] == "9.0.0"


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
        _mock_version(mock)
        mock.get("/rest-api/declared.json").respond(200, content=data)
        mock.get("/rest-api/chunked.json").respond(200, content=_chunks(data))
        declared = await _dispatch("/rest-api/declared.json")
        chunked = await _dispatch("/rest-api/chunked.json")

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
    data = b'"' + b'\\"' * 494 + b'"'
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/quotes.json").respond(200, content=data)
        result = await _dispatch("/rest-api/quotes.json")

    assert result.status == "error"
    assert f"file of {len(data)} bytes" in result.extras["exception_message"]


@pytest.mark.asyncio
async def test_compressed_body_is_measured_after_decoding(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small gzip body that decodes to a file over the limit stops at the limit."""
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "4096")
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    small = b'{"x":"' + b"x" * 990 + b'"}'
    bomb = b"x" * 1_000_000
    assert len(gzip.compress(bomb)) < 2048
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/small.json").respond(
            200,
            content=gzip.compress(small),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        mock.get("/rest-api/bomb.json").respond(
            200,
            content=gzip.compress(bomb),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        fits = await _dispatch("/rest-api/small.json")
        too_big = await _dispatch("/rest-api/bomb.json")

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
    """Decoding stops at the limit: the decoded bytes never fill memory first."""
    limit = 4096
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", str(limit))
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    bomb_size = 32 * 1024 * 1024
    wire = compress(b"x" * bomb_size)
    assert len(wire) < 64 * 1024
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/bomb.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(wire, 4096),
                headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
            )
        )
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            result = await _dispatch("/rest-api/bomb.json")
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
        _mock_version(mock)
        route = mock.get("/rest-api/br.json").respond(
            200, content=b"\x0b\x00\x80", headers={"Content-Encoding": "br"}
        )
        result = await _dispatch("/rest-api/br.json")

    assert result.status == "error"
    assert "content encoding 'br'" in result.extras["exception_message"]
    assert route.calls.last.request.headers["accept-encoding"] == "gzip, deflate"


@pytest.mark.asyncio
async def test_gzip_body_of_several_members_gives_the_whole_file_under_the_limit(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gzip body may hold several members in a row; the read decodes them all."""
    limit = 4096
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", str(limit))
    get_settings.cache_clear()
    await _register_and_resolve(_stub_embedding)
    first, second = b'{"a":"' + b"a" * 500 + b'",', b'"b":"' + b"b" * 500 + b'"}'
    whole = first + second
    joined = gzip.compress(first) + gzip.compress(second)
    over = gzip.compress(b"x" * 3000) + gzip.compress(b"y" * 3000)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/joined.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(joined, 7),
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        )
        mock.get("/rest-api/over.json").respond(
            200, content=over, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"}
        )
        result = await _dispatch("/rest-api/joined.json")
        too_big = await _dispatch("/rest-api/over.json")

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
    """A compressed body that does not decode to one complete stream is refused."""
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/broken.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(wire, 5),
                headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
            )
        )
        result = await _dispatch("/rest-api/broken.json")

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
    from meho_backplane.connectors._shared.api_file_read import assert_fits_result_handle

    answer = {
        "path": "/rest-api/fit.json",
        "status": 200,
        "content_type": "application/json",
        "size": 0,
        "sha256": "0" * 64,
        "version": "9.0.0",
        "build": "21000001",
        "version_error": None,
        "redirect_to": None,
        "secrets_replaced": 0,
        "parts": 0,
    }
    size = limit
    while size > 0:
        data = b'"' + b"x" * (size - 2) + b'"'
        items = [{"index": i, "text": p.decode()} for i, p in enumerate(split_parts(data))]
        answer.update(size=size, sha256=hashlib.sha256(data).hexdigest(), parts=len(items))
        try:
            assert_fits_result_handle(
                answer,
                items,
                operator_sub="op-reads-logs",
                op_id="vrli.api_file.get",
                max_bytes=limit,
            )
        except ApiFileTooLargeError:
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
    target = _LogsTarget()
    operator = _make_operator()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/fit.json").respond(
            200, content=data, headers={"Content-Type": "application/json"}
        )
        mock.get("/rest-api/over.json").respond(
            200, content=data[:-1] + b"x" * 200 + b'"', headers={"Content-Type": "application/json"}
        )
        result = await dispatch(
            operator=operator,
            connector_id=VRLI_CONNECTOR_ID,
            op_id="vrli.api_file.get",
            target=target,
            params={"path": "/rest-api/fit.json"},
        )
        over = await _dispatch("/rest-api/over.json")

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
async def test_example_the_redaction_rules_match_comes_back_replaced_and_counted(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A documentation example that looks like a secret comes back as the placeholder."""
    await _register_and_resolve(_stub_embedding)
    set_default_reducer(JsonFluxReducer(store=ResultHandleStore(_FakeValkey())))  # type: ignore[arg-type]
    page = (
        "<pre>GET /api/v2/hosts HTTP/1.1\n"
        "Authorization: Bearer example-token-value-0123456789\n</pre>"
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/auth.html").respond(
            200, text=page, headers={"Content-Type": "text/html"}
        )
        result = await _dispatch("/rest-api/auth.html")

    assert result.status == "ok", result.error
    body = result.result
    joined = "".join(item["text"] for item in body["items"])
    assert "[REDACTED:authorization_header]" in joined
    assert body["secrets_replaced"] == 1
    assert body["size"] == len(joined.encode())
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()
    assert "example-token-value" not in _rendered(result)


@pytest.mark.asyncio
async def test_api_file_with_a_bearer_example_in_a_description_comes_back_replaced(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The vendor's Logs 9.0 API file has ``Authorization: Bearer`` examples in its text."""
    await _register_and_resolve(_stub_embedding)
    spec = {
        "openapi": "3.0.1",
        "info": {
            "title": "Logs API",
            "description": "Send the session id on each call:\n"
            "Authorization: Bearer example-session-id-0123456789",
        },
        "paths": {},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/openapi.json").respond(
            200, text=json.dumps(spec, indent=2), headers={"Content-Type": "application/json"}
        )
        result = await _dispatch("/rest-api/openapi.json")

    assert result.status == "ok", result.error
    body = result.result
    joined = "".join(item["text"] for item in body["items"])
    copy = json.loads(joined)
    assert copy["info"]["title"] == "Logs API"
    assert "[REDACTED:authorization_header]" in copy["info"]["description"]
    assert body["secrets_replaced"] == 1
    assert body["size"] == len(joined.encode())
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()
    assert "example-session-id-0123456789" not in _rendered(result)


@pytest.mark.asyncio
async def test_secret_whose_label_ends_one_part_comes_back_replaced(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """``password=`` would end the first part and the value start the second.

    Each part alone does not match the rules, so a check of the parts alone
    would miss it. The rules run on the whole text value before the file is
    cut, so the value comes back replaced.
    """
    instance, _ = await _register_and_resolve(_stub_embedding)
    label = b" password="
    page = b"x" * (PART_BYTES - len(label)) + label + b"example-secret-value-0123456789 tail\n"
    parts = [part.decode() for part in split_parts(page)]
    assert parts[0].endswith("password=")
    assert parts[1].startswith("example-secret-value")
    for index, text in enumerate(parts):
        alone = apply_connector_boundary_redaction(
            {"index": index, "text": text}, connector_id=VRLI_CONNECTOR_ID, tenant=None, op=None
        )
        assert alone.redacted == alone.raw, "a part alone must not match; the gap is real"

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/split.html").respond(
            200, content=page, headers={"Content-Type": "text/html"}
        )
        body = await instance.api_file_get(
            _make_operator(), _LogsTarget(), {"path": "/rest-api/split.html"}
        )

    joined = "".join(item["text"] for item in body["items"])
    assert "[REDACTED:" in joined
    assert body["secrets_replaced"] == 1
    assert body["size"] == len(joined.encode())
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()
    assert "example-secret-value" not in json.dumps(body)


def _private_key_block(kind: str, body: str) -> str:
    """Build a PEM private-key block at run time, so no key header sits in this file."""
    header = " ".join(word for word in ("BEGIN", kind, "PRIVATE", "KEY") if word)
    footer = header.replace("BEGIN", "END")
    return f"-----{header}-----\n{body}\n-----{footer}-----\n"


#: Longer than the 4096 characters the shared rules match in one value.
_LONG_SECRET = "example-secret-value-" + "0123456789" * 420

#: Pages with a secret in every form the code reviews found, in the
#: formats the read accepts: ``(content type, path, page, secret)``.
_SECRET_PAGES: list[Any] = [
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        "<pre>POST /api/v2/sessions\n</pre><pre>"
        '{"username": "admin", "password": "example-secret-value-0123456789"}</pre>',
        "example-secret-value-0123456789",
        id="json-example-in-an-html-page",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        "<pre>POST /api/v2/sessions\n"
        '{"username": "admin", "password": "example-secret-value-0123456789"}</pre>',
        "example-secret-value-0123456789",
        id="json-example-after-the-request-line-in-one-block",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        '<pre><code>{<span class="k">"username"</span>: <span class="s">"admin"</span>, '
        '<span class="k">"password"</span>: '
        '<span class="s">"example-secret-value-0123456789"</span>}</code></pre>',
        "example-secret-value-0123456789",
        id="json-example-coloured-with-span",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        '<pre><span id="password">example-secret-value-0123456789</span>{"ok": true}</pre>',
        "example-secret-value-0123456789",
        id="secret-child-element-before-json-in-one-block",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        '<pre><span id="password">{"password": "example-secret-value-0123456789"}</span></pre>',
        "example-secret-value-0123456789",
        id="secret-child-element-holding-the-json",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        '<pre>{"<span id="password">password</span>": "example-secret-value-0123456789"}</pre>',
        "example-secret-value-0123456789",
        id="secret-child-element-as-the-json-key",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        '<pre>{"x":{"password":"example-secret-value-0123456789"},"x":<span>1</span>}</pre>',
        "example-secret-value-0123456789",
        id="json-with-a-repeated-key-in-a-highlighted-block",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        '<pre>{"example":"{\\"x\\":{\\"password\\":\\"example-secret-value-0123456789\\"},'
        '\\"x\\":<span>1</span>}"}</pre>',
        "example-secret-value-0123456789",
        id="json-with-a-repeated-key-in-an-embedded-string-in-a-highlighted-block",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        "<pre><span>x</span>:\n  password: example-secret-value-0123456789\nx: 1</pre>",
        "example-secret-value-0123456789",
        id="yaml-with-a-repeated-key-in-a-highlighted-block",
    ),
    pytest.param(
        "text/html",
        "/rest-api/login.html",
        "<pre><span>user</span>: admin\n# password: example-secret-value-0123456789</pre>",
        "example-secret-value-0123456789",
        id="yaml-with-a-comment-in-a-highlighted-block",
    ),
    pytest.param(
        "application/json",
        _API_DOCS,
        '{"username": "admin", "password": "it\'s-a-secret-0123456789"}',
        "it's-a-secret-0123456789",
        id="json-value-with-an-apostrophe",
    ),
    pytest.param(
        "application/json",
        _API_DOCS,
        '{"userId": "u1", "sessionId": "example-session-id-0123456789", "ttl": 1800}',
        "example-session-id-0123456789",
        id="json-session-id",
    ),
    pytest.param(
        "application/json",
        _API_DOCS,
        '{"sessionToken": "example-session-token-0123456789"}',
        "example-session-token-0123456789",
        id="json-session-token",
    ),
    pytest.param(
        "application/json",
        _API_DOCS,
        '{"licenseKey": "example-licence-key-value-0123456789"}',
        "example-licence-key-value-0123456789",
        id="json-licence-key",
    ),
    pytest.param(
        "application/json",
        _API_DOCS,
        '{"username": "admin", "password": "' + _LONG_SECRET + '"}',
        _LONG_SECRET,
        id="json-value-longer-than-the-shared-rules-match",
    ),
    pytest.param(
        "application/json",
        _API_DOCS,
        json.dumps({"example": _private_key_block("RSA", "example-private-key-body")}),
        "example-private-key-body",
        id="json-private-key-block",
    ),
    pytest.param(
        "application/yaml",
        "/rest-api/openapi.yaml",
        'username: admin\npassword: "it\'s-a-secret-0123456789"\n',
        "it's-a-secret-0123456789",
        id="yaml-unquoted-name",
    ),
    pytest.param(
        "application/yaml",
        "/rest-api/openapi.yaml",
        "sessionId: example-session-id-0123456789\n",
        "example-session-id-0123456789",
        id="yaml-session-id",
    ),
    pytest.param(
        "application/yaml",
        "/rest-api/openapi.yaml",
        "username: admin\npassword: |\n  example-block-secret-0123456789\n",
        "example-block-secret-0123456789",
        id="yaml-literal-block",
    ),
    pytest.param(
        "application/yaml",
        "/rest-api/openapi.yaml",
        "username: admin\npassword: >\n  example-folded-secret\n  0123456789\n",
        "example-folded-secret",
        id="yaml-folded-block",
    ),
]


def _rendered(result: Any) -> str:
    return (result.error or "") + json.dumps(result.result) + json.dumps(result.extras, default=str)


@pytest.mark.parametrize(("content_type", "path", "page", "secret"), _SECRET_PAGES)
@pytest.mark.asyncio
async def test_secret_comes_back_as_the_placeholder_and_is_counted(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    content_type: str,
    path: str,
    page: str,
    secret: str,
) -> None:
    """The file is parsed; every value under a secret name is replaced before the parts are cut."""
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(path).respond(200, text=page, headers={"Content-Type": content_type})
        result = await _dispatch(path)

    assert result.status == "ok", result.error
    body = result.result
    joined = "".join(item["text"] for item in body["items"])
    assert REDACTED in joined
    assert body["secrets_replaced"] >= 1
    assert body["size"] == len(joined.encode())
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()
    assert secret not in _rendered(result)


@pytest.mark.asyncio
async def test_spec_with_secrets_keeps_its_shape_and_counts_each_value(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    spec = {
        "swagger": "2.0",
        "paths": {"/api/v2/sessions": {"post": {"x-example": {"password": "made-up-1"}}}},
        "definitions": {"Session": {"properties": {"sessionId": {"type": "string"}}}},
    }
    copy = {
        "swagger": "2.0",
        "paths": {"/api/v2/sessions": {"post": {"x-example": {"password": REDACTED}}}},
        "definitions": {"Session": {"properties": {"sessionId": REDACTED}}},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(_API_DOCS).respond(
            200, text=json.dumps(spec, indent=2), headers={"Content-Type": "application/json"}
        )
        result = await _dispatch(_API_DOCS)

    assert result.status == "ok", result.error
    assert json.loads("".join(item["text"] for item in result.result["items"])) == copy
    assert result.result["secrets_replaced"] == 2


@pytest.mark.asyncio
async def test_secret_that_crosses_a_part_boundary_comes_back_replaced(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The whole file is checked before it is cut, so a part boundary cannot hide a value."""
    await _register_and_resolve(_stub_embedding)
    label = '{"filler": "'
    page = label + "x" * (PART_BYTES - len(label) - 2) + '", "password": "made-up-secret-1"}'
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(_API_DOCS).respond(200, text=page, headers={"Content-Type": "application/json"})
        result = await _dispatch(_API_DOCS)

    assert result.status == "ok", result.error
    assert result.result["parts"] == 2
    assert result.result["secrets_replaced"] == 1
    assert "made-up-secret-1" not in _rendered(result)


@pytest.mark.asyncio
async def test_file_that_does_not_parse_is_refused_with_the_reason(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/broken.json").respond(
            200, text='{"password": "made-up-2"', headers={"Content-Type": "application/json"}
        )
        mock.get("/rest-api/app.js").respond(
            200, text='var password = "made-up-3";', headers={"Content-Type": "text/javascript"}
        )
        broken = await _dispatch("/rest-api/broken.json")
        script = await _dispatch("/rest-api/app.js")

    assert broken.status == "error"
    assert "not well-formed JSON" in broken.extras["exception_message"]
    assert script.status == "error"
    assert "no known format" in script.extras["exception_message"]
    for result in (broken, script):
        assert "made-up-" not in _rendered(result)


@pytest.mark.asyncio
async def test_secret_in_the_redirect_query_is_replaced_and_counted(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(_REFERENCE).respond(
            302, headers={"Location": "/rest-api/?group=a&sessionId=made-up-session-4"}
        )
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    assert result.result["redirect_to"] == f"/rest-api/?group=a&sessionId={REDACTED}"
    assert result.result["secrets_replaced"] == 1
    assert "made-up-session-4" not in _rendered(result)


# ---------------------------------------------------------------------------
# The body of a 401 or 440 answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 440])
@pytest.mark.parametrize(("content_type", "path", "page", "secret"), _SECRET_PAGES)
@pytest.mark.asyncio
async def test_session_expiry_body_is_checked_and_the_retry_still_runs(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    status: int,
    content_type: str,
    path: str,
    page: str,
    secret: str,
) -> None:
    """The dispatcher copies the 401 or 440 body into the error result; only a checked copy."""
    _, loader = await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = _mock_login(mock)
        mock.get(_VERSION).respond(200, json=_VERSION_PAYLOAD)
        docs = mock.get(path).respond(status, text=page, headers={"Content-Type": content_type})
        result = await _dispatch(path)

    assert result.status == "error"
    assert str(status) in (result.error or "")
    assert docs.call_count == 2
    assert login.call_count == 2
    assert loader.calls == 1
    assert secret not in _rendered(result)


@pytest.mark.parametrize("status", [401, 440])
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", "session expired; password=it's-a-secret-0123456789"),
        ("application/json", '{"errorMessage": "session expired", "password": "made-up-5"'),
    ],
    ids=["plain-text", "json-that-does-not-parse"],
)
@pytest.mark.asyncio
async def test_session_expiry_body_that_cannot_be_checked_is_withheld(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    status: int,
    content_type: str,
    body: str,
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        docs = mock.get(_API_DOCS).respond(
            status, text=body, headers={"Content-Type": content_type}
        )
        result = await _dispatch(_API_DOCS)

    assert result.status == "error"
    assert docs.call_count == 2
    rendered = _rendered(result)
    assert "it's-a-secret-0123456789" not in rendered and "made-up-5" not in rendered
    assert "session expired" not in rendered, "the whole body is withheld, not only the secret"


# ---------------------------------------------------------------------------
# The flight recorder gets the checked copy only
# ---------------------------------------------------------------------------


@contextmanager
def _recording() -> Iterator[flight_recorder_capture._CaptureScope]:
    """Turn the flight recorder on for this task, with the op context the dispatcher sets."""
    scope = flight_recorder_capture._CaptureScope(
        audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None
    )
    op = flight_recorder_capture._OpContext(
        op_id="vrli.api_file.get",
        connector_id=VRLI_CONNECTOR_ID,
        impl_id=VRLI_IMPL_ID,
        product=VRLI_PRODUCT,
        tags=("read-only",),
        body_paths=(),
        delete_shaped_patterns=(),
    )
    token = flight_recorder_capture._active_capture_var.set(scope)
    op_token = flight_recorder_capture._op_context_var.set(op)
    try:
        yield scope
    finally:
        flight_recorder_capture._op_context_var.reset(op_token)
        flight_recorder_capture._active_capture_var.reset(token)


def _recorded_call(scope: flight_recorder_capture._CaptureScope, path: str) -> dict[str, Any]:
    spans = [span for span in scope.spans if span.name.endswith(path)]
    assert len(spans) == 1, [span.name for span in scope.spans]
    return dict(spans[0].attributes or {})


@pytest.mark.parametrize(("content_type", "path", "page", "secret"), _SECRET_PAGES)
@pytest.mark.asyncio
async def test_flight_recorder_gets_the_checked_copy_of_the_file(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    content_type: str,
    path: str,
    page: str,
    secret: str,
) -> None:
    instance, _ = await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(path).respond(200, text=page, headers={"Content-Type": content_type})
        with _recording() as scope:
            answer = await instance.api_file_get(_make_operator(), _LogsTarget(), {"path": path})

    recorded = _recorded_call(scope, path)
    assert secret not in json.dumps(recorded, default=str)
    joined = "".join(item["text"] for item in answer["items"])
    recorded_body = recorded["response_body"]
    if isinstance(recorded_body, (dict, list)):
        assert recorded_body == json.loads(joined), "the recorder holds the checked copy"
    else:
        assert recorded_body in (joined, "[MEHO-OMITTED:redaction-uncertain]")


@pytest.mark.parametrize("status", [401, 440])
@pytest.mark.parametrize(("content_type", "path", "page", "secret"), _SECRET_PAGES)
@pytest.mark.asyncio
async def test_flight_recorder_gets_the_checked_copy_of_a_session_expiry_body(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    status: int,
    content_type: str,
    path: str,
    page: str,
    secret: str,
) -> None:
    instance, _ = await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(path).respond(status, text=page, headers={"Content-Type": content_type})
        with _recording() as scope, pytest.raises(httpx.HTTPStatusError) as raised:
            await instance.api_file_get(_make_operator(), _LogsTarget(), {"path": path})

    assert raised.value.response.status_code == status
    assert secret not in raised.value.response.text
    assert secret not in json.dumps(_recorded_call(scope, path), default=str)


@pytest.mark.asyncio
async def test_flight_recorder_gets_no_body_when_the_file_is_refused(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    instance, _ = await _register_and_resolve(_stub_embedding)
    path = "/rest-api/app.js"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(path).respond(
            200, text='var password = "made-up-6";', headers={"Content-Type": "text/javascript"}
        )
        with _recording() as scope, pytest.raises(ValueError, match="no known format"):
            await instance.api_file_get(_make_operator(), _LogsTarget(), {"path": path})

    assert "made-up-6" not in json.dumps(_recorded_call(scope, path), default=str)


@pytest.mark.asyncio
async def test_non_text_content_is_refused_naming_the_content_type(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/rest-api/logo.openapi.json").respond(
            200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"}
        )
        mock.get("/rest-api/bad.json").respond(
            200, content=b"\xff\xfe\x00bad", headers={"Content-Type": "text/plain"}
        )
        png = await _dispatch("/rest-api/logo.openapi.json")
        bad = await _dispatch("/rest-api/bad.json")

    assert png.status == "error" and "image/png" in png.extras["exception_message"]
    assert bad.status == "error" and "text/plain" in bad.extras["exception_message"]


@pytest.mark.asyncio
async def test_version_read_500_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login(mock)
        version = mock.get(_VERSION).respond(500, json={"errorMessage": "x"})
        mock.get(_REFERENCE).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    assert version.call_count == 1
    body = result.result
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] is None
    assert body["build"] is None
    assert "500" in body["version_error"]


@pytest.mark.asyncio
async def test_version_read_connection_error_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login(mock)
        mock.get(_VERSION).mock(side_effect=httpx.ConnectError("refused"))
        mock.get(_REFERENCE).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    body = result.result
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] is None
    assert body["build"] is None
    assert body["version_error"].startswith("ConnectError:")


@pytest.mark.asyncio
async def test_version_answer_without_a_version_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_login(mock)
        mock.get(_VERSION).respond(200, json={"releaseName": "no version key"})
        mock.get(_REFERENCE).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    body = result.result
    assert body["version"] is None
    assert body["build"] is None
    assert body["version_error"] == "version answer names no version"


# ---------------------------------------------------------------------------
# Secrets and registration shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_holds_no_password_or_session_token(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(_REFERENCE).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_REFERENCE)

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert _PASSWORD not in rendered
    assert _SESSION_ID not in rendered
    assert "Bearer " not in rendered


@pytest.mark.parametrize(
    ("impl_id", "registrar"),
    [
        (VRLI_IMPL_ID, register_vrli_typed_operations),
        (VRLI8_IMPL_ID, register_vrli8_typed_operations),
    ],
)
@pytest.mark.asyncio
async def test_registered_op_is_typed_safe_and_documented_on_both_implementations(
    _stub_embedding: AsyncMock, session: AsyncSession, impl_id: str, registrar: Any
) -> None:
    await registrar()
    row = (
        await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == VRLI_PRODUCT,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id == "vrli.api_file.get",
            )
        )
    ).scalar_one()
    assert "vrli.api_file.get" in {op.op_id for op in VRLI_TYPED_OPS}
    assert row.source_kind == "typed"
    assert row.handler_ref is not None
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert "read-only" in row.tags
    assert row.parameter_schema["additionalProperties"] is False
    assert row.parameter_schema["examples"] == [{"path": "<path>"}]
    assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
    assert "\n" not in row.summary and len(row.description) <= 300
