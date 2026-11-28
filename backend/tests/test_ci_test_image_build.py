# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A test that builds an image takes its base image from the runner.

Docker Hub can answer "429 Too Many Requests" while a test builds its image,
and the test then fails for a reason that has nothing to do with the change.
So:

1. Every test image build goes through ``tests/_test_image_build.py``. It
   builds with ``pull=False``, so Docker uses the base image on the runner
   and does not ask a registry again. A failed build names only the tag,
   never Docker's own output.
2. The base image of each build is a ``MEHO_TEST_*_IMAGE`` variable that
   every job running the build sets through the image proxy. The step "Pull
   the test images" pulls each such variable before pytest
   (``tests/test_ci_pull_test_images.py``), so the base image is on the
   runner when the build starts.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any, ClassVar

import docker.errors
import pytest

from tests import _test_image_build
from tests import test_ci_pull_test_images as pull_step
from tests._ci_image_check import (
    SECRETS_STEP,
    TESTS,
    VARIABLE,
    files_of_job,
    image_block,
    jobs,
    jobs_running_tests,
    sources_of,
)

_HELPER = "build_test_image"
_BOOKWORM = "MEHO_TEST_DEBIAN_BOOKWORM_IMAGE"
_TRIXIE = "MEHO_TEST_DEBIAN_TRIXIE_IMAGE"
_BIND9_FILES = {
    "test_g3_4_bind9_e2e.py",
    "test_connectors_bind9_container.py",
    "test_connectors_bind9_container_trixie.py",
}


