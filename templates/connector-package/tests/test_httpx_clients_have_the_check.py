# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every ``httpx`` client the package builds itself carries the kit's check.

The check of the caller's block runs right before each request. An
``httpx`` client gets it as a request hook:
``httpx.AsyncClient(..., event_hooks={"request": [kit.check_before_send]})``.

The test reads every file of the package with :mod:`ast`. It does not run
them. It finds a call of ``AsyncClient`` or ``Client`` that does not name
``check_before_send`` in its ``event_hooks``. It cannot see the sends of a
client that is not ``httpx`` (a vendor library, a database driver, an SSH
connection): those call ``await kit.check_before_send()`` right before each
call that sends, and each needs a test that runs it.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

#: The package's own folder, as a module name. Rename it with the folder.
PACKAGE = "meho_backplane.connectors.example_widget"

_CLIENT_NAMES = {"AsyncClient", "Client"}


def _package_folder() -> Path:
    spec = importlib.util.find_spec(PACKAGE)
    assert spec is not None and spec.submodule_search_locations, f"{PACKAGE} not found"
    return Path(next(iter(spec.submodule_search_locations)))


def _is_httpx_client(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr in _CLIENT_NAMES
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        )
    return isinstance(func, ast.Name) and func.id in _CLIENT_NAMES


def _has_the_check(node: ast.AST) -> bool:
    return any(
        (isinstance(part, ast.Attribute) and part.attr == "check_before_send")
        or (isinstance(part, ast.Name) and part.id == "check_before_send")
        for part in ast.walk(node)
    )


def _names_the_check(call: ast.Call) -> bool:
    """Whether ``event_hooks`` has the check under its ``"request"`` key."""
    for keyword in call.keywords:
        if keyword.arg != "event_hooks":
            continue
        hooks = keyword.value
        if not isinstance(hooks, ast.Dict):
            return _has_the_check(hooks)
        return any(
            isinstance(key, ast.Constant) and key.value == "request" and _has_the_check(value)
            for key, value in zip(hooks.keys, hooks.values, strict=True)
        )
    return False


def clients_without_the_check(source: str) -> list[int]:
    """The line of every ``httpx`` client in *source* that has no check hook."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _is_httpx_client(node) and not _names_the_check(node)
    )


def test_every_httpx_client_of_the_package_has_the_check() -> None:
    """No file of the package builds an ``httpx`` client without the request hook."""
    folder = _package_folder()
    files = sorted(folder.rglob("*.py"))
    assert files, f"no Python files in {folder}"
    found = {
        path.relative_to(folder).as_posix(): lines
        for path in files
        if (lines := clients_without_the_check(path.read_text(encoding="utf-8")))
    }
    assert found == {}, (
        "These files build an httpx client without the check of the caller's block. Add "
        f'event_hooks={{"request": [kit.check_before_send]}} (file: lines): {found}'
    )


def test_check_finds_a_client_without_the_hook() -> None:
    """A client with no hook, a hook of another name, or no request hook is found."""
    assert clients_without_the_check("import httpx\nc = httpx.AsyncClient()\n") == [2]
    assert clients_without_the_check("from httpx import AsyncClient\nc = AsyncClient()\n") == [2]
    assert clients_without_the_check("import httpx\nc = httpx.Client(timeout=5)\n") == [2]
    other = 'import httpx\nc = httpx.AsyncClient(event_hooks={"request": [log_it]})\n'
    assert clients_without_the_check(other) == [2]
    response_only = (
        'import httpx\nc = httpx.AsyncClient(event_hooks={"response": [kit.check_before_send]})\n'
    )
    assert clients_without_the_check(response_only) == [2]


def test_check_passes_a_client_with_the_hook() -> None:
    """The kit function as a request hook, by attribute or by name, passes."""
    attribute = (
        "import httpx, meho_connector_kit as kit\n"
        'c = httpx.AsyncClient(event_hooks={"request": [kit.check_before_send]})\n'
    )
    by_name = (
        "import httpx\n"
        "from meho_connector_kit import check_before_send\n"
        'c = httpx.AsyncClient(event_hooks={"request": [check_before_send]})\n'
    )
    assert clients_without_the_check(attribute) == []
    assert clients_without_the_check(by_name) == []
