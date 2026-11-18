# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the safe pfSense read ``pfsense.config.history`` (#4989).

All data is synthetic: made-up users, descriptions and passwords. The
listing text has the shape the firewall command prints: a ``@@FILE`` line
(name, read status, size), the ``<revision>`` block, and a ``@@BACKUPCOUNT``
line (read status, value) at the end. The tests at the end run the real
listing command with ``/bin/sh`` over the files in
``tests/fixtures/pfsense_config_history/``.

Coverage:

* the list: newest first, the live file marked, ``since``, the backup count
  (stored and default), a revision without a block, a revision block whose
  description holds a secret shape;
* the diff of one revision against the one before it: changed sections only,
  the oldest revision, an unknown revision, one section, a diff that is too
  long;
* secrets: a revision that changes a password field shows no password in
  the answer, but the section is flagged; nothing leaks in the error text;
* failures: a failed listing, an empty listing, a failed file read, a file
  that cannot be parsed raise an error and never return an empty answer;
* a save between the listing and the file read;
* the parameter checks and the op metadata.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.pfsense.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.pfsense.ops_config_history import (
    _LIST_CMD,
    DEFAULT_BACKUP_COUNT,
    MAX_DIFF_CHARS,
    PfSenseConfigHistoryError,
    parse_history_listing,
)
from meho_backplane.connectors.pfsense.redaction import REDACTED
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.settings import get_settings
from tests.test_connectors_pfsense_config_redaction import FAKE_BCRYPT, FAKE_PSK


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


_TARGET = _StubTarget("fw-01", "fw-01.test.invalid", 22, "meho/testing/pfsense/fw-01")

_T_OLD = 1763000000
_T_MID = 1763100000
_T_NEW = 1763200000


