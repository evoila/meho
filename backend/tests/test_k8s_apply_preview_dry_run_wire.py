# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dry run of ``preview_operation`` for ``k8s.apply``, on the wire.

Each test runs ``preview_operation``, the entry point that the MCP tool and
the REST route share, with the real Kubernetes client
(``kubernetes_asyncio``) against an HTTP stand-in server that records the
method, path, query and login of every request. The client starts cold: no
cached list of the cluster's API.

* Every request that can change something carries ``dryRun=All``. The only
  ``GET`` requests read the cluster's API list. Every request carries the
  target's cluster account, never the caller's token.
* An answer that is not a decision of the cluster (401, 503) makes the
  document ``not_checked``; a refusal (422) stays ``refused``.
* When the cluster refuses the login on the first request, the note says
  that the cluster login failed, in a plain sentence.
* When the caller is cancelled after the cluster got the dry run, MEHO still
  writes the one audit row of the read.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from kubernetes_asyncio import config
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import meta_tools
from meho_backplane.settings import get_settings
from tests.test_k8s_apply_card_through_dispatch import (
    _CONNECTOR_ID,
    _TENANT,
    _kubernetes_registry,
    _login,
    _register_ops,
    _Target,
)
from tests.test_ui_approvals import _bff_env, _seed_tenant

__all__ = ["_bff_env", "_kubernetes_registry"]

_TARGET_TOKEN = "target-account-token-for-tests"
_TARGET = _Target(id=uuid.UUID("00000000-0000-0000-0000-0000000a7a12"))

#: The paths of the cluster's API list that the client reads.
_API_LIST_PATH = re.compile(r"^/(version|api|api/v1|apis|apis/[a-z0-9.-]+/v1)$")

_API_V1 = {
    "kind": "APIResourceList",
    "groupVersion": "v1",
    "resources": [
        {"name": "configmaps", "kind": "ConfigMap", "namespaced": True, "verbs": ["patch"]},
        {"name": "namespaces", "kind": "Namespace", "namespaced": False, "verbs": ["patch"]},
    ],
}
_GROUPS = {
    "kind": "APIGroupList",
    "groups": [
        {
            "name": group,
            "versions": [{"groupVersion": f"{group}/v1", "version": "v1"}],
            "preferredVersion": {"groupVersion": f"{group}/v1", "version": "v1"},
        }
        for group in ("apps", "networking.k8s.io")
    ],
}
_GROUP_RESOURCES = {
    "apps": ("deployments", "Deployment"),
    "networking.k8s.io": ("ingresses", "Ingress"),
}


class _StandInServer:
    """An HTTP stand-in Kubernetes API server that records every request.

    An apply answers with the status in the name of the object (``cm-503``
    gets 503); any other name gets 201. With *refuse_login* every request
    gets 401. With *hang* an apply waits until it is cancelled; ``applied``
    is set when the first apply arrives.
    """

    def __init__(self, *, refuse_login: bool = False, hang: bool = False) -> None:
        self.refuse_login = refuse_login
        self.hang = hang
        self.applied = asyncio.Event()
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
            }
        )
        if self.refuse_login:
            return _status(401, "the login was refused")
        if request.method == "GET":
            return self._api_list(request.path)
        self.applied.set()
        if self.hang:
            await asyncio.Event().wait()
        name = request.path.rsplit("/", 1)[-1]
        code = int(name[-3:]) if name[-3:].isdigit() else 201
        if code >= 300:
            return _status(code, f"answer {code} for {name}")
        return web.json_response(await request.json(), status=code)

    def _api_list(self, path: str) -> web.Response:
        if path == "/version":
            return web.json_response({"major": "1", "minor": "30", "gitVersion": "v1.30.0"})
        if path == "/api":
            return web.json_response({"kind": "APIVersions", "versions": ["v1"]})
        if path == "/api/v1":
            return web.json_response(_API_V1)
        if path == "/apis":
            return web.json_response(_GROUPS)
        group = path.removeprefix("/apis/").removesuffix("/v1")
        if group in _GROUP_RESOURCES:
            plural, kind = _GROUP_RESOURCES[group]
            resource = {"name": plural, "kind": kind, "namespaced": True, "verbs": ["patch"]}
            return web.json_response(
                {"kind": "APIResourceList", "groupVersion": f"{group}/v1", "resources": [resource]}
            )
        return _status(404, "not found")


def _status(code: int, message: str) -> web.Response:
    body = {"kind": "Status", "apiVersion": "v1", "status": "Failure", "message": message}
    return web.json_response({**body, "code": code}, status=code)


@asynccontextmanager
async def _serve(server: _StandInServer) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def _kubeconfig(host: str) -> dict[str, Any]:
    """The target's own cluster account, as its stored kubeconfig gives it."""
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": host}}],
        "users": [{"name": "u", "user": {"token": _TARGET_TOKEN}}],
        "contexts": [{"name": "x", "context": {"cluster": "c", "user": "u"}}],
        "current-context": "x",
    }


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("K8S_APPLY_PREVIEW_DRY_RUN", "true")
    monkeypatch.setattr(
        meta_tools, "_resolve_target_or_error", AsyncMock(return_value=(_TARGET, None))
    )
    # The client keeps its list of the cluster's API in the temp folder; a
    # new folder for each test keeps the client cold.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    get_settings.cache_clear()
    _seed_tenant(_TENANT, "apply-wire")
    asyncio.run(_register_ops())
    yield
    get_settings.cache_clear()


