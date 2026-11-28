# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI pulls the test images before pytest, and no container test skips in CI.

A short break of the image registry made container tests skip, and the
skip limit check then failed the run. So:

1. Every job of ``ci.yml`` that runs container tests has the step "Pull the
   test images" before its pytest step. The step runs
   ``scripts/ci/pull_test_images.sh``, which pulls every image the job sets.
2. The script looks on the runner first and uses an image that is there,
   with no pull. A missing image gets at most 3 pulls, 10 s apart, each
   under the job's time limit ``PULL_TIME_LIMIT``; then it fails. The
   script never prints Docker's own output (the logs of this repository
   are public).
3. Under CI (``GITHUB_ACTIONS`` set) a fixture whose container does not
   start fails the test instead of skipping it.

The script tests use a stand-in ``docker``, ``sleep`` and ``timeout`` on the PATH.
"""

from __future__ import annotations

import ast
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._ci_image_check import (
    REPO,
    TESTS,
    is_container_source,
    jobs,
    jobs_running_tests,
    sources_of,
)

_SCRIPT = REPO / "scripts" / "ci" / "pull_test_images.sh"
_STEP = "Pull the test images"
_PYTEST = re.compile(r"\buv\s+run\s+pytest\b")
_IMAGE_LINE = re.compile(
    r"^\s*((?:MEHO_TEST_[A-Z0-9_]+_IMAGE|RYUK_CONTAINER_IMAGE)):\s*PROXY(?:_ONLY)?/(\S+)\s*$",
    re.MULTILINE,
)
_NOISE = "docker-output-that-must-not-show"
_LIMIT = "50"


def _container_jobs() -> list[str]:
    return sorted(
        job_id
        for job_id, files in jobs_running_tests().items()
        if any(is_container_source(path) for path in sources_of(files))
    )


def _step_index(steps: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool]) -> int:
    return next(i for i, step in enumerate(steps) if match(step))


# --- 1. the step in the workflow --------------------------------------------


def test_the_container_jobs_come_from_the_workflow() -> None:
    assert {
        "python-unit-shard",
        "python-unit-shard-second-site",
        "python-outside-backend",
        "python-coverage",
        "python-integration-shard",
        "python-timing-tests",
        "python-migration-tests",
    } <= set(_container_jobs())


@pytest.mark.parametrize("job_id", _container_jobs())
def test_each_container_job_pulls_its_images_before_pytest(job_id: str) -> None:
    steps = jobs("ci.yml")[job_id]["steps"]
    pytest_at = _step_index(steps, lambda s: bool(_PYTEST.search(str(s.get("run", "")))))
    pulls = [i for i, step in enumerate(steps) if step.get("name") == _STEP]
    assert pulls, f"{job_id} has no step {_STEP!r}"
    assert len(pulls) == 1
    pull_at = pulls[0]
    assert pull_at < pytest_at, f"{job_id}: {_STEP!r} must come before pytest"
    step = steps[pull_at]
    assert "scripts/ci/pull_test_images.sh" in step["run"]
    assert step.get("timeout-minutes")
    assert "if" not in step, "the pull runs on every event"
    # The images must be set before the pull, and a Docker Hub login too.
    for i, other in enumerate(steps):
        sets_images = bool(_IMAGE_LINE.search(str(other.get("run", ""))))
        login = str(other.get("name", "")).startswith("Docker Hub login")
        if sets_images or login:
            assert i < pull_at, f"{job_id}: {other.get('name')!r} must come before the pull"


@pytest.mark.parametrize("job_id", _container_jobs())
def test_three_pulls_and_two_pauses_fit_into_the_step_limit(job_id: str) -> None:
    step = next(s for s in jobs("ci.yml")[job_id]["steps"] if s.get("name") == _STEP)
    limit = int(step["env"]["PULL_TIME_LIMIT"])
    assert 3 * limit + 2 * 10 <= int(step["timeout-minutes"]) * 60


def _fake_bin(
    tmp_path: Path, pull_fails: int, on_runner: bool, pull: str, timeout: str
) -> tuple[Path, Path]:
    """A ``docker``, a ``sleep`` and a ``timeout`` that log their calls to ``calls.log``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    count = tmp_path / "pull-count"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        f'echo "{_NOISE} stdout"\n'
        f'echo "{_NOISE} stderr" >&2\n'
        'if [ "$1" = pull ]; then\n'
        f"  {pull}\n"
        f'  n=$(( $(cat "{count}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{count}"\n'
        f'  [ "$n" -gt {pull_fails} ] && exit 0 || exit 7\n'
        "fi\n"
        f'[ "$1 $2" = "image inspect" ] && exit {0 if on_runner else 1}\n'
        "exit 3\n"
    )
    sleep = bin_dir / "sleep"
    sleep.write_text(f'#!/usr/bin/env bash\necho "sleep $*" >> "{log}"\n')
    timeout_bin = bin_dir / "timeout"
    timeout_bin.write_text(f'#!/usr/bin/env bash\necho "timeout $*" >> "{log}"\n{timeout}\n')
    for path in (docker, sleep, timeout_bin):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def _run(
    tmp_path: Path,
    env: dict[str, str],
    *args: str,
    pull_fails: int = 0,
    on_runner: bool = False,
    pull: str = ":",
    timeout: str = 'shift; exec "$@"',
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir, log = _fake_bin(tmp_path, pull_fails, on_runner, pull, timeout)
    result = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "PULL_TIME_LIMIT": _LIMIT, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _job_env(job_id: str) -> tuple[dict[str, str], set[str]]:
    """The image variables the job sets (with a made-up proxy) and the images it needs."""
    job = jobs("ci.yml")[job_id]
    env: dict[str, str] = {}
    for step in job["steps"]:
        for name, path in _IMAGE_LINE.findall(str(step.get("run", ""))):
            env[name] = f"proxy.example.test/hub/{path}"
    for name, value in job.get("env", {}).items():
        if name == "MEHO_TEST_K3S_TAGS":
            env[name] = str(value)
    wanted = set(env.values()) - {env.get("MEHO_TEST_K3S_TAGS", "")}
    if "MEHO_TEST_K3S_TAGS" in env:
        name = env["MEHO_TEST_K3S_IMAGE"].rpartition(":")[0]
        wanted |= {f"{name}:{tag}" for tag in env["MEHO_TEST_K3S_TAGS"].split(",")}
    return env, wanted


@pytest.mark.parametrize("job_id", [j for j in _container_jobs() if j != "python-outside-backend"])
def test_the_step_pulls_every_image_the_job_sets(job_id: str, tmp_path: Path) -> None:
    env, wanted = _job_env(job_id)
    assert wanted, f"{job_id} sets no test image"
    result, calls = _run(tmp_path, env)
    assert result.returncode == 0, result.stdout
    pulled = {call.split()[-1] for call in calls if call.startswith("pull ")}
    assert pulled == wanted


def test_the_outside_backend_job_pulls_the_chart_postgres_image() -> None:
    job = jobs("ci.yml")["python-outside-backend"]
    # testcontainers starts its Ryuk image first; the pull step reads the variable.
    assert job.get("env", {}).get("RYUK_CONTAINER_IMAGE") == "testcontainers/ryuk:0.8.1"
    step = next(s for s in job["steps"] if s["name"] == _STEP)
    assert "pgvector_test_image()" in step["run"]
    assert 'pull_test_images.sh" "$image"' in step["run"]


# --- 2. the script ----------------------------------------------------------


def test_a_pull_that_fails_twice_then_works_passes(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {}, "example/image:1", pull_fails=2)
    assert result.returncode == 0
    assert [c for c in calls if c.startswith("pull")] == ["pull --quiet example/image:1"] * 3
    assert calls.count("sleep 10") == 2
    assert result.stdout.count("Trying the pull of example/image:1 again in 10 s.") == 2
    assert "exit code 7" in result.stdout
    assert "Pulled example/image:1 (try 3 of 3)." in result.stdout
    assert _NOISE not in result.stdout + result.stderr


def test_three_failed_pulls_fail_when_the_image_is_not_on_the_runner(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {}, "example/image:1", pull_fails=3)
    assert result.returncode == 1
    assert len([c for c in calls if c.startswith("pull")]) == 3
    assert calls.count("sleep 10") == 2
    assert (
        "SETUP FAILURE, not a test failure: the test image example/image:1 did not pull "
        "after 3 tries and is not on the runner." in result.stdout
    )
    assert _NOISE not in result.stdout + result.stderr


def test_an_image_on_the_runner_passes_with_no_pull(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {}, "example/image:1", pull_fails=3, on_runner=True)
    assert result.returncode == 0
    assert calls == ["image inspect example/image:1"]
    assert "The copy on the runner is used, with no pull." in result.stdout
    assert _NOISE not in result.stdout + result.stderr


def test_images_on_the_runner_need_no_pull_and_no_pause(tmp_path: Path) -> None:
    images = [f"example/image:{n}" for n in range(1, 6)]
    result, calls = _run(tmp_path, {}, *images, pull_fails=99, on_runner=True)
    assert result.returncode == 0, result.stdout
    assert calls == [f"image inspect {i}" for i in images]
    assert result.stdout.count("The copy on the runner is used, with no pull.") == 5
    assert _NOISE not in result.stdout + result.stderr


def test_images_on_the_runner_pass_fast_when_a_pull_never_answers(tmp_path: Path) -> None:
    images = [f"example/image:{n}" for n in range(1, 6)]
    started = time.monotonic()
    result, calls = _run(tmp_path, {}, *images, on_runner=True, pull="exec /bin/sleep 600")
    assert time.monotonic() - started < 10
    assert result.returncode == 0, result.stdout
    assert not [c for c in calls if c.startswith(("pull", "timeout"))]


def test_a_missing_image_whose_pull_hangs_fails_under_the_time_limit(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {}, "example/image:1", timeout="exit 124")
    assert result.returncode == 1
    assert [c for c in calls if c.startswith("timeout")] == [
        f"timeout {_LIMIT} docker pull --quiet example/image:1"
    ] * 3
    assert calls.count("sleep 10") == 2
    assert result.stdout.count("exit code 124") == 3
    assert (
        "SETUP FAILURE, not a test failure: the test image example/image:1 did not pull "
        "after 3 tries and is not on the runner." in result.stdout
    )


def test_a_failed_image_does_not_stop_the_pull_of_the_others(tmp_path: Path) -> None:
    env = {"MEHO_TEST_A_IMAGE": "example/a:1", "RYUK_CONTAINER_IMAGE": "example/ryuk:1"}
    result, calls = _run(tmp_path, env, pull_fails=3)
    assert result.returncode == 1
    pulled = [c.split()[-1] for c in calls if c.startswith("pull")]
    assert pulled == ["example/a:1"] * 3 + ["example/ryuk:1"]


def test_images_come_from_the_variables_once_each(tmp_path: Path) -> None:
    env = {
        "MEHO_TEST_A_IMAGE": "example/a:1",
        "MEHO_TEST_B_IMAGE": "example/a:1",
        "MEHO_TEST_EMPTY_IMAGE": "",
        "MEHO_TEST_K3S_IMAGE": "registry.example.test:5000/rancher/k3s:v1",
        "MEHO_TEST_K3S_TAGS": "v1, v2",
        "OTHER_IMAGE": "example/not-a-test-image:1",
    }
    result, calls = _run(tmp_path, env)
    assert result.returncode == 0
    pulled = sorted(c.split()[-1] for c in calls if c.startswith("pull"))
    assert pulled == [
        "example/a:1",
        "registry.example.test:5000/rancher/k3s:v1",
        "registry.example.test:5000/rancher/k3s:v2",
    ]


def test_no_image_at_all_fails(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, {})
    assert result.returncode == 1
    assert "names no test image" in result.stdout
    assert calls == []


# --- 3. no container test skips in CI ---------------------------------------


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == name
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "pytest"
    ]


