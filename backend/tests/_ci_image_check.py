# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reader behind ``tests/test_ci_test_images.py``.

It reads the test files and the workflows, and answers three questions:

* Which image does a test file start, and does it come from a
  ``MEHO_TEST_*_IMAGE`` variable? (``literal_images``)
* Which test files does a CI job run, and which image variables do they read,
  with the helper modules and ``conftest.py`` files they use?
  (``files_of_job``, ``needed_variables``)
* Which commands of a job pull an image that does not come through the CI
  image proxy? (``outside_pulls``)

This is a helper, not a test (the leading underscore keeps pytest from
collecting it). See ``docs/codebase/devops.md``, "Test images come through
the image proxy".
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
EXCEPTIONS_FILE = TESTS / "ci_test_image_exceptions.json"

#: This helper and its test name image variables as data, not to start a container.
_CHECK_FILES = frozenset({Path(__file__).resolve(), TESTS / "test_ci_test_images.py"})

SECRETS_STEP = "Read the CI repository secrets"
VARIABLE = re.compile(r"MEHO_TEST_[A-Z0-9_]+_IMAGE")
_IMAGE_LINE = re.compile(
    r"^\s*((?:MEHO_TEST_[A-Z0-9_]+_IMAGE|RYUK_CONTAINER_IMAGE)):\s*(\S+)\s*$", re.MULTILINE
)
# A name with a tag or a digest, with or without a registry and path.
_IMAGE_NAME = re.compile(
    r"^(?:[a-z0-9.-]+(?::\d+)?/)?(?:[a-z0-9._-]+/)*[a-z0-9._-]+"
    r"(?::[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$"
)
# Official images have no path ("mongo:8"). Outside the image place of a
# container start, a bare "name:tag" counts only when the name is a known
# image: other bare pairs ("host:8080") are not images.
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

# A shell command that pulls or builds from an image, or starts a cluster
# that pulls its own images.
_PULL_COMMAND = re.compile(
    r"\bdocker\s+(?:pull|run|create|build|compose|buildx\s+build)\b"
    r"|\bdocker-compose\b|\bpodman\s+(?:pull|run|build)\b|\bkind\s+create\s+cluster\b"
)
# A step action that pulls or builds from an image.
_PULL_ACTION = re.compile(r"^docker://|setup-kind|docker/build-push-action|docker/bake-action")
_PROXY_VARIABLE = re.compile(r"\$\{?(MEHO_TEST_[A-Z0-9_]+_IMAGE)\b")

# How a workflow step picks the test files that pytest runs.
_PYTEST = re.compile(r"\b(?:uv\s+run\s+(?:python\s+-m\s+)?|python\s+-m\s+)pytest\b")
_FIND = re.compile(r"\bfind\s+([^\s)]+)\s+-name\s+'([^']+)'((?:\s+-not\s+-path\s+'[^']+')*)")
_NOT_PATH = re.compile(r"-not\s+-path\s+'([^']+)'")
_GREP = re.compile(r"\bgrep\s+-rl\s+--include='([^']+)'\s+'([^']+)'\s+([^\s)]+)")
_LIST_SCRIPT = re.compile(r"\bpython\s+(tests/_outside_backend_tests\.py)\b")
_COPY = re.compile(r"\bcp\s+-r\s+(\S+)\s+(\S+)")
_EVENT_IS = re.compile(r"github\.event_name\s*==\s*'(\w+)'")


def exceptions() -> dict[str, list[dict[str, str]]]:
    data: dict[str, list[dict[str, str]]] = json.loads(EXCEPTIONS_FILE.read_text())
    return data


@cache
def jobs(workflow: str) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    found: dict[str, dict[str, Any]] = data["jobs"]
    return found


# ---------------------------------------------------------------------------
# Images in test files
# ---------------------------------------------------------------------------


def _is_image_name(text: str) -> bool:
    if not _IMAGE_NAME.match(text):
        return False
    name = text.split("@", 1)[0]
    if "/" in name:
        return True
    return "@" in text or name.split(":", 1)[0] in _BARE_IMAGE_NAMES


