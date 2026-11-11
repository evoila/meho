# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The journal command run in a real shell, and the stored results of the set reads.

The journal tests run the generated ``linux.journal.tail`` command with
the local ``sh`` against a stand-in ``journalctl`` under ``tmp_path``.
They check the exit code that the pipe would hide, and that a cut at the
byte cap never splits a UTF-8 character. The stdout is decoded strictly,
as the SSH transport does.

The reducer tests give the output of ``linux.journal.tail`` and
``linux.dir.list`` to the JSONFlux reducer, over the row and the byte
threshold, and read the stored rows back through the handle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_inspect import (
    LinuxInspectError,
    build_journal_tail_command,
    parse_journal_output,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets
from tests.test_operations_jsonflux_reducer import _FakeStore

_linux_only = pytest.mark.skipif(sys.platform != "linux", reason="needs a POSIX sh, head and awk")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_SECRET_PATH = "meho/testing/linux/inspect-results-test"
_TARGET = _StubTarget("linux-inspect-results", "linux-host.test.invalid", 22, _SECRET_PATH)


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets({_SECRET_PATH: {"username": "root", "password": "made-up-pw-2"}}):
        yield


# ---------------------------------------------------------------------------
# The journal command in a real shell
# ---------------------------------------------------------------------------


def _entry(i: int, text: str = "nfsd: grace period ends") -> str:
    return json.dumps(
        {"__REALTIME_TIMESTAMP": "1700000000000000", "PRIORITY": "4", "MESSAGE": f"{i} {text}"},
        ensure_ascii=False,
    )


def _run_journal(tmp_path: Path, lines: list[str], rc: int, max_bytes: int) -> str:
    """Run the generated command against a fake ``journalctl``; return its strict stdout."""
    data = tmp_path / "journal.out"
    data.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    fake = tmp_path / "journalctl"
    fake.write_text(f'#!/bin/sh\ncat "{data}"\nexit {rc}\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    proc = subprocess.run(
        ["sh", "-c", build_journal_tail_command({}, max_bytes=max_bytes)],
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    return proc.stdout.decode("utf-8", "strict")


@_linux_only
def test_journal_command_reports_a_failed_journalctl(tmp_path: Path) -> None:
    out = _run_journal(tmp_path, [_entry(1)], rc=1, max_bytes=1024 * 1024)
    with pytest.raises(LinuxInspectError, match="exit 1"):
        parse_journal_output(out)


@_linux_only
def test_journal_command_under_the_cap_returns_every_row(tmp_path: Path) -> None:
    out = _run_journal(tmp_path, [_entry(i) for i in range(3)], rc=0, max_bytes=1024 * 1024)
    rows, truncated = parse_journal_output(out)
    assert [r["message"] for r in rows] == [f"{i} nfsd: grace period ends" for i in range(3)]
    assert truncated is False


@_linux_only
@pytest.mark.parametrize("char", ["ö", "磁"])
def test_journal_command_cut_keeps_whole_utf8_lines(tmp_path: Path, char: str) -> None:
    lines = [_entry(i, "Größe überschritten — 磁盘已满") for i in range(9100)]
    data = "".join(line + "\n" for line in lines).encode()
    # The cap ends one byte into a character of two or three bytes.
    cap = data.find(char.encode(), 200_000) + 1
    with pytest.raises(UnicodeDecodeError):
        data[:cap].decode("utf-8", "strict")
    out = _run_journal(tmp_path, lines, rc=0, max_bytes=cap)
    assert len(out.encode()) <= cap + len("MEHO_JOURNAL_CUT\n")
    rows, truncated = parse_journal_output(out)
    assert truncated is True
    assert 0 < len(rows) < 9100
    assert rows[-1]["message"] == f"{len(rows) - 1} Größe überschritten — 磁盘已满"


@_linux_only
def test_journal_command_cap_right_after_a_line_break_is_truncated(tmp_path: Path) -> None:
    lines = [_entry(i) for i in range(50)]
    data = "".join(line + "\n" for line in lines).encode()
    # The cap ends exactly after the line break of row 20.
    cap = len("".join(line + "\n" for line in lines[:21]).encode())
    assert data[cap - 1 : cap] == b"\n"
    out = _run_journal(tmp_path, lines, rc=0, max_bytes=cap)
    rows, truncated = parse_journal_output(out)
    assert truncated is True
    assert [r["message"] for r in rows] == [f"{i} nfsd: grace period ends" for i in range(21)]


@_linux_only
def test_journal_command_output_of_exactly_the_cap_is_whole(tmp_path: Path) -> None:
    lines = [_entry(i) for i in range(5)]
    body = "".join(line + "\n" for line in lines).encode()
    # The whole output: the rows, a blank line, then the exit code line.
    cap = len(body) + len(b"\nMEHO_JOURNAL_RC=0\n")
    out = _run_journal(tmp_path, lines, rc=0, max_bytes=cap)
    rows, truncated = parse_journal_output(out)
    assert truncated is False
    assert len(rows) == 5


# ---------------------------------------------------------------------------
# Stored results of the set reads
# ---------------------------------------------------------------------------


def _proc(stdout: str) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = ""
    proc.exit_status = 0
    return proc


async def _handler_result(op: str, params: dict[str, Any], stdout: str) -> Any:
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = _proc(stdout)
        return await getattr(connector, op)(_TARGET, params)


def _journal_stdout(count: int, text: str) -> str:
    return "".join(_entry(i, text) + "\n" for i in range(count)) + "\nMEHO_JOURNAL_RC=0\n"


def _dir_stdout(count: int, name_pad: str) -> str:
    records = "".join(
        f"f\t{i}\t1700000000.5\t644\troot\tfile-{i:03d}{name_pad}\0" for i in range(count)
    )
    path_hex = b"/srv/export\n".hex()
    return f"{path_hex}\n===MEHO_CONTENT===\n{records}MEHO_FIND_RC=0\n"


_CASES = [
    pytest.param("journal_tail", {}, _journal_stdout(60, "ok"), 60, id="journal-rows"),
    pytest.param("journal_tail", {}, _journal_stdout(5, "x" * 2000), 5, id="journal-bytes"),
    pytest.param("dir_list", {"path": "/srv/export"}, _dir_stdout(60, ""), 60, id="dir-rows"),
    pytest.param(
        "dir_list", {"path": "/srv/export"}, _dir_stdout(5, "-" * 1500), 5, id="dir-bytes"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("op", "params", "stdout", "count"), _CASES)
async def test_set_read_over_a_threshold_is_stored_behind_a_handle(
    op: str, params: dict[str, Any], stdout: str, count: int
) -> None:
    payload = await _handler_result(op, params, stdout)
    assert payload["total"] == count
    store = _FakeStore()
    reducer = JsonFluxReducer(sample_size=5, store=store, max_spill_rows=10000)
    context = {
        "op_id": f"linux.{op.replace('_', '.')}",
        "operator_sub": "op-a",
        "tenant_id": "00000000-0000-0000-0000-00000000a0a0",
    }

    _summary, handle = await reducer.reduce(payload, None, context)

    assert handle is not None
    assert handle.fetch_more.drill_in.available is True
    window = await store.fetch_window(
        tenant_id=context["tenant_id"],
        operator_sub="op-a",
        handle_id=handle.handle_id,
        offset=0,
        limit=count,
    )
    assert window is not None
    assert window["total_rows"] == count
    assert window["rows"] == payload["rows"]
