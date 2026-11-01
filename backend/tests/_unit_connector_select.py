# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pick a short unit test run for a pull request that changes one connector.

A pull request that changes only one connector folder does not need the
whole unit suite. CI then runs one job with the tests that can break:

* the tests of that connector, and of every connector that imports it;
* a fixed contract set (:data:`CONTRACT_TESTS`): registry, catalog,
  operation contract and app start tests that look at every connector,
  and tests that scan the source of every connector or all docs.

The merge queue and pushes to ``main`` always run the whole suite, so a
test this tool leaves out still runs before the change lands on ``main``.

Two commands:

``detect``
    Reads the changed files of a pull request (one path per line, relative
    to the repository root) on stdin. Prints the connector name when the
    short run is safe, or nothing. It fails closed: anything it does not
    recognise gives an empty answer, which means the full run. Used by the
    ``changes`` job in ``.github/workflows/ci.yml``. Needs only the Python
    standard library, because that job does not install the project.

``select --connector NAME``
    Reads the unit test files (relative to ``backend/``) on stdin, the same
    list the shard split gets. Prints the test files of the short run. Used
    by the ``python-unit-shard`` job when ``detect`` found a connector.

The short run is safe only when:

* every changed file that is not ``.md`` is in one folder
  ``backend/src/meho_backplane/connectors/<name>/``, or is a test file of
  that connector (``backend/tests/test_connectors_<name>_*.py``,
  ``backend/tests/test_connectors_<name>.py``,
  ``backend/tests/_<name>_fixtures.py``);
* the folder is a connector: not ``_shared`` or another folder that starts
  with ``_``, and not one of :data:`NOT_CONNECTORS`;
* no code outside the connector folders imports the connector, directly or
  through another connector that imports it. Such code is shared by many
  tests, so only the full run can check it;
* no ``conftest.py`` of the unit tests names the connector.

"Imports" means an ``import`` statement (absolute or relative) or the
dotted name ``meho_backplane.connectors.<name>`` anywhere in the file, for
example in a ``mock.patch`` target string.

This is CI tooling, not a test (the leading underscore keeps pytest from
collecting it). Its tests are in ``tests/test_unit_connector_select.py``.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

CONNECTORS_PACKAGE = "meho_backplane.connectors"
SRC_CONNECTORS = "backend/src/meho_backplane/connectors"

# Folders under connectors/ that are shared code, not a connector.
NOT_CONNECTORS = frozenset({"adapters", "profiles"})

# Test files that look at every connector at once. A change to one
# connector can break them without touching the connector's own tests.
CONTRACT_TESTS: tuple[str, ...] = (
    # The app starts and imports every connector folder.
    "tests/test_app_starts.py",
    # Connector registry, old and new tables.
    "tests/test_connectors_registry.py",
    "tests/test_connectors_registry_v2.py",
    # The start-up check of the connector folders.
    "tests/test_connector_packages.py",
    # The connector catalog and how ingest uses it.
    "tests/test_operations_ingest_catalog.py",
    "tests/test_api_v1_connectors_ingest.py",
    # Every typed operation: registration, contract rules, metadata.
    "tests/test_operations_typed_register.py",
    "tests/test_operation_contracts.py",
    "tests/test_typed_connectors_metadata.py",
    "tests/test_destructive_builder_conformance.py",
    "tests/test_credential_write_preview_conformance.py",
    "tests/test_operations_jsonflux_conformance.py",
    "tests/test_broadcast_classifier_coverage.py",
    # Every typed operation in the flight recorder.
    "tests/test_flight_recorder_typed_spans.py",
    "tests/test_flight_recorder_secret_field_guard.py",
    # Generated reference docs, maturity labels and the MCP tool list.
    "tests/test_reference_docs_drift.py",
    "tests/test_maturity_surface_drift.py",
    "tests/test_mcp_surface_conformance.py",
    # The target product list in the OpenAPI document.
    "tests/test_openapi_target_product_enum.py",
    # Read-back hints in every connector file and every docs page.
    "tests/test_connectors_no_phantom_readback_tool.py",
    # Broadcast publish calls anywhere in the package.
    "tests/test_broadcast_agent_intent_cutover.py",
    # Audit row calls anywhere in the package.
    "tests/test_audit_event_class.py",
    "tests/test_audit_wrapper_cutover.py",
    # Sandbox mode rules for every file in the package.
    "tests/test_sandbox_mode_audit_mark.py",
    "tests/test_sandbox_mode_boot.py",
    # One class name that must exist only once in the package.
    "tests/test_gateway_assignment_api.py",
    # ``sudo`` command lines in every connector folder.
    "tests/test_connectors_sudo_argv_gate.py",
    # Imports of a leaving VMware folder, read from every module in the package.
    "tests/test_split_ready_imports.py",
)