def _proc(stdout: str = "", exit_status: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.exit_status = exit_status
    return proc


def _revision_block(time: int, user: str, description: str) -> str:
    return (
        "\t<revision>\n"
        f"\t\t<time>{time}</time>\n"
        f"\t\t<description>{description}</description>\n"
        f"\t\t<username><![CDATA[{user}]]></username>\n"
        "\t</revision>\n"
    )


def _config(time: int, user: str, description: str, *, rules: tuple[str, ...], hash_: str) -> str:
    rule_xml = "".join(
        f"\t\t<rule>\n\t\t\t<type>pass</type>\n\t\t\t<descr>{r}</descr>\n\t\t</rule>\n"
        for r in rules
    )
    return (
        '<?xml version="1.0"?>\n<pfsense>\n\t<version>23.3</version>\n'
        + _revision_block(time, user, description)
        + "\t<system>\n\t\t<hostname>fw-01</hostname>\n"
        f"\t\t<user>\n\t\t\t<name>admin</name>\n\t\t\t<bcrypt-hash>{hash_}</bcrypt-hash>\n"
        "\t\t</user>\n\t</system>\n"
        f"\t<filter>\n{rule_xml}\t</filter>\n</pfsense>\n"
    )


_OTHER_HASH = "$2y$10$" + "z" * 22 + "Y" * 31
_FILES: dict[str, str] = {
    "config.xml": _config(
        _T_NEW,
        "alice@192.0.2.10 (Local Database)",
        "Firewall: Rules: edited",
        rules=("allow web", "allow dns"),
        hash_=FAKE_BCRYPT,
    ),
    f"backup/config-{_T_MID}.xml": _config(
        _T_MID,
        "bob@192.0.2.11 (Local Database)",
        "Firewall: Rules: added",
        rules=("allow web",),
        hash_=FAKE_BCRYPT,
    ),
    f"backup/config-{_T_OLD}.xml": _config(
        _T_OLD,
        "bob@192.0.2.11 (Local Database)",
        "Initial",
        rules=("allow web",),
        hash_=_OTHER_HASH,
    ),
}


def _listing(files: dict[str, str], backup_count: str = "") -> str:
    out = ""
    for name, text in files.items():
        start = text.index("\t<revision>")
        end = text.index("</revision>") + len("</revision>\n")
        out += f"@@FILE {name} 0 {len(text)}\n" + text[start:end]
    return out + f"@@BACKUPCOUNT 0 {backup_count}\n"


def _shell(files: dict[str, str], backup_count: str = "") -> Any:
    """Return a stand-in for ``_run_command`` that answers the listing and ``cat``."""

    async def run(target: Any, command: str, operator: Any = None) -> Any:
        if command.startswith("cd /cf/conf"):
            return _proc(_listing(files, backup_count))
        assert command.startswith("cat /cf/conf/"), command
        name = command.removeprefix("cat /cf/conf/")
        if name not in files:
            return _proc("", exit_status=1)
        return _proc(files[name])

    return run


async def _call(params: dict[str, Any], files: dict[str, str] | None = None, **kw: str) -> Any:
    connector = PfSenseConnector()
    with patch.object(
        connector, "_run_command", new=AsyncMock(side_effect=_shell(files or _FILES, **kw))
    ):
        return await connector.config_history(_TARGET, params)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_is_newest_first_and_marks_the_live_file() -> None:
    result = await _call({})
    assert [row["time"] for row in result["rows"]] == [_T_NEW, _T_MID, _T_OLD]
    assert [row["current"] for row in result["rows"]] == [True, False, False]
    first = result["rows"][0]
    assert first["user"] == "alice@192.0.2.10 (Local Database)"
    assert first["description"] == "Firewall: Rules: edited"
    assert first["time_utc"] == "2025-11-15T09:46:40Z"
    assert first["size"] == len(_FILES["config.xml"])
    assert result["total"] == result["kept"] == 3


@pytest.mark.asyncio
async def test_list_says_how_far_back_it_goes() -> None:
    default = await _call({})
    assert default["backup_count"] == DEFAULT_BACKUP_COUNT == 30
    assert default["backup_count_is_default"] is True
    stored = await _call({}, backup_count="5")
    assert stored["backup_count"] == 5
    assert stored["backup_count_is_default"] is False


@pytest.mark.asyncio
async def test_since_keeps_only_newer_revisions_but_kept_counts_all() -> None:
    result = await _call({"since": _T_MID})
    assert [row["time"] for row in result["rows"]] == [_T_NEW, _T_MID]
    assert result["total"] == 2
    assert result["kept"] == 3


@pytest.mark.asyncio
async def test_since_after_every_revision_is_an_empty_list_not_an_error() -> None:
    result = await _call({"since": _T_NEW + 1})
    assert result["rows"] == []
    assert result["kept"] == 3


def test_a_backup_without_a_revision_block_takes_its_time_from_the_file_name() -> None:
    output = f"@@FILE backup/config-{_T_OLD}.xml 0 120\n@@BACKUPCOUNT 0 \n"
    revisions, _count = parse_history_listing(output)
    assert [(r.time, r.user, r.description) for r in revisions] == [(_T_OLD, None, None)]


def test_a_secret_shape_in_the_description_is_replaced() -> None:
    files = dict(_FILES)
    files["config.xml"] = _config(
        _T_NEW, "alice", f"password {FAKE_BCRYPT}", rules=("a",), hash_=FAKE_BCRYPT
    )
    revisions, _count = parse_history_listing(_listing(files))
    live = next(r for r in revisions if r.current)
    assert live.description == REDACTED
    assert FAKE_BCRYPT not in repr(revisions)


def test_the_live_file_wins_when_a_backup_has_the_same_time() -> None:
    files = {
        "config.xml": _FILES["config.xml"],
        f"backup/config-{_T_NEW}.xml": _FILES["config.xml"],
    }
    revisions, _count = parse_history_listing(_listing(files))
    assert len(revisions) == 1
    assert revisions[0].current is True


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_shows_what_one_revision_changed_against_the_one_before() -> None:
    result = await _call({"revision": _T_NEW})
    assert result["previous_revision"] == _T_MID
    assert result["user"] == "alice@192.0.2.10 (Local Database)"
    assert [s["section"] for s in result["changed_sections"]] == ["filter"]
    section = result["changed_sections"][0]
    assert section["added"] >= 1
    assert section["removed"] == 0
    assert "allow dns" in result["diff"]
    assert result["diff_omitted"] is False


@pytest.mark.asyncio
async def test_diff_of_a_revision_does_not_use_the_revision_block() -> None:
    result = await _call({"revision": _T_NEW})
    assert "revision" not in {s["section"] for s in result["changed_sections"]}
    assert "<time>" not in result["diff"]


@pytest.mark.asyncio
async def test_the_oldest_revision_has_nothing_to_compare() -> None:
    result = await _call({"revision": _T_OLD})
    assert result["previous_revision"] is None
    assert result["changed_sections"] == []
    assert result["diff"] is None
    assert "oldest" in result["note"]


@pytest.mark.asyncio
async def test_an_unknown_revision_is_an_error() -> None:
    with pytest.raises(PfSenseConfigHistoryError, match="not kept"):
        await _call({"revision": 5})


@pytest.mark.asyncio
async def test_a_section_returns_only_that_sections_diff() -> None:
    result = await _call({"revision": _T_NEW, "section": "filter"})
    assert "allow dns" in result["diff"]
    assert result["changed_total"] == 1


@pytest.mark.asyncio
async def test_a_section_that_did_not_change_is_an_error_naming_the_changed_ones() -> None:
    with pytest.raises(PfSenseConfigHistoryError, match="changed sections: filter"):
        await _call({"revision": _T_NEW, "section": "system"})


@pytest.mark.asyncio
async def test_a_long_diff_is_left_out_and_one_section_can_still_be_asked_for() -> None:
    many = tuple(f"rule number {i} " + "x" * 80 for i in range(MAX_DIFF_CHARS // 90 + 50))
    files = dict(_FILES)
    files["config.xml"] = _config(_T_NEW, "alice", "bulk", rules=many, hash_=FAKE_BCRYPT)
    result = await _call({"revision": _T_NEW}, files)
    assert result["diff"] is None
    assert result["diff_omitted"] is True
    assert [s["section"] for s in result["changed_sections"]] == ["filter"]
    assert result["changed_sections"][0]["added"] > 1000
    assert "section parameter" in result["note"]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_changed_password_is_flagged_but_never_shown() -> None:
    """The oldest-to-middle step changes only the admin password hash."""
    result = await _call({"revision": _T_MID})
    by_name = {s["section"]: s for s in result["changed_sections"]}
    assert by_name["system"]["only_hidden_values_changed"] is True
    assert by_name["system"]["added"] == by_name["system"]["removed"] == 0
    dumped = json.dumps(result) + repr(result)
    assert FAKE_BCRYPT not in dumped
    assert _OTHER_HASH not in dumped


@pytest.mark.asyncio
async def test_a_changed_pre_shared_key_never_appears_in_the_answer() -> None:
    old = _FILES["config.xml"].replace(
        "<hostname>fw-01</hostname>",
        f"<hostname>fw-01</hostname>\n\t\t<password>{FAKE_PSK}</password>",
    )
    new = old.replace(FAKE_PSK, FAKE_PSK + "-changed")
    files = {
        "config.xml": new,
        f"backup/config-{_T_MID}.xml": old.replace(str(_T_NEW), str(_T_MID)),
    }
    result = await _call({"revision": _T_NEW}, files)
    dumped = json.dumps(result) + repr(result)
    assert FAKE_PSK not in dumped
    assert result["changed_sections"][0]["only_hidden_values_changed"] is True


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_parsed_fails_without_quoting_it() -> None:
    files = dict(_FILES)
    files["config.xml"] = files["config.xml"].replace("</pfsense>", f"<x>{FAKE_BCRYPT}")
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new=AsyncMock(side_effect=_shell(files))),
        pytest.raises(PfSenseConfigHistoryError) as excinfo,
    ):
        await connector.config_history(_TARGET, {"revision": _T_NEW})
    envelope = result_connector_error("pfsense.config.history", excinfo.value, 1.0)
    assert FAKE_BCRYPT not in envelope.model_dump_json()
    assert FAKE_BCRYPT not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Failures are not answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_listing_raises() -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new=AsyncMock(return_value=_proc("", 1))),
        pytest.raises(RuntimeError, match="exit 1"),
    ):
        await connector.config_history(_TARGET, {})