def _arguments(manifest: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": "k8s.apply",
        "target": _TARGET.name,
        "params": {"manifest": manifest},
    }


def _preview(
    monkeypatch: pytest.MonkeyPatch, server: _StandInServer, manifest: str, caller: Operator
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with _serve(server) as host:
            api_client = await config.new_client_from_config_dict(_kubeconfig(host))
            monkeypatch.setattr(
                KubernetesConnector, "_get_api_client", AsyncMock(return_value=api_client)
            )
            try:
                return await meta_tools.preview_operation(caller, _arguments(manifest))
            finally:
                await api_client.close()

    return asyncio.run(_run())


def _read_rows(caller: Operator) -> list[AuditLog]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.operator_sub == caller.sub, AuditLog.method == "PREVIEW"
                )
            )
            return list(result.scalars().all())

    return asyncio.run(_do())


def _doc(api_version: str, kind: str, name: str) -> str:
    return f"apiVersion: {api_version}\nkind: {kind}\nmetadata: {{name: {name}, namespace: prod}}\n"


def test_every_request_that_can_change_something_is_a_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    caller = _login("writer")
    manifest = "---\n".join(
        (
            _doc("v1", "ConfigMap", "settings"),
            _doc("apps/v1", "Deployment", "web"),
            _doc("networking.k8s.io/v1", "Ingress", "web"),
        )
    )

    answer = _preview(monkeypatch, server, manifest, caller)

    assert answer["status"] == "ok", answer
    assert answer["plan_shown"] is True, answer
    changes = [doc["change"] for doc in answer["proposed_effect"]["preview"]["documents"]]
    assert changes == ["create", "create", "create"]
    applies = [r for r in server.requests if r["method"] != "GET"]
    assert len(applies) == 3
    # The client started cold, so it read the API list first.
    assert any(r["path"] == "/version" for r in server.requests)
    for request in server.requests:
        if request["method"] == "GET":
            assert _API_LIST_PATH.match(request["path"]), request
        else:
            assert request["query"].get("dryRun") == "All", request
        login = request["headers"].get("Authorization")
        assert login == f"Bearer {_TARGET_TOKEN}", request
        assert caller.raw_jwt not in str(request["headers"])


def test_an_answer_that_is_not_a_decision_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    manifest = "---\n".join(
        _doc("v1", "ConfigMap", name) for name in ("cm-401", "cm-503", "cm-422", "cm-201")
    )

    answer = _preview(monkeypatch, server, manifest, _login("writer"))

    preview = answer["proposed_effect"]["preview"]
    documents = preview["documents"]
    assert [doc["change"] for doc in documents] == [
        "not_checked",
        "not_checked",
        "refused",
        "create",
    ]
    assert "HTTP 401" in documents[0]["reason"]
    assert "HTTP 503" in documents[1]["reason"]
    assert "answer 422 for cm-422" in documents[2]["error"]
    assert preview["not_checked"] == [1, 2]
    assert answer["plan_shown"] is False
    assert "every part" in answer["plan_note"]


def test_a_refused_cluster_login_gives_a_plain_note(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StandInServer(refuse_login=True)

    answer = _preview(monkeypatch, server, _doc("v1", "ConfigMap", "settings"), _login("writer"))

    assert answer["status"] == "ok"
    assert answer["plan_shown"] is False
    assert "proposed_effect" not in answer
    note = answer["plan_note"]
    assert "the cluster login failed" in note
    assert "HTTP 401" in note
    assert not re.search(r"\b[A-Z][A-Za-z]*(Error|Exception)\b", note), note
    assert all(r["method"] == "GET" for r in server.requests)


def test_a_cancelled_preview_still_writes_its_read_row(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StandInServer(hang=True)
    caller = _login("writer-cancelled")

    async def _run() -> None:
        async with _serve(server) as host:
            api_client = await config.new_client_from_config_dict(_kubeconfig(host))
            monkeypatch.setattr(
                KubernetesConnector, "_get_api_client", AsyncMock(return_value=api_client)
            )
            try:
                task = asyncio.ensure_future(
                    meta_tools.preview_operation(
                        caller, _arguments(_doc("v1", "ConfigMap", "settings"))
                    )
                )
                await asyncio.wait_for(server.applied.wait(), timeout=5)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                assert task.cancelled()
            finally:
                await api_client.close()

    asyncio.run(_run())

    applies = [r for r in server.requests if r["method"] != "GET"]
    assert len(applies) == 1
    assert applies[0]["query"].get("dryRun") == "All"
    rows = _read_rows(caller)
    assert len(rows) == 1
    assert rows[0].payload["op_class"] == "read"
    assert rows[0].payload["result_status"] == "cancelled"
    assert rows[0].payload["documents"] == 1
    assert rows[0].target_id == _TARGET.id