_DOTTED = re.compile(r"\bmeho_backplane\.connectors\.([A-Za-z0-9_]+)")


def connector_names(src_connectors: Path) -> frozenset[str]:
    """Return the connector folder names under ``src_connectors``."""
    return frozenset(
        path.name
        for path in src_connectors.iterdir()
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and not path.name.startswith("_")
        and path.name not in NOT_CONNECTORS
    )


def referenced_connectors(text: str, module: str | None, names: frozenset[str]) -> set[str]:
    """Return the connectors in ``names`` that the Python source ``text`` uses.

    ``module`` is the dotted name of the file (``a.b.c`` for ``a/b/c.py``,
    ``a.b`` for ``a/b/__init__.py``). It resolves relative imports. Pass
    ``None`` for a file outside the package; it then has no relative
    imports to resolve.
    """
    found = {match for match in _DOTTED.findall(text) if match in names}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # A file Python cannot parse: treat it as using every connector, so
        # the caller falls back to the full run.
        return set(names)
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, module)
            if base is None:
                continue
            targets = [base] + [f"{base}.{alias.name}" for alias in node.names]
        for target in targets:
            parts = target.split(".")
            if (
                parts[:2] == ["meho_backplane", "connectors"]
                and len(parts) > 2
                and parts[2] in names
            ):
                found.add(parts[2])
    return found


def _resolve_from(node: ast.ImportFrom, module: str | None) -> str | None:
    if node.level == 0:
        return node.module
    if module is None:
        return None
    package = module.split(".")
    # ``module`` of an ``__init__.py`` is the package itself; for any other
    # file the package is the parent. The caller passes ``a.b.__init__`` for
    # ``a/b/__init__.py`` so both cases drop exactly one part here.
    base = package[: len(package) - node.level]
    if len(base) < 1:
        return None
    if node.module:
        base = [*base, *node.module.split(".")]
    return ".".join(base)


def _src_owner(path: PurePosixPath, names: frozenset[str]) -> str | None:
    """Return the connector folder ``path`` lives in, or ``None`` for core code."""
    parts = path.parts
    if parts[:2] == ("meho_backplane", "connectors") and len(parts) > 3 and parts[2] in names:
        return parts[2]
    return None


def importers_by_connector(src_root: Path, names: frozenset[str]) -> dict[str, set[str | None]]:
    """Map each connector to the owners of the source files that use it.

    ``src_root`` is ``backend/src``. An owner is a connector name, or
    ``None`` for code outside the connector folders. A file never counts
    as an importer of its own connector.
    """
    result: dict[str, set[str | None]] = {name: set() for name in names}
    for path in sorted(src_root.rglob("*.py")):
        rel = PurePosixPath(path.relative_to(src_root).as_posix())
        module = ".".join(rel.with_suffix("").parts)
        owner = _src_owner(rel, names)
        for used in referenced_connectors(path.read_text(encoding="utf-8"), module, names):
            if used != owner:
                result[used].add(owner)
    return result


def connector_closure(name: str, importers: dict[str, set[str | None]]) -> frozenset[str] | None:
    """Return ``name`` plus every connector that imports it, directly or not.

    Returns ``None`` when code outside the connector folders imports any of
    them: then only the full run is safe.
    """
    seen = {name}
    todo = [name]
    while todo:
        current = todo.pop()
        for owner in importers.get(current, set()):
            if owner is None:
                return None
            if owner not in seen:
                seen.add(owner)
                todo.append(owner)
    return frozenset(seen)


def _test_owner(filename: str, names: frozenset[str]) -> str | None:
    """Return the connector a test file name belongs to (longest name wins)."""
    owners = [
        name
        for name in names
        if filename in (f"test_connectors_{name}.py", f"_{name}_fixtures.py")
        or (filename.startswith(f"test_connectors_{name}_") and filename.endswith(".py"))
    ]
    return max(owners, key=len) if owners else None


