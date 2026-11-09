# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.file.write`` keeps the mode and owner of a file that already exists.

These tests run the rendered write script with the local ``bash`` against a
file under ``tmp_path``. The script has no root-only step, so it runs as the
test user. This checks what the script really does, not only its text.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from meho_backplane.connectors.linux.ops_write import (
    build_file_write_script,
    parse_file_write_output,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the write script uses GNU stat -c")


def _run(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _leftover_temp_files(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(".meho-write.")]


@pytest.mark.parametrize("backup", [True, False])
def test_existing_file_keeps_its_mode_and_owner(tmp_path: Path, backup: bool) -> None:
    target = tmp_path / "db.example.zone"
    target.write_text("old\n")
    target.chmod(0o644)
    before = target.stat()

    proc = _run(build_file_write_script(str(target), "new\n", backup=backup, validate_command=None))

    assert proc.returncode == 0, proc.stderr
    assert "===MEHO_WRITE_OK===" in proc.stdout
    assert target.read_text() == "new\n"
    after = target.stat()
    assert _mode(target) == 0o644
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert _leftover_temp_files(tmp_path) == []


def test_existing_file_with_special_mode_bits_keeps_them(tmp_path: Path) -> None:
    target = tmp_path / "app.conf"
    target.write_text("old\n")
    target.chmod(0o2640)

    proc = _run(build_file_write_script(str(target), "new\n", backup=True, validate_command=None))

    assert proc.returncode == 0, proc.stderr
    assert _mode(target) == 0o2640


def test_new_file_is_created_0600(tmp_path: Path) -> None:
    target = tmp_path / "new.conf"

    proc = _run(build_file_write_script(str(target), "x\n", backup=True, validate_command=None))

    assert proc.returncode == 0, proc.stderr
    assert target.read_text() == "x\n"
    assert _mode(target) == 0o600
    assert not (tmp_path / "new.conf.meho.bak").exists()


def test_failed_rollback_restores_the_original_mode(tmp_path: Path) -> None:
    target = tmp_path / "named.conf"
    target.write_text("good\n")
    target.chmod(0o644)

    proc = _run(
        build_file_write_script(str(target), "bad\n", backup=True, validate_command="false")
    )

    assert proc.returncode == 3
    assert "===MEHO_WRITE_VALIDATE_FAILED===" in proc.stdout
    assert target.read_text() == "good\n"
    assert _mode(target) == 0o644


def test_mode_copy_failure_stops_before_the_rename(tmp_path: Path) -> None:
    """When the mode cannot be copied, the original file must stay untouched."""
    target = tmp_path / "zone.db"
    target.write_text("original\n")
    target.chmod(0o644)

    # A fake chmod that allows only the first ``chmod 0600`` and fails the copy.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_chmod = fake_bin / "chmod"
    real_chmod = shutil.which("chmod")
    assert real_chmod is not None
    fake_chmod.write_text(
        f'#!/bin/sh\nif [ "$1" = 0600 ]; then exec {real_chmod} "$@"; fi\nexit 1\n'
    )
    fake_chmod.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    script = build_file_write_script(str(target), "replaced\n", backup=False, validate_command=None)
    proc = _run(script, env=env)

    assert proc.returncode == 4
    assert "===MEHO_WRITE_ERR:mode===" in proc.stderr
    assert "===MEHO_WRITE_OK===" not in proc.stdout
    assert target.read_text() == "original\n"
    assert _mode(target) == 0o644
    assert _leftover_temp_files(tmp_path) == []

    result = parse_file_write_output(
        str(target), proc.stdout, proc.returncode, backup=False, validated=False
    )
    assert result["written"] is False
    assert result["error"] == "atomic file write did not complete"
