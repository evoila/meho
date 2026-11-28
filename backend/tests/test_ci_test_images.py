# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every image a CI test uses comes through the CI image proxy.

A test that pulls or builds from Docker Hub can fail with "429 Too Many
Requests", and a required check then fails for a reason that has nothing to
do with the change. So CI sends each test image through the image proxy
cache. These checks keep it that way:

1. A test file that starts containers names no image in plain text. The
   image place of a container start or a pod spec holds a value read from a
   ``MEHO_TEST_*_IMAGE`` variable, or a ``FROM`` line takes a build argument.
2. Each job that runs pytest (found from the workflow: its ``find``,
   ``grep -rl`` and file lists) sets every image variable that its test
   files, their ``conftest.py`` files and their helper modules read, with
   ``PROXY/`` or ``PROXY_ONLY/`` in its step "Read the CI repository
   secrets".
3. Each command of a test job that pulls an image names a variable that the
   job sets through the proxy, in pull request runs and in merge-queue runs.

The exception list is ``tests/ci_test_image_exceptions.json``. The reader is
``tests/_ci_image_check.py``. See ``docs/codebase/devops.md``, "Test images
come through the image proxy".
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from typing import Any

import pytest
import yaml

from tests._ci_image_check import (
    BACKEND,
    REPO,
    SECRETS_STEP,
    TESTS,
    allowed_unset,
    container_jobs,
    excepted_jobs,
    exceptions,
    files_of_job,
    image_block,
    jobs,
    jobs_running_tests,
    literal_images,
    merge_queue_runs_self_hosted,
    missing_variables,
    needed_variables,
    outside_pulls,
    scanned_files,
    stale_variable_exceptions,
    variables_not_through_proxy,
)

_EVENTS = ("pull_request", "merge_group")


def _without_line(job: dict[str, Any], variable: str) -> dict[str, Any]:
    """A copy of *job* whose image block has no line for *variable*."""
    changed = copy.deepcopy(job)
    for step in changed["steps"]:
        if step.get("name") == SECRETS_STEP:
            step["run"] = "\n".join(
                line for line in step["run"].splitlines() if f"{variable}:" not in line
            )
    return changed


# ---------------------------------------------------------------------------
# The repository today
# ---------------------------------------------------------------------------


def test_no_test_names_an_image_in_plain_text() -> None:
    allowed = {(e["file"], e["image"]) for e in exceptions()["images_in_tests"]}
    bad = [
        f"{path.relative_to(TESTS)}: {image}"
        for path in scanned_files()
        for image in literal_images(path.read_text())
        if (str(path.relative_to(TESTS)), image) not in allowed
    ]
    assert not bad, (
        "A test names an image in plain text. Read it from a MEHO_TEST_*_IMAGE variable "
        "(or a build argument), and set the variable with PROXY/ in ci.yml:\n" + "\n".join(bad)
    )


def test_each_job_sets_the_image_variables_of_its_tests_through_the_proxy() -> None:
    files_by_job = {
        job_id: files
        for job_id, files in jobs_running_tests().items()
        if ("ci.yml", job_id) not in excepted_jobs()
    }
    bad = missing_variables(jobs("ci.yml"), files_by_job, allowed_unset())
    assert not bad, (
        "A job runs a test that reads these image variables, but does not set them "
        f"with PROXY/ or PROXY_ONLY/ in the step '{SECRETS_STEP}': {bad}"
    )


@pytest.mark.parametrize("event", _EVENTS)
def test_no_workflow_step_of_a_test_job_pulls_from_outside(event: str) -> None:
    bad = [
        f"{workflow}: job {job_id}: {pull}"
        for workflow in ("ci.yml", "chart.yml")
        for job_id, job in jobs(workflow).items()
        if (workflow, job_id) not in excepted_jobs()
        for pull in outside_pulls(job, event)
    ]
    assert not bad, (
        "A command pulls an image that does not come through the CI image proxy. Use "
        "the proxy, or add the job to tests/ci_test_image_exceptions.json with a reason:\n"
        + "\n".join(bad)
    )