def detect(changed: Iterable[str], repo_root: Path) -> str:
    """Return the connector name when the short run is safe, else ``""``."""
    src_connectors = repo_root / SRC_CONNECTORS
    if not src_connectors.is_dir():
        return ""
    names = connector_names(src_connectors)
    src_hits: set[str] = set()
    test_hits: set[str] = set()
    paths = [line.strip() for line in changed if line.strip()]
    for path in paths:
        if path.endswith(".md"):
            continue
        pure = PurePosixPath(path)
        parts = pure.parts
        if path.startswith(f"{SRC_CONNECTORS}/") and len(parts) > 5:
            src_hits.add(parts[4])
        elif len(parts) == 3 and parts[:2] == ("backend", "tests"):
            owner = _test_owner(parts[2], names)
            if owner is None:
                return ""
            test_hits.add(owner)
        else:
            return ""
    if len(src_hits) != 1:
        return ""
    name = next(iter(src_hits))
    if name not in names or test_hits - {name}:
        return ""
    importers = importers_by_connector(repo_root / "backend" / "src", names)
    closure = connector_closure(name, importers)
    if closure is None:
        return ""
    tests_root = repo_root / "backend" / "tests"
    for conftest in sorted(tests_root.rglob("conftest.py")):
        if "integration" in conftest.relative_to(tests_root).parts[:1]:
            continue
        used = referenced_connectors(conftest.read_text(encoding="utf-8"), None, names)
        if used & closure:
            return ""
    return name


def select(connector: str, candidates: Iterable[str], backend_root: Path) -> list[str]:
    """Return the test files of the short run for ``connector``.

    ``candidates`` are test file paths relative to ``backend_root``. Raises
    ``ValueError`` when the short run is not safe for ``connector`` or a
    contract test file is missing: the CI job must then fail, not pass with
    too few tests.
    """
    src_connectors = backend_root / "src" / "meho_backplane" / "connectors"
    names = connector_names(src_connectors)
    if connector not in names:
        raise ValueError(f"{connector!r} is not a connector folder")
    closure = connector_closure(connector, importers_by_connector(backend_root / "src", names))
    if closure is None:
        raise ValueError(f"code outside the connector folders imports {connector!r}")
    files = sorted({line.strip() for line in candidates if line.strip()})
    missing = [name for name in CONTRACT_TESTS if name not in files]
    if missing:
        raise ValueError(f"contract test files not found: {', '.join(missing)}")

    # Helper modules (not test files) that use one of the connectors. A test
    # that imports such a helper is selected too.
    tests_root = backend_root / "tests"
    helpers = {
        path.stem
        for path in tests_root.rglob("*.py")
        if not path.name.startswith("test_")
        and path.name != "conftest.py"
        and "integration" not in path.relative_to(tests_root).parts[:1]
        and referenced_connectors(path.read_text(encoding="utf-8"), None, names) & closure
    }
    helper_pattern = (
        re.compile(r"\b(" + "|".join(re.escape(stem) for stem in sorted(helpers)) + r")\b")
        if helpers
        else None
    )

    selected = set(CONTRACT_TESTS)
    for name in files:
        if _test_owner(PurePosixPath(name).name, names) in closure:
            selected.add(name)
            continue
        text = (backend_root / name).read_text(encoding="utf-8")
        uses_helper = helper_pattern is not None and helper_pattern.search(text) is not None
        if uses_helper or referenced_connectors(text, None, names) & closure:
            selected.add(name)
    return sorted(selected)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    detect_parser = sub.add_parser("detect", help="print the connector name, or nothing")
    detect_parser.add_argument("--repo-root", type=Path, default=Path("."))
    select_parser = sub.add_parser("select", help="print the test files of the short run")
    select_parser.add_argument("--connector", required=True)
    select_parser.add_argument("--backend-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    if args.command == "detect":
        print(detect(sys.stdin, args.repo_root))
        return 0
    try:
        selected = select(args.connector, sys.stdin, args.backend_root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    contract = sum(1 for name in selected if name in CONTRACT_TESTS)
    print(
        f"connector {args.connector}: {len(selected)} test files "
        f"({contract} contract files, {len(selected) - contract} connector files)",
        file=sys.stderr,
    )
    for name in selected:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
