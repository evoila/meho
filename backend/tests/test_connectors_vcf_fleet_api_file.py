# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``fleet.api_file.get`` read of the vcf-fleet (``/lcm``) connector.

What is covered, with a respx-mocked appliance and no ingested catalog:

* an accepted path: status, content type, size, SHA-256, version, build,
  the ``Lcm-API-Version`` header and the parts, with HTTP Basic on the read;
* refused paths: no HTTP request goes out;
* a 404 reaches the answer; a redirect is not followed, and a relative
  ``Location`` is resolved against the path that was read;
* a 401 on the read is an error, and the appliance is not asked again with
  the same login;
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
* a file with a password comes back with the placeholder and the count; a
  file that does not parse is refused;
* a page the secret-redaction rules would change stops with an error, also
  when the secret's label ends one part and its value starts the next;
* content that is not text is refused with its content type;
* the version read fails (the VCF 9.0 HTTP 500) and the file still comes
  back, with the ``Lcm-API-Version`` header and the reason;
* no password in the result; registration-shape invariants;
* the flight recorder gets the checked copy of the file, and no body when
  the file is refused; a 401 body is checked before it is raised, and the
  error result and the audit row hold no secret.

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
from meho_backplane.connectors._shared.api_file_read import (
    PART_BYTES,
    ApiFileTooLargeError,
    assert_fits_result_handle,
    split_parts,
)
from meho_backplane.connectors._shared.api_file_secrets import REDACTED
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vcf_fleet import (
    FLEET_CONNECTOR_ID,
    FLEET_IMPL_ID,
    FLEET_PRODUCT,
    FLEET_TYPED_OPS,
    FLEET_VERSION,
    VcfFleetConnector,
)
from meho_backplane.connectors.vcf_fleet.api_file import (
    DOCS_FOLDERS,
    DOCS_PAGES,
    FleetApiFilePathError,
    check_path,
)
from meho_backplane.connectors.vcf_fleet.version import parse_appliance_version
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction.middleware import apply_connector_boundary_redaction
from meho_backplane.settings import get_settings

_HOST = "fleet-docs.test.invalid"
_BASE_URL = f"https://{_HOST}"
_ABOUT = "/lcm/lcops/api/v2/about"
_LIFECYCLE_DOCS = "/api/swagger-ui/index.html"
_CMS_DOCS = "/lcm/cms/api/swagger-ui/index.html"
_API_DOCS = "/api/v2/api-docs"
_PASSWORD = "docs-password-value"
_ABOUT_PAYLOAD: dict[str, Any] = {
    "apiVersion": "8.0",
    "productVersion": "8.18.0.0",
    "buildNumber": "24001234",
    "releaseDate": "2026-01-01",
}
_LCM_HEADER = {"Lcm-API-Version": "8.0"}
_HTML = "<html><head><title>Lifecycle APIs</title></head><body>docs</body></html>"


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
        product=FLEET_PRODUCT, version=FLEET_VERSION, impl_id=FLEET_IMPL_ID, cls=VcfFleetConnector
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


class _FleetTarget:
    """Target satisfying both ``VcfFleetTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = FLEET_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "fleet-docs"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/fleet-docs"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-fleet",
        name="Fleet Reads Operator",
        email=None,
        raw_jwt="op.reads.fleet.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


class _CredsLoader:
    """Stub HTTP Basic credentials; counts how often the connector asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _target: object, _operator: Operator) -> dict[str, str]:
        self.calls += 1
        return {"username": "admin@local", "password": _PASSWORD}


async def _register_and_resolve(
    _stub_embedding: AsyncMock,
) -> tuple[VcfFleetConnector, _CredsLoader]:
    await VcfFleetConnector.register_operations()
    instance = get_or_create_connector_instance(VcfFleetConnector)
    loader = _CredsLoader()
    instance._creds = type(instance._creds)(loader, product_label="fleet")  # type: ignore[attr-defined]
    return instance, loader


def _mock_version(mock: respx.MockRouter) -> respx.Route:
    return mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD, headers=_LCM_HEADER)


