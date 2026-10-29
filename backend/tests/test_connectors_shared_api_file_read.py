# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the shared API file read steps (:mod:`connectors._shared.api_file_read`).

The steps are called directly, with an ``httpx.MockTransport``:

* the body is read once, redirects are not followed, and ``gzip`` /
  ``deflate`` bodies are decoded in bounded steps: a small compressed
  body that decodes past the limit stops at the limit, several gzip
  members decode whole, a cut stream or bytes after a deflate stream stop
  with an error, and an encoding the read cannot bound is refused;
* the parts keep characters whole and join back to the file;
* text checks, the stored-record check, the one redaction pass over the
  whole text, the redaction check and the redirect path;
* the steps every read starts and ends with: the flight recorder gets
  the checked copy only after the last checks pass, no body for a
  refused file, and a ``401`` is raised with a checked body or none,
  also when its body cannot be read.

Fixture values are made up.
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import yaml

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.api_file_read import (
    PART_BYTES,
    ApiFileNotTextError,
    ApiFileRedactedError,
    ApiFileTooLargeError,
    FetchedApiFile,
    assert_fits_result_handle,
    assert_not_redacted,
    decode_text,
    fetch_checked_api_file,
    fetch_without_redirects,
    finish_api_file_read,
    redirect_target,
    split_parts,
    text_items,
    text_redactor,
)
from meho_backplane.connectors._shared.api_file_secrets import (
    REDACTED,
    ApiFileFormatError,
    check_api_file,
)
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.settings import get_settings

_BASE = "https://files.test.invalid"