def _base_variables(path: Path) -> set[str]:
    """The image variables that the build arguments of each build in *path* read."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _HELPER
        ):
            args = [*node.args[2:], *(k.value for k in node.keywords)]
            found |= {name for arg in args for name in VARIABLE.findall(ast.unparse(arg))}
    return found


def _builds(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == _HELPER
        for n in ast.walk(tree)
    )


def base_images_not_pulled(job: dict[str, Any]) -> list[str]:
    """``file: VARIABLE`` for each base image of a build that *job* does not pull."""
    files = files_of_job(job) or set()
    pulled = {name for name, path in image_block(job).items() if path.startswith("PROXY/")}
    return sorted(
        f"{source.relative_to(TESTS)}: {name}"
        for source in sources_of(files)
        if _builds(source)
        for name in _base_variables(source) - pulled
    )


def _bind9_jobs() -> list[str]:
    return sorted(
        job_id
        for job_id, files in jobs_running_tests().items()
        if any(path.name in _BIND9_FILES for path in files)
    )


# --- 2. the base images are on the pull list ---------------------------------


def test_the_bind9_tests_run_in_a_job() -> None:
    assert _bind9_jobs()


@pytest.mark.parametrize("job_id", _bind9_jobs())
def test_the_bind9_jobs_pull_both_debian_base_images(job_id: str, tmp_path: Path) -> None:
    env, _ = pull_step._job_env(job_id)
    result, calls = pull_step._run(tmp_path, env)
    assert result.returncode == 0, result.stdout
    pulled = {call.split()[-1] for call in calls if call.startswith("pull ")}
    assert {
        "proxy.example.test/hub/library/debian:bookworm-slim",
        "proxy.example.test/hub/library/debian:trixie-slim",
    } <= pulled


def test_each_bind9_build_reads_its_base_image_variable() -> None:
    integration = TESTS / "integration"
    assert _base_variables(integration / "test_connectors_bind9_container.py") == {_BOOKWORM}
    assert _base_variables(integration / "test_g3_4_bind9_e2e.py") == {_BOOKWORM}
    assert _base_variables(integration / "test_connectors_bind9_container_trixie.py") == {_TRIXIE}


@pytest.mark.parametrize("job_id", sorted(jobs_running_tests()))
def test_every_job_pulls_the_base_images_of_its_builds(job_id: str) -> None:
    assert base_images_not_pulled(jobs("ci.yml")[job_id]) == []


def test_the_check_finds_a_base_image_that_is_not_pulled() -> None:
    job = copy.deepcopy(jobs("ci.yml")[_bind9_jobs()[0]])
    for step in job["steps"]:
        if step.get("name") == SECRETS_STEP:
            step["run"] = "\n".join(
                line for line in step["run"].splitlines() if f"{_TRIXIE}:" not in line
            )
    assert base_images_not_pulled(job) == [
        f"integration/test_connectors_bind9_container_trixie.py: {_TRIXIE}"
    ]


def test_every_test_image_build_goes_through_the_helper() -> None:
    direct = sorted(
        str(path.relative_to(TESTS))
        for path in TESTS.rglob("*.py")
        if path.name != "_test_image_build.py"
        and path != Path(__file__).resolve()
        and "DockerImage(" in path.read_text()
    )
    assert direct == [], f"build test images with tests/_test_image_build.py: {direct}"


# --- 1. the build uses the base image on the runner --------------------------

_BASE = "proxy.example.test/hub/library/debian:trixie-slim"
_NOISE = "docker-output-that-must-not-show"


class _BuiltImage:
    id = "sha256:0123456789abcdef"


class _StandInDocker:
    """Stands in for testcontainers' ``DockerClient``: a Docker with a local image store.

    Like the Docker daemon, a build pulls its base image when it is asked to
    (``pull=True``) or when the image is not on the runner.
    """

    on_runner: ClassVar[set[str]] = set()
    registry_calls: ClassVar[list[str]] = []
    dockerfiles: ClassVar[list[str]] = []
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, **kwargs: object) -> None:
        pass

    def build(self, path: str, **kwargs: Any) -> tuple[object, list[object]]:
        type(self).dockerfiles.append((Path(path) / kwargs["dockerfile"]).read_text())
        if self.fail_with is not None:
            raise self.fail_with
        base = kwargs["buildargs"]["DEBIAN_IMAGE"]
        if kwargs.get("pull", False) or base not in self.on_runner:
            type(self).registry_calls.append(base)
        return _BuiltImage(), []


@pytest.fixture
def docker_stand_in(monkeypatch: pytest.MonkeyPatch) -> type[_StandInDocker]:
    monkeypatch.setattr("testcontainers.core.image.DockerClient", _StandInDocker)
    monkeypatch.setattr(_StandInDocker, "on_runner", set())
    monkeypatch.setattr(_StandInDocker, "registry_calls", [])
    monkeypatch.setattr(_StandInDocker, "dockerfiles", [])
    monkeypatch.setattr(_StandInDocker, "fail_with", None)
    return _StandInDocker


def test_the_build_does_not_pull_a_base_image_that_is_on_the_runner(
    docker_stand_in: type[_StandInDocker],
) -> None:
    docker_stand_in.on_runner = {_BASE}
    _test_image_build.build_test_image(
        "ARG DEBIAN_IMAGE\nFROM ${DEBIAN_IMAGE}\n", "t:1", {"DEBIAN_IMAGE": _BASE}
    )
    assert docker_stand_in.registry_calls == []
    assert docker_stand_in.dockerfiles == ["ARG DEBIAN_IMAGE\nFROM ${DEBIAN_IMAGE}\n"]


def test_the_build_pulls_a_base_image_that_is_not_on_the_runner(
    docker_stand_in: type[_StandInDocker],
) -> None:
    _test_image_build.build_test_image("FROM ${DEBIAN_IMAGE}\n", "t:1", {"DEBIAN_IMAGE": _BASE})
    assert docker_stand_in.registry_calls == [_BASE]


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.BuildError(f"{_NOISE} reason", [{"stream": f"{_NOISE} log"}]),
        docker.errors.APIError(f"{_NOISE} api"),
    ],
    ids=["build-error", "api-error"],
)
def test_a_failed_build_names_only_the_tag(
    docker_stand_in: type[_StandInDocker], error: Exception
) -> None:
    docker_stand_in.fail_with = error
    with pytest.raises(RuntimeError) as caught:
        _test_image_build.build_test_image(
            "FROM ${DEBIAN_IMAGE}\n", "meho-test-x:1", {"DEBIAN_IMAGE": _BASE}
        )
    assert "meho-test-x:1" in str(caught.value)
    assert _NOISE not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