def _env_default_nodes(tree: ast.AST) -> set[int]:
    """Ids of the string nodes that may name a public default image.

    These are the default of an image variable read
    (``os.environ.get("MEHO_TEST_X_IMAGE", "<default>")``), a table row that
    holds the variable name next to its default, and the value of a constant
    with ``DEFAULT`` in its name. ``_image_place_problems`` makes sure that
    such a constant never goes straight into a container start.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) >= 2:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and VARIABLE.fullmatch(first.value)
            ):
                allowed.update(id(n) for n in ast.walk(node.args[1]))
        if isinstance(node, ast.Tuple | ast.List) and any(
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and VARIABLE.fullmatch(item.value)
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


def _container_classes(tree: ast.AST) -> set[str]:
    """The names in *tree* of the testcontainers classes that start a container."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("testcontainers"):
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name.endswith("Container") or alias.name == "DockerCompose"
            )
    # A local subclass of such a class starts a container too.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id in names for base in node.bases
        ):
            names.add(node.name)
    return names


def _bindings(tree: ast.AST) -> dict[str, list[ast.expr]]:
    """Each plain name in *tree* with the values assigned to it."""
    found: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign | ast.NamedExpr)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            found.setdefault(node.target.id, []).append(node.value)
    return found


def _is_text(node: ast.expr, bindings: dict[str, list[ast.expr]], seen: frozenset[str]) -> bool:
    """Whether *node* is fixed text (a string, an f-string, or a name bound to one)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _is_text(node.left, bindings, seen) or _is_text(node.right, bindings, seen)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == "format" and _is_text(node.func.value, bindings, seen)
    if isinstance(node, ast.Name) and node.id not in seen:
        if "DEFAULT" in node.id:
            return True
        return any(
            _is_text(value, bindings, seen | {node.id}) for value in bindings.get(node.id, [])
        )
    return False


def _image_place_problems(tree: ast.AST) -> list[str]:
    """Container starts and pod images whose image is fixed text, or missing.

    The image place is the first argument or ``image=`` of a testcontainers
    class, the ``image`` key of a dict (a pod or container spec), and
    ``image=`` of any other call (a Kubernetes client model).
    """
    classes = _container_classes(tree)
    bindings = _bindings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        places: list[ast.expr] = []
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            keyword = next((k.value for k in node.keywords if k.arg == "image"), None)
            if name in classes:
                image = node.args[0] if node.args else keyword
                if image is None and name != "DockerCompose":
                    found.append(f"{name}() starts its own default image")
                if name == "DockerCompose":
                    found.append("DockerCompose starts the images of a compose file")
                places.extend([image] if image is not None else [])
            elif keyword is not None:
                places.append(keyword)
        if isinstance(node, ast.Dict):
            places.extend(
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "image"
            )
        found.extend(
            p.value if isinstance(p, ast.Constant) else ast.unparse(p)
            for p in places
            if _is_text(p, bindings, frozenset())
        )
    return found


def literal_images(source: str) -> list[str]:
    """Images in *source* that do not come from a variable or a build argument."""
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
    return [*found, *(p for p in _image_place_problems(tree) if p not in found)]


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


@cache
def _variables_of(path: Path) -> frozenset[str]:
    return frozenset(image_variables(path.read_text()))


@cache
def is_container_source(path: Path) -> bool:
    """Whether *path* imports testcontainers or the Docker SDK."""
    modules = _imported_modules(ast.parse(path.read_text()))
    return any(module.split(".")[0] in {"testcontainers", "docker"} for module in modules)


def _module_path(name: str) -> Path | None:
    if not name.startswith("tests"):
        return None
    base = BACKEND.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


@cache
def helpers_of(path: Path) -> frozenset[Path]:
    """The modules under ``tests/`` that *path* imports, also through other helpers."""
    tree = ast.parse(path.read_text())
    inside = path.is_relative_to(BACKEND)
    package = ".".join(path.relative_to(BACKEND).parts[:-1]) if inside else ""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parent = package.rsplit(".", node.level - 1)[0] if node.level else ""
            module = ".".join(part for part in (parent, node.module or "") if part)
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    found: set[Path] = set()
    for name in names:
        helper = _module_path(name)
        if helper is not None and helper != path:
            found.add(helper)
    for helper in list(found):
        found |= helpers_of(helper)
    return frozenset(found)


def image_variables(source: str) -> set[str]:
    """The ``MEHO_TEST_*_IMAGE`` names that *source* names."""
    tree = ast.parse(source)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and VARIABLE.fullmatch(node.value)
    }


def _conftests_of(path: Path) -> set[Path]:
    return {
        folder / "conftest.py"
        for folder in path.parents
        if folder.is_relative_to(TESTS) and (folder / "conftest.py").is_file()
    }


def sources_of(files: set[Path]) -> set[Path]:
    """*files*, their ``conftest.py`` files and every helper module they import."""
    found = set(files)
    for path in files:
        found |= _conftests_of(path)
    for path in list(found):
        found |= helpers_of(path)
    return found


def needed_variables(files: set[Path]) -> set[str]:
    """The image variables that the container tests in *files* read.

    A file that starts containers counts with every helper module it imports
    (``tests/_pgvector_image.py``, for example). A file that starts none
    counts only through the helpers and ``conftest.py`` files that do.
    """
    names: set[str] = set()
    for source in sources_of(files) - _CHECK_FILES:
        if is_container_source(source):
            for part in {source} | helpers_of(source):
                names |= _variables_of(part)
    return names


def scanned_files() -> list[Path]:
    """The files whose image names the check reads.

    Every file under ``tests/integration`` and ``tests/migrations``, and
    every other file that imports testcontainers or the Docker SDK, with the
    helper modules they import. Plain unit tests hold pod specs as test data
    and start nothing, so they are not read.
    """
    files: set[Path] = set()
    for path in TESTS.rglob("*.py"):
        if path in _CHECK_FILES:
            continue
        top = path.relative_to(TESTS).parts[0]
        if top in {"integration", "migrations"} or is_container_source(path):
            files.add(path)
    for path in list(files):
        files |= helpers_of(path) - _CHECK_FILES
    return sorted(files)


# ---------------------------------------------------------------------------
# Test jobs in the workflows
# ---------------------------------------------------------------------------


def _commands(text: str) -> list[str]:
    """The shell commands of a ``run`` block, without comment lines."""
    lines = [line for line in text.splitlines() if not line.strip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    return [part.strip() for part in re.split(r"\n|;|&&|\|\||\|", joined) if part.strip()]


def _run_text(step: dict[str, Any]) -> str:
    return "\n".join(_commands(str(step.get("run", ""))))


def _working_folder(job: dict[str, Any], step: dict[str, Any]) -> Path:
    folder = step.get("working-directory") or job.get("defaults", {}).get("run", {}).get(
        "working-directory", "."
    )
    return (REPO / str(folder)).resolve()


def _found(folder: Path, root: str, pattern: str, skip: list[str]) -> set[Path]:
    return {
        path
        for path in (folder / root).rglob(pattern)
        if not any(fnmatch.fnmatch(str(path.relative_to(folder)), glob) for glob in skip)
    }


def _pytest_paths(text: str, folder: Path, copies: dict[str, Path]) -> set[Path]:
    found: set[Path] = set()
    for command in _commands(text):
        match = _PYTEST.search(command)
        for word in command[match.end() :].split() if match else []:
            if not word.startswith("tests"):
                continue
            path = copies.get(word.rstrip("/"), folder / word)
            found |= set(path.rglob("test_*.py")) if path.is_dir() else {path}
    return found


def files_of_job(job: dict[str, Any]) -> set[Path] | None:
    """The test files that *job* runs, or ``None`` when it runs no pytest.

    It reads the ``find``, ``grep -rl`` and ``tests/_outside_backend_tests.py``
    lists and the paths on the pytest command line of every step. A pytest
    step whose files it cannot read raises ``ValueError``, so a new form of
    step makes the check fail instead of going unread.
    """
    from tests._outside_backend_tests import scan as outside_backend_scan

    copies: dict[str, Path] = {}
    files: set[Path] | None = None
    for step in job.get("steps", []):
        text = _run_text(step)
        folder = _working_folder(job, step)
        for source, target in _COPY.findall(text):
            copies[target.rstrip("/")] = (folder / source).resolve()
        if not _PYTEST.search(text):
            continue
        found = _pytest_paths(text, folder, copies)
        for root, pattern, skips in _FIND.findall(text):
            found |= _found(folder, root, pattern, _NOT_PATH.findall(skips))
        for pattern, needle, root in _GREP.findall(text):
            found |= {p for p in _found(folder, root, pattern, []) if needle in p.read_text()}
        if _LIST_SCRIPT.search(text):
            found |= {BACKEND / name for name in outside_backend_scan()}
        if not found:
            raise ValueError(f"cannot tell which test files this step runs: {step.get('name')}")
        files = (files or set()) | found
    return files


@cache
def jobs_running_tests(workflow: str = "ci.yml") -> dict[str, set[Path]]:
    """Each job of *workflow* that runs pytest, with the test files it runs."""
    found: dict[str, set[Path]] = {}
    for job_id, job in jobs(workflow).items():
        files = files_of_job(job)
        if files is not None:
            found[job_id] = files
    return found


def _runs_for(item: dict[str, Any], event: str | None) -> bool:
    """Whether a step runs for *event* (``None``: for any event)."""
    named = _EVENT_IS.findall(str(item.get("if", "")))
    return event is None or not named or event in named


def image_block(job: dict[str, Any], event: str | None = None) -> dict[str, str]:
    """The image lines of the job's step "Read the CI repository secrets"."""
    for step in job.get("steps", []):
        if step.get("name") == SECRETS_STEP and _runs_for(step, event):
            return dict(_IMAGE_LINE.findall(str(step.get("run", ""))))
    return {}