def test_a_merge_queue_run_of_an_excepted_job_pulls_through_the_proxy() -> None:
    """An exception covers the GitHub-hosted runs only.

    A job that the merge queue sends to the self-hosted pool reaches the
    proxy there, so its merge-queue run pulls nothing from outside.
    """
    queued = [
        (workflow, job_id)
        for workflow, job_id in sorted(excepted_jobs())
        if merge_queue_runs_self_hosted(jobs(workflow)[job_id])
    ]
    assert ("ci.yml", "redaction-compare") in queued
    bad = {
        job_id: pulls
        for workflow, job_id in queued
        if (pulls := outside_pulls(jobs(workflow)[job_id], "merge_group"))
    }
    assert not bad, bad


def test_every_exception_has_a_reason_and_still_applies() -> None:
    entries = exceptions()
    for kind in ("images_in_tests", "variables_not_set", "workflow_jobs"):
        for entry in entries[kind]:
            assert entry["reason"].strip(), entry
    for entry in entries["workflow_jobs"]:
        job = jobs(entry["workflow"]).get(entry["job"])
        assert job is not None, f"the job of this exception is gone: {entry}"
        files = jobs_running_tests(entry["workflow"]).get(entry["job"], set())
        unset = variables_not_through_proxy(job, needed_variables(files))
        assert outside_pulls(job, "pull_request") or unset, (
            f"this job no longer pulls from outside: {entry}"
        )
    stale = stale_variable_exceptions(
        entries["variables_not_set"], jobs("ci.yml"), jobs_running_tests()
    )
    assert not stale, f"these variables_not_set entries no longer apply: {stale}"
    for entry in entries["images_in_tests"]:
        source = (TESTS / entry["file"]).read_text()
        assert entry["image"] in literal_images(source), (
            f"this exception no longer applies: {entry}"
        )


def test_the_jobs_that_run_tests_come_from_the_workflow() -> None:
    found = jobs_running_tests()
    for job_id in (
        "python-unit-shard",
        "python-coverage",
        "python-integration-shard",
        "python-migration-tests",
        "python-timing-tests",
        "python-outside-backend",
    ):
        assert found.get(job_id), job_id
    timing = {p.relative_to(TESTS).as_posix() for p in found["python-timing-tests"]}
    assert "integration/test_topology_path_pruning.py" in timing
    unit = {p.relative_to(TESTS).parts[0] for p in found["python-unit-shard"]}
    assert not unit & {"integration", "migrations"}
    assert {"integration", "migrations"} <= {
        p.relative_to(TESTS).parts[0]
        for p in found["python-coverage"]
        | found["python-integration-shard"]
        | found["python-migration-tests"]
    }


@pytest.mark.parametrize("job_id", container_jobs())
def test_the_cleanup_helper_of_testcontainers_comes_through_the_proxy(job_id: str) -> None:
    """The library starts its Ryuk helper image on every daemon that runs a container test.

    It names ``testcontainers/ryuk`` on Docker Hub by default. The variable
    ``RYUK_CONTAINER_IMAGE`` is the library's own switch for another name.
    """
    from testcontainers.core.config import testcontainers_config

    job = jobs("ci.yml")[job_id]
    assert not variables_not_through_proxy(job, {"RYUK_CONTAINER_IMAGE"})
    # Same repository and tag as the library's default, behind the proxy name.
    default = "/".join(testcontainers_config.ryuk_image.rsplit("/", 2)[-2:])
    assert image_block(job)["RYUK_CONTAINER_IMAGE"].endswith(f"/{default}")


@pytest.mark.parametrize(
    ("variable", "subchart"),
    [
        ("MEHO_TEST_CORE_KEYCLOAK_IMAGE", "keycloak"),
        ("MEHO_TEST_CORE_OPENBAO_IMAGE", "openbao"),
    ],
)
def test_core_images_follow_the_chart_digest(variable: str, subchart: str) -> None:
    """The CI image of a chart component is the digest that the chart pins.

    A tag can move to another build, so CI names the image by the chart's digest.
    """
    values = yaml.safe_load(
        (REPO / "deploy" / "charts" / "meho" / "charts" / subchart / "values.yaml").read_text()
    )
    digest = values["image"]["digest"]
    assert digest.startswith("sha256:")
    block = image_block(jobs("ci.yml")["python-integration-shard"])
    assert block[variable].endswith(f"/{subchart}/{subchart}@{digest}"), block[variable]