class _ChunkedStream(httpx.AsyncByteStream):
    """A wire body that arrives in fixed chunks, as a server would send it."""

    def __init__(self, data: bytes, chunk: int) -> None:
        self._data = data
        self._chunk = chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._data), self._chunk):
            yield self._data[start : start + self._chunk]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _wire(status: int, data: bytes = b"", headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body is still to be streamed, as the transport gives it."""
    return httpx.Response(status, stream=_ChunkedStream(data, 1024), headers=headers)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_BASE, transport=httpx.MockTransport(handler))


async def _fetch(response: httpx.Response, *, max_bytes: int = 4096) -> httpx.Response:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    async with _client(handler) as client:
        answer = await fetch_without_redirects(
            client, "/file.yaml", headers={"x-made-up": "1"}, extensions={}, max_bytes=max_bytes
        )
    assert len(seen) == 1
    assert seen[0].headers["accept-encoding"] == "gzip, deflate"
    assert seen[0].headers["x-made-up"] == "1"
    return answer


def _operator() -> Operator:
    return Operator(
        sub="op-files",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e0"),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_plain_body_comes_back_whole() -> None:
    answer = await _fetch(_wire(200, b"openapi: 3.0.1\n"))
    assert answer.status_code == 200
    assert answer.content == b"openapi: 3.0.1\n"


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    answer = await _fetch(_wire(302, headers={"Location": "/elsewhere"}))
    assert answer.status_code == 302
    assert answer.headers["location"] == "/elsewhere"


@pytest.mark.asyncio
async def test_plain_body_over_the_limit_stops() -> None:
    with pytest.raises(ApiFileTooLargeError, match="file of 5000 bytes"):
        await _fetch(_wire(200, b"x" * 5000, headers={"Content-Length": "5000"}))
    with pytest.raises(ApiFileTooLargeError, match="more than 4096 bytes"):
        await _fetch(_wire(200, b"x" * 5000))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "compress"), [("gzip", gzip.compress), ("deflate", zlib.compress)]
)
async def test_compressed_body_is_measured_after_decoding(encoding: str, compress: Any) -> None:
    small = b"y" * 1000
    answer = await _fetch(_wire(200, compress(small), headers={"Content-Encoding": encoding}))
    assert answer.content == small
    assert "content-encoding" not in answer.headers

    bomb = compress(b"x" * 32 * 1024 * 1024)
    with pytest.raises(ApiFileTooLargeError, match="more than 4096 bytes"):
        await _fetch(
            httpx.Response(
                200, stream=_ChunkedStream(bomb, 4096), headers={"Content-Encoding": encoding}
            )
        )


@pytest.mark.asyncio
async def test_gzip_body_of_several_members_decodes_whole() -> None:
    wire = gzip.compress(b"first ") + gzip.compress(b"second")
    answer = await _fetch(
        httpx.Response(200, stream=_ChunkedStream(wire, 7), headers={"Content-Encoding": "gzip"})
    )
    assert answer.content == b"first second"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "wire"),
    [
        ("gzip", gzip.compress(b"c" * 3000)[:-12]),
        ("deflate", zlib.compress(b"c" * 3000)[:-4]),
        ("deflate", zlib.compress(b"ok") + b"tail"),
    ],
)
async def test_cut_or_overrun_compressed_stream_stops(encoding: str, wire: bytes) -> None:
    with pytest.raises(httpx.DecodingError):
        await _fetch(
            httpx.Response(
                200, stream=_ChunkedStream(wire, 5), headers={"Content-Encoding": encoding}
            )
        )


@pytest.mark.asyncio
async def test_encoding_the_read_cannot_bound_is_refused() -> None:
    with pytest.raises(ApiFileNotTextError, match="'br'"):
        await _fetch(_wire(200, b"\x0b\x00", headers={"Content-Encoding": "br"}))


def test_split_parts_keeps_characters_whole_and_round_trips() -> None:
    data = ("é" * 40_000 + "a" + "€" * 30_000).encode()
    parts = split_parts(data)
    assert b"".join(parts) == data
    assert all(len(part) <= PART_BYTES for part in parts)
    for part in parts:
        part.decode("utf-8")
    assert split_parts(b"") == []
    items = text_items(data)
    assert [item["index"] for item in items] == list(range(len(parts)))
    assert "".join(item["text"] for item in items).encode() == data


def test_decode_text_refuses_what_is_not_utf8_text() -> None:
    assert decode_text(httpx.Response(200, content=b"a: 1"), "/f.yaml") == "a: 1"
    with pytest.raises(ApiFileNotTextError, match="image/png"):
        decode_text(
            httpx.Response(200, content=b"\x89PNG", headers={"Content-Type": "image/png"}), "/f"
        )
    with pytest.raises(ApiFileNotTextError, match="text/plain"):
        decode_text(
            httpx.Response(200, content=b"\xff\xfe", headers={"Content-Type": "text/plain"}), "/f"
        )


def test_stored_record_that_does_not_fit_stops_with_the_size() -> None:
    data = b'"' * 990
    answer = {"path": "/f.json", "size": len(data)}
    with pytest.raises(ApiFileTooLargeError, match="file of 990 bytes"):
        assert_fits_result_handle(
            answer, text_items(data), operator_sub="op-files", op_id="made.up.get", max_bytes=1000
        )
    assert_fits_result_handle(
        answer, text_items(b"x" * 10), operator_sub="op-files", op_id="made.up.get", max_bytes=1000
    )


def test_text_the_redaction_rules_would_change_stops_without_the_value() -> None:
    clean = {"path": "/f.yaml", "items": [{"index": 0, "text": "openapi: 3.0.1\n"}]}
    assert_not_redacted(
        clean, "openapi: 3.0.1\n", _operator(), connector_id="made-rest-1.0", op_id="made.up.get"
    )
    text = "Authorization: Bearer made-up-token-value-0123456789\n"
    answer = {"path": "/f.yaml", "items": [{"index": 0, "text": text}]}
    with pytest.raises(ApiFileRedactedError) as caught:
        assert_not_redacted(
            answer, text, _operator(), connector_id="made-rest-1.0", op_id="made.up.get"
        )
    assert "strip-authorization-header" in str(caught.value)
    assert "made-up-token-value" not in str(caught.value)


def test_text_redactor_replaces_matches_in_one_value_and_counts_them() -> None:
    rule = text_redactor(_operator(), connector_id="made-rest-1.0", op_id="made.up.get")
    assert rule("openapi: 3.0.1\n") == ("openapi: 3.0.1\n", 0)
    text = (
        "Example:\nAuthorization: Basic bWFkZS11cDpleGFtcGxl\n"
        "Also: Authorization: Bearer made-up-token-value-0123456789\n"
    )
    redacted, count = rule(text)
    assert count == 2
    assert redacted.count("[REDACTED:authorization_header]") == 2
    assert "bWFkZS11cDpleGFtcGxl" not in redacted
    assert "made-up-token-value" not in redacted
    # The redacted copy passes the final check, so the read returns it.
    answer = {"path": "/f.yaml", "items": text_items(redacted.encode())}
    assert_not_redacted(
        answer, redacted, _operator(), connector_id="made-rest-1.0", op_id="made.up.get"
    )


def test_text_redactor_as_the_text_rule_keeps_the_file_format() -> None:
    """A plain YAML scalar that becomes a placeholder is written back quoted."""
    rule = text_redactor(_operator(), connector_id="made-rest-1.0", op_id="made.up.get")
    document = "info:\n  note: Bearer made-up-token-value-0123456789\n  count: 3\n"
    checked = check_api_file(
        document, content_type="application/yaml", path="/f.yaml", text_rule=rule
    )
    assert checked.secrets_replaced == 1
    assert yaml.safe_load(checked.text) == {"info": {"note": "[REDACTED:bearer_token]", "count": 3}}


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (None, None),
        ("/next.yaml", "/next.yaml"),
        ("next.yaml?group=a", "/dir/next.yaml?group=a"),
        ("https://other.test.invalid/x?y=1#z", "https://other.test.invalid/x"),
    ],
)
def test_redirect_target_shapes(location: str | None, expected: str | None) -> None:
    assert redirect_target(location, httpx.URL(f"{_BASE}/dir/file.yaml")) == expected


# ---------------------------------------------------------------------------
# fetch_checked_api_file: what the flight recorder and a raised 401 carry
# ---------------------------------------------------------------------------

_SECRET = "made-up-recorder-secret"
_SECRET_JSON = b'{"swagger": "2.0", "x-example": {"password": "made-up-recorder-secret"}}'
# A credential inside a text value under a key that is not a secret name. The
# JSON escape is a literal backslash-n on the wire; only the parsed value
# holds the line break the header rule crosses.
_CREDENTIAL_IN_TEXT = b'{"detail": "Authorization:\\nBasic made-up-recorder-secret"}'
_REDACTED_HEADER = b"[REDACTED:authorization_header]"
_JSON = {"Content-Type": "application/json"}


def _recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        flight_recorder_capture,
        "record_vendor_call",
        lambda _span, **kwargs: recorded.append(kwargs),
    )
    return recorded


async def _fetch_checked(response: httpx.Response) -> FetchedApiFile:
    rule = text_redactor(_operator(), connector_id="made-rest-1.0", op_id="made.up.get")
    async with _client(lambda _request: response) as client:
        return await fetch_checked_api_file(
            client,
            "/file.json?group=a",
            path="/file.json",
            headers={"x-made-up": "1"},
            extensions={},
            max_bytes=4096,
            text_rule=None,
            error_text_rule=rule,
        )


@pytest.mark.asyncio
async def test_fetch_checked_records_nothing_until_the_read_records_the_checked_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _recorded_calls(monkeypatch)
    fetched = await _fetch_checked(_wire(200, _SECRET_JSON, headers=_JSON))

    assert fetched.response.status_code == 200
    assert REDACTED in fetched.checked.text
    assert recorded == []
    fetched.record(fetched.checked.text.encode())
    (call,) = recorded
    assert call["method"] == "GET"
    assert call["request_headers"] == {"x-made-up": "1"}
    assert call["response"] is fetched.response
    assert call["response_body"] == fetched.checked.text.encode()
    assert _SECRET.encode() not in call["response_body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "headers", "error"),
    [
        (_SECRET_JSON[:-2], _JSON, ApiFileFormatError),
        (b"\x89PNG", {"Content-Type": "image/png"}, ApiFileNotTextError),
    ],
)
async def test_fetch_checked_gives_the_recorder_no_body_for_a_refused_file(
    monkeypatch: pytest.MonkeyPatch, body: bytes, headers: dict[str, str], error: type[Exception]
) -> None:
    recorded = _recorded_calls(monkeypatch)
    with pytest.raises(error):
        await _fetch_checked(_wire(200, body, headers=headers))

    (call,) = recorded
    assert call["response"].status_code == 200
    assert call["response_body"] == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "headers", "expected"),
    [
        (_SECRET_JSON, _JSON, REDACTED.encode()),
        (_CREDENTIAL_IN_TEXT, _JSON, _REDACTED_HEADER),
        (b"password=made-up-recorder-secret", {"Content-Type": "text/plain"}, b""),
        (b"\xff\xfe", {"Content-Type": "application/octet-stream"}, b""),
        # A lone surrogate parses; the check withholds that value, the rest stays.
        (b'{"message": "\\ud800"}', _JSON, REDACTED.encode()),
        # A number longer than Python parses from text stops the JSON parse.
        (b'{"n": ' + b"9" * 4500 + b"}", _JSON, b""),
    ],
)
async def test_fetch_checked_raises_a_401_with_a_checked_body_or_none(
    monkeypatch: pytest.MonkeyPatch, body: bytes, headers: dict[str, str], expected: bytes
) -> None:
    """The status and the headers stay, so the dispatcher's login retry still runs."""
    recorded = _recorded_calls(monkeypatch)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _fetch_checked(_wire(401, body, headers={**headers, "x-vendor": "kept"}))

    raised = caught.value.response
    assert raised.status_code == 401
    assert raised.headers["x-vendor"] == "kept"
    assert raised.request.url.path == "/file.json"
    if expected:
        assert expected in raised.content
    else:
        assert raised.content == b""
    assert _SECRET.encode() not in raised.content
    (call,) = recorded
    assert call["response"].status_code == 401
    assert call["response_body"] == raised.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "body", "headers"),
    [
        ("declared over the limit", b"x" * 5000, {"Content-Length": "5000"}),
        ("streamed over the limit", b"x" * 5000, {}),
        ("encoding the read cannot bound", b"x", {"Content-Encoding": "br"}),
        ("cut gzip stream", gzip.compress(b"x" * 100)[:-8], {"Content-Encoding": "gzip"}),
        (
            "bytes after the deflate stream",
            zlib.compress(b"x") + b"tail",
            {"Content-Encoding": "deflate"},
        ),
    ],
)
async def test_401_whose_body_cannot_be_read_comes_back_with_no_body(
    name: str, body: bytes, headers: dict[str, str]
) -> None:
    """The dispatcher must see the ``401``, whatever the body holds."""
    answer = await _fetch(_wire(401, body, headers={**headers, "x-vendor": "kept"}))

    assert answer.status_code == 401, name
    assert answer.content == b""
    assert answer.headers["x-vendor"] == "kept"
    assert "content-encoding" not in answer.headers
    assert "content-length" not in answer.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "body", "headers"),
    [
        ("streamed over the limit", b"x" * 5000, {}),
        ("encoding the read cannot bound", b"x", {"Content-Encoding": "br"}),
        ("cut gzip stream", gzip.compress(b"x" * 100)[:-8], {"Content-Encoding": "gzip"}),
    ],
)
async def test_fetch_checked_raises_a_401_whose_body_cannot_be_read_with_no_body(
    monkeypatch: pytest.MonkeyPatch, name: str, body: bytes, headers: dict[str, str]
) -> None:
    recorded = _recorded_calls(monkeypatch)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _fetch_checked(_wire(401, body, headers={**headers, **_JSON}))

    raised = caught.value.response
    assert raised.status_code == 401, name
    assert raised.content == b""
    (call,) = recorded
    assert call["response"].status_code == 401
    assert call["response_body"] == b""


