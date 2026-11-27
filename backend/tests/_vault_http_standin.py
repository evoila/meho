# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stand-in Vault: a plain HTTP server on the local host, for the real ``hvac`` client.

It answers the few Vault requests the block tests send: the JWT login, the
end of a login, a KV v2 item (read, write, patch), the capabilities request
and the mount read. It keeps the KV items and counts each request.

:meth:`VaultStandIn.on_request` runs a step while one request is on the
wire, before Vault answers it: a test blocks the caller there. The step runs
on the test's event loop, so it can write a real block row.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from meho_backplane.settings import get_settings


@dataclass
class _Hook:
    method: str
    path_part: str
    step: Callable[[], Awaitable[None]]
    done: bool = False


@dataclass
class VaultStandIn:
    """What the stand-in Vault holds and what it received."""

    url: str = ""
    requests: list[tuple[str, str]] = field(default_factory=list)
    items: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    hooks: list[_Hook] = field(default_factory=list)
    loop: asyncio.AbstractEventLoop | None = None

    def on_request(self, method: str, path_part: str, step: Callable[[], Awaitable[None]]) -> None:
        """Run *step* once, while the first matching request waits for its answer."""
        self.hooks.append(_Hook(method, path_part, step))

    def sent(self, method: str, path_part: str) -> int:
        """How many requests with *method* and *path_part* in the path Vault received."""
        return sum(1 for m, p in self.requests if m == method and path_part in p)

    def _run_hooks(self, method: str, path: str) -> None:
        for hook in self.hooks:
            if not hook.done and hook.method == method and hook.path_part in path:
                hook.done = True
                assert self.loop is not None
                asyncio.run_coroutine_threadsafe(_call(hook.step), self.loop).result(timeout=30)


async def _call(step: Callable[[], Awaitable[None]]) -> None:
    await step()


def _kv_key(path: str) -> str | None:
    parts = path.split("/", 4)  # "", "v1", mount, "data", item path
    if len(parts) == 5 and parts[3] == "data":
        return f"{parts[2]}/{parts[4]}"
    return None


def _handler(state: VaultStandIn) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw) if raw else {}

        def _reply(self, status: int, payload: dict[str, Any] | None = None) -> None:
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            body = self._body()
            state.requests.append((method, path))
            state._run_hooks(method, path)
            self._answer(method, path, body)

        def _answer(self, method: str, path: str, body: dict[str, Any]) -> None:
            if path.startswith("/v1/auth/") and path.endswith("/login"):
                auth = {"client_token": "standin-login-token", "accessor": "standin-accessor"}
                self._reply(200, {"auth": {**auth, "lease_duration": 60, "policies": []}})
            elif path == "/v1/auth/token/revoke-self":
                self._reply(204)
            elif path == "/v1/sys/capabilities-self":
                paths = body.get("paths") or []
                grant = ["create", "read", "update"]
                self._reply(200, {"capabilities": grant, **dict.fromkeys(paths, grant)})
            elif path.startswith("/v1/sys/internal/ui/mounts/"):
                self._reply(200, {"data": {"type": "kv", "options": {"version": "2"}}})
            elif (key := _kv_key(path)) is not None:
                self._kv(method, key, body)
            else:
                self._reply(404, {"errors": []})

        def _kv(self, method: str, key: str, body: dict[str, Any]) -> None:
            version, data = state.items.get(key, (0, {}))
            if method == "GET":
                if version == 0:
                    self._reply(404, {"errors": []})
                    return
                meta = {"version": version, "deletion_time": "", "destroyed": False}
                self._reply(200, {"data": {"data": data, "metadata": meta}})
                return
            if method == "PATCH":
                if version == 0:
                    self._reply(404, {"errors": []})
                    return
                data = {**data, **(body.get("data") or {})}
            else:
                data = dict(body.get("data") or {})
            state.items[key] = (version + 1, data)
            self._reply(200, {"data": {"version": version + 1}})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_LIST(self) -> None:
            self._handle("LIST")

    return Handler


@contextmanager
def vault_standin(monkeypatch: pytest.MonkeyPatch) -> Iterator[VaultStandIn]:
    """Start the stand-in Vault and point ``VAULT_ADDR`` at it.

    Call it inside a running test, so the hooks can reach the event loop.
    """
    state = VaultStandIn(loop=asyncio.get_running_loop())
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VAULT_ADDR", state.url)
    get_settings.cache_clear()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        get_settings.cache_clear()
