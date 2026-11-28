# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``start_test_container`` (tests/_container_ready.py).

A container that does not start ends the test with a fixed message: the
image without its registry part and the kind of error. The text of the
Docker daemon's error is in no output line, no traceback and no junit file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._container_ready import ContainerStartRetryWarning, start_test_container

# A made-up text in the daemon's errors and in the registry part of the image.
_MARKER = "zz-daemon-marker-41c9"
_IMAGE = f"{_MARKER}.registry.example/library/mongo:7"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class APIError(Exception):
    """Stands in for ``docker.errors.APIError``: a status code and the daemon's text."""

    def __init__(self, status: int) -> None:
        super().__init__(
            f"{status} Client Error for http+docker://{_MARKER}/v1.47/images/create: {_MARKER}"
        )
        self.status_code = status


def _api_error(status: int) -> APIError:
    """A Docker client error with the marker in its text."""
    return APIError(status)


class _StandInContainer:
    """A container made but not started; ``start()`` does what its plan says."""

    def __init__(self, outcome: bool | BaseException) -> None:
        self.outcome = outcome
        self.ready = outcome is True
        self.stopped = False

    def start(self) -> _StandInContainer:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self

    def stop(self) -> None:
        self.stopped = True


def _maker(
    *tries: bool | BaseException,
) -> tuple[list[_StandInContainer], Callable[[], _StandInContainer]]:
    """Each try is ``True`` (ready), ``False`` (no answer) or an error that ``start()`` raises."""
    made: list[_StandInContainer] = []

    def make() -> _StandInContainer:
        container = _StandInContainer(tries[len(made)])
        made.append(container)
        return container

    return made, make


def _probe(container: _StandInContainer) -> None:
    if not container.ready:
        raise ConnectionRefusedError(f"connection refused {_MARKER}")


def _start(make: Callable[[], _StandInContainer]) -> _StandInContainer:
    fake = _FakeClock()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ContainerStartRetryWarning)
        return start_test_container(
            make,
            image=_IMAGE,
            what="mongodb",
            probe=_probe,
            interval=1,
            clock=fake.clock,
            sleep=fake.sleep,
        )


@pytest.fixture
def on_a_laptop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def under_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")


def test_returns_the_container_when_it_starts(on_a_laptop: None) -> None:
    made, make = _maker(True)

    assert _start(make) is made[0]
    assert not made[0].stopped


def test_no_probe_means_a_container_that_started_is_ready(on_a_laptop: None) -> None:
    made, make = _maker(False)

    assert start_test_container(make, image=_IMAGE, what="x") is made[0]


def test_makes_a_fresh_container_for_the_second_try(on_a_laptop: None) -> None:
    made, make = _maker(TimeoutError(_MARKER), True)

    assert _start(make) is made[1]
    assert made[0].stopped
    assert not made[1].stopped


@pytest.mark.parametrize(
    ("tries", "kind"),
    [
        ((_api_error(500),), "start failed (APIError)"),
        (
            (RuntimeError(f"Container exited. Container error: {_MARKER}"),),
            "start failed (RuntimeError)",
        ),
        ((_api_error(429),), "the registry refused the pull, too many requests (APIError)"),
        (
            (TimeoutError(_MARKER), _api_error(429)),
            "the registry refused the pull, too many requests (APIError)",
        ),
    ],
)
def test_a_start_error_gives_the_fixed_message(
    on_a_laptop: None, tries: tuple[BaseException, ...], kind: str
) -> None:
    made, make = _maker(*tries)

    with pytest.raises(pytest.skip.Exception) as excinfo:
        _start(make)

    assert str(excinfo.value) == f"container did not start: mongodb (image mongo:7): {kind}"
    assert excinfo.value.__context__ is None
    assert all(container.stopped for container in made)


def test_both_tries_without_an_answer_name_both_tries(on_a_laptop: None) -> None:
    made, make = _maker(False, RuntimeError(_MARKER))

    with pytest.raises(pytest.skip.Exception) as excinfo:
        _start(make)

    assert str(excinfo.value) == (
        "container not ready: mongodb (image mongo:7) failed on both tries\n"
        "  try 1: no answer in 60 s (ContainerNotReadyError)\n"
        "  try 2: start failed (RuntimeError)"
    )
    assert excinfo.value.__context__ is None
    assert made[0].stopped


def test_under_ci_the_test_fails_without_a_traceback(under_ci: None) -> None:
    _, make = _maker(_api_error(500))

    with pytest.raises(pytest.fail.Exception) as excinfo:
        _start(make)

    assert (
        str(excinfo.value)
        == "container did not start: mongodb (image mongo:7): start failed (APIError)"
    )
    assert excinfo.value.pytrace is False
    assert excinfo.value.__context__ is None


_INNER_TEST = """
import pytest

from tests.test_container_start import _MARKER, _api_error, _maker, _start


_CASES = ["first", "rate-limit-first", "rate-limit-second", "exited-second", "no-answer"]


@pytest.fixture(params=_CASES)
def container(request):
    tries = {
        "first": (_api_error(500),),
        "rate-limit-first": (_api_error(429),),
        "rate-limit-second": (TimeoutError(_MARKER), _api_error(429)),
        "exited-second": (False, RuntimeError("Container exited. Container error: " + _MARKER)),
        "no-answer": (False, False),
    }[request.param]
    _, make = _maker(*tries)
    return _start(make)


def test_uses_the_container(container):
    assert container
"""


@pytest.mark.parametrize("ci", [True, False], ids=["fail-under-ci", "skip-on-a-laptop"])
def test_the_daemon_text_is_in_no_output_and_no_junit_file(tmp_path: Path, ci: bool) -> None:
    """Run a real pytest with the CI flags: the marker is in no output line and no junit file."""
    (tmp_path / "test_inner.py").write_text(_INNER_TEST)
    junit = tmp_path / "junit.xml"
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(backend)}
    env.pop("GITHUB_ACTIONS", None)
    if ci:
        env["GITHUB_ACTIONS"] = "true"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            "-rA",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={junit}",
            str(tmp_path / "test_inner.py"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=False,
    )

    output = result.stdout + result.stderr
    if ci:
        assert result.returncode == 1, output
        assert "5 errors" in output
    else:
        assert result.returncode == 0, output
        assert "5 skipped" in output
    assert output.count("container did not start: mongodb (image mongo:7)") >= 2
    assert "failed on both tries" in output
    assert _MARKER not in output
    assert _MARKER not in junit.read_text()
