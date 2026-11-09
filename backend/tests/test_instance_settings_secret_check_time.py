# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings secret check takes time in step with the file size.

Also: a string or key longer than the longest settings field is refused
before the secret check reads it.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from meho_backplane.instance_settings import schema
from meho_backplane.instance_settings.schema import (
    MAX_STRING_CHARS,
    find_over_long_strings,
    find_secret_shaped_values,
    parse_settings_file,
)

_HEAD = "apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n    - targets:\n"
_FILE_CHARS = 256 * 1024
_HEADER = "eyJhbGciOiJIUzI1NiJ9"
_PAYLOAD = "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
# Built from parts, so no line of this file holds a whole made-up key.
_KEY_LINE = "PRIVATE " + "KEY-----"
_BEGIN = f"-----BEGIN {_KEY_LINE}"
_END = f"-----END {_KEY_LINE}"
_PEM_KEY = f"{_BEGIN}\nMIIBOgIBAAJBAKj34GkxFhD9\n{_END}"


def _file_of(unit: str) -> bytes:
    """A 256 KiB settings file: target notes made of *unit*, each under the limit."""
    note = unit * ((MAX_STRING_CHARS - 1000) // len(unit))
    targets = []
    for index in range(_FILE_CHARS // len(note) + 1):
        targets.append(
            f"        - name: t{index}\n          product: ssh\n          host: h\n"
            f"          notes: {json.dumps(note)}\n"
        )
    content = (_HEAD + "".join(targets)).encode()
    assert len(content) >= _FILE_CHARS
    return content


_SLOW_SHAPES = [
    pytest.param(_BEGIN, id="begin-lines"),
    pytest.param(f"-----BEGIN RSA {_KEY_LINE}\n", id="begin-lines-with-label"),
    pytest.param("-eyJaaaaaaaa", id="jwt-starts"),
    pytest.param(f" {_HEADER}.{_PAYLOAD}", id="jwt-parts-without-end"),
]


@pytest.mark.parametrize("unit", _SLOW_SHAPES)
def test_a_large_file_of_secret_starts_is_checked_fast(unit: str) -> None:
    content = _file_of(unit)
    started = time.perf_counter()
    parsed = parse_settings_file(content)
    elapsed = time.perf_counter() - started
    assert parsed.mistakes == []
    assert elapsed < 0.5


@pytest.mark.parametrize("unit", _SLOW_SHAPES)
async def test_the_event_loop_stays_free_while_a_large_file_is_checked(unit: str) -> None:
    content = _file_of(unit)
    largest_gap = 0.0
    done = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal largest_gap
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            largest_gap = max(largest_gap, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    try:
        await asyncio.to_thread(parse_settings_file, content)
    finally:
        done.set()
        await beat
    assert largest_gap < 0.1


@pytest.mark.parametrize(
    "value",
    [
        f"{_HEADER}.{_PAYLOAD}.",
        f"x-{_HEADER}.{_PAYLOAD}.sig",
        f"token={_HEADER}.{_PAYLOAD}.sig;",
        f"{'-eyJaaaaaaaa' * 50} {_HEADER}.{_PAYLOAD}.sig",
        _PEM_KEY,
        _BEGIN * 50 + f"\nabc\n{_END}",
        f"-----BEGIN EC {_KEY_LINE}x{_END}",
    ],
)
def test_secret_shapes_are_still_found(value: str) -> None:
    assert len(find_secret_shaped_values({"notes": value})) == 1


@pytest.mark.parametrize(
    "value",
    [
        f"a{_HEADER}.{_PAYLOAD}.sig",
        f"{_HEADER}.{_PAYLOAD}",
        f"{_HEADER}. {_PAYLOAD}.sig",
        f"eyJhbGc.{_PAYLOAD}.sig",
        f"{_HEADER}.eyJzdWIi.sig",
        "eyJub3RoaW5nIjoxfQ.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
        _BEGIN * 50,
        f"{_END}\n{_BEGIN}",
        _BEGIN + _END,
    ],
)
def test_near_misses_pass(value: str) -> None:
    assert find_secret_shaped_values({"notes": value}) == []


def test_a_value_over_the_limit_is_refused_before_the_secret_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(schema, "find_secret_shaped_values", lambda *a: calls.append(a) or [])
    content = (
        _HEAD + "        - name: t\n          product: ssh\n          host: h\n"
        f"          notes: {'a' * (MAX_STRING_CHARS + 1)}\n"
    ).encode()

    parsed = parse_settings_file(content)

    assert parsed.model is None
    assert parsed.mistakes == [
        "`spec.tenants[0].targets[0].notes` is longer than 64,000 characters; "
        "the settings file holds no text that long."
    ]
    assert calls == []


def test_a_key_over_the_limit_is_refused_and_not_shown_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(schema, "find_secret_shaped_values", lambda *a: calls.append(a) or [])
    long_key = "k" * (MAX_STRING_CHARS + 1)
    content = (
        _HEAD + "        - name: t\n          product: ssh\n          host: h\n"
        f"          extras:\n            ? {long_key}\n            : v\n"
    ).encode()

    parsed = parse_settings_file(content)

    assert parsed.model is None
    assert parsed.mistakes == [
        "`spec.tenants[0].targets[0].extras` has a key name longer than 64,000 "
        "characters; the settings file holds no text that long."
    ]
    assert calls == []


def test_a_value_at_the_limit_passes() -> None:
    assert find_over_long_strings({"notes": "a" * MAX_STRING_CHARS, "list": ["b"]}) == []


def test_the_limit_covers_lists_and_nested_mappings() -> None:
    too_long = "a" * (MAX_STRING_CHARS + 1)
    assert find_over_long_strings({"spec": {"aliases": ["ok", too_long]}}) == [
        "`spec.aliases[1]` is longer than 64,000 characters; "
        "the settings file holds no text that long."
    ]
