# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every image a CI test uses comes through the CI image proxy.

A test that pulls or builds from Docker Hub can fail with "429 Too Many
Requests", and a required check then fails for a reason that has nothing to
do with the change. So CI sends each test image through the image proxy
cache. These checks keep it that way:

1. A test file that starts containers does not name an image in plain text.
   The image comes from a ``MEHO_TEST_*_IMAGE`` variable, or from a build
   argument in the ``FROM`` line of a Dockerfile.
2. Every job that runs such a test sets the variable with ``PROXY/`` or
   ``PROXY_ONLY/`` in its image block (the step "Read the CI repository
   secrets").
3. A workflow step of a test job that pulls an image, or starts a kind
   cluster, is on the exception list with a reason.

The exception list is ``tests/ci_test_image_exceptions.json``. See
``docs/codebase/devops.md``, "Test images come through the image proxy".
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_TESTS = Path(__file__).resolve().parent
_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"
_EXCEPTIONS_FILE = _TESTS / "ci_test_image_exceptions.json"

_SECRETS_STEP = "Read the CI repository secrets"
_VARIABLE = re.compile(r"MEHO_TEST_[A-Z0-9_]+_IMAGE")
_IMAGE_LINE = re.compile(r"^\s*(MEHO_TEST_[A-Z0-9_]+_IMAGE):\s*(\S+)\s*$", re.MULTILINE)
# A name with a tag or a digest, with or without a registry and path.
_IMAGE_NAME = re.compile(
    r"^(?:[a-z0-9.-]+(?::\d+)?/)?(?:[a-z0-9._-]+/)*[a-z0-9._-]+"
    r"(?::[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$"
)
# Official images have no path ("mongo:8"), so a bare "name:tag" counts only
# when the name is a known image. Other bare pairs ("host:8080") are not images.
_BARE_IMAGE_NAMES = frozenset(
    {
        "alpine",
        "busybox",
        "debian",
        "mariadb",
        "memcached",
        "mongo",
        "mysql",
        "nginx",
        "node",
        "postgres",
        "python",
        "redis",
        "ubuntu",
    }
)
_FROM_LINE = re.compile(r"^[ \t]*FROM[ \t]+(?:--\S+[ \t]+)?(\S+)", re.MULTILINE)
_DOCKERFILE_WORD = re.compile(r"^[ \t]*(?:RUN|COPY|ENV|CMD|ARG)[ \t]", re.MULTILINE)
_PULL_STEP = re.compile(r"docker\s+(?:pull|run|build)\b|kind\s+create\s+cluster|actions/setup-kind")

#: Test job in ci.yml -> the test directories whose files it runs.
_JOB_OF_DIRECTORY = {
    "integration": ("python-integration-shard",),
    "migrations": ("python-migration-tests",),
}
_UNIT_JOBS = ("python-unit-shard", "python-coverage")
#: Helpers and conftest files are imported by tests of every job.
_ALL_JOBS = (*_UNIT_JOBS, "python-integration-shard", "python-migration-tests")
#: Workflows whose jobs run tests and are read for pulls.
_TEST_WORKFLOWS = ("ci.yml", "chart.yml")


def _exceptions() -> dict[str, list[dict[str, str]]]:
    data: dict[str, list[dict[str, str]]] = json.loads(_EXCEPTIONS_FILE.read_text())
    return data


def _is_image_name(text: str) -> bool:
    if not _IMAGE_NAME.match(text):
        return False
    name = text.split("@", 1)[0]
    if "/" in name:
        return True
    return "@" in text or name.split(":", 1)[0] in _BARE_IMAGE_NAMES


def _env_default_nodes(tree: ast.AST) -> set[int]:
    """Ids of the string nodes that are allowed to name an image.

    These are the default of an environment variable read
    (``os.environ.get("MEHO_TEST_X_IMAGE", "<default>")``), a table row that
    holds the variable name next to its default, and every value assigned to
    a name with ``DEFAULT`` in it.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) >= 2:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _VARIABLE.fullmatch(first.value)
            ):
                allowed.update(id(n) for n in ast.walk(node.args[1]))
        if isinstance(node, ast.Tuple | ast.List) and any(
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and _VARIABLE.fullmatch(item.value)
            for item in node.elts
        ):
            allowed.update(id(n) for n in ast.walk(node))
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(
                isinstance(t, ast.Name) and "DEFAULT" in t.id for t in targets
            ):
                allowed.update(id(n) for n in ast.walk(node.value))
    return allowed


def literal_images(source: str) -> list[str]:
    """Image names in *source* that do not come from a variable or a build argument."""
    tree = ast.parse(source)
    allowed = _env_default_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in allowed:
            continue
        text = node.value
        if _is_image_name(text):
            found.append(text)
        if _DOCKERFILE_WORD.search(text):
            found.extend(
                ref
                for ref in _FROM_LINE.findall(text)
                if not ref.startswith("$") and ref != "scratch"
            )
    return found


def _container_test_files() -> list[Path]:
    """The test files that import testcontainers or build an image from a Dockerfile."""
    files: list[Path] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text()
        if "testcontainers" not in source and "DockerImage" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(module.split(".")[0] == "testcontainers" for module in modules):
                files.append(path)
                break
    return files


def image_variables(source: str) -> set[str]:
    """The ``MEHO_TEST_*_IMAGE`` names that *source* reads."""
    tree = ast.parse(source)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _VARIABLE.fullmatch(node.value)
    }


def jobs_of(path: Path) -> tuple[str, ...]:
    """The CI jobs that run the test file *path*."""
    relative = path.relative_to(_TESTS)
    if len(relative.parts) == 1:
        helper = relative.name == "conftest.py" or relative.name.startswith("_")
        return _ALL_JOBS if helper else _UNIT_JOBS
    return _JOB_OF_DIRECTORY.get(relative.parts[0], _UNIT_JOBS)


def image_block(job: dict[str, Any]) -> dict[str, str]:
    """The ``MEHO_TEST_*_IMAGE`` lines of the job's step "Read the CI repository secrets"."""
    for step in job.get("steps", []):
        if step.get("name") == _SECRETS_STEP:
            return dict(_IMAGE_LINE.findall(step.get("run", "")))
    return {}


def variables_not_through_proxy(job: dict[str, Any], names: set[str]) -> list[str]:
    """Names in *names* that the job does not set with ``PROXY/`` or ``PROXY_ONLY/``."""
    block = image_block(job)
    return sorted(
        name for name in names if not block.get(name, "").startswith(("PROXY/", "PROXY_ONLY/"))
    )


def pulling_steps(job: dict[str, Any]) -> list[str]:
    """The steps of *job* that pull an image or start a kind cluster, apart from the proxy."""
    found: list[str] = []
    for step in job.get("steps", []):
        text = f"{step.get('run', '')}\n{step.get('uses', '')}"
        if _PULL_STEP.search(text) and not re.search(r"MEHO_TEST_\w+_IMAGE|CI_IMAGE_PROXY", text):
            found.append(str(step.get("name") or step.get("uses") or step.get("run", ""))[:80])
    if "container" in job or "services" in job:
        found.append("job-level container or services")
    return found


def _jobs(workflow: str) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load((_WORKFLOWS / workflow).read_text())
    jobs: dict[str, dict[str, Any]] = data["jobs"]
    return jobs


# ---------------------------------------------------------------------------
# The repository today
# ---------------------------------------------------------------------------


def test_no_test_names_an_image_in_plain_text() -> None:
    allowed = {(e["file"], e["image"]) for e in _exceptions()["images_in_tests"]}
    bad = [
        f"{path.relative_to(_TESTS)}: {image}"
        for path in _container_test_files()
        for image in literal_images(path.read_text())
        if (str(path.relative_to(_TESTS)), image) not in allowed
    ]
    assert not bad, (
        "A test names an image in plain text. Read it from a MEHO_TEST_*_IMAGE variable "
        "(or a build argument), and set the variable with PROXY/ in ci.yml:\n" + "\n".join(bad)
    )


def test_each_job_sets_the_image_variables_of_its_tests_through_the_proxy() -> None:
    jobs = _jobs("ci.yml")
    needed: dict[str, set[str]] = {}
    for path in _container_test_files():
        names = image_variables(path.read_text())
        for job_id in jobs_of(path) if names else ():
            needed.setdefault(job_id, set()).update(names)
    optional = {e["variable"] for e in _exceptions()["variables_not_set"]}
    bad = {
        job_id: missing
        for job_id, names in needed.items()
        if (missing := variables_not_through_proxy(jobs[job_id], names - optional))
    }
    assert not bad, (
        "A job runs a test that reads these image variables, but does not set them "
        f"with PROXY/ or PROXY_ONLY/ in the step '{_SECRETS_STEP}': {bad}"
    )


def test_no_workflow_step_of_a_test_job_pulls_from_outside() -> None:
    allowed = {(e["workflow"], e["job"]) for e in _exceptions()["workflow_jobs"]}
    bad = [
        f"{workflow}: job {job_id}: {step}"
        for workflow in _TEST_WORKFLOWS
        for job_id, job in _jobs(workflow).items()
        if (workflow, job_id) not in allowed
        for step in pulling_steps(job)
    ]
    assert not bad, (
        "A step pulls an image that does not come through the CI image proxy. Use "
        "the proxy, or add the job to tests/ci_test_image_exceptions.json with a reason:\n"
        + "\n".join(bad)
    )


def test_every_exception_has_a_reason_and_still_applies() -> None:
    exceptions = _exceptions()
    for kind in ("images_in_tests", "variables_not_set", "workflow_jobs"):
        for entry in exceptions[kind]:
            assert entry["reason"].strip(), entry
    for entry in exceptions["workflow_jobs"]:
        job = _jobs(entry["workflow"]).get(entry["job"])
        assert job is not None, f"the job of this exception is gone: {entry}"
        assert pulling_steps(job), f"this job no longer pulls from outside: {entry}"
    for entry in exceptions["images_in_tests"]:
        source = (_TESTS / entry["file"]).read_text()
        assert entry["image"] in literal_images(source), (
            f"this exception no longer applies: {entry}"
        )


@pytest.mark.parametrize(
    ("variable", "subchart"),
    [
        ("MEHO_TEST_CORE_KEYCLOAK_IMAGE", "keycloak"),
        ("MEHO_TEST_CORE_OPENBAO_IMAGE", "openbao"),
    ],
)
def test_core_images_follow_the_chart_tag(variable: str, subchart: str) -> None:
    """The CI image of a chart component is the tag that the chart pins."""
    values = yaml.safe_load(
        (_REPO / "deploy" / "charts" / "meho" / "charts" / subchart / "values.yaml").read_text()
    )
    block = image_block(_jobs("ci.yml")["python-integration-shard"])
    assert block[variable].endswith(f"/{subchart}/{subchart}:{values['image']['tag']}")


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


def test_a_job_that_sets_a_variable_without_the_proxy_is_found() -> None:
    def job(line: str) -> dict[str, Any]:
        return {"steps": [{"name": _SECRETS_STEP, "run": f"bash x <<'I'\n{line}\nI\n"}]}

    names = {"MEHO_TEST_X_IMAGE"}
    assert variables_not_through_proxy(job("MEHO_TEST_X_IMAGE: mongo:8"), names) == [
        "MEHO_TEST_X_IMAGE"
    ]
    assert variables_not_through_proxy(job(""), names) == ["MEHO_TEST_X_IMAGE"]
    assert variables_not_through_proxy(job("MEHO_TEST_X_IMAGE: PROXY/library/mongo:8"), names) == []
    assert variables_not_through_proxy(job("MEHO_TEST_X_IMAGE: PROXY_ONLY/a/b:1"), names) == []


@pytest.mark.parametrize(
    "step",
    [
        {"run": "docker pull valkey/valkey:9.0-alpine"},
        {"run": 'docker run --rm "${IMAGE}" true'},
        {"run": "kind create cluster --name x"},
        {"uses": "./.github/actions/setup-kind"},
    ],
)
def test_a_step_that_pulls_from_outside_is_found(step: dict[str, str]) -> None:
    assert pulling_steps({"steps": [step]})


def test_a_step_that_pulls_through_the_proxy_is_not_found() -> None:
    assert not pulling_steps({"steps": [{"run": 'docker pull "${MEHO_TEST_X_IMAGE}"'}]})
    assert pulling_steps({"container": {"image": "x"}, "steps": []})


def test_the_job_of_a_test_file_is_known() -> None:
    assert jobs_of(_TESTS / "integration" / "test_x.py") == ("python-integration-shard",)
    assert jobs_of(_TESTS / "migrations" / "test_x.py") == ("python-migration-tests",)
    assert jobs_of(_TESTS / "test_x.py") == _UNIT_JOBS
    assert jobs_of(_TESTS / "_pgvector_image.py") == _ALL_JOBS
