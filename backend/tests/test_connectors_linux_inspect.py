# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the linux-ssh journal, filesystem usage and directory reads.

Covers ``linux.journal.tail``, ``linux.fs.usage`` and ``linux.dir.list``
(command construction, injection safety, allow-list and cap checks, the
failure cases, the JSONFlux wrapping), plus the uptime and load fields of
``linux.about`` and the ``zero_length`` marker of ``linux.file.read``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.linux import LINUX_OPS, LinuxSshConnector
from meho_backplane.connectors.linux.ops import (
    LINUX_WHEN_TO_USE_BY_GROUP,
    MAX_TAIL_LINES,
    PathConfinementError,
)
from meho_backplane.connectors.linux.ops_inspect import (
    DIR_LIST_EXTRA_ROOTS_ENV,
    LinuxInspectError,
    build_dir_list_command,
    build_fs_usage_command,
    build_journal_tail_command,
    confine_dir_path,
    dir_list_roots,
    parse_dir_list_output,
    parse_fs_usage_output,
    parse_journal_output,
    validate_journal_priority,
    validate_journal_time,
)
from meho_backplane.settings import get_settings
from tests._ssh_vault_stub import stub_ssh_vault_secrets


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv(DIR_LIST_EXTRA_ROOTS_ENV, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_SECRET_PATH = "meho/testing/linux/inspect-test"
_TARGET = _StubTarget("linux-inspect-test", "linux-host.test.invalid", 22, _SECRET_PATH)


@pytest.fixture(autouse=True)
def _vault_secrets() -> Iterator[None]:
    with stub_ssh_vault_secrets({_SECRET_PATH: {"username": "root", "password": "made-up-pw-1"}}):
        yield


def _proc(*, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.exit_status = exit_status
    return proc


async def _run(op: str, params: dict[str, Any], proc: Any) -> tuple[Any, str]:
    """Call the handler with *proc* as the host answer; return (result, command)."""
    connector = LinuxSshConnector()
    with patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.return_value = proc
        result = await getattr(connector, op)(_TARGET, params)
    return result, mock_cmd.await_args_list[0].args[1]


def _hex(path: str) -> str:
    return (path + "\n").encode().hex()


def _dir_stdout(path: str, records: list[str], rc: int = 0) -> str:
    return (
        f"{_hex(path)}\n===MEHO_CONTENT===\n"
        + "".join(rec + "\0" for rec in records)
        + f"MEHO_FIND_RC={rc}\n"
    )


def _rec(name: str, kind: str = "f", size: int = 10) -> str:
    return f"{kind}\t{size}\t1700000000.5\t644\troot\t{name}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_NEW = {
    "linux.journal.tail": "log",
    "linux.fs.usage": "storage",
    "linux.dir.list": "file",
}


def test_new_ops_are_safe_read_only_and_grouped() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    for op_id, group in _NEW.items():
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert op.group_key == group
        assert group in LINUX_WHEN_TO_USE_BY_GROUP
        assert op.parameter_schema["additionalProperties"] is False
        assert op_id in LINUX_WHEN_TO_USE_BY_GROUP[group]
        assert op.llm_instructions and op.response_schema


def test_new_ops_declare_no_secret_parameter() -> None:
    by_id = {op.op_id: op for op in LINUX_OPS}
    for op_id in _NEW:
        for name in by_id[op_id].parameter_schema["properties"]:
            assert "password" not in name and "secret" not in name


# ---------------------------------------------------------------------------
# journal.tail
# ---------------------------------------------------------------------------


def test_journal_command_default_is_bounded() -> None:
    cmd = build_journal_tail_command({})
    assert "journalctl -o json --all --no-pager -n 200;" in cmd
    assert f"head -c {8 * 1024 * 1024 + 1} " in cmd


def test_journal_command_caps_lines_at_the_log_tail_maximum() -> None:
    cmd = build_journal_tail_command({"lines": 10**6})
    assert f"-n {MAX_TAIL_LINES};" in cmd


def test_journal_command_quotes_every_value() -> None:
    cmd = build_journal_tail_command(
        {"unit": "nfs-server.service", "since": "2 hours ago", "until": "-5min", "priority": "ERR"}
    )
    assert "-u nfs-server.service" in cmd
    assert "--since='2 hours ago'" in cmd
    assert "--until=-5min" in cmd
    assert "-p err" in cmd


@pytest.mark.parametrize("unit", ["x; reboot", "$(id)", "a b", "", "x`id`", "x\nid", 5])
def test_journal_rejects_bad_unit_before_building_a_command(unit: Any) -> None:
    with pytest.raises(ValueError):
        build_journal_tail_command({"unit": unit})


@pytest.mark.parametrize("bad", ["1h; id", "$(id)", "a'b", "x" * 65, "", " ", "a|b", "a\nb"])
def test_journal_rejects_bad_time(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_journal_time(bad)
    with pytest.raises(ValueError):
        build_journal_tail_command({"since": bad})


@pytest.mark.parametrize("bad", ["8", "-1", "err;id", "", "loud"])
def test_journal_rejects_bad_priority(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_journal_priority(bad)


def test_journal_lines_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_journal_tail_command({"lines": 0})


def _journal_line(**fields: Any) -> str:
    entry = {
        "__REALTIME_TIMESTAMP": "1700000000000000",
        "_SYSTEMD_UNIT": "nfs-server.service",
        "PRIORITY": "3",
        "MESSAGE": "boom",
        **fields,
    }
    return json.dumps(entry)


def _journal_stdout(lines: list[str], rc: int = 0) -> str:
    """Host answer of the journal command: the lines, then the journalctl exit code."""
    return "".join(line + "\n" for line in lines) + f"\nMEHO_JOURNAL_RC={rc}\n"


@pytest.mark.asyncio
async def test_journal_tail_returns_rows_in_the_reducer_envelope() -> None:
    out = _journal_stdout([_journal_line(), _journal_line(MESSAGE="again")])
    result, _ = await _run("journal_tail", {}, _proc(stdout=out))
    assert result["total"] == 2
    assert result["truncated"] is False
    assert result["rows"][0] == {
        "ts": "2023-11-14T22:13:20+00:00",
        "unit": "nfs-server.service",
        "priority": 3,
        "message": "boom",
        "message_truncated": False,
    }


@pytest.mark.asyncio
async def test_journal_tail_edge_rows() -> None:
    lines = [
        _journal_line(MESSAGE=[104, 105]),
        _journal_line(MESSAGE=None, _SYSTEMD_UNIT=None, SYSLOG_IDENTIFIER="kernel"),
        _journal_line(PRIORITY="x", __REALTIME_TIMESTAMP="nope"),
        "not json",
    ]
    result, _ = await _run("journal_tail", {}, _proc(stdout=_journal_stdout(lines)))
    rows = result["rows"]
    assert rows[0]["message"] == "hi"
    assert rows[1]["unit"] == "kernel" and rows[1]["message"] is None
    assert rows[2]["priority"] is None and rows[2]["ts"] is None
    assert rows[3] == {
        "ts": None,
        "unit": None,
        "priority": None,
        "message": "not json",
        "message_truncated": False,
    }


def test_journal_repeated_message_field_is_joined() -> None:
    rows, _ = parse_journal_output(_journal_stdout([_journal_line(MESSAGE=["a", [98], None])]))
    assert rows[0]["message"] == "a\nb"


@pytest.mark.asyncio
async def test_journal_tail_long_message_is_cut_and_marked() -> None:
    long = "x" * 5000
    lines = [_journal_line(MESSAGE=long), "y" * 5000]
    result, _ = await _run("journal_tail", {}, _proc(stdout=_journal_stdout(lines)))
    for row in result["rows"]:
        assert len(row["message"]) == 4096 and row["message_truncated"] is True
    assert result["truncated"] is False


def test_journal_output_cut_by_the_byte_cap_is_truncated() -> None:
    rows, truncated = parse_journal_output(_journal_line() + "\nMEHO_JOURNAL_CUT\n")
    assert len(rows) == 1 and truncated is True


@pytest.mark.parametrize(
    "stdout",
    [
        _journal_stdout([_journal_line()], rc=1),
        _journal_line() + "\n",
        _journal_line() + '\n{"MESSAGE": "cut o',
    ],
)
def test_journal_output_without_a_clean_exit_code_is_an_error(stdout: str) -> None:
    with pytest.raises(LinuxInspectError):
        parse_journal_output(stdout)


@pytest.mark.asyncio
async def test_journal_tail_no_journalctl_is_an_error() -> None:
    with pytest.raises(LinuxInspectError, match="no journalctl"):
        await _run("journal_tail", {}, _proc(exit_status=127))


@pytest.mark.asyncio
async def test_journal_tail_failed_exit_is_an_error() -> None:
    with pytest.raises(LinuxInspectError):
        await _run("journal_tail", {}, _proc(exit_status=1))


@pytest.mark.asyncio
async def test_journal_tail_empty_with_stderr_is_an_error_not_an_empty_answer() -> None:
    err = "No journal files were opened due to insufficient permissions."
    with pytest.raises(LinuxInspectError, match="insufficient permissions"):
        await _run("journal_tail", {}, _proc(stderr=err))


@pytest.mark.asyncio
async def test_journal_tail_rows_with_stderr_carry_a_warning() -> None:
    result, _ = await _run(
        "journal_tail",
        {},
        _proc(stdout=_journal_stdout([_journal_line()]), stderr="Hint: not seeing all"),
    )
    assert result["warning"] == "Hint: not seeing all"


@pytest.mark.asyncio
async def test_journal_tail_a_genuinely_empty_window_is_an_empty_list() -> None:
    result, _ = await _run(
        "journal_tail", {"since": "1 min ago"}, _proc(stdout=_journal_stdout([]))
    )
    assert result["rows"] == [] and result["total"] == 0


# ---------------------------------------------------------------------------
# fs.usage
# ---------------------------------------------------------------------------

_DF_HEADER = "Filesystem Type 1B-blocks Used Avail Inodes IUsed\n"


@pytest.mark.asyncio
async def test_fs_usage_returns_a_flat_dict() -> None:
    out = _DF_HEADER + "/dev/sdb1 xfs 1000 400 600 5000 120\n"
    result, cmd = await _run("fs_usage", {"path": "/srv/export"}, _proc(stdout=out))
    assert result == {
        "path": "/srv/export",
        "filesystem": "/dev/sdb1",
        "fstype": "xfs",
        "size_bytes": 1000,
        "used_bytes": 400,
        "avail_bytes": 600,
        "inodes_total": 5000,
        "inodes_used": 120,
    }
    assert "'/srv/export'" in cmd or "/srv/export" in cmd
    assert "--output=source,fstype,size,used,avail,itotal,iused" in cmd
    assert "rows" not in result


def test_fs_usage_source_with_spaces_and_no_inodes() -> None:
    out = _DF_HEADER + "server:/a b nfs4 10 4 6 - -\n"
    parsed = parse_fs_usage_output(out)
    assert parsed["filesystem"] == "server:/a b"
    assert parsed["inodes_total"] is None and parsed["inodes_used"] is None


@pytest.mark.parametrize(
    "out",
    ["", _DF_HEADER, _DF_HEADER + "a xfs x y z 1 1\n", _DF_HEADER + "a b\n", "a\nb\nc\n"],
)
def test_fs_usage_unreadable_answer_raises(out: str) -> None:
    with pytest.raises(LinuxInspectError):
        parse_fs_usage_output(out)


@pytest.mark.asyncio
async def test_fs_usage_df_failure_is_an_error() -> None:
    with pytest.raises(LinuxInspectError, match="usage was not read"):
        await _run("fs_usage", {"path": "/srv/gone"}, _proc(exit_status=1))
    with pytest.raises(LinuxInspectError, match="no df"):
        await _run("fs_usage", {"path": "/srv/x"}, _proc(exit_status=127))


@pytest.mark.asyncio
async def test_fs_usage_allows_the_filesystem_root_only_for_usage() -> None:
    out = _DF_HEADER + "/dev/sda1 ext4 10 4 6 100 10\n"
    result, _ = await _run("fs_usage", {"path": "/"}, _proc(stdout=out))
    assert result["path"] == "/"
    with pytest.raises(PathConfinementError):
        confine_dir_path("/")


@pytest.mark.parametrize(
    "path",
    ["/root", "/home/x", "relative", "/srv/../root", "/etc/../root", "/srv\n/x", "", "/proc/1"],
)
@pytest.mark.asyncio
async def test_fs_usage_and_dir_list_reject_paths_before_ssh(path: str) -> None:
    connector = LinuxSshConnector()
    for op in ("fs_usage", "dir_list"):
        with (
            patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
            pytest.raises(PathConfinementError),
        ):
            await getattr(connector, op)(_TARGET, {"path": path})
        mock_cmd.assert_not_awaited()


def test_fs_usage_command_quotes_the_path() -> None:
    assert "'/srv/a b; id'" in build_fs_usage_command("/srv/a b; id")


# ---------------------------------------------------------------------------
# dir.list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dir_list_returns_sorted_rows_in_the_reducer_envelope() -> None:
    out = _dir_stdout("/srv/export", [_rec("b.txt"), _rec("a dir", "d", 4096), _rec("l", "l")])
    result, cmd = await _run("dir_list", {"path": "/srv/export"}, _proc(stdout=out))
    assert [r["name"] for r in result["rows"]] == ["a dir", "b.txt", "l"]
    assert [r["type"] for r in result["rows"]] == ["directory", "file", "symlink"]
    assert result["rows"][1] == {
        "name": "b.txt",
        "type": "file",
        "size_bytes": 10,
        "mtime": "2023-11-14T22:13:20.500000+00:00",
        "mode": "644",
        "owner": "root",
    }
    assert result["total"] == 3
    assert (result["exists"], result["truncated"], result["incomplete"]) == (True, False, False)
    assert "-maxdepth 1" in cmd and "-name '.*' -prune -o" in cmd


def test_dir_list_command_depth_hidden_and_quoting() -> None:
    cmd = build_dir_list_command("/srv/a b; id", 2, True)
    assert "-maxdepth 2" in cmd
    assert "-prune" not in cmd
    assert "'/srv/a b; id'" in cmd
    assert "head -z -n 5002" in cmd


@pytest.mark.parametrize("depth", [0, 3, -1])
@pytest.mark.asyncio
async def test_dir_list_depth_is_limited(depth: int) -> None:
    connector = LinuxSshConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError, match="depth"),
    ):
        await connector.dir_list(_TARGET, {"path": "/srv", "depth": depth})
    mock_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_dir_list_row_cap_sets_truncated() -> None:
    records = [_rec(f"f{i:05d}") for i in range(5001)]
    result, _ = await _run("dir_list", {"path": "/srv"}, _proc(stdout=_dir_stdout("/srv", records)))
    assert result["total"] == 5000 and result["truncated"] is True


@pytest.mark.asyncio
async def test_dir_list_cut_listing_without_rc_is_truncated_not_an_error() -> None:
    records = [_rec(f"f{i:05d}") for i in range(5002)]
    out = f"{_hex('/srv')}\n===MEHO_CONTENT===\n" + "".join(r + "\0" for r in records) + "MEHO_F"
    result, _ = await _run("dir_list", {"path": "/srv"}, _proc(stdout=out))
    assert result["truncated"] is True and result["total"] == 5000


@pytest.mark.asyncio
async def test_dir_list_find_error_marks_incomplete() -> None:
    out = _dir_stdout("/srv", [_rec("a")], rc=1)
    result, _ = await _run("dir_list", {"path": "/srv"}, _proc(stdout=out))
    assert result["incomplete"] is True and result["total"] == 1


@pytest.mark.asyncio
async def test_dir_list_names_with_tabs_and_line_breaks_stay_one_row() -> None:
    out = _dir_stdout("/srv", [_rec("a\tb\nc")])
    result, _ = await _run("dir_list", {"path": "/srv"}, _proc(stdout=out))
    assert result["rows"][0]["name"] == "a\tb\nc"


@pytest.mark.asyncio
async def test_dir_list_empty_directory_is_an_empty_list() -> None:
    result, _ = await _run("dir_list", {"path": "/srv"}, _proc(stdout=_dir_stdout("/srv", [])))
    assert result["rows"] == [] and result["exists"] is True


@pytest.mark.asyncio
async def test_dir_list_missing_directory_reports_exists_false() -> None:
    result, _ = await _run(
        "dir_list", {"path": "/srv/x"}, _proc(stdout="===MEHO_FILE_MISSING===\n")
    )
    assert result["exists"] is False and result["rows"] == []


@pytest.mark.parametrize(
    "stdout",
    [
        "===MEHO_FILE_UNREADABLE===\n",
        "",
        "garbage",
        "zz\n===MEHO_CONTENT===\nMEHO_FIND_RC=0\n",
        _dir_stdout("/srv", [_rec("a")])[:-15],
        _dir_stdout("/srv", ["bad record"]),
    ],
)
@pytest.mark.asyncio
async def test_dir_list_unclear_answers_raise(stdout: str) -> None:
    with pytest.raises((LinuxInspectError, PathConfinementError)):
        await _run("dir_list", {"path": "/srv"}, _proc(stdout=stdout))


@pytest.mark.asyncio
async def test_dir_list_nonzero_exit_raises() -> None:
    with pytest.raises(LinuxInspectError):
        await _run("dir_list", {"path": "/srv"}, _proc(exit_status=1))


@pytest.mark.parametrize("real", ["/root", "/proc", "/etc/ssl/private/x", "/"])
@pytest.mark.asyncio
async def test_dir_list_refuses_a_real_path_outside_the_allow_list(real: str) -> None:
    out = _dir_stdout(real, [_rec("secret-file")])
    with pytest.raises(PathConfinementError):
        await _run("dir_list", {"path": "/srv/link"}, _proc(stdout=out))


@pytest.mark.asyncio
async def test_dir_list_accepts_a_symlink_that_stays_inside() -> None:
    out = _dir_stdout("/mnt/real", [_rec("a")])
    result, _ = await _run("dir_list", {"path": "/srv/link"}, _proc(stdout=out))
    assert result["path"] == "/srv/link" and result["total"] == 1


def test_dir_list_output_with_a_newline_in_the_real_path_is_refused() -> None:
    out = _dir_stdout("/srv/a\nb", [])
    with pytest.raises(PathConfinementError):
        parse_dir_list_output(out, "/srv/x")


# ---------------------------------------------------------------------------
# allow-list
# ---------------------------------------------------------------------------


def test_default_roots_hold_the_read_roots_and_data_roots() -> None:
    roots = dir_list_roots()
    for root in ("/etc", "/var/log", "/srv", "/mnt", "/export"):
        assert root in roots


def test_extra_roots_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PathConfinementError):
        confine_dir_path("/backups/nightly")
    monkeypatch.setenv(DIR_LIST_EXTRA_ROOTS_ENV, "/backups: /store/ ")
    assert confine_dir_path("/backups/nightly") == "/backups/nightly"
    assert confine_dir_path("/store/x") == "/store/x"
    with pytest.raises(PathConfinementError):
        confine_dir_path("/backupsX")