async def _dispatch(path: str) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=FLEET_CONNECTOR_ID,
        op_id="fleet.api_file.get",
        target=_FleetTarget(),
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
        about = _mock_version(mock)
        route = mock.get(_LIFECYCLE_DOCS).respond(
            200, text=_HTML, headers={"Content-Type": "text/html", **_LCM_HEADER}
        )
        result = await _dispatch(_LIFECYCLE_DOCS)

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert about.call_count == 1
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")
    body = result.result
    assert body["path"] == _LIFECYCLE_DOCS
    assert body["status"] == 200
    assert body["content_type"] == "text/html"
    assert body["size"] == len(_HTML.encode())
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] == "8.18.0.0"
    assert body["build"] == "24001234"
    assert body["version_error"] is None
    assert body["lcm_api_version"] == "8.0"
    assert body["redirect_to"] is None
    assert body["parts"] == 1
    assert body["items"] == [{"index": 0, "text": _HTML}]


@pytest.mark.asyncio
async def test_query_string_with_allowed_keys_is_sent(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        route = mock.get(_API_DOCS).respond(200, json={"swagger": "2.0"})
        result = await _dispatch(f"{_API_DOCS}?group=lcops&apiVersion=2")

    assert result.status == "ok", result.error
    sent = route.calls[0].request.url
    assert sent.params["group"] == "lcops"
    assert sent.params["apiVersion"] == "2"
    assert result.result["path"] == f"{_API_DOCS}?group=lcops&apiVersion=2"


# ---------------------------------------------------------------------------
# Refused paths: no HTTP request goes out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../api/swagger-ui/",
        "/api/swagger-ui/../../lcm/lcops/api/v2/environments",
        "//other-host.test.invalid/api/swagger-ui/",
        "https://other-host.test.invalid/api/swagger-ui/",
        "other-host.test.invalid/api/swagger-ui/",
        "/api/swagger-ui/%2e%2e/environments",
        "/api/swagger-ui%2f..%2fx",
        "/api/swagger-ui/\\x",
        "/api/swagger-ui/#frag",
        "/api/swagger-ui/page;x",
        "/lcm/lcops/api/v2/environments;swagger.json",
        "/lcm/lcops/api/v2/environments%3Bswagger.json",
        "/api/swagger-ui/?foo=1",
        "/api/swagger-ui/?group=a&token=b",
        "/api/swagger-ui",
        "/api/swagger-ui.htmlx",
        "/lcm/cms/api/swagger-ui",
        "/lcm/lcops/api/v2/environments",
        "/lcm/lcops/api/v2/about",
        "/lcm/locker/api/v2/passwords",
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
        assert not mock.calls, f"path {path!r} reached the appliance"

    assert result.status == "error"
    assert result.error


def test_check_path_accepts_the_documented_shapes() -> None:
    for page in DOCS_PAGES:
        assert check_path(page) == (page, "")
    for folder in DOCS_FOLDERS:
        assert check_path(folder) == (folder, "")
        assert check_path(f"{folder}index.html") == (f"{folder}index.html", "")
    assert check_path("/api/v2/api-docs?group=x") == ("/api/v2/api-docs", "group=x")
    assert check_path("/lcm/cms/api/v2/api-docs") == ("/lcm/cms/api/v2/api-docs", "")
    assert check_path("/api/swagger-resources") == ("/api/swagger-resources", "")
    assert check_path("/api/swagger-config") == ("/api/swagger-config", "")
    assert check_path("/docs/spec/openapi.yaml") == ("/docs/spec/openapi.yaml", "")
    with pytest.raises(FleetApiFilePathError):
        check_path("/api/swagger-ui/?group=a&group=b&other=c")


# ---------------------------------------------------------------------------
# Status, redirect, 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_status_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/missing.json").respond(404, json={"error": "not found"})
        result = await _dispatch("/api/swagger-ui/missing.json")

    assert result.status == "ok", result.error
    assert result.result["status"] == 404
    assert result.result["items"] == [{"index": 0, "text": '{"error":"not found"}'}]


@pytest.mark.asyncio
async def test_redirect_is_reported_and_not_followed(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        first = mock.get("/api/swagger-ui.html").respond(
            301, headers={"Location": f"{_BASE_URL}{_LIFECYCLE_DOCS}"}
        )
        second = mock.get(_LIFECYCLE_DOCS).respond(
            200, text=_HTML, headers={"Content-Type": "text/html"}
        )
        result = await _dispatch("/api/swagger-ui.html")

    assert result.status == "ok", result.error
    assert first.call_count == 1
    assert second.call_count == 0
    assert result.result["status"] == 301
    assert result.result["redirect_to"] == _LIFECYCLE_DOCS


@pytest.mark.parametrize(
    ("read_path", "location", "expected"),
    [
        (_LIFECYCLE_DOCS, "swagger.json", "/api/swagger-ui/swagger.json"),
        (_API_DOCS, "?group=lcops", f"{_API_DOCS}?group=lcops"),
        (_LIFECYCLE_DOCS, "../v2/api-docs", "/api/v2/api-docs"),
        (_CMS_DOCS, "/lcm/cms/api/swagger-ui/", "/lcm/cms/api/swagger-ui/"),
        (
            _LIFECYCLE_DOCS,
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


@pytest.mark.asyncio
async def test_401_on_the_read_is_an_error_and_drops_the_cached_login(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    _, loader = await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        docs = mock.get(_LIFECYCLE_DOCS).respond(401, json={"error": "Bad credentials"})
        result = await _dispatch(_LIFECYCLE_DOCS)

    assert result.status == "error"
    assert "401" in (result.error or "")
    assert docs.call_count >= 1
    # The login was read for the first try and again after the 401 dropped it.
    assert loader.calls == docs.call_count


# ---------------------------------------------------------------------------
# Big files: the joined parts give the checked copy
# ---------------------------------------------------------------------------


def _minified_json_of_a_few_mb() -> bytes:
    rows = [
        {"path": f"/lcm/lcops/api/v2/resource-{i}", "summary": "made-up summary " * 8, "n": i}
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
    target = _FleetTarget()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/big.json").respond(
            200, content=data, headers={"Content-Type": content_type}
        )
        answer = await instance.api_file_get(
            _make_operator(), target, {"path": "/api/swagger-ui/big.json"}
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
        mock.get("/api/swagger-ui/big.json").respond(
            200, content=data, headers={"Content-Type": "application/json"}
        )
        result = await _dispatch("/api/swagger-ui/big.json")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["sha256"] == hashlib.sha256(data).hexdigest()
    assert summary["size"] == len(data)
    assert summary["parts"] == summary["row_count"]
    assert summary["version"] == "8.18.0.0"


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
        mock.get("/api/swagger-ui/declared.json").respond(200, content=data)
        mock.get("/api/swagger-ui/chunked.json").respond(200, content=_chunks(data))
        declared = await _dispatch("/api/swagger-ui/declared.json")
        chunked = await _dispatch("/api/swagger-ui/chunked.json")

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
        mock.get("/api/swagger-ui/quotes.json").respond(200, content=data)
        result = await _dispatch("/api/swagger-ui/quotes.json")

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
        mock.get("/api/swagger-ui/small.json").respond(
            200,
            content=gzip.compress(small),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        mock.get("/api/swagger-ui/bomb.json").respond(
            200,
            content=gzip.compress(bomb),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
        fits = await _dispatch("/api/swagger-ui/small.json")
        too_big = await _dispatch("/api/swagger-ui/bomb.json")

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
        mock.get("/api/swagger-ui/bomb.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(wire, 4096),
                headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
            )
        )
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            result = await _dispatch("/api/swagger-ui/bomb.json")
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
        route = mock.get("/api/swagger-ui/br.json").respond(
            200, content=b"\x0b\x00\x80", headers={"Content-Encoding": "br"}
        )
        result = await _dispatch("/api/swagger-ui/br.json")

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
        mock.get("/api/swagger-ui/joined.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(joined, 7),
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        )
        mock.get("/api/swagger-ui/over.json").respond(
            200, content=over, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"}
        )
        result = await _dispatch("/api/swagger-ui/joined.json")
        too_big = await _dispatch("/api/swagger-ui/over.json")

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
        mock.get("/api/swagger-ui/broken.json").mock(
            return_value=httpx.Response(
                200,
                stream=_ChunkedStream(wire, 5),
                headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
            )
        )
        result = await _dispatch("/api/swagger-ui/broken.json")

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
    answer = {
        "path": "/api/swagger-ui/fit.json",
        "status": 200,
        "content_type": "application/json",
        "size": 0,
        "sha256": "0" * 64,
        "version": "8.18.0.0",
        "build": "24001234",
        "version_error": None,
        "lcm_api_version": None,
        "redirect_to": None,
        "parts": 0,
        "secrets_replaced": 0,
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
                operator_sub="op-reads-fleet",
                op_id="fleet.api_file.get",
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
    target = _FleetTarget()
    operator = _make_operator()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/fit.json").respond(
            200, content=data, headers={"Content-Type": "application/json"}
        )
        mock.get("/api/swagger-ui/over.json").respond(
            200, content=data[:-1] + b"x" * 200 + b'"', headers={"Content-Type": "application/json"}
        )
        result = await dispatch(
            operator=operator,
            connector_id=FLEET_CONNECTOR_ID,
            op_id="fleet.api_file.get",
            target=target,
            params={"path": "/api/swagger-ui/fit.json"},
        )
        over = await _dispatch("/api/swagger-ui/over.json")

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
        "<pre>GET /lcm/lcops/api/v2/environments HTTP/1.1\n"
        "Authorization: Bearer example-token-value-0123456789\n</pre>"
    )
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/auth.html").respond(
            200, text=page, headers={"Content-Type": "text/html"}
        )
        result = await _dispatch("/api/swagger-ui/auth.html")

    assert result.status == "error"
    message = result.extras["exception_message"]
    assert "strip-authorization-header" in message
    assert "1 place(s)" in message
    assert "the exact file cannot be returned" in message
    assert "example-token-value" not in json.dumps(result.result) + json.dumps(
        result.extras, default=str
    )


@pytest.mark.asyncio
async def test_secret_that_crosses_a_part_boundary_stops_with_an_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """``password=`` ends the first part and the value starts the second."""
    await _register_and_resolve(_stub_embedding)
    set_default_reducer(JsonFluxReducer(store=ResultHandleStore(_FakeValkey())))  # type: ignore[arg-type]
    label = b" password="
    page = b"x" * (PART_BYTES - len(label)) + label + b"example-secret-value-0123456789 tail\n"
    parts = [part.decode() for part in split_parts(page)]
    assert parts[0].endswith("password=")
    assert parts[1].startswith("example-secret-value")
    for index, text in enumerate(parts):
        alone = apply_connector_boundary_redaction(
            {"index": index, "text": text}, connector_id=FLEET_CONNECTOR_ID, tenant=None, op=None
        )
        assert alone.redacted == alone.raw, "a part alone must not match; the gap is real"

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/split.html").respond(
            200, content=page, headers={"Content-Type": "text/html"}
        )
        result = await _dispatch("/api/swagger-ui/split.html")

    assert result.status == "error"
    message = result.extras["exception_message"]
    assert "the exact file cannot be returned" in message
    assert "1 place(s)" in message
    assert "example-secret-value" not in json.dumps(result.result) + json.dumps(
        result.extras, default=str
    )


@pytest.mark.asyncio
async def test_file_with_a_password_comes_back_with_the_placeholder_and_the_count(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The file is parsed; every value under a secret name is replaced before the parts are cut."""
    await _register_and_resolve(_stub_embedding)
    spec = {
        "swagger": "2.0",
        "paths": {"/login": {"post": {"x-example": {"user": "admin", "password": "made-up-1"}}}},
        "definitions": {"Login": {"properties": {"password": {"type": "string"}}}},
    }
    copy = {
        "swagger": "2.0",
        "paths": {"/login": {"post": {"x-example": {"user": "admin", "password": REDACTED}}}},
        "definitions": {"Login": {"properties": {"password": REDACTED}}},
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/login.json").respond(
            200, text=json.dumps(spec, indent=2), headers={"Content-Type": "application/json"}
        )
        result = await _dispatch("/api/swagger-ui/login.json")

    assert result.status == "ok", result.error
    body = result.result
    joined = "".join(item["text"] for item in body["items"])
    assert json.loads(joined) == copy
    assert body["secrets_replaced"] == 2
    assert body["size"] == len(joined.encode())
    assert body["sha256"] == hashlib.sha256(joined.encode()).hexdigest()
    assert "made-up-1" not in json.dumps(body)


@pytest.mark.asyncio
async def test_file_that_does_not_parse_is_refused_with_the_reason(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/broken.json").respond(
            200, text='{"password": "made-up-2"', headers={"Content-Type": "application/json"}
        )
        mock.get("/api/swagger-ui/app.js").respond(
            200, text='var password = "made-up-3";', headers={"Content-Type": "text/javascript"}
        )
        broken = await _dispatch("/api/swagger-ui/broken.json")
        script = await _dispatch("/api/swagger-ui/app.js")

    assert broken.status == "error"
    assert "not well-formed JSON" in broken.extras["exception_message"]
    assert script.status == "error"
    assert "no known format" in script.extras["exception_message"]
    for result in (broken, script):
        rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
        assert "made-up-" not in rendered


@pytest.mark.asyncio
async def test_non_text_content_is_refused_naming_the_content_type(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get("/api/swagger-ui/logo.openapi.json").respond(
            200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"}
        )
        mock.get("/api/swagger-ui/bad.json").respond(
            200, content=b"\xff\xfe\x00bad", headers={"Content-Type": "text/plain"}
        )
        png = await _dispatch("/api/swagger-ui/logo.openapi.json")
        bad = await _dispatch("/api/swagger-ui/bad.json")

    assert png.status == "error" and "image/png" in png.extras["exception_message"]
    assert bad.status == "error" and "text/plain" in bad.extras["exception_message"]


@pytest.mark.asyncio
async def test_version_read_500_still_returns_the_file_with_the_header(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The VCF 9.0 case: about answers 500 once, the file still comes back."""
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        about = mock.get(_ABOUT).respond(500, json={"errorCode": "LCMCOMMON", "errorMessage": "x"})
        mock.get(_LIFECYCLE_DOCS).respond(
            200, text=_HTML, headers={"Content-Type": "text/html", **_LCM_HEADER}
        )
        result = await _dispatch(_LIFECYCLE_DOCS)

    assert result.status == "ok", result.error
    assert about.call_count == 1
    body = result.result
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] is None
    assert body["build"] is None
    assert "500" in body["version_error"]
    assert body["lcm_api_version"] == "8.0"


@pytest.mark.asyncio
async def test_version_read_bad_gzip_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A version body that fails to decode raises inside ``client.get``;
    the file still comes back."""
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        about = mock.get(_ABOUT).respond(
            200,
            stream=_ChunkedStream(b"\x1f\x8b\x08\x00not-gzip-at-all", 4096),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        mock.get(_LIFECYCLE_DOCS).respond(
            200, text=_HTML, headers={"Content-Type": "text/html", **_LCM_HEADER}
        )
        result = await _dispatch(_LIFECYCLE_DOCS)

    assert result.status == "ok", result.error
    assert about.call_count == 1
    body = result.result
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] is None
    assert body["build"] is None
    assert body["version_error"].startswith("DecodingError:")
    assert body["lcm_api_version"] == "8.0"


@pytest.mark.asyncio
async def test_version_read_redirect_loop_still_returns_the_file(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A version endpoint that redirects to itself raises inside ``client.get``;
    the file still comes back."""
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        about = mock.get(_ABOUT).respond(302, headers={"Location": _ABOUT})
        mock.get(_LIFECYCLE_DOCS).respond(
            200, text=_HTML, headers={"Content-Type": "text/html", **_LCM_HEADER}
        )
        result = await _dispatch(_LIFECYCLE_DOCS)

    assert result.status == "ok", result.error
    assert about.call_count > 1
    body = result.result
    assert body["sha256"] == hashlib.sha256(_HTML.encode()).hexdigest()
    assert body["version"] is None
    assert body["build"] is None
    assert body["version_error"].startswith("TooManyRedirects:")
    assert body["lcm_api_version"] == "8.0"


@pytest.mark.parametrize(
    ("payload", "version", "build"),
    [
        (_ABOUT_PAYLOAD, "8.18.0.0", "24001234"),
        ({"productVersion": "8.16.0.0", "buildNumber": 23000001}, "8.16.0.0", "23000001"),
        ({"productVersion": "8.14.0.0"}, "8.14.0.0", None),
        ({"apiVersion": "8.0"}, None, None),
        ([], None, None),
    ],
)
def test_parse_appliance_version_shapes(
    payload: Any, version: str | None, build: str | None
) -> None:
    read = parse_appliance_version(payload)
    assert (read.version, read.build) == (version, build)
    assert (read.error is None) == (version is not None)


# ---------------------------------------------------------------------------
# Secrets and registration shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_holds_no_password(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(_LIFECYCLE_DOCS).respond(200, text=_HTML, headers={"Content-Type": "text/html"})
        result = await _dispatch(_LIFECYCLE_DOCS)

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    assert _PASSWORD not in rendered
    assert "Basic " not in rendered


@pytest.mark.asyncio
async def test_registered_op_is_typed_safe_and_documented(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await VcfFleetConnector.register_operations()
    row = (
        await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == FLEET_PRODUCT,
                EndpointDescriptor.impl_id == FLEET_IMPL_ID,
                EndpointDescriptor.op_id == "fleet.api_file.get",
            )
        )
    ).scalar_one()
    assert "fleet.api_file.get" in {op.op_id for op in FLEET_TYPED_OPS}
    assert row.source_kind == "typed"
    assert row.handler_ref is not None
    assert row.safety_level == "safe"
    assert row.requires_approval is False
    assert "read-only" in row.tags
    assert row.parameter_schema["additionalProperties"] is False
    assert row.parameter_schema["examples"] == [{"path": "<path>"}]
    assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
    assert "\n" not in row.summary and len(row.description) <= 300


# ---------------------------------------------------------------------------
# The flight recorder and the 401 body: only the checked copy leaves the read
# ---------------------------------------------------------------------------

_RECORDER_SECRET = "made-up-recorder-secret"
_SECRET_JSON = json.dumps({"swagger": "2.0", "x-example": {"password": _RECORDER_SECRET}})
_SECRET_FILE = "/api/swagger-ui/secret.json"


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
        _mock_version(mock)
        mock.get(_SECRET_FILE).respond(
            200, text=_SECRET_JSON, headers={"Content-Type": "application/json"}
        )
        result = await _dispatch(_SECRET_FILE)

    assert result.status == "ok", result.error
    (call,) = _file_calls(recorded, _SECRET_FILE)
    assert REDACTED.encode() in call["response_body"]
    assert _RECORDER_SECRET.encode() not in call["response_body"]


@pytest.mark.asyncio
async def test_flight_recorder_gets_no_body_when_the_file_is_refused(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded_calls(monkeypatch)
    await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        mock.get(_SECRET_FILE).respond(
            200, text=_SECRET_JSON[:-2], headers={"Content-Type": "application/json"}
        )
        result = await _dispatch(_SECRET_FILE)

    assert result.status == "error"
    (call,) = _file_calls(recorded, _SECRET_FILE)
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
        _mock_version(mock)
        mock.get("/api/swagger-ui/quotes.json").respond(200, content=data)
        result = await _dispatch("/api/swagger-ui/quotes.json")

    assert result.status == "error"
    assert "does not fit in one result handle" in result.extras["exception_message"]
    (call,) = _file_calls(recorded, "/api/swagger-ui/quotes.json")
    assert call["response"].status_code == 200
    assert call["response_body"] == b""


@pytest.mark.asyncio
async def test_401_body_is_checked_before_it_is_raised_and_the_login_is_dropped(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP Basic login has no session to renew, so a 401 is the answer; the body is checked."""
    recorded = _recorded_calls(monkeypatch)
    _, loader = await _register_and_resolve(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_version(mock)
        docs = mock.get(_SECRET_FILE).respond(
            401, text=_SECRET_JSON, headers={"Content-Type": "application/json"}
        )
        result = await _dispatch(_SECRET_FILE)

    assert result.status == "error"
    assert "401" in (result.error or "")
    assert docs.call_count == 1
    assert loader.calls == docs.call_count
    rendered = (
        (result.error or "") + json.dumps(result.result) + json.dumps(result.extras, default=str)
    )
    assert _RECORDER_SECRET not in rendered
    (call,) = _file_calls(recorded, _SECRET_FILE)
    assert REDACTED.encode() in call["response_body"]
    assert _RECORDER_SECRET.encode() not in call["response_body"]
    payloads = await _audit_payloads("fleet.api_file.get")
    assert payloads
    assert all(_RECORDER_SECRET not in payload for payload in payloads)
