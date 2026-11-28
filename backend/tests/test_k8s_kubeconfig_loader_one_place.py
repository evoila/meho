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

#: Loaders of ``kubernetes_asyncio.config`` that read a kubeconfig.
_LOADER_NAMES = frozenset(
    {
        "load_kube_config_from_dict",
        "new_client_from_config_dict",
        "load_kube_config",
        "new_client_from_config",
        "KubeConfigLoader",
    }
)

#: The one module, and the functions in it, allowed to call a loader.
_ALLOWED_MODULE = Path("meho_backplane/connectors/kubernetes/kubeconfig_schema.py")
_ALLOWED_FUNCTIONS = frozenset({"load_passive_kubeconfig", "new_passive_api_client"})


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def find_loader_uses(source: str, *, allowed: bool) -> list[tuple[int, str]]:
    """Return ``(line, name)`` for each loader call or import in *source*.

    With *allowed* set, calls inside the helper functions are not reported.
    """
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        in_helper = allowed and function in _ALLOWED_FUNCTIONS
        if isinstance(node, ast.Call) and not in_helper:
            name = _called_name(node)
            if name in _LOADER_NAMES:
                found.append((node.lineno, name))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "kubernetes_asyncio"
        ):
            found.extend((node.lineno, a.name) for a in node.names if a.name in _LOADER_NAMES)
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), None)
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