@pytest.mark.parametrize("bad", ["/", "relative", "//"])
def test_a_wrong_extra_root_fails_closed(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(DIR_LIST_EXTRA_ROOTS_ENV, bad)
    with pytest.raises(ValueError):
        dir_list_roots()


def test_key_paths_stay_refused_under_an_allowed_root() -> None:
    for path in ("/etc/ssl/private/x", "/srv/app/server.key", "/etc/shadow"):
        with pytest.raises(PathConfinementError):
            confine_dir_path(path)


# ---------------------------------------------------------------------------
# about: uptime and load; file.read: zero_length
# ---------------------------------------------------------------------------

_FP = (
    "===MEHO_HOSTNAME===\nh1\n===MEHO_KERNEL===\n6.8.0\n===MEHO_INIT===\nsystemd\n"
    '===MEHO_OSRELEASE===\nID=ubuntu\nVERSION_ID="24.04"\n'
)


@pytest.mark.asyncio
async def test_about_returns_uptime_and_load_averages() -> None:
    out = _FP + "===MEHO_UPTIME===\n12345.67 999.00\n===MEHO_LOADAVG===\n0.10 0.25 0.50 1/200 999\n"
    result, cmd = await _run("about", {}, _proc(stdout=out))
    assert result["uptime_seconds"] == 12345.67
    assert (result["load_1m"], result["load_5m"], result["load_15m"]) == (0.10, 0.25, 0.50)
    assert "/proc/uptime" in cmd and "/proc/loadavg" in cmd


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail",
    [
        "",
        "===MEHO_UPTIME===\n===MEHO_LOADAVG===\n",
        "===MEHO_UPTIME===\nx y\n===MEHO_LOADAVG===\na b\n",
    ],
)
async def test_about_uptime_and_load_are_null_when_missing_or_unreadable(tail: str) -> None:
    result, _ = await _run("about", {}, _proc(stdout=_FP + tail))
    assert result["uptime_seconds"] is None
    assert result["load_1m"] is None and result["load_15m"] is None
    assert result["hostname"] == "h1"