_SAMPLE_PROXY = "hidden-proxy.invalid:5443/mirror-path"
_VERSION_MODULES = (
    "tests/integration/test_connectors_mongodb_container.py",
    "tests/integration/test_connectors_postgres_container.py",
)


def _collected_ids(env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *_VERSION_MODULES,
        ],
        cwd=BACKEND,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_the_version_runs_have_public_test_ids() -> None:
    """The test ids go into the test report that CI uploads, so they hold no proxy name."""
    images = {
        "MEHO_TEST_MONGODB_7_IMAGE": "library/mongo:7",
        "MEHO_TEST_MONGODB_8_IMAGE": "library/mongo:8",
        "MEHO_TEST_POSTGRES_16_IMAGE": "library/postgres:16-alpine",
        "MEHO_TEST_POSTGRES_17_IMAGE": "library/postgres:17-alpine",
        "MEHO_TEST_POSTGRES_18_IMAGE": "library/postgres:18-alpine",
    }
    ids = _collected_ids({name: f"{_SAMPLE_PROXY}/{image}" for name, image in images.items()})
    params = {line.rsplit("[", 1)[-1].rstrip("]") for line in ids}
    assert params == {"mongo-7", "mongo-8", "postgres-16", "postgres-17", "postgres-18"}
    one = _collected_ids(
        {
            "MEHO_TEST_MONGODB_IMAGE": f"{_SAMPLE_PROXY}/library/mongo:8",
            "MEHO_TEST_POSTGRES_IMAGE": f"{_SAMPLE_PROXY}/library/postgres:17",
        }
    )
    for line in ids + one:
        for part in ("hidden-proxy", "invalid", "5443", "mirror-path", "library"):
            assert part not in line, line


# ---------------------------------------------------------------------------
# Each case fails once (the check works)
# ---------------------------------------------------------------------------


def test_a_plain_image_name_is_found() -> None:
    assert literal_images(
        'IMAGE = "mongo:8"\nB = "quay.io/openbao/openbao@sha256:' + "a" * 64 + '"'
    )
    assert literal_images('c = DockerContainer("osixia/openldap:1.5.0")') == [
        "osixia/openldap:1.5.0"
    ]
    assert literal_images('x = {"image": "busybox:1.36"}') == ["busybox:1.36"]


_TC = "from testcontainers.core.container import DockerContainer\n"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_TC + 'c = DockerContainer("rabbitmq:3.13-management")', id="bare-name"),
        pytest.param(
            _TC + 'IMAGE = "nats:2.10"\nc = DockerContainer(IMAGE)', id="bare-name-in-a-constant"
        ),
        pytest.param(_TC + 'c = DockerContainer("alpine")', id="known-name-without-a-tag"),
        pytest.param(_TC + 'c = DockerContainer(image="hello-world")', id="name-without-a-tag"),
        pytest.param(
            _TC + 'TAG = "1.5.0"\nc = DockerContainer(f"osixia/openldap:{TAG}")', id="f-string"
        ),
        pytest.param(
            _TC + 'NAME = "osixia/openldap:" + "1.5.0"\nc = DockerContainer(NAME)', id="joined"
        ),
        pytest.param(
            _TC + '_DEFAULT_X = "rabbitmq:4"\nc = DockerContainer(_DEFAULT_X)',
            id="default-constant",
        ),
        pytest.param(
            _TC + '_DEFAULT_X = "x"\nimage = _DEFAULT_X\nc = DockerContainer(image)',
            id="default-constant-through-a-name",
        ),
        pytest.param(
            "from testcontainers.redis import RedisContainer\nc = RedisContainer()",
            id="class-own-default",
        ),
        pytest.param(
            "from testcontainers.compose import DockerCompose\nc = DockerCompose('.')", id="compose"
        ),
        pytest.param('pod = {"spec": {"containers": [{"image": "hello-world"}]}}', id="pod-spec"),
        pytest.param('c = V1Container(name="x", image="busybox")', id="client-model"),
    ],
)
def test_each_form_of_a_fixed_image_is_found(source: str) -> None:
    assert literal_images(source)