@pytest.mark.asyncio
async def test_an_empty_listing_raises_and_is_not_an_empty_history() -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new=AsyncMock(return_value=_proc("", 0))),
        pytest.raises(PfSenseConfigHistoryError, match="no configuration files"),
    ):
        await connector.config_history(_TARGET, {})


@pytest.mark.asyncio
async def test_a_failed_file_read_raises() -> None:
    files = dict(_FILES)

    async def run(target: Any, command: str, operator: Any = None) -> Any:
        if command.startswith("cd /cf/conf"):
            return _proc(_listing(files))
        return _proc("", exit_status=1)

    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new=AsyncMock(side_effect=run)),
        pytest.raises(RuntimeError, match="reading revision"),
    ):
        await connector.config_history(_TARGET, {"revision": _T_NEW})


# ---------------------------------------------------------------------------
# Parameters and metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"revision": "17"},
        {"revision": True},
        {"revision": -1},
        {"since": 1.5},
        {"section": "filter"},
        {"revision": _T_NEW, "since": 1},
        {"revision": _T_NEW, "section": "../etc"},
        {"revision": _T_NEW, "section": "a b"},
    ],
)
async def test_bad_parameters_are_refused_before_any_ssh_call(params: dict[str, Any]) -> None:
    connector = PfSenseConnector()
    with (
        patch.object(connector, "_run_command", new_callable=AsyncMock) as mock_cmd,
        pytest.raises(ValueError),
    ):
        await connector.config_history(_TARGET, params)
    mock_cmd.assert_not_awaited()


