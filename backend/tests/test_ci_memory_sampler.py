# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/memory_sampler.sh`` (the CI memory sampler line).

The tests give the script a made-up cgroup folder and made-up progress files.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "memory_sampler.sh"
_MIB = 1024 * 1024


def _cgroup(tmp_path: Path, used_mib: int, limit: str) -> Path:
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.current").write_text(f"{used_mib * _MIB}\n")
    (cg / "memory.max").write_text(f"{limit}\n")
    (cg / "memory.stat").write_text("anon 1\ninactive_file 0\n")
    return cg


def _run(tmp_path: Path, cg: Path, progress: Path) -> str:
    env = {
        **os.environ,
        "MEMORY_CG_DIR": str(cg),
        "MEHO_CI_PROGRESS_DIR": str(progress),
        "RUNNER_TEMP": str(tmp_path),
    }
    done = subprocess.run(
        ["bash", str(_SCRIPT), "line"], env=env, capture_output=True, text=True, check=True
    )
    return done.stdout


def test_below_the_mark_prints_one_line(tmp_path: Path) -> None:
    out = _run(tmp_path, _cgroup(tmp_path, 3000, str(7000 * _MIB)), tmp_path / "none")
    assert out.count("\n") == 1
    assert "in use 3000 MiB of 7000 MiB (42%)" in out
    assert "Tests running now" not in out


def test_above_the_mark_names_the_running_tests(tmp_path: Path) -> None:
    progress = tmp_path / "progress"
    progress.mkdir()
    (progress / "gw0.txt").write_text("tests/test_a.py::test_one\n")
    (progress / "gw1.txt").write_text("tests/test_b.py::test_two\n")
    out = _run(tmp_path, _cgroup(tmp_path, 6200, str(7000 * _MIB)), progress)
    assert "(88%)" in out
    assert "Tests running now" in out
    assert "gw0 runs: tests/test_a.py::test_one" in out
    assert "gw1 runs: tests/test_b.py::test_two" in out


def test_above_the_mark_without_progress_files_says_so(tmp_path: Path) -> None:
    out = _run(tmp_path, _cgroup(tmp_path, 6900, str(7000 * _MIB)), tmp_path / "none")
    assert "running tests are unknown" in out


def test_no_limit_still_prints_a_line(tmp_path: Path) -> None:
    out = _run(tmp_path, _cgroup(tmp_path, 1000, "max"), tmp_path / "none")
    assert "in use 1000 MiB, no limit known" in out


def test_no_memory_files_is_not_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no cgroup v2 memory files" in _run(tmp_path, empty, tmp_path / "none")


def test_unknown_command_exits_2() -> None:
    done = subprocess.run(["bash", str(_SCRIPT), "bogus"], capture_output=True, text=True)
    assert done.returncode == 2


def test_progress_hook_writes_the_running_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "gw0"
    import tests.conftest as conftest_module

    monkeypatch.setenv("MEHO_CI_PROGRESS_DIR", str(tmp_path))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", name)
    conftest_module.pytest_runtest_logstart("tests/test_x.py::test_y", ("f", 1, "t"))
    assert (tmp_path / f"{name}.txt").read_text() == "tests/test_x.py::test_y\n"


def test_progress_hook_does_nothing_without_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.conftest as conftest_module

    watched = tmp_path / "watched"
    watched.mkdir()
    monkeypatch.delenv("MEHO_CI_PROGRESS_DIR", raising=False)
    monkeypatch.chdir(watched)
    conftest_module.pytest_runtest_logstart("tests/test_x.py::test_y", ("f", 1, "t"))
    assert list(watched.iterdir()) == []