def variables_not_through_proxy(
    job: dict[str, Any], names: set[str], event: str | None = None
) -> list[str]:
    """Names in *names* that the job does not set with ``PROXY/`` or ``PROXY_ONLY/``."""
    block = image_block(job, event)
    return sorted(
        name for name in names if not block.get(name, "").startswith(("PROXY/", "PROXY_ONLY/"))
    )


def outside_pulls(job: dict[str, Any], event: str | None = None) -> list[str]:
    """Each command or action of *job* that pulls an image from outside the proxy.

    Every pull command is read on its own. It passes only when it names an
    image variable that the job sets through the proxy. A step that does not
    run for *event* is left out.
    """
    found: list[str] = []
    block_names = set(image_block(job, event)) - set(
        variables_not_through_proxy(job, set(image_block(job, event)), event)
    )
    for step in job.get("steps", []):
        if not _runs_for(step, event):
            continue
        uses = str(step.get("uses", ""))
        if _PULL_ACTION.search(uses):
            found.append(uses)
        for command in _commands(str(step.get("run", ""))):
            if not _PULL_COMMAND.search(command):
                continue
            named = set(_PROXY_VARIABLE.findall(command))
            if not named or not named <= block_names:
                found.append(command[:100])
    if "container" in job or "services" in job:
        found.append("job-level container or services")
    return found