def _op() -> Any:
    return next(op for op in PFSENSE_OPS if op.op_id == "pfsense.config.history")


def test_op_is_a_safe_typed_read_in_the_config_group() -> None:
    op = _op()
    assert (op.safety_level, op.requires_approval, op.group_key) == ("safe", False, "config")
    assert op.parameter_schema["additionalProperties"] is False
    assert op.llm_instructions["when_to_use"]
    assert "pfsense.config.history" in _WHEN_TO_USE_BY_GROUP["config"]


def test_listing_and_diff_answers_match_the_response_schema() -> None:
    validator = Draft202012Validator(_op().response_schema)
    revisions, _ = parse_history_listing(_listing(_FILES))
    from meho_backplane.connectors.pfsense.ops_config_history import _revision_row

    validator.validate(
        {
            "rows": [_revision_row(r) for r in revisions],
            "total": 3,
            "kept": 3,
            "backup_count": 30,
            "backup_count_is_default": True,
        }
    )
    validator.validate(
        {
            "revision": 1,
            "previous_revision": None,
            "changed_sections": [],
            "changed_total": 0,
            "diff": None,
            "diff_omitted": False,
        }
    )


@pytest.mark.asyncio
async def test_real_answers_match_the_response_schema() -> None:
    validator = Draft202012Validator(_op().response_schema)
    for params in ({}, {"revision": _T_NEW}, {"revision": _T_OLD}, {"revision": _T_MID}):
        validator.validate(await _call(params))


# ---------------------------------------------------------------------------
# A failed read inside the listing is an error, not a file without a block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        "@@FILE config.xml 1 \n@@BACKUPCOUNT 0 \n",
        "@@FILE config.xml 0 \n@@BACKUPCOUNT 0 \n",
        f"@@FILE config.xml 0 900\n@@FILE backup/config-{_T_OLD}.xml 1 900\n@@BACKUPCOUNT 0 \n",
        "@@FILE config.xml 0 900\n@@BACKUPCOUNT 1 \n",
        "@@FILE config.xml 0 900\n",
    ],
    ids=[
        "live-file-not-read",
        "live-file-size-missing",
        "backup-not-read",
        "backup-count-not-read",
        "answer-stops-early",
    ],
)
def test_a_failed_read_in_the_listing_raises(output: str) -> None:
    with pytest.raises(PfSenseConfigHistoryError, match=r"could not read|stopped early"):
        parse_history_listing(output)