def _read_stdout(path: str, size: int, content: str) -> str:
    return f"{_hex(path)}\n{size}\n===MEHO_CONTENT===\n{content}"


@pytest.mark.asyncio
async def test_file_read_marks_an_empty_proc_read_as_zero_length() -> None:
    out = _read_stdout("/proc/loadavg", 0, "")
    result, _ = await _run("file_read", {"path": "/proc/loadavg"}, _proc(stdout=out))
    assert result["zero_length"] is True and result["exists"] is True


@pytest.mark.asyncio
async def test_file_read_does_not_mark_an_empty_regular_file() -> None:
    out = _read_stdout("/var/lib/app/state", 0, "")
    result, _ = await _run("file_read", {"path": "/var/lib/app/state"}, _proc(stdout=out))
    assert result["zero_length"] is False and result["size_bytes"] == 0


@pytest.mark.asyncio
async def test_file_read_does_not_mark_a_proc_read_with_content() -> None:
    out = _read_stdout("/proc/loadavg", 0, "0.1 0.2 0.3 1/2 3\n")
    result, _ = await _run("file_read", {"path": "/proc/loadavg"}, _proc(stdout=out))
    assert result["zero_length"] is False


@pytest.mark.asyncio
async def test_file_read_missing_file_is_not_zero_length() -> None:
    result, _ = await _run(
        "file_read", {"path": "/proc/loadavg"}, _proc(stdout="===MEHO_FILE_MISSING===\n")
    )
    assert result["zero_length"] is False and result["exists"] is False
