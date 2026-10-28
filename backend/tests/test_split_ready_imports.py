# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No public module imports a connector folder that leaves in the split (#5272).

The VMware connector folders and ``salt`` move out of this repository into
their own packages. After that move the public code must still import and
run. So no module outside those folders may import one of them.

The test reads every module under ``meho_backplane`` with :mod:`ast`. It
does not import them. It checks plain imports, ``from`` imports and
relative imports, also the ones inside a function body.
"""

from __future__ import annotations

import ast
from pathlib import Path

import meho_backplane

#: The connector folders that leave the public tree in the split.
LEAVING_FOLDERS: frozenset[str] = frozenset(
    {
        "vmware_rest",
        "nsx",
        "sddc_manager",
        "sddc_vcf5",
        "vcf_automation",
        "vra8",
        "vcf_fleet",
        "fleet_lcm",
        "vcf_installer",
        "vcf_logs",
        "vrli8",
        "vcf_operations",
        "vrops8",
        "vcd",
        "holodeck",
        "hcx",
        "srm",
        "vr",
        "vro",
        "salt",
    }
)

#: Public modules that still import a leaving folder today, as paths under
#: ``meho_backplane/``. The Task that moves the VMware-only modules out of
#: ``connectors/_shared/`` (#5273) empties this list.
KNOWN_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "connectors/_shared/vcfa_session.py",
    }
)

_PACKAGE_ROOT = Path(meho_backplane.__file__).parent
_PACKAGE_NAME = "meho_backplane"


def _module_parts(path: Path) -> tuple[str, ...]:
    """Dotted name parts of the module at *path* (``__init__`` names the package)."""
    rel = path.relative_to(_PACKAGE_ROOT).with_suffix("")
    parts = (_PACKAGE_NAME, *rel.parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def _is_package(path: Path) -> bool:
    return path.name == "__init__.py"


def _imported_modules(tree: ast.AST, parts: tuple[str, ...], is_package: bool) -> list[str]:
    """Every module name an import statement in *tree* can load, made absolute.

    For ``from a import b`` both ``a`` and ``a.b`` are listed, because ``b``
    can be a submodule.
    """
    package = parts if is_package else parts[:-1]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[: len(package) - (node.level - 1)])
                if node.module:
                    base.append(node.module)
                module = ".".join(base)
            else:
                module = node.module or ""
            found.append(module)
            found.extend(f"{module}.{alias.name}" for alias in node.names)
    return found


def _leaving_folder(module: str) -> str | None:
    """The leaving folder *module* belongs to, else ``None``."""
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == _PACKAGE_NAME and parts[1] == "connectors":
        return parts[2] if parts[2] in LEAVING_FOLDERS else None
    return None


def _violations(source: str, parts: tuple[str, ...], is_package: bool) -> list[str]:
    tree = ast.parse(source)
    return sorted(
        {
            module
            for module in _imported_modules(tree, parts, is_package)
            if _leaving_folder(module) is not None
        }
    )


def _public_modules() -> list[Path]:
    """Every module under ``meho_backplane`` that is not in a leaving folder."""
    modules = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(_PACKAGE_ROOT).parts
        if len(rel) >= 2 and rel[0] == "connectors" and rel[1] in LEAVING_FOLDERS:
            continue
        modules.append(path)
    return modules


def _all_violations() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in _public_modules():
        found = _violations(
            path.read_text(encoding="utf-8"), _module_parts(path), _is_package(path)
        )
        if found:
            result[path.relative_to(_PACKAGE_ROOT).as_posix()] = found
    return result


def test_leaving_folders_exist() -> None:
    """Every listed folder is a real connector folder, so a typo cannot hide one."""
    connectors = _PACKAGE_ROOT / "connectors"
    missing = sorted(name for name in LEAVING_FOLDERS if not (connectors / name).is_dir())
    assert missing == []


def test_no_public_module_imports_a_leaving_folder() -> None:
    """No module outside the leaving folders imports one, except the known list."""
    violations = {
        path: modules for path, modules in _all_violations().items() if path not in KNOWN_EXCEPTIONS
    }
    assert violations == {}, (
        "These public modules import a connector folder that leaves in the split. "
        "Move the shared code into a public module, or move the module into the "
        f"connector folder: {violations}"
    )


def test_known_exceptions_still_import_a_leaving_folder() -> None:
    """A listed module that no longer needs its place on the list must leave it.

    A module that was moved away (its file is gone) is skipped here.
    """
    violations = _all_violations()
    stale = sorted(
        path
        for path in KNOWN_EXCEPTIONS
        if (_PACKAGE_ROOT / path).is_file() and path not in violations
    )
    assert stale == [], f"Remove these from KNOWN_EXCEPTIONS: {stale}"


def test_guard_catches_each_import_form() -> None:
    """The check finds absolute, ``from``, relative and in-function imports."""
    core = ("meho_backplane", "operations", "_errors")
    cases = {
        "import meho_backplane.connectors.vmware_rest.soap": True,
        "from meho_backplane.connectors.vmware_rest.soap import parse_soap_fault": True,
        "from meho_backplane.connectors import salt": True,
        "from ..connectors.nsx import connector": True,
        "def f():\n    from meho_backplane.connectors.vro import x\n": True,
        "from meho_backplane.connectors._shared.soap_fault import parse_soap_fault": False,
        "from meho_backplane.connectors import vault": False,
        "from meho_backplane.connectors.vrops_like import x": False,
        "from . import _branches": False,
    }
    for source, expected in cases.items():
        assert bool(_violations(source, core, is_package=False)) is expected, source


def test_guard_resolves_relative_imports_from_a_package() -> None:
    """In an ``__init__.py`` a one-dot import means the package itself."""
    package = ("meho_backplane", "connectors")
    assert _violations("from .vmware_rest import soap", package, is_package=True) == [
        "meho_backplane.connectors.vmware_rest",
        "meho_backplane.connectors.vmware_rest.soap",
    ]
    assert _violations("from .vault import x", package, is_package=True) == []