def _fails_in_ci(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if (
            isinstance(node, ast.If)
            and "GITHUB_ACTIONS" in ast.unparse(node.test)
            and any(_calls(stmt, "fail") for stmt in node.body)
        ):
            return True
    return False


def _starts_a_container(body: list[ast.stmt]) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "start"
        for stmt in body
        for n in ast.walk(stmt)
    )


def test_no_integration_container_start_skips_in_ci() -> None:
    problems = []
    for path in sorted((TESTS / "integration").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not _starts_a_container(node.body):
                continue
            for handler in node.handlers:
                if _calls(handler, "skip") and not _fails_in_ci(handler):
                    problems.append(f"{path.relative_to(TESTS)}:{handler.lineno}")
    assert problems == [], (
        "a container start that fails skips the test also in CI; "
        'add `if os.environ.get("GITHUB_ACTIONS"): pytest.fail(reason)` before the skip'
    )


class _RefusingContainer:
    """Stands in for ``DockerContainer``: every setting call works, ``start`` fails."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __getattr__(self, name: str) -> Callable[..., _RefusingContainer]:
        return lambda *args, **kwargs: self

    def start(self) -> None:
        raise RuntimeError("image not found")


@pytest.fixture
def vault_fixture(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Iterator[str]]:
    from tests.integration import test_scheduler_vault_credentials_dev_e2e as module

    monkeypatch.setattr(module, "DOCKER_AVAILABLE", True)
    monkeypatch.setattr("testcontainers.core.container.DockerContainer", _RefusingContainer)
    fixture: Callable[[], Iterator[str]] = module.vault_dev_addr.__wrapped__
    return fixture


def test_a_fixture_fails_in_ci_when_its_container_does_not_start(
    vault_fixture: Callable[[], Iterator[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(pytest.fail.Exception, match="vault dev container failed to start"):
        next(vault_fixture())


def test_the_same_fixture_skips_outside_ci(
    vault_fixture: Callable[[], Iterator[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="vault dev container failed to start"):
        next(vault_fixture())
