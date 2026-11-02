# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pick the unit tests that a pull request can break, with a test map (#5642).

The test map says which tests run each source file. The nightly CI run
makes it from its coverage data. A pull request that would run the full
unit suite then runs only the tests the map picks, in one job, plus the
fixed contract set of the one-connector run
(``CONTRACT_TESTS`` in ``tests/_unit_connector_select.py``).

The merge queue and pushes to ``main`` always run the full suite, so a
test this tool leaves out still runs before the change lands on ``main``.

Two commands:

``build`` (the nightly ``python-coverage-combine`` job)
    Reads the combined coverage data, made with ``dynamic_context =
    test_function`` (each line carries the names of the test functions
    that ran it), and the pytest junit files of the same run, one per
    coverage shard. Writes the map as JSON. Needs ``coverage``. Makes no
    map when a shard did not finish (its junit file reports a crashed
    worker) or did not save its coverage (none of its tests is in the
    data): a map from partial data would pick too few tests.

``pick`` (the ``changes`` job)
    Reads the changed files of a pull request (one path per line, relative
    to the repository root) on stdin. Prints one line that says what runs
    and why. When the map can pick, it writes the picked tests (test files
    and pytest test ids, relative to ``backend/``) to ``--out``; when the
    full suite must run, it writes nothing. Needs only the Python standard
    library, because the ``changes`` job does not install the project.

The rules of ``pick``. Any doubt means the full suite:

* no changed files, no map, a map older than :data:`MAX_AGE_DAYS` days,
  a map of another format, or any error;
* a changed docs file (``docs/`` or ``*.md``) picks nothing;
* a changed test file (``test_*.py``) picks its own tests; one under
  ``tests/integration/`` or ``tests/migrations/`` picks nothing, because
  other jobs run those;