def merge_queue_runs_self_hosted(job: dict[str, Any]) -> bool:
    """Whether a merge-queue run of *job* runs on the self-hosted pool."""
    runs_on = str(job.get("runs-on", ""))
    return "merge_group" in runs_on and "meho-runners" in runs_on


# ---------------------------------------------------------------------------
# The rules, on any set of jobs (the tests feed them changed copies too)
# ---------------------------------------------------------------------------


def missing_variables(
    jobs_by_id: dict[str, dict[str, Any]],
    files_by_job: dict[str, set[Path]],
    allowed: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Each job whose tests read image variables it does not set through the proxy.

    *allowed* holds, for each job, the variables that its entries in
    ``variables_not_set`` let it leave unset.
    """
    bad: dict[str, list[str]] = {}
    for job_id, files in files_by_job.items():
        names = needed_variables(files) - allowed.get(job_id, set())
        missing = variables_not_through_proxy(jobs_by_id[job_id], names)
        if missing:
            bad[job_id] = missing
    return bad


def stale_variable_exceptions(
    entries: list[dict[str, Any]],
    jobs_by_id: dict[str, dict[str, Any]],
    files_by_job: dict[str, set[Path]],
) -> list[str]:
    """Each ``variables_not_set`` entry that no longer applies.

    An entry applies while each job it lists runs a test that reads the
    variable and leaves the variable unset.
    """
    stale: list[str] = []
    for entry in entries:
        name = entry["variable"]
        for job_id in entry["jobs"]:
            files = files_by_job.get(job_id)
            if (
                files is None
                or name not in needed_variables(files)
                or not variables_not_through_proxy(jobs_by_id[job_id], {name})
            ):
                stale.append(f"{name} in {job_id}")
    return stale


def allowed_unset() -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for entry in exceptions()["variables_not_set"]:
        for job_id in entry["jobs"]:
            allowed.setdefault(job_id, set()).add(entry["variable"])
    return allowed


def excepted_jobs() -> set[tuple[str, str]]:
    return {(e["workflow"], e["job"]) for e in exceptions()["workflow_jobs"]}


def container_jobs() -> list[str]:
    """The jobs of ``ci.yml`` that run container tests and are not on the exception list."""
    return sorted(
        job_id
        for job_id, files in jobs_running_tests().items()
        if ("ci.yml", job_id) not in excepted_jobs() and needed_variables(files)
    )
