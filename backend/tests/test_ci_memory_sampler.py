# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/memory_sampler.sh`` (the CI memory sampler line).

The tests give the script a made-up cgroup folder, made-up progress files and
a made-up ``/proc`` folder. They also check that the CI test jobs start the
sampler and show its lines.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "memory_sampler.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_MIB = 1024 * 1024


def _cgroup(tmp_path: Path, used_mib: int, limit: str) -> Path:
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.current").write_text(f"{used_mib * _MIB}\n")
    (cg / "memory.max").write_text(f"{limit}\n")
    (cg / "memory.stat").write_text("anon 1\ninactive_file 0\n")
    return cg


def _proc(tmp_path: Path, rss_mib: dict[int, int]) -> Path:
    proc = tmp_path / "proc"
    for pid, mib in rss_mib.items():
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "status").write_text(f"Name:\tpython\nVmRSS:\t{mib * 1024} kB\n")
    return proc


def _progress(tmp_path: Path, lines: dict[str, str]) -> Path:
    progress = tmp_path / "progress"
    progress.mkdir()
    for worker, line in lines.items():
        (progress / f"{worker}.txt").write_text(line + "\n")
    return progress


def _run(tmp_path: Path, cg: Path, progress: Path, proc: Path | None = None) -> str:
    env = {
        **os.environ,
        "MEMORY_CG_DIR": str(cg),
        "MEHO_CI_PROGRESS_DIR": str(progress),
        "RUNNER_TEMP": str(tmp_path),
    }
    if proc is not None:
        env["MEMORY_PROC_DIR"] = str(proc)
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
    progress = _progress(
        tmp_path, {"gw0": "101 tests/test_a.py::test_one", "gw1": "102 tests/test_b.py::test_two"}
    )
    proc = _proc(tmp_path, {101: 500, 102: 900})
    out = _run(tmp_path, _cgroup(tmp_path, 6200, str(7000 * _MIB)), progress, proc)
    assert "(88%)" in out
    assert "largest pytest worker: gw1 pid 102 900 MiB" in out
    assert "Tests running now" in out
    assert "gw0 pid 101 500 MiB runs: tests/test_a.py::test_one" in out
    assert "gw1 pid 102 900 MiB runs: tests/test_b.py::test_two" in out


def test_a_bigger_process_that_is_no_worker_is_not_named(tmp_path: Path) -> None:
    progress = _progress(
        tmp_path, {"gw0": "101 tests/test_a.py::test_one", "gw1": "102 tests/test_b.py::test_two"}
    )
    # pid 999 stands for the runner or another program: more memory, no worker.
    proc = _proc(tmp_path, {101: 1200, 102: 300, 999: 5000})
    out = _run(tmp_path, _cgroup(tmp_path, 3000, str(7000 * _MIB)), progress, proc)
    assert "largest pytest worker: gw0 pid 101 1200 MiB" in out
    assert "999" not in out


def test_an_ended_worker_is_not_the_largest(tmp_path: Path) -> None:
    progress = _progress(
        tmp_path, {"gw0": "101 tests/test_a.py::test_one", "gw1": "102 tests/test_b.py::test_two"}
    )
    proc = _proc(tmp_path, {102: 300})
    out = _run(tmp_path, _cgroup(tmp_path, 6500, str(7000 * _MIB)), progress, proc)
    assert "largest pytest worker: gw1 pid 102 300 MiB" in out
    assert "gw0 pid 101 (ended) ran: tests/test_a.py::test_one" in out


def test_the_real_proc_folder_gives_the_memory_of_a_worker(tmp_path: Path) -> None:
    if not Path(f"/proc/{os.getpid()}/status").is_file():
        pytest.skip("no /proc folder here")
    progress = _progress(tmp_path, {"main": f"{os.getpid()} tests/test_a.py::test_one"})
    out = _run(tmp_path, _cgroup(tmp_path, 1000, str(7000 * _MIB)), progress)
    assert f"largest pytest worker: main pid {os.getpid()} " in out
    assert "largest pytest worker: unknown" not in out


def test_above_the_mark_without_progress_files_says_so(tmp_path: Path) -> None:
    out = _run(tmp_path, _cgroup(tmp_path, 6900, str(7000 * _MIB)), tmp_path / "none")
    assert "largest pytest worker: unknown" in out
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
    conftest_module.pytest_runtest_protocol(_item("tests/test_x.py::test_y"), None)
    assert (tmp_path / f"{name}.txt").read_text() == f"{os.getpid()} tests/test_x.py::test_y\n"


def test_progress_hook_does_nothing_without_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.conftest as conftest_module

    watched = tmp_path / "watched"
    watched.mkdir()
    monkeypatch.delenv("MEHO_CI_PROGRESS_DIR", raising=False)
    monkeypatch.chdir(watched)
    conftest_module.pytest_runtest_protocol(_item("tests/test_x.py::test_y"), None)
    assert list(watched.iterdir()) == []


def _item(nodeid: str) -> pytest.Item:
    return cast(pytest.Item, SimpleNamespace(nodeid=nodeid))


def _steps(job: str) -> list[dict[str, Any]]:
    workflow = cast(dict[str, Any], yaml.safe_load(_WORKFLOW.read_text()))
    return cast(list[dict[str, Any]], workflow["jobs"][job]["steps"])


@pytest.mark.parametrize(
    "job", ["python-unit-shard", "python-coverage", "python-integration-shard"]
)
def test_the_test_job_starts_the_sampler_and_shows_its_lines(job: str) -> None:
    steps = _steps(job)
    runs = [str(step.get("run", "")) for step in steps]
    start = next(i for i, run in enumerate(runs) if 'memory_sampler.sh" run' in run)
    shown = [
        (i, step)
        for i, step in enumerate(steps)
        if 'tail -n +1 -F "${RUNNER_TEMP}/memory-log.txt"' in str(step.get("run", ""))
    ]
    assert len(shown) == 1
    pytest_index, pytest_step = shown[0]
    assert start < pytest_index
    assert "uv run pytest" in pytest_step["run"]
    assert pytest_step["env"]["MEHO_CI_PROGRESS_DIR"] == "${{ runner.temp }}/pytest-progress"
    stops = [
        step
        for step in steps[pytest_index + 1 :]
        if "memory-sampler.pid" in str(step.get("run", "")) and step.get("if") == "always()"
    ]
    assert stops, f"{job} has no step that stops the sampler"
