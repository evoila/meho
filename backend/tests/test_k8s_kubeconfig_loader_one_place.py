# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard: only the passive-check helpers hand a kubeconfig to the library.

Every kubeconfig mapping MEHO loads goes through
:func:`~meho_backplane.connectors.kubernetes.kubeconfig_schema.load_passive_kubeconfig`
or
:func:`~meho_backplane.connectors.kubernetes.kubeconfig_schema.new_passive_api_client`.
Both run ``enforce_passive_kubeconfig`` first. This test scans
``backend/src`` and fails when any other code calls (or imports) a
kubeconfig loader of ``kubernetes_asyncio``, so a new path cannot skip
the check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Names of ``kubernetes_asyncio.config`` that read a kubeconfig or run
#: what one names.
_LOADER_NAMES = frozenset(
    {
        "load_kube_config_from_dict",
        "new_client_from_config_dict",
        "load_kube_config",
        "new_client_from_config",
        "KubeConfigLoader",
        "load_config",
        "KubeConfigMerger",
        "list_kube_config_contexts",
        "refresh_token",
    }
)

_LIBRARY = "kubernetes_asyncio"

#: The one module, and the functions in it, allowed to call a loader.
_ALLOWED_MODULE = Path("meho_backplane/connectors/kubernetes/kubeconfig_schema.py")
_ALLOWED_FUNCTIONS = frozenset({"load_passive_kubeconfig", "new_passive_api_client"})


def _from_library(module: str | None) -> bool:
    return module is not None and (module == _LIBRARY or module.startswith(f"{_LIBRARY}."))


def _library_bindings(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Return the local names bound to library modules and to listed names.

    The first set holds names that stand for a module of the library
    (``import kubernetes_asyncio``, ``from kubernetes_asyncio import
    config as c``). The map holds local names of listed loaders imported
    from the library, mapped to the loader name.
    """
    modules: set[str] = set()
    loaders: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _from_library(alias.name):
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and _from_library(node.module):
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in _LOADER_NAMES:
                    loaders[local] = alias.name
                else:
                    modules.add(local)
    return modules, loaders


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _called_loader(node: ast.Call, modules: set[str], loaders: dict[str, str]) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return loaders.get(func.id)
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _LOADER_NAMES
        and _root_name(func.value) in modules
    ):
        return func.attr
    return None


def find_loader_uses(source: str, *, allowed: bool) -> list[tuple[int, str]]:
    """Return ``(line, name)`` for each library loader call or import in *source*.

    A name counts only when the imports of *source* bind it to
    ``kubernetes_asyncio``; a function of MEHO's own with the same name
    is not reported. With *allowed* set, calls inside the helper
    functions are not reported.
    """
    tree = ast.parse(source)
    modules, loaders = _library_bindings(tree)
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        in_helper = allowed and function in _ALLOWED_FUNCTIONS
        if isinstance(node, ast.Call) and not in_helper:
            name = _called_loader(node, modules, loaders)
            if name is not None:
                found.append((node.lineno, name))
        if isinstance(node, ast.ImportFrom) and _from_library(node.module):
            found.extend((node.lineno, a.name) for a in node.names if a.name in _LOADER_NAMES)
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


def test_no_kubeconfig_loader_outside_the_passive_helpers() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC)
        uses = find_loader_uses(path.read_text(), allowed=relative == _ALLOWED_MODULE)
        offenders.extend(f"{relative}:{line} {name}" for line, name in uses)
    assert offenders == [], (
        "call load_passive_kubeconfig / new_passive_api_client instead of a "
        f"kubernetes_asyncio kubeconfig loader: {offenders}"
    )


def test_helpers_are_the_ones_that_call_the_loaders() -> None:
    source = (_SRC / _ALLOWED_MODULE).read_text()
    names = {name for _, name in find_loader_uses(source, allowed=False)}
    assert names == {"load_kube_config_from_dict", "new_client_from_config_dict"}


@pytest.mark.parametrize(
    "snippet",
    [
        "from kubernetes_asyncio import config\n"
        "async def f(d, c):\n"
        "    await config.load_kube_config_from_dict(config_dict=d, client_configuration=c)\n",
        "from kubernetes_asyncio.config import new_client_from_config_dict as build\n",
        "import kubernetes_asyncio\n"
        "async def f(d):\n"
        "    return await kubernetes_asyncio.config.new_client_from_config_dict(d)\n",
        "async def load_passive_kubeconfig(d, c):\n"
        "    from kubernetes_asyncio.config.kube_config import KubeConfigLoader\n"
        "    KubeConfigLoader(config_dict=d)\n",
    ],
)
def test_guard_catches_a_direct_loader_call(snippet: str) -> None:
    assert find_loader_uses(snippet, allowed=False) != []


@pytest.mark.parametrize("name", sorted(_LOADER_NAMES))
def test_guard_reports_each_listed_name_of_the_library(name: str) -> None:
    imported = f"from kubernetes_asyncio.config import {name}\n{name}()\n"
    assert find_loader_uses(imported, allowed=False) == [(1, name), (2, name)]
    called = f"from kubernetes_asyncio import config\nconfig.{name}()\n"
    assert find_loader_uses(called, allowed=False) == [(2, name)]


def test_guard_reports_a_call_on_the_library_config_module_under_another_name() -> None:
    source = "from kubernetes_asyncio import config as c\nc.load_config(config_file='x')\n"
    assert find_loader_uses(source, allowed=False) == [(2, "load_config")]


def test_guard_skips_meho_functions_with_a_listed_name() -> None:
    source = (
        "from meho_backplane.session_store import object_store\n"
        "def load_config():\n"
        "    return {}\n"
        "async def refresh_token(token):\n"
        "    return token\n"
        "async def f(store, flow):\n"
        "    load_config()\n"
        "    object_store.load_config()\n"
        "    await refresh_token('t')\n"
        "    await flow.refresh_token('t')\n"
    )
    assert find_loader_uses(source, allowed=False) == []
