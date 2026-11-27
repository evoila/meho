# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every direct request of a connector that is not a read is a named login or a traced send.

The pooled client of a connector that comes with MEHO gives the change rule
to every request that is neither a read nor a named login. So a login that
the code does not name ends on a redirect that a read would follow. This test
walks every connector module of MEHO and finds each direct call of a client
with a method that is not a read. Each one must be:

* inside a ``with login_request():`` block (a named login), or
* one of the few calls in :data:`_NOT_THE_POOLED_CLIENT`: the core send
  helpers themselves, and calls on a client that is not the pooled client
  and follows no redirect.

A write that a connector sends itself goes through
``HttpConnector._send_traced``, so it gets the change rule and one trace
entry for each request that was sent. A new direct write without a login
name makes this test fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

import meho_backplane.connectors as connectors_package

_ROOT = Path(connectors_package.__file__).parent

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_CALLS = frozenset({"post", "put", "patch", "delete"})
_GENERIC_CALLS = frozenset({"request", "stream", "send", "build_request"})
_CLIENT_FACTORIES = frozenset({"_http_client", "_ephemeral_http_client", "_build_http_client"})

#: ``<module path>::<function>`` -> why its direct request is not a named login.
_NOT_THE_POOLED_CLIENT: dict[str, str] = {
    "adapters/http.py::_stream_capped_json_response": (
        "the send of _request_json and _post_json: the operation's own request"
    ),
    "adapters/http.py::_send_traced": "the traced send of a typed connector's own write",
    "azure/_http.py::send": "Azure's own client, which follows no redirect",
    "_shared/microsoft_login.py::fetch_client_credentials_token": (
        "a new httpx client of its own, which follows no redirect"
    ),
    "github/session.py::exchange_jwt_for_installation_token": (
        "a new httpx client of its own, which follows no redirect"
    ),
    "kubernetes/wcp.py::wcp_login": "a new httpx client of its own, which follows no redirect",
    "net/fingerprint.py::_vsphere_about_call": "the probe client of the net tools",
    "net/http_probe.py::_walk_redirects": "the probe client of the net tools",
    "secret/handoff.py::_unwrap": "the store client of the hand-off, which follows no redirect",
}


def _is_login_block(node: ast.With | ast.AsyncWith) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call)
        and ast.unparse(item.context_expr.func).endswith("login_request")
        for item in node.items
    )


def _client_names(function: ast.AST) -> set[str]:
    """Names in *function* bound to a client that a connector's client factory made."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
            if isinstance(value, ast.Await):
                value = value.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr in _CLIENT_FACTORIES
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _is_client(base: ast.expr, client_names: set[str]) -> bool:
    text = ast.unparse(base)
    return text in client_names or "client" in text.lower() or "http" in text.lower()


def _is_non_read_call(node: ast.Call) -> bool:
    assert isinstance(node.func, ast.Attribute)
    if node.func.attr in _WRITE_CALLS:
        return True
    if node.func.attr not in _GENERIC_CALLS:
        return False
    first = node.args[0] if node.args else None
    return not (isinstance(first, ast.Constant) and str(first.value).upper() in _READ_METHODS)


def _unnamed_direct_requests(path: Path) -> list[tuple[str, int]]:
    """``(<function>, <line>)`` of each direct non-read request outside a login block."""
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST, function: str, client_names: set[str], named: bool) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function, client_names = node.name, _client_names(node)
        if isinstance(node, ast.With | ast.AsyncWith) and _is_login_block(node):
            named = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_client(node.func.value, client_names)
            and _is_non_read_call(node)
            and not named
        ):
            found.append((function, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, function, client_names, named)

    walk(ast.parse(path.read_text(encoding="utf-8")), "<module>", set(), False)
    return found


def _all_unnamed() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        relative = path.relative_to(_ROOT).as_posix()
        for function, line in _unnamed_direct_requests(path):
            found.setdefault(f"{relative}::{function}", []).append(line)
    return found


def test_each_direct_request_that_is_not_a_read_is_named() -> None:
    unnamed = {
        where: lines
        for where, lines in _all_unnamed().items()
        if where not in _NOT_THE_POOLED_CLIENT
    }
    assert not unnamed, (
        "A connector sends a request that is not a read directly on a client, "
        "outside a login_request() block. Name a login with login_request(); "
        f"send a write through HttpConnector._send_traced: {unnamed}"
    )


def test_each_listed_call_is_still_there() -> None:
    stale = set(_NOT_THE_POOLED_CLIENT) - set(_all_unnamed())
    assert not stale, f"Remove these entries, the call is gone or named: {sorted(stale)}"


def test_the_walk_finds_an_unnamed_direct_write(tmp_path: Path) -> None:
    module = tmp_path / "made_up.py"
    module.write_text(
        "async def write(self, target):\n"
        "    c = await self._http_client(target)\n"
        "    return await c.put('/items/7', json={})\n"
        "async def login(self, target):\n"
        "    client = await self._http_client(target)\n"
        "    with login_request():\n"
        "        return await client.post('/api/session')\n"
        "async def read(self, target):\n"
        "    client = await self._http_client(target)\n"
        "    return await client.request('GET', '/items')\n",
        encoding="utf-8",
    )
    assert _unnamed_direct_requests(module) == [("write", 3)]
