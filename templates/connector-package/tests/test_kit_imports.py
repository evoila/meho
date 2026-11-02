# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The connector package imports MEHO only through the connector kit.

A connector package may import ``meho_connector_kit`` and its own folder.
Every other MEHO module is private: it can change in any MEHO release.

The test reads every file of the package with :mod:`ast`. It does not run
them. It checks plain imports, ``from`` imports and relative imports, also
the ones inside a function body. It finds the package's folder where Python
would import it from, so it checks the files MEHO loads.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

#: The package's own folder, as a module name. Rename it with the folder.
PACKAGE = "meho_backplane.connectors.example_widget"

#: MEHO's own top-level package. Only the package's own folder in it may be imported.
_MEHO = "meho_backplane"


def _package_folder() -> Path:
    spec = importlib.util.find_spec(PACKAGE)
    assert spec is not None and spec.submodule_search_locations, f"{PACKAGE} not found"
    return Path(next(iter(spec.submodule_search_locations)))


def _allowed(module: str) -> bool:
    """Whether a file of the package may import *module*."""
    if module.split(".")[0] != _MEHO:
        return True
    return module == PACKAGE or module.startswith(f"{PACKAGE}.")


def _module_parts(folder: Path, path: Path) -> tuple[str, ...]:
    """The module name of *path*, as parts (``__init__`` names the package)."""
    parts = (*PACKAGE.split("."), *path.relative_to(folder).with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def violations(source: str, parts: tuple[str, ...], *, is_package: bool) -> list[str]:
    """Every MEHO module outside the kit and the package that *source* imports."""
    package = parts if is_package else parts[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if not _allowed(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[: len(package) - (node.level - 1)])
                if node.module:
                    base.append(node.module)
                module = ".".join(base)
            else:
                module = node.module or ""
            # ``from a import b`` is fine when ``a`` or ``a.b`` is allowed (``b`` can be a module).
            found.update(
                f"{module}.{alias.name}"
                for alias in node.names
                if not _allowed(module) and not _allowed(f"{module}.{alias.name}")
            )
    return sorted(found)


def test_package_imports_meho_only_through_the_kit() -> None:
    """No file of the package imports a MEHO module other than the kit or the package."""
    folder = _package_folder()
    files = sorted(folder.rglob("*.py"))
    assert files, f"no Python files in {folder}"
    found = {
        path.relative_to(folder).as_posix(): bad
        for path in files
        if (
            bad := violations(
                path.read_text(encoding="utf-8"),
                _module_parts(folder, path),
                is_package=path.name == "__init__.py",
            )
        )
    }
    assert found == {}, (
        "These files import MEHO modules outside the connector kit. Import the names "
        f"from meho_connector_kit instead: {found}"
    )


def test_check_finds_each_import_form() -> None:
    """The check finds absolute, ``from``, relative and in-function imports."""
    module = (*PACKAGE.split("."), "connector")
    cases = {
        "import meho_backplane.connectors.registry": True,
        "from meho_backplane.connectors.base import Connector": True,
        "from meho_backplane.connectors import registry": True,
        "from meho_backplane import operations": True,
        "from .. import registry": True,
        "from ..base import Connector": True,
        "def f():\n    from meho_backplane.auth.vault import x\n": True,
        "import meho_connector_kit": False,
        "from meho_connector_kit import HttpConnector": False,
        "from . import operations": False,
        "from .operations import OPERATIONS": False,
        f"from {PACKAGE}.operations import OPERATIONS": False,
        "from meho_backplane.connectors import example_widget": False,
        "import httpx": False,
    }
    for source, expected in cases.items():
        assert bool(violations(source, module, is_package=False)) is expected, source


def test_check_reads_relative_imports_of_the_package_file() -> None:
    """In ``__init__.py`` one dot is the package itself, two dots leave it."""
    parts = tuple(PACKAGE.split("."))
    assert violations("from .connector import X", parts, is_package=True) == []
    assert violations("from ..registry import X", parts, is_package=True) == [
        "meho_backplane.connectors.registry.X"
    ]
