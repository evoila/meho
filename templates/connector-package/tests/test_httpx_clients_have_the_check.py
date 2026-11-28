# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every ``httpx`` client the package builds itself carries the kit's check.

The check of the caller's block runs right before each request. An
``httpx.AsyncClient`` gets it as a request hook:
``httpx.AsyncClient(..., event_hooks={"request": [kit.check_before_send]})``.

The test reads every file of the package with :mod:`ast`. It does not run
them. It finds a call of ``AsyncClient`` that does not name
``check_before_send`` under the ``"request"`` key of its ``event_hooks``,
and every call of the synchronous ``Client``. It cannot see the sends of a
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

#: Files that build another client than an ``httpx.AsyncClient`` with the request
#: hook. The key is the file's path inside the package folder. The value is one
#: line that says where the file calls ``await kit.check_before_send()``.
EXCEPTIONS: dict[str, str] = {}

_CLIENT_NAMES = {"AsyncClient", "Client"}
#: ``httpx.Client`` calls a hook without ``await``, so the async check never runs.
_SYNC_CLIENT = "Client"


def _package_folder() -> Path:
    spec = importlib.util.find_spec(PACKAGE)
    assert spec is not None and spec.submodule_search_locations, f"{PACKAGE} not found"
    return Path(next(iter(spec.submodule_search_locations)))


def _client_name(call: ast.Call) -> str | None:
    """``AsyncClient`` or ``Client`` when *call* builds an ``httpx`` client."""
    func = call.func
    if isinstance(func, ast.Attribute):
        is_client = (
            func.attr in _CLIENT_NAMES
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        )
        return func.attr if is_client else None
    return func.id if isinstance(func, ast.Name) and func.id in _CLIENT_NAMES else None


def _has_the_check(node: ast.AST) -> bool:
    return any(
        (isinstance(part, ast.Attribute) and part.attr == "check_before_send")
        or (isinstance(part, ast.Name) and part.id == "check_before_send")
        for part in ast.walk(node)
    )


def _is_dict_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == "dict"


def _names_the_check(call: ast.Call) -> bool:
    """Whether ``event_hooks`` has the check under its ``"request"`` key."""
    for keyword in call.keywords:
        if keyword.arg != "event_hooks":
            continue
        hooks = keyword.value
        if isinstance(hooks, ast.Call) and _is_dict_call(hooks):
            return any(
                item.arg == "request" and _has_the_check(item.value) for item in hooks.keywords
            )
        if not isinstance(hooks, ast.Dict):
            return False
        return any(
            isinstance(key, ast.Constant) and key.value == "request" and _has_the_check(value)
            for key, value in zip(hooks.keys, hooks.values, strict=True)
        )
    return False


def clients_without_the_check(source: str) -> list[int]:
    """The line of every ``httpx`` client in *source* where the check does not run.

    A synchronous ``Client`` is always on the list, also with the hook.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (name := _client_name(node)) is not None
        and (name == _SYNC_CLIENT or not _names_the_check(node))
    )


def files_without_the_check(folder: Path, exceptions: dict[str, str]) -> dict[str, list[int]]:
    """The lines of each file in *folder*, not in *exceptions*, with a client without the check."""
    files = sorted(folder.rglob("*.py"))
    assert files, f"no Python files in {folder}"
    return {
        name: lines
        for path in files
        if (name := path.relative_to(folder).as_posix()) not in exceptions
        and (lines := clients_without_the_check(path.read_text(encoding="utf-8")))
    }


def test_every_httpx_client_of_the_package_has_the_check() -> None:
    """No file of the package builds an ``httpx`` client without the request hook."""
    found = files_without_the_check(_package_folder(), EXCEPTIONS)
    assert found == {}, (
        "These files build an httpx client without the check of the caller's block "
        f"(file: lines): {found}. Use an httpx.AsyncClient with "
        'event_hooks={"request": [kit.check_before_send]}, or list the file in EXCEPTIONS '
        "with one line that says where it calls await kit.check_before_send()."
    )


def test_a_file_in_exceptions_is_left_out(tmp_path: Path) -> None:
    """A file with another client is found, and left out once it is in the exceptions."""
    (tmp_path / "fetch.py").write_text(
        "import httpx\nclient = httpx.Client(base_url='https://t')\n", encoding="utf-8"
    )
    assert files_without_the_check(tmp_path, {}) == {"fetch.py": [2]}
    listed = {"fetch.py": "fetch() calls the check right before asyncio.to_thread"}
    assert files_without_the_check(tmp_path, listed) == {}


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
    sync_client = (
        'import httpx\nc = httpx.Client(event_hooks={"request": [kit.check_before_send]})\n'
    )
    assert clients_without_the_check(sync_client) == [2]
    response_by_call = (
        "import httpx\nc = httpx.AsyncClient(event_hooks=dict(response=[kit.check_before_send]))\n"
    )
    assert clients_without_the_check(response_by_call) == [2]
    response_by_merge = (
        "import httpx\nc = httpx.AsyncClient(event_hooks=default_hooks"
        ' | {"response": [kit.check_before_send]})\n'
    )
    assert clients_without_the_check(response_by_merge) == [2]
    response_by_helper = (
        "import httpx\nc = httpx.AsyncClient("
        "event_hooks=make_hooks(response=[kit.check_before_send]))\n"
    )
    assert clients_without_the_check(response_by_helper) == [2]


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