def test_a_read_file_without_a_revision_block_is_not_an_error() -> None:
    output = f"@@FILE config.xml 0 900\n@@FILE backup/config-{_T_OLD}.xml 0 800\n@@BACKUPCOUNT 0 \n"
    revisions, count = parse_history_listing(output)
    assert [(r.time, r.current) for r in revisions] == [(_T_OLD, False)]
    assert count is None


# ---------------------------------------------------------------------------
# A save between the listing and the file read
# ---------------------------------------------------------------------------

_T_SAVED = _T_NEW + 60


def _shell_with_save(files: dict[str, str], after_save: dict[str, str]) -> Any:
    """The listing sees *files*; every ``cat`` after it sees *after_save*."""

    async def run(target: Any, command: str, operator: Any = None) -> Any:
        if command.startswith("cd /cf/conf"):
            return _proc(_listing(files))
        name = command.removeprefix("cat /cf/conf/")
        if name not in after_save:
            return _proc("", exit_status=1)
        return _proc(after_save[name])

    return run


async def _call_with_save(params: dict[str, Any], after_save: dict[str, str]) -> Any:
    connector = PfSenseConnector()
    with patch.object(
        connector,
        "_run_command",
        new=AsyncMock(side_effect=_shell_with_save(_FILES, after_save)),
    ):
        return await connector.config_history(_TARGET, params)


@pytest.mark.asyncio
async def test_a_save_after_the_listing_reads_the_live_revision_from_its_backup() -> None:
    """pfSense moved the listed live file to backup/config-<time>.xml and wrote a new one."""
    after_save = dict(_FILES)
    after_save[f"backup/config-{_T_NEW}.xml"] = _FILES["config.xml"]
    after_save["config.xml"] = _config(
        _T_SAVED, "mallory", "Firewall: Rules: deleted", rules=(), hash_=FAKE_BCRYPT
    )
    result = await _call_with_save({"revision": _T_NEW}, after_save)
    assert result == await _call({"revision": _T_NEW})
    assert result["user"] == "alice@192.0.2.10 (Local Database)"
    assert "allow dns" in result["diff"]
    assert "mallory" not in json.dumps(result)


@pytest.mark.asyncio
async def test_a_save_after_the_listing_without_the_backup_raises() -> None:
    after_save = dict(_FILES)
    after_save["config.xml"] = _config(
        _T_SAVED, "mallory", "Firewall: Rules: deleted", rules=(), hash_=FAKE_BCRYPT
    )
    with pytest.raises(PfSenseConfigHistoryError, match="saved again"):
        await _call_with_save({"revision": _T_NEW}, after_save)


@pytest.mark.asyncio
async def test_a_backup_that_holds_another_revision_raises() -> None:
    after_save = dict(_FILES)
    after_save[f"backup/config-{_T_MID}.xml"] = _FILES[f"backup/config-{_T_OLD}.xml"]
    with pytest.raises(PfSenseConfigHistoryError, match="saved again"):
        await _call_with_save({"revision": _T_NEW}, after_save)


# ---------------------------------------------------------------------------
# The real listing command over files in the pfSense on-disk format
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pfsense_config_history"
_FX_T1 = 1762000000
_FX_T2 = 1762090000
_FX_T3 = 1762180000
_SH = shutil.which("sh")


def _fixture_files() -> dict[str, str]:
    names = ("config.xml", f"backup/config-{_FX_T2}.xml", f"backup/config-{_FX_T1}.xml")
    return {name: (_FIXTURE_DIR / name).read_text() for name in names}


def _run_list_cmd(conf_dir: Path) -> subprocess.CompletedProcess[str]:
    assert _SH is not None
    command = _LIST_CMD.replace("/cf/conf", str(conf_dir))
    return subprocess.run(
        [_SH, "-c", command], capture_output=True, text=True, check=False, timeout=30
    )


