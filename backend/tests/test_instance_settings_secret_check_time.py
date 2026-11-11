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
from typing import Any

import pytest

from meho_backplane.instance_settings import schema, service
from meho_backplane.instance_settings.schema import (
    MAX_STRING_CHARS,
    find_over_long_strings,
    find_secret_shaped_values,
    parse_settings_file,
)
from meho_backplane.instance_settings.service import (
    SettingsExportRefusedError,
    dump_settings_yaml,
    export_settings,
)
from tests._time_limits import check_time_limit

_HEAD = "apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n    - targets:\n"
_FILE_CHARS = 256 * 1024
_CHECK_LIMIT_S = 5.0
_LOOP_GAP_LIMIT_S = 1.0
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


@pytest.mark.slow_runner_tolerant
@pytest.mark.parametrize("unit", _SLOW_SHAPES)
def test_a_large_file_of_secret_starts_is_checked_fast(unit: str) -> None:
    content = _file_of(unit)
    started = time.perf_counter()
    parsed = parse_settings_file(content)
    elapsed = time.perf_counter() - started
    assert parsed.mistakes == []
    # Healthy time is a few milliseconds. The limit is wide so a busy runner
    # does not fail it; a check that grows with the square of the size takes
    # minutes on this file.
    check_time_limit(elapsed, _CHECK_LIMIT_S, f"the check took {elapsed:.2f} s")


@pytest.mark.slow_runner_tolerant
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
    check_time_limit(largest_gap, _LOOP_GAP_LIMIT_S, f"the event loop paused {largest_gap:.2f} s")


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


def test_a_repeated_key_over_the_limit_is_not_checked_or_shown_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[int] = []
    secret_kind = schema._secret_kind
    monkeypatch.setattr(
        schema, "_secret_kind", lambda text: checked.append(len(text)) or secret_kind(text)
    )
    long_key = "k" * (MAX_STRING_CHARS + 1)
    content = (
        _HEAD + "        - name: t\n          product: ssh\n          host: h\n"
        f"          extras:\n            ? {long_key}\n            : a\n"
        f"            ? {long_key}\n            : b\n"
    ).encode()

    parsed = parse_settings_file(content)

    assert parsed.model is None
    assert parsed.mistakes == [
        "Line 12: the key `<long key>` appears twice in the same block.",
        "`spec.tenants[0].targets[0].extras` has a key name longer than 64,000 "
        "characters; the settings file holds no text that long.",
    ]
    assert all(length <= MAX_STRING_CHARS for length in checked)


def _patch_export_state(monkeypatch: pytest.MonkeyPatch, notes: str) -> None:
    block = {
        "id": "00000000-0000-0000-0000-000000000001",
        "policies": None,
        "conventions": [],
        "targets": [{"name": "t", "product": "ssh", "host": "h", "notes": notes}],
    }

    async def load_tenant_blocks(*_args: Any) -> dict[str, dict[str, Any]]:
        return {block["id"]: block}

    async def migration_head(*_args: Any) -> None:
        return None

    monkeypatch.setattr(service, "load_tenant_blocks", load_tenant_blocks)
    monkeypatch.setattr(service, "_migration_head", migration_head)


async def test_the_export_refuses_a_stored_value_the_import_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_export_state(monkeypatch, "a" * (MAX_STRING_CHARS + 1))

    with pytest.raises(SettingsExportRefusedError) as refused:
        await export_settings(None, [])  # type: ignore[arg-type]

    assert refused.value.mistakes == [
        "`spec.tenants[0].targets[0].notes` is longer than 64,000 characters; "
        "the settings file holds no text that long."
    ]


async def test_an_exported_long_value_at_the_limit_imports_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_export_state(monkeypatch, "a" * MAX_STRING_CHARS)

    document = await export_settings(None, [])  # type: ignore[arg-type]
    parsed = parse_settings_file(dump_settings_yaml(document).encode())

    assert parsed.mistakes == []
