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
* text checks, the stored-record check, the redaction check and the
  redirect path.

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

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.api_file_read import (
    PART_BYTES,
    ApiFileNotTextError,
    ApiFileRedactedError,
    ApiFileTooLargeError,
    assert_fits_result_handle,
    assert_not_redacted,
    decode_text,
    fetch_without_redirects,
    redirect_target,
    split_parts,
    text_items,
)
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