def _conf_dir(tmp_path: Path) -> Path:
    conf = tmp_path / "conf"
    shutil.copytree(_FIXTURE_DIR, conf)
    return conf


@pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")
def test_the_real_listing_command_reads_the_fixture_files(tmp_path: Path) -> None:
    proc = _run_list_cmd(_conf_dir(tmp_path))
    assert proc.returncode == 0, proc.stderr
    revisions, count = parse_history_listing(proc.stdout)
    assert count == 10
    files = _fixture_files()
    assert [(r.time, r.file, r.current, r.size) for r in revisions] == [
        (_FX_T3, "config.xml", True, len(files["config.xml"].encode())),
        (_FX_T2, f"backup/config-{_FX_T2}.xml", False, len(files[f"backup/config-{_FX_T2}.xml"])),
        (_FX_T1, f"backup/config-{_FX_T1}.xml", False, len(files[f"backup/config-{_FX_T1}.xml"])),
    ]
    assert revisions[0].user == "operator@198.51.100.21 (Local Database)"
    assert revisions[0].description == (
        "operator@198.51.100.21 (Local Database): Updated user admin and DNS servers"
    )
    assert revisions[2].user == "admin@198.51.100.20 (Local Database)"


@pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")
@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a file without read permission")
def test_the_real_listing_command_reports_a_file_it_cannot_read(tmp_path: Path) -> None:
    conf = _conf_dir(tmp_path)
    unreadable = conf / "backup" / f"config-{_FX_T1}.xml"
    unreadable.chmod(0)
    try:
        proc = _run_list_cmd(conf)
    finally:
        unreadable.chmod(0o600)
    assert f"@@FILE backup/config-{_FX_T1}.xml 1" in proc.stdout
    with pytest.raises(PfSenseConfigHistoryError, match=f"could not read backup/config-{_FX_T1}"):
        parse_history_listing(proc.stdout)


async def _call_on_fixture_dir(conf: Path, params: dict[str, Any]) -> Any:
    """Run the op with the listing from the real command and ``cat`` from the files."""

    async def run(target: Any, command: str, operator: Any = None) -> Any:
        if command.startswith("cd /cf/conf"):
            proc = _run_list_cmd(conf)
            return _proc(proc.stdout, proc.returncode)
        path = conf / command.removeprefix("cat /cf/conf/")
        return _proc(path.read_text()) if path.is_file() else _proc("", 1)

    connector = PfSenseConnector()
    with patch.object(connector, "_run_command", new=AsyncMock(side_effect=run)):
        return await connector.config_history(_TARGET, params)


@pytest.mark.asyncio
@pytest.mark.skipif(_SH is None, reason="needs a POSIX sh")
async def test_the_fixture_diffs_show_the_rule_and_hide_the_password(tmp_path: Path) -> None:
    conf = _conf_dir(tmp_path)

    added_rule = await _call_on_fixture_dir(conf, {"revision": _FX_T2})
    assert added_rule["previous_revision"] == _FX_T1
    assert [s["section"] for s in added_rule["changed_sections"]] == ["filter"]
    assert added_rule["changed_sections"][0]["removed"] == 0
    assert "+\t\t\t<descr>Allow DNS</descr>" in added_rule["diff"]
    assert "+\t\t\t\t<port>53</port>" in added_rule["diff"]

    password = await _call_on_fixture_dir(conf, {"revision": _FX_T3})
    assert password["previous_revision"] == _FX_T2
    assert password["user"] == "operator@198.51.100.21 (Local Database)"
    system = password["changed_sections"][0]
    assert (system["section"], system["added"], system["removed"]) == ("system", 2, 1)
    assert "+\t\t<backupcount>10</backupcount>" in password["diff"]
    assert "bcrypt-hash" not in password["diff"]
    assert "-\t\t<dnsserver>192.0.2.53</dnsserver>" in password["diff"]
    assert "+\t\t<dnsserver>192.0.2.54</dnsserver>" in password["diff"]
    dumped = json.dumps(password) + json.dumps(added_rule)
    assert "$2y$10$" not in dumped