def test_an_image_read_from_its_variable_is_allowed() -> None:
    source = (
        "import os\n" + _TC + '_DEFAULT_X = "rabbitmq:4"\n'
        'image = os.environ.get("MEHO_TEST_X_IMAGE", _DEFAULT_X)\n'
        "c = DockerContainer(image)\n"
        'd = DockerContainer(os.environ.get("MEHO_TEST_Y_IMAGE", "nats:2.10"))\n'
        'pod = {"image": os.environ["MEHO_TEST_Z_IMAGE"]}\n'
    )
    assert literal_images(source) == []


def test_every_integration_and_migration_file_is_read() -> None:
    """A test there can start a pod on a live cluster without testcontainers."""
    read = set(scanned_files())
    for folder in ("integration", "migrations"):
        assert set((TESTS / folder).rglob("test_*.py")) <= read


def test_a_dockerfile_from_line_with_a_name_is_found() -> None:
    assert literal_images('D = """\nFROM debian:trixie-slim\nRUN true\n"""') == [
        "debian:trixie-slim"
    ]
    assert literal_images('D = """\nFROM ${BASE}\nRUN true\n"""') == []


def test_a_name_from_a_variable_default_is_allowed() -> None:
    source = (
        'import os\nI = os.environ.get("MEHO_TEST_X_IMAGE", "mongo:8")\n'
        '_DEFAULT_IMAGES = ("rancher/k3s:v1", "postgres:17")\n'
        'D = """\nARG B=debian:bookworm-slim\nFROM ${B}\n"""\n'
    )
    assert literal_images(source) == []


def test_text_that_is_not_an_image_is_not_found() -> None:
    assert literal_images('a = "localhost:8080"\nc = "key:value"\nd = "hello world"') == []


def _job(line: str, *steps: dict[str, Any]) -> dict[str, Any]:
    secrets = {"name": SECRETS_STEP, "run": f"bash x <<'I'\n{line}\nI\n"}
    return {"steps": [secrets, *steps]}


def test_a_job_that_sets_a_variable_without_the_proxy_is_found() -> None:
    names = {"MEHO_TEST_X_IMAGE"}
    assert variables_not_through_proxy(_job("MEHO_TEST_X_IMAGE: mongo:8"), names) == [
        "MEHO_TEST_X_IMAGE"
    ]
    assert variables_not_through_proxy(_job(""), names) == ["MEHO_TEST_X_IMAGE"]
    assert (
        variables_not_through_proxy(_job("MEHO_TEST_X_IMAGE: PROXY/library/mongo:8"), names) == []
    )
    assert variables_not_through_proxy(_job("MEHO_TEST_X_IMAGE: PROXY_ONLY/a/b:1"), names) == []


@pytest.mark.parametrize(
    "step",
    [
        pytest.param({"run": "docker pull valkey/valkey:9.0-alpine"}, id="pull"),
        pytest.param({"run": 'docker run --rm "${IMAGE}" true'}, id="run"),
        pytest.param({"run": "kind create cluster --name x"}, id="kind"),
        pytest.param({"uses": "./.github/actions/setup-kind"}, id="setup-kind"),
        pytest.param({"run": "docker compose up -d"}, id="compose"),
        pytest.param({"run": "docker-compose up -d"}, id="old-compose"),
        pytest.param({"run": "docker buildx build -t x ."}, id="buildx"),
        pytest.param({"uses": "docker://alpine:3.20"}, id="docker-action"),
        pytest.param(
            {"run": "# the image comes from CI_IMAGE_PROXY\ndocker pull mongo:8"},
            id="proxy-only-in-a-comment",
        ),
        pytest.param(
            {"run": 'docker pull "${MEHO_TEST_X_IMAGE}"\ndocker pull mongo:8'},
            id="proxy-pull-and-outside-pull",
        ),
        pytest.param(
            {"run": 'docker pull "${MEHO_TEST_X_IMAGE}" && docker pull mongo:8'},
            id="two-pulls-on-one-line",
        ),
        pytest.param({"run": 'docker pull "${MEHO_TEST_Y_IMAGE}"'}, id="variable-not-in-the-block"),
    ],
)
def test_a_step_that_pulls_from_outside_is_found(step: dict[str, str]) -> None:
    assert outside_pulls(_job("MEHO_TEST_X_IMAGE: PROXY/library/mongo:8", step))