* a changed source file (``backend/src/**.py``) picks the tests that ran
  it. When no test ran it (its code runs only on import, like most
  ``*_schemas.py`` files), import analysis picks every test file that
  imports it, directly or through other modules, and the tests that the
  map links to those other modules. When a unit ``conftest.py`` imports
  the file itself, the full suite runs. Import analysis does not follow
  ``conftest.py`` files any further: the main ``conftest.py`` imports the
  app, so almost every file would pick the full suite;
* every other changed file means the full suite: a migration, a template
  or other data file, a ``conftest.py`` or test helper, ``pyproject.toml``,
  ``uv.lock``, a workflow, the Go CLI, and a source file the map does not
  know (for example a new one);
* :data:`MAX_TESTS` or more picked tests means the full suite: one job
  would take too long. The map says how many test items each test runs.
  A test file the pull request changed, or that the map does not know,
  is counted from its source instead (the larger of that count and the
  map's count): its test functions, times the cases of each
  ``parametrize`` list that is written out or named at module level.
  When that count is uncertain, the full suite runs: a parametrize list
  built by code, a ``pytestmark`` with parametrize, a fixture with
  ``params``, ``pytest_generate_tests``, a test class with a base from
  another file, or a bare decorator name from another file (it may hold
  a parametrize mark).

A test id from the map can be out of date: the map may be days old, and
the pull request may rename a test. A test id whose file is gone is
dropped. A test id that the file no longer defines picks the whole file
instead, so pytest never gets an id it cannot find.

This is CI tooling, not a test (the leading underscore keeps pytest from
collecting it). Its tests are in ``tests/test_unit_test_map.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, TypeGuard

if __package__:
    from . import _unit_connector_select as connector_select
else:  # Run as a script: python3 backend/tests/_unit_test_map.py
    import _unit_connector_select as connector_select

MAP_FORMAT = 1
MAP_FILE = "unit-test-map.json"
MAX_AGE_DAYS = 7
MAX_TESTS = 2000

# Test folders that other jobs run, never the unit shards.
OTHER_SUITES = ("tests/integration/", "tests/migrations/")

_DOCS = re.compile(r"(^docs/|\.md$)")


# --- build -------------------------------------------------------------------


# pytest-xdist reports a test whose worker died with this text (dsession.py).
_CRASHED = "crashed while running"


@dataclass
class Shard:
    """What the pytest junit file of one coverage shard says."""

    name: str
    test_ids: list[str]
    crashed: list[str]  # Test ids whose xdist worker died while running them.


def read_junit(path: Path, backend_root: Path) -> Shard:
    """Return the pytest test ids of every test case in a junit file.

    A test case carries ``classname`` (the dotted file path plus the class
    names) and ``name`` (the function name with its parameters). The path
    is the longest dotted prefix of ``classname`` that is a ``.py`` file
    under ``backend_root``.
    """
    from defusedxml.ElementTree import parse

    shard = Shard(path.name, [], [])
    for case in parse(path).getroot().iter("testcase"):
        dotted = case.get("classname", "").split(".")
        name = case.get("name", "")
        for cut in range(len(dotted), 0, -1):
            file = "/".join(dotted[:cut]) + ".py"
            if (backend_root / file).is_file():
                test_id = "::".join([file, *dotted[cut:], name])
                shard.test_ids.append(test_id)
                reports = (child.get("message", "") + (child.text or "") for child in case)
                if any(_CRASHED in report for report in reports):
                    shard.crashed.append(test_id)
                break
    return shard


def _check_shard(shard: Shard, recorded: set[str]) -> None:
    """Refuse a shard that did not finish or whose coverage data is missing.

    xdist reports a crashed worker as a failed test, and the tests after
    it never run. A worker writes its coverage data only at the end of its
    session, so after a crash the shard has a junit file but no data. One
    worker runs a whole shard (``-n 1``), so the data of a shard is there
    when any of its tests is recorded.
    """
    if shard.crashed:
        raise ValueError(
            f"{shard.name}: a worker crashed while running {shard.crashed[0]},"
            " so the shard did not finish"
        )
    names = {context_name(function_id(test_id)) for test_id in shard.test_ids}
    if not names & recorded:
        raise ValueError(
            f"{shard.name}: none of its {len(names)} tests is in the coverage data,"
            " so the shard did not save its coverage"
        )


def function_id(test_id: str) -> str:
    """Drop the parameters: ``tests/a.py::C::t[x]`` -> ``tests/a.py::C::t``."""
    return test_id.split("[", 1)[0]


def context_name(function: str) -> str:
    """Return the name coverage's ``test_function`` context gives a test.

    ``tests/a/test_b.py::C::t`` -> ``tests.a.test_b.C.t``.
    """
    path, _, rest = function.partition("::")
    return path.removesuffix(".py").replace("/", ".") + "." + rest.replace("::", ".")


def coverage_contexts(data_file: str) -> dict[str, set[str]]:
    """Read the coverage data: repo path of a source file -> test functions that ran it."""
    from coverage import CoverageData

    data = CoverageData(data_file)
    data.read()
    result: dict[str, set[str]] = {}
    for measured in data.measured_files():
        names: set[str] = set()
        for contexts in data.contexts_by_lineno(measured).values():
            names.update(name for name in contexts if name)
        result["backend/" + measured] = names
    return result


def build_map(
    contexts: dict[str, set[str]], shards: Iterable[Shard], commit: str, made_at: datetime
) -> dict[str, Any]:
    """Return the map: every measured source file -> the tests that ran it.

    Tests are pytest test ids of functions (without parameters), with the
    number of test items each one runs. Tests under ``tests/integration/``
    and ``tests/migrations/`` are left out. A source file that no test ran
    is in the map with an empty list, so ``pick`` can tell it from a file
    the map does not know. Raises ``ValueError`` when a shard did not
    finish or did not save its coverage (:func:`_check_shard`).
    """
    recorded = set[str]().union(*contexts.values())
    items: list[str] = []
    for shard in shards:
        _check_shard(shard, recorded)
        items += [test_id for test_id in shard.test_ids if not test_id.startswith(OTHER_SUITES)]
    counts: dict[str, int] = defaultdict(int)
    for test_id in items:
        counts[function_id(test_id)] += 1
    tests = sorted(counts)
    index = {context_name(function): number for number, function in enumerate(tests)}
    files = {
        path: sorted(index[name] for name in names if name in index)
        for path, names in sorted(contexts.items())
    }
    return {
        "format": MAP_FORMAT,
        "commit": commit,
        "made_at": made_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_tests": len(items),
        "tests": [[function, counts[function]] for function in tests],
        "files": files,
    }


# --- pick --------------------------------------------------------------------


class FullSuiteError(Exception):
    """The full suite must run; the message says why."""


@dataclass
class TestMap:
    commit: str
    made_at: datetime
    total_tests: int
    tests: list[tuple[str, int]]
    files: dict[str, list[int]]

    @classmethod
    def load(cls, path: Path, now: datetime) -> TestMap:
        if not path.is_file():
            raise FullSuiteError("no test map")
        if zipfile.is_zipfile(path):
            # The artifact as GitHub sends it: a zip with the one JSON file.
            with zipfile.ZipFile(path) as archive:
                raw = json.loads(archive.read(MAP_FILE))
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format") != MAP_FORMAT:
            raise FullSuiteError(f"the test map has format {raw.get('format')!r}, not {MAP_FORMAT}")
        made_at = datetime.strptime(raw["made_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if now - made_at > timedelta(days=MAX_AGE_DAYS):
            raise FullSuiteError(
                f"the test map is older than {MAX_AGE_DAYS} days (made {made_at:%Y-%m-%d})"
            )
        return cls(
            commit=str(raw["commit"]),
            made_at=made_at,
            total_tests=int(raw["total_tests"]),
            tests=[(str(test), int(count)) for test, count in raw["tests"]],
            files={str(path): [int(i) for i in ids] for path, ids in raw["files"].items()},
        )


def _is_test_file(rel: PurePosixPath) -> bool:
    return rel.name.startswith("test_") and rel.suffix == ".py"


class ImportGraph:
    """Which Python files of ``backend/src`` and ``backend/tests`` import which.

    Files are repo paths. Importing ``a.b.c`` runs ``a/__init__.py`` and
    ``a/b/__init__.py`` too, so the files of every dotted prefix count.
    ``conftest.py`` files are kept apart (:attr:`conftests`): the graph does
    not follow them. ``tests/integration/`` and ``tests/migrations/`` are
    left out. ``deleted`` names source files (repo paths) that the pull
    request removed: files that still import them are found too.
    """

    def __init__(self, repo_root: Path, deleted: Iterable[str] = ()) -> None:
        backend = repo_root / "backend"
        modules: dict[str, str] = {}
        sources: dict[str, tuple[str, str]] = {}
        for repo_path in deleted:
            modules[_module_name(PurePosixPath(repo_path).relative_to("backend/src"))] = repo_path
        # (folder the dotted names start in, folder to scan)
        for base, folder in ((backend / "src", backend / "src"), (backend, backend / "tests")):
            for path in sorted(folder.rglob("*.py")):
                rel = PurePosixPath(path.relative_to(base).as_posix())
                if str(rel).startswith(OTHER_SUITES):
                    continue
                repo_path = path.relative_to(repo_root).as_posix()
                modules[_module_name(rel)] = repo_path
                text = path.read_text(encoding="utf-8")
                sources[repo_path] = (text, ".".join(rel.with_suffix("").parts))
        self.files = frozenset(sources)
        self.conftests = frozenset(p for p in sources if p.endswith("/conftest.py"))
        self.deps: dict[str, frozenset[str]] = {}
        for repo_path, (text, module) in sources.items():
            used = connector_select.imported_names(text, module)
            if used is None:
                # Python cannot parse it: count it as importing everything.
                self.deps[repo_path] = self.files
                continue
            self.deps[repo_path] = frozenset(
                modules[prefix]
                for name in used
                for prefix in _prefixes(name)
                if prefix in modules and modules[prefix] != repo_path
            )
        self._users: dict[str, set[str]] = defaultdict(set)
        for repo_path, deps in self.deps.items():
            if repo_path not in self.conftests:
                for dep in deps:
                    self._users[dep].add(repo_path)

    def importers(self, changed: str) -> set[str]:
        """Return ``changed`` and every file that imports it, directly or not.

        ``conftest.py`` files are not followed.
        """
        seen = {changed}
        todo = [changed]
        while todo:
            for user in self._users.get(todo.pop(), ()):
                if user not in seen:
                    seen.add(user)
                    todo.append(user)
        return seen


def _module_name(rel: PurePosixPath) -> str:
    """``a/b/c.py`` -> ``a.b.c``; ``a/b/__init__.py`` -> ``a.b``."""
    parts = rel.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _prefixes(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts) + 1)]


def _defines(tree: ast.Module, names: list[str]) -> bool:
    """Tell whether the module defines the test ``names`` (``[class..., function]``)."""
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    scope: ast.Module | ast.ClassDef = tree
    for class_name in names[:-1]:
        found = [n for n in scope.body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if not found:
            return False
        scope = found[0]
    todo = [scope]
    seen: set[str] = set()
    while todo:
        current = todo.pop()
        for node in current.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == names[-1]:
                return True
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == names[-1] for target in node.targets
            ):
                return True
        if isinstance(current, ast.ClassDef):
            # A test inherited from a class in the same file.
            for base in current.bases:
                if isinstance(base, ast.Name) and base.id in classes and base.id not in seen:
                    seen.add(base.id)
                    todo.append(classes[base.id])
    return False


def _is_test_function(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
        "test"
    )


def _is_mark(node: ast.AST, name: str) -> TypeGuard[ast.Call]:
    """Tell whether ``node`` is ``pytest.mark.<name>(...)`` or ``<name>(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == name) or (
        isinstance(func, ast.Name) and func.id == name
    )


def _constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level names bound once by a plain assignment, and never changed after."""
    bound: dict[str, int] = defaultdict(int)
    touched: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for name in (n for target in targets for n in ast.walk(target)):
                if isinstance(name, ast.Name):
                    bound[name.id] += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            touched.add(node.value.id)  # ``NAME.append(...)`` and the like.
    return {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and bound[node.targets[0].id] == 1
        and node.targets[0].id not in touched
    }


def _cases(decorators: list[ast.expr], constants: dict[str, ast.expr]) -> int:
    """Return how many items the ``parametrize`` decorators make of one test.

    A decorator can be a module-level name that holds a parametrize mark
    (``_CASES = pytest.mark.parametrize(...)``, then ``@_CASES``). A bare
    name the module does not bind may be such a mark from another file,
    so it makes the count uncertain.
    """
    count = 1
    for decorator in decorators:
        if isinstance(decorator, ast.Name):
            if decorator.id not in constants:
                raise ValueError(f"the decorator {decorator.id} comes from another file")
            decorator = constants[decorator.id]
        if _is_mark(decorator, "parametrize"):
            count *= _parametrize_cases(decorator, constants)
        elif not isinstance(decorator, (ast.Call, ast.Attribute)) or any(
            _is_mark(node, "parametrize") for node in ast.walk(decorator)
        ):
            raise ValueError("a decorator that may hold a parametrize mark")
    return count


def _parametrize_cases(mark: ast.Call, constants: dict[str, ast.expr]) -> int:
    """Return the number of items in the list of one ``parametrize`` mark."""
    keywords = {keyword.arg: keyword.value for keyword in mark.keywords}
    values = mark.args[1] if len(mark.args) > 1 else keywords.get("argvalues")
    if isinstance(values, ast.Name):
        values = constants.get(values.id)
    if not isinstance(values, (ast.List, ast.Tuple)) or any(
        isinstance(item, ast.Starred) for item in values.elts
    ):
        raise ValueError("a parametrize list that is not written out")
    return len(values.elts)


def _class_count(
    cls: ast.ClassDef, classes: dict[str, ast.ClassDef], constants: dict[str, ast.expr]
) -> int:
    """Count the test items of a ``Test*`` class, with the tests of its bases in the file."""
    chain = [cls]
    for current in chain:  # Grows while the loop runs.
        for base in current.bases:
            if not (isinstance(base, ast.Name) and base.id in classes):
                raise ValueError(f"the test class {cls.name} has a base from another file")
            if classes[base.id] not in chain:
                chain.append(classes[base.id])
    # A parametrize mark on a class, or on a base, applies to every test of the class.
    factor = 1
    for current in chain:
        factor *= _cases(current.decorator_list, constants)
    methods: dict[str, list[ast.expr]] = {}
    count = 0
    for current in chain:  # The class comes first: its own definition wins.
        for node in current.body:
            if _is_test_function(node) and node.name not in methods:
                methods[node.name] = node.decorator_list
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                count += _class_count(node, classes, constants) * factor
    return count + sum(_cases(decorators, constants) for decorators in methods.values()) * factor


def _file_test_count(tree: ast.Module) -> int:
    """Count the test items pytest collects from a test file.

    Follows pytest's default names (``test*`` functions, ``Test*`` classes)
    and ``@pytest.mark.parametrize`` with a list or tuple written out, or
    named at module level. Raises ``ValueError`` when the count is
    uncertain: a parametrize list built by code, a ``pytestmark`` with
    parametrize, a fixture with ``params``, ``pytest_generate_tests``, a
    test class with a base from another file, or a bare decorator name
    from another file.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "pytest_generate_tests":
                raise ValueError("pytest_generate_tests")
            for decorator in node.decorator_list:
                if _is_mark(decorator, "fixture") and any(
                    keyword.arg == "params" for keyword in decorator.keywords
                ):
                    raise ValueError("a fixture with params")
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            if any(_is_mark(call, "parametrize") for call in ast.walk(node.value)):
                raise ValueError("a parametrize mark in pytestmark")
    constants = _constants(tree)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    count = 0
    for node in tree.body:
        if _is_test_function(node):
            count += _cases(node.decorator_list, constants)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            count += _class_count(node, classes, constants)
    return count


@dataclass
class Picked:
    """What ``pick`` decided. ``tests`` is empty when the full suite runs."""

    line: str
    tests: list[str]


def _full_reason(path: str) -> str:
    """Return why the changed ``path`` needs the full suite, or ``""``."""
    rel = PurePosixPath(path)
    if path.startswith("backend/alembic/"):
        return f"a migration changed ({path})"
    if path.startswith("backend/tests/"):
        return "" if _is_test_file(rel) else f"a test helper or test data changed ({path})"
    if path.startswith("backend/src/") and rel.suffix == ".py":
        return ""
    return f"the test map cannot judge {path}"


def _resolve(test_map: TestMap, numbers: set[int], files: set[str], backend: Path) -> list[str]:
    """Turn map tests and whole test files into the list pytest gets."""
    trees: dict[str, ast.Module | None] = {}

    def tree(file: str) -> ast.Module | None:
        if file not in trees:
            path = backend / file
            trees[file] = ast.parse(path.read_text(encoding="utf-8")) if path.is_file() else None
        return trees[file]

    ids: set[str] = set()
    for number in numbers:
        test_id = test_map.tests[number][0]
        file, *names = test_id.split("::")
        if file in files:
            continue
        module = tree(file)
        if module is None:
            continue  # The test file is gone, so are its tests.
        if _defines(module, names):
            ids.add(test_id)
        else:
            files.add(file)
    ids = {test_id for test_id in ids if test_id.split("::", 1)[0] not in files}
    return sorted(files) + sorted(ids)


def _count(test_map: TestMap, selected: list[str], changed: set[str], backend: Path) -> int:
    """Count the test items of the selection; conservative for ``changed`` test files.

    A test id, or a test file the pull request did not change, counts what
    the map says. A test file the pull request changed, or that the map
    does not know, is counted from its source (:func:`_file_test_count`),
    and the larger of that and the map's count is used: the map cannot
    see the new tests, and the source cannot see what a fixture from
    ``conftest.py`` multiplies.
    """
    per_test = dict(test_map.tests)
    per_file: dict[str, int] = defaultdict(int)
    for test_id, count in test_map.tests:
        per_file[test_id.split("::", 1)[0]] += count
    total = 0
    for entry in selected:
        if "::" in entry:
            total += per_test[entry]
        elif entry in per_file and entry not in changed:
            total += per_file[entry]
        else:
            tree = ast.parse((backend / entry).read_text(encoding="utf-8"))
            try:
                total += max(per_file.get(entry, 0), _file_test_count(tree))
            except ValueError as reason:
                raise FullSuiteError(f"cannot count the tests of {entry} ({reason})") from None
    return total


def pick(changed: Iterable[str], map_path: Path, repo_root: Path, now: datetime) -> Picked:
    """Decide what the unit test job runs for a pull request."""
    try:
        return _pick(changed, map_path, repo_root, now)
    except FullSuiteError as reason:
        return Picked(f"full suite: {reason}", [])


def _pick(changed: Iterable[str], map_path: Path, repo_root: Path, now: datetime) -> Picked:
    test_map = TestMap.load(map_path, now)
    backend = repo_root / "backend"
    numbers: set[int] = set()
    files: set[str] = set()
    changed_tests: set[str] = set()
    no_test: list[str] = []
    paths = sorted({line.strip() for line in changed if line.strip()})
    if not paths:
        raise FullSuiteError("no changed files")
    for path in paths:
        if _DOCS.search(path):
            continue
        reason = _full_reason(path)
        if reason:
            raise FullSuiteError(reason)
        inner = path.removeprefix("backend/")
        if path.startswith("backend/tests/"):
            if not inner.startswith(OTHER_SUITES) and (backend / inner).is_file():
                files.add(inner)
                changed_tests.add(inner)
        elif path not in test_map.files:
            raise FullSuiteError(f"the test map does not know {path}")
        elif test_map.files[path]:
            numbers.update(test_map.files[path])
        else:
            no_test.append(path)

    if no_test:
        graph = ImportGraph(repo_root, [p for p in no_test if not (repo_root / p).is_file()])
        for path in no_test:
            for conftest in sorted(graph.conftests):
                if path in graph.deps[conftest]:
                    raise FullSuiteError(f"{conftest} imports {path}")
            for user in graph.importers(path):
                if user.startswith("backend/tests/") and _is_test_file(PurePosixPath(user)):
                    files.add(user.removeprefix("backend/"))
                numbers.update(test_map.files.get(user, ()))

    missing = [name for name in connector_select.CONTRACT_TESTS if not (backend / name).is_file()]
    if missing:
        raise FullSuiteError(f"contract test files not found: {', '.join(missing)}")
    files.update(connector_select.CONTRACT_TESTS)

    selected = _resolve(test_map, numbers, files, backend)
    count = _count(test_map, selected, changed_tests, backend)
    if count >= MAX_TESTS:
        raise FullSuiteError(f"the test map picked {count:,} tests, the limit is {MAX_TESTS - 1:,}")
    return Picked(
        f"test map from {test_map.made_at:%Y-%m-%d} (commit {test_map.commit[:7]}): "
        f"{count:,} of {test_map.total_tests:,} tests",
        selected,
    )


# --- command line -------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="make the map from coverage data")
    build_parser.add_argument("--data", required=True, help="combined coverage data file")
    build_parser.add_argument("--commit", required=True)
    build_parser.add_argument("--out", type=Path, required=True)
    build_parser.add_argument("--backend-root", type=Path, default=Path("."))
    build_parser.add_argument("junit", nargs="+", type=Path, help="pytest junit files")
    pick_parser = sub.add_parser("pick", help="pick the unit tests of a pull request")
    pick_parser.add_argument(
        "--map", type=Path, required=True, help=f"the map: {MAP_FILE}, or the artifact zip"
    )
    pick_parser.add_argument("--out", type=Path, required=True)
    pick_parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    if args.command == "build":
        shards = [read_junit(path, args.backend_root) for path in args.junit]
        try:
            result = build_map(coverage_contexts(args.data), shards, args.commit, now)
        except ValueError as reason:
            print(f"no test map: {reason}", file=sys.stderr)
            return 1
        args.out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        linked = sum(1 for ids in result["files"].values() if ids)
        print(
            f"test map: {len(result['tests'])} test functions ({result['total_tests']} tests),"
            f" {linked} of {len(result['files'])} source files linked to a test"
        )
        return 0

    try:
        picked = pick(sys.stdin, args.map, args.repo_root, now)
    except Exception as exc:  # Any error means the full suite.
        picked = Picked(f"full suite: the test map tool failed ({type(exc).__name__}: {exc})", [])
    if picked.tests:
        args.out.write_text("".join(f"{entry}\n" for entry in picked.tests), encoding="utf-8")
    print(picked.line)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
