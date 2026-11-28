# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The package builds every ``httpx`` client with ``kit.build_http_client``.

``kit.build_http_client(...)`` returns an ``httpx.AsyncClient`` with the check
of the caller's block as its request hook, so the check runs right before each
request. It is the one supported way.

The test reads every file of the package with :mod:`ast`. It does not run
them. It looks up what each name means in the file's imports, so
``import httpx as hx`` or ``from httpx import AsyncClient as C`` does not hide
a client. It reports each use of a name that builds an ``httpx`` client:

- ``httpx.AsyncClient`` and ``httpx.Client``: a call, a class that inherits
  from it, or any other use such as ``factory = httpx.AsyncClient``. A type
  annotation is fine.
- the request functions of the ``httpx`` module (``httpx.get``,
  ``httpx.post``, ``httpx.request``, ``httpx.stream``, ...): each builds a
  client for one request;
- ``from httpx import *``;
- a transport that retries by itself (``httpx.AsyncHTTPTransport(retries=3)``):
  its retries go out without the check.

A file that must use a blocking client goes into ``EXCEPTIONS``. The test
cannot see clients that are not ``httpx`` (a vendor library, a database
driver, an SSH connection): those call ``await kit.check_before_send()``
right before each call that sends, and each needs a test that runs it.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

#: The package's own folder, as a module name. Rename it with the folder.
PACKAGE = "meho_backplane.connectors.example_widget"

#: Files that use the blocking ``httpx.Client``. The key is the file's path
#: inside the package folder. The value is one line that says where the file
#: calls ``await kit.check_before_send()``. An entry leaves out only the uses of
#: ``httpx.Client`` in that file: every other client in it is still reported.
EXCEPTIONS: dict[str, str] = {}

_CLIENTS = {"AsyncClient", "Client"}
_BLOCKING_CLIENT = "Client"
_REQUEST_FUNCTIONS = {
    "request",
    "stream",
    "get",
    "options",
    "head",
    "post",
    "put",
    "patch",
    "delete",
}
_TRANSPORTS = {"AsyncHTTPTransport", "HTTPTransport"}


def _package_folder() -> Path:
    spec = importlib.util.find_spec(PACKAGE)
    assert spec is not None and spec.submodule_search_locations, f"{PACKAGE} not found"
    return Path(next(iter(spec.submodule_search_locations)))