def test_a_step_that_pulls_through_the_proxy_is_not_found() -> None:
    job = _job(
        "MEHO_TEST_X_IMAGE: PROXY/library/mongo:8", {"run": 'docker pull "${MEHO_TEST_X_IMAGE}"'}
    )
    assert outside_pulls(job) == []
    assert outside_pulls({"container": {"image": "x"}, "steps": []})


def test_a_proxy_step_for_the_merge_queue_only_does_not_count_on_pull_requests() -> None:
    job = _job(
        "MEHO_TEST_X_IMAGE: PROXY/library/python:3",
        {"run": 'docker run "${MEHO_TEST_X_IMAGE:-${IMAGE}}"'},
    )
    job["steps"][0]["if"] = "github.event_name == 'merge_group'"
    assert outside_pulls(job, "merge_group") == []
    assert outside_pulls(job, "pull_request")


def test_the_merge_queue_run_of_redaction_compare_needs_its_proxy_line() -> None:
    job = jobs("ci.yml")["redaction-compare"]
    assert outside_pulls(job, "merge_group") == []
    assert outside_pulls(_without_line(job, "MEHO_TEST_PYTHON_IMAGE"), "merge_group")


@pytest.mark.parametrize(
    ("job_id", "variable"),
    [
        ("python-migration-tests", "MEHO_TEST_PGVECTOR_IMAGE"),
        ("python-timing-tests", "MEHO_TEST_PGVECTOR_IMAGE"),
        ("python-integration-shard", "MEHO_TEST_MONGODB_8_IMAGE"),
    ],
)
def test_a_job_without_the_line_of_a_variable_its_tests_read_is_found(
    job_id: str, variable: str
) -> None:
    """The migration job reads the variable only through tests/_pgvector_image.py."""
    files = {job_id: jobs_running_tests()[job_id]}
    real = jobs("ci.yml")
    assert missing_variables(real, files, allowed_unset()) == {}
    changed = {**real, job_id: _without_line(real[job_id], variable)}
    assert missing_variables(changed, files, allowed_unset()) == {job_id: [variable]}


def test_an_obsolete_variables_not_set_entry_is_found() -> None:
    files = jobs_running_tests()
    real = exceptions()["variables_not_set"]
    assert stale_variable_exceptions(real, jobs("ci.yml"), files) == []
    made_up = [
        {"variable": "MEHO_TEST_GONE_IMAGE", "jobs": ["python-integration-shard"], "reason": "x"},
        {"variable": "MEHO_TEST_PGVECTOR_IMAGE", "jobs": ["python-migration-tests"], "reason": "x"},
        {"variable": "MEHO_TEST_POSTGRES_IMAGE", "jobs": ["no-such-job"], "reason": "x"},
    ]
    assert len(stale_variable_exceptions(made_up, jobs("ci.yml"), files)) == 3


def test_the_test_files_of_a_step_are_read() -> None:
    by_find = "mapfile -t F < <(find tests/migrations -name 'test_*.py' | sort)"
    by_grep = "mapfile -t F < <(grep -rl --include='test_*.py' 'mark.slow_runner_tolerant' tests)"
    job = {
        "defaults": {"run": {"working-directory": "backend"}},
        "steps": [
            {"run": f'{by_find}\nuv run pytest "${{F[@]}}"'},
            {"run": f'{by_grep}\nuv run pytest "${{F[@]}}"'},
            {"run": "uv run pytest -q tests/test_time_limits.py"},
        ],
    }
    found = {p.relative_to(TESTS).as_posix() for p in files_of_job(job) or set()}
    assert "migrations/test_migration_rollback.py" in found
    assert "integration/test_topology_path_pruning.py" in found
    assert "test_time_limits.py" in found
    assert files_of_job({"steps": [{"run": "echo no tests here"}]}) is None
    with pytest.raises(ValueError, match="cannot tell"):
        files_of_job({"steps": [{"name": "x", "run": 'uv run pytest "${FILES[@]}"'}]})