def _answer_for(body: bytes, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "path": "/file.json",
        "status": 200,
        "size": len(body),
        "sha256": "0" * 64,
        "version": None,
        "parts": len(items),
    }


@pytest.mark.asyncio
async def test_finish_records_the_checked_copy_only_when_the_last_checks_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size boundary: the file fits the download limit but not the stored record."""
    recorded = _recorded_calls(monkeypatch)
    fetched = await _fetch_checked(_wire(200, _SECRET_JSON, headers=_JSON))
    body = fetched.checked.text.encode()
    items = text_items(body)
    assert recorded == []
    operator = _operator()
    common: dict[str, Any] = {"connector_id": "made-rest-1.0", "op_id": "made.up.get"}

    with pytest.raises(ApiFileTooLargeError):
        finish_api_file_read(
            fetched, _answer_for(body, items), items, operator, max_bytes=len(body), **common
        )
    (refused,) = recorded
    assert refused["response_body"] == b""

    answer = finish_api_file_read(
        fetched, _answer_for(body, items), items, operator, max_bytes=4096, **common
    )
    assert answer["items"] == items
    assert [call["response_body"] for call in recorded] == [b"", body]
    assert _SECRET.encode() not in body


@pytest.mark.asyncio
async def test_finish_records_no_body_when_the_redaction_check_refuses_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _recorded_calls(monkeypatch)
    wire = b'{"detail": "Authorization: Bearer made-up-token-value-0123456789"}'
    fetched = await _fetch_checked(_wire(200, wire, headers=_JSON))
    body = fetched.checked.text.encode()
    items = text_items(body)

    with pytest.raises(ApiFileRedactedError):
        finish_api_file_read(
            fetched,
            _answer_for(body, items),
            items,
            _operator(),
            connector_id="made-rest-1.0",
            op_id="made.up.get",
            max_bytes=4096,
        )
    (refused,) = recorded
    assert refused["response_body"] == b""