def _imports(tree: ast.Module) -> dict[str, str]:
    """Each name the file's imports bind, with the full name it stands for."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    names[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _full_name(node: ast.AST, names: dict[str, str]) -> list[str] | None:
    """The parts of the full name *node* stands for, when it starts with an import."""
    if isinstance(node, ast.Name):
        return names[node.id].split(".") if node.id in names else None
    if isinstance(node, ast.Attribute):
        base = _full_name(node.value, names)
        return [*base, node.attr] if base else None
    return None


def _annotations(tree: ast.Module) -> set[int]:
    """The ids of the nodes inside a type annotation."""
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.TypeAlias):
            roots.append(node.value)
    return {id(part) for root in roots for part in ast.walk(root)}


def _retries_other_than_zero(call: ast.Call) -> bool:
    return any(
        keyword.arg == "retries"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value == 0)
        for keyword in call.keywords
    )


def _finding(node: ast.AST, names: dict[str, str], skip: set[int]) -> str | None:
    """What *node* builds, or ``None`` when it builds no ``httpx`` client."""
    if isinstance(node, ast.ImportFrom) and node.module == "httpx":
        return "httpx.*" if any(alias.name == "*" for alias in node.names) else None
    if isinstance(node, ast.Call):
        parts = _full_name(node.func, names)
        if parts and parts[0] == "httpx" and parts[-1] in _TRANSPORTS:
            return "retries" if _retries_other_than_zero(node) else None
        return None
    if not isinstance(node, ast.Name | ast.Attribute) or not isinstance(node.ctx, ast.Load):
        return None
    if id(node) in skip:
        return None
    parts = _full_name(node, names)
    if not parts or parts[0] != "httpx":
        return None
    if parts[-1] in _CLIENTS or (len(parts) == 2 and parts[1] in _REQUEST_FUNCTIONS):
        return parts[-1]
    return None


def clients_found(source: str, *, blocking_client_allowed: bool = False) -> list[int]:
    """The line of each way to build an ``httpx`` client in *source*, other than the kit's.

    With *blocking_client_allowed*, the uses of ``httpx.Client`` are left out.
    """
    tree = ast.parse(source)
    names = _imports(tree)
    skip = _annotations(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if (found := _finding(node, names, skip)) is not None
            and not (blocking_client_allowed and found == _BLOCKING_CLIENT)
        }
    )


def files_with_clients(folder: Path, exceptions: dict[str, str]) -> dict[str, list[int]]:
    """The lines of each file in *folder* that build an ``httpx`` client another way."""
    files = sorted(folder.rglob("*.py"))
    assert files, f"no Python files in {folder}"
    names = {path.relative_to(folder).as_posix() for path in files}
    missing = sorted(set(exceptions) - names)
    assert not missing, f"EXCEPTIONS lists files that are not in the package: {missing}"
    return {
        name: lines
        for path in files
        if (
            lines := clients_found(
                path.read_text(encoding="utf-8"),
                blocking_client_allowed=(name := path.relative_to(folder).as_posix()) in exceptions,
            )
        )
    }


def exceptions_text(exceptions: dict[str, str]) -> str:
    """The list of exceptions, one file per line."""
    if not exceptions:
        return "EXCEPTIONS: none"
    return "\n".join(
        ["EXCEPTIONS (httpx.Client allowed):"]
        + [f"- {name}: {where}" for name, where in sorted(exceptions.items())]
    )


def test_every_httpx_client_of_the_package_comes_from_the_kit() -> None:
    """No file of the package builds an ``httpx`` client another way."""
    print(exceptions_text(EXCEPTIONS))
    found = files_with_clients(_package_folder(), EXCEPTIONS)
    assert found == {}, (
        "These files build an httpx client another way than kit.build_http_client "
        f"(file: lines): {found}. Use kit.build_http_client(...). A file that must use "
        "the blocking httpx.Client goes into EXCEPTIONS, with one line that says where "
        "it calls await kit.check_before_send()."
    )


_IMPORTS = "import httpx\nimport meho_connector_kit as kit\n"


def test_the_kit_function_passes() -> None:
    """A client from the kit, an annotation and other ``httpx`` names pass."""
    source = _IMPORTS + (
        "async def fetch(url: str, client: httpx.AsyncClient | None = None) -> httpx.Response:\n"
        "    client = kit.build_http_client(base_url='https://t', follow_redirects=False,\n"
        "        transport=httpx.AsyncHTTPTransport(retries=0))\n"
        "    try:\n"
        "        return await client.get(url)\n"
        "    except httpx.HTTPError:\n"
        "        raise\n"
        "type Pool = dict[str, httpx.AsyncClient]\n"
    )
    assert clients_found(source) == []


def test_every_other_way_is_found() -> None:
    """Each way to build an ``httpx`` client other than the kit's is found."""
    cases = {
        "direct": "c = httpx.AsyncClient()\n",
        "with the hook": (
            'c = httpx.AsyncClient(event_hooks={"request": [kit.check_before_send]})\n'
        ),
        "blocking": 'c = httpx.Client(event_hooks={"request": [kit.check_before_send]})\n',
        "hooks by name": "c = httpx.AsyncClient(event_hooks=hooks)\n",
        "hook replaced by a spread": (
            'c = httpx.AsyncClient(event_hooks={"request": [kit.check_before_send], **d})\n'
        ),
        "hook replaced by a second key": (
            'c = httpx.AsyncClient(event_hooks={"request": [kit.check_before_send],'
            ' "request": []})\n'
        ),
        "subclass": "class NoRedirects(httpx.AsyncClient): ...\n",
        "alias": "factory = httpx.AsyncClient\n",
        "partial": "import functools\nc = functools.partial(httpx.AsyncClient, timeout=5)\n",
        "request function": "r = httpx.get('https://t')\n",
        "transport retries": "t = httpx.AsyncHTTPTransport(retries=2)\n",
        "transport retries by name": "t = httpx.AsyncHTTPTransport(retries=n)\n",
        "blocking transport retries": "t = httpx.HTTPTransport(retries=1)\n",
    }
    for case, line in cases.items():
        source = _IMPORTS + line
        assert clients_found(source) == [source.count("\n")], case


def test_a_client_under_another_import_name_is_found() -> None:
    """The name is looked up in the imports, so renaming it hides nothing."""
    assert clients_found("import httpx as hx\nc = hx.AsyncClient()\n") == [2]
    assert clients_found("from httpx import AsyncClient as C\nc = C()\n") == [2]
    assert clients_found("from httpx import Client\nclass S(Client):\n    pass\n") == [2]
    assert clients_found("import httpx._client as m\nc = m.AsyncClient()\n") == [2]
    assert clients_found("from httpx import post as send\nsend('https://t')\n") == [2]
    assert clients_found("from httpx import *\n") == [1]
    assert clients_found("AsyncClient = object\nc = AsyncClient()\n") == []


def test_a_listed_file_leaves_out_only_its_blocking_client(tmp_path: Path) -> None:
    """A listed file passes with ``httpx.Client``, and still reports any other client."""
    (tmp_path / "fetch.py").write_text(
        "import httpx\nsync_client = httpx.Client(base_url='https://t', follow_redirects=False)\n",
        encoding="utf-8",
    )
    listed = {"fetch.py": "fetch() calls the check right before asyncio.to_thread"}
    assert files_with_clients(tmp_path, {}) == {"fetch.py": [2]}
    assert files_with_clients(tmp_path, listed) == {}

    (tmp_path / "fetch.py").write_text(
        "import httpx\nsync_client = httpx.Client()\nasync_client = httpx.AsyncClient()\n",
        encoding="utf-8",
    )
    assert files_with_clients(tmp_path, listed) == {"fetch.py": [3]}


def test_the_exceptions_are_printed_and_must_exist(tmp_path: Path) -> None:
    """The list names each file and its line; an entry for a missing file fails."""
    listed = {"fetch.py": "fetch() calls the check right before asyncio.to_thread"}
    assert exceptions_text(listed) == (
        "EXCEPTIONS (httpx.Client allowed):\n"
        "- fetch.py: fetch() calls the check right before asyncio.to_thread"
    )
    assert exceptions_text({}) == "EXCEPTIONS: none"
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    try:
        files_with_clients(tmp_path, listed)
    except AssertionError as error:
        assert "fetch.py" in str(error)
    else:
        raise AssertionError("a missing file in EXCEPTIONS must fail")
