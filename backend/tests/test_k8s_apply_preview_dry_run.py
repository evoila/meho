# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``preview_operation`` of ``k8s.apply`` shows a dry run on the cluster.

Each test goes through the real ``preview_dispatch`` over a stand-in
Kubernetes API server that records every request.

* With ``K8S_APPLY_PREVIEW_DRY_RUN`` on, the preview shows for each document
  whether the apply creates it, changes it or is refused, with
  ``plan_shown: true``. Every request carries ``dryRun=All``, and the preview
  makes no approval request and no audit row.
* A Secret value of the manifest is not in the dry-run result of the preview.
* A cluster that does not answer in time gives the time-out note and the
  rest of the preview.
* A login with the Read-only role gets no dry run.
* With the switch off, the preview sends nothing and answers as before.
* The text of the ``preview_operation`` tool names the dry run only while the
  switch is on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from sqlalchemy import func, select

import meho_backplane.mcp.tools.operations  # noqa: F401 — registers the tool
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import tool_for_settings
from meho_backplane.mcp.tools._preview_typed_writes_text import K8S_APPLY_DRY_RUN_TEXT_SWAP
from meho_backplane.operations import _composite_preview
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import build_operator
from tests.test_k8s_apply_card_through_dispatch import (
    _CONNECTOR_ID,
    _TENANT,
    _kubernetes_registry,
    _login,
    _register_ops,
    _Target,
)
from tests.test_k8s_apply_object_summary import _Response
from tests.test_ui_approvals import _bff_env, _seed_tenant

__all__ = ["_bff_env", "_kubernetes_registry"]

_SWITCH = "K8S_APPLY_PREVIEW_DRY_RUN"


class _StandInCluster:
    """A stand-in API server: 201 for a new object, 200 for one in *live*.

    A kind of *refused_kinds* is refused with 422, as an admission check
    refuses it. A namespace of *missing_namespaces* gives 404. With *hang*
    each apply waits until it is cancelled.
    """

    def __init__(
        self,
        *,
        live: frozenset[str] = frozenset(),
        refused_kinds: frozenset[str] = frozenset(),
        missing_namespaces: frozenset[str] = frozenset(),
        hang: bool = False,
    ) -> None:
        self.live = live
        self.refused_kinds = refused_kinds
        self.missing_namespaces = missing_namespaces
        self.hang = hang
        self.requests: list[dict[str, Any]] = []
        self.resources = SimpleNamespace(get=self._discover)

    async def _discover(self, *, api_version: str, kind: str) -> Any:
        return SimpleNamespace(kind=kind, namespaced=kind != "Namespace")

    async def server_side_apply(self, resource: Any, **kwargs: Any) -> _Response:
        self.requests.append({"kind": resource.kind, **kwargs})
        if self.hang:
            await asyncio.Event().wait()
        if kwargs.get("namespace") in self.missing_namespaces:
            return _Response(404, {"kind": "Status", "message": "namespaces web not found"})
        if resource.kind in self.refused_kinds:
            return _Response(422, {"kind": "Status", "message": "refused by an admission check"})
        body = kwargs["body"]
        return _Response(200 if kwargs["name"] in self.live else 201, body)


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_SWITCH, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SWITCH, "true")
    get_settings.cache_clear()


def _setup(monkeypatch: pytest.MonkeyPatch, cluster: _StandInCluster) -> None:
    _seed_tenant(_TENANT, "apply-preview")
    asyncio.run(_register_ops())

    async def _dynamic_client(_api_client: Any) -> Any:
        return cluster

    for module in ("ops_write_dangerous", "ops_write_apply_dry_run"):
        monkeypatch.setattr(
            f"meho_backplane.connectors.kubernetes.{module}.DynamicClient", _dynamic_client
        )
    monkeypatch.setattr(
        KubernetesConnector, "_get_api_client", AsyncMock(return_value=MagicMock(close=AsyncMock()))
    )


def _preview(manifest: str, operator: Operator | None = None) -> dict[str, Any]:
    return asyncio.run(
        preview_dispatch(
            operator=operator or _login("writer"),
            connector_id=_CONNECTOR_ID,
            op_id="k8s.apply",
            target=_Target(),
            params={"manifest": manifest},
        )
    )


def _counts() -> tuple[int, int]:
    async def _do() -> tuple[int, int]:
        async with get_sessionmaker()() as session:
            requests = (
                await session.execute(select(func.count()).select_from(ApprovalRequest))
            ).scalar_one()
            audits = (
                await session.execute(select(func.count()).select_from(AuditLog))
            ).scalar_one()
        return int(requests), int(audits)

    return asyncio.run(_do())


_THREE_OBJECTS = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: prod}\n"
    "data: {mode: fast}\n"
    "---\n"
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: prod}\n"
    "spec: {template: {spec: {containers: [{name: app, image: web:2}]}}}\n"
    "---\n"
    "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: web, namespace: prod}\n"
    "spec: {rules: []}\n"
)


def _changes(answer: dict[str, Any]) -> list[tuple[int, str, str]]:
    documents = answer["proposed_effect"]["preview"]["documents"]
    return [(doc["document"], doc["kind"], doc["change"]) for doc in documents]


def test_the_preview_shows_a_create_a_change_and_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster(live=frozenset({"web"}), refused_kinds=frozenset({"Ingress"}))
    _setup(monkeypatch, cluster)
    before = _counts()

    answer = _preview(_THREE_OBJECTS)

    assert answer["status"] == "ok", answer
    assert answer["plan_shown"] is True
    assert "plan_note" not in answer
    assert _changes(answer) == [
        (1, "ConfigMap", "create"),
        (2, "Deployment", "change"),
        (3, "Ingress", "refused"),
    ]
    refused = answer["proposed_effect"]["preview"]["documents"][2]
    assert "refused by an admission check" in refused["error"]
    assert refused["namespace"] == "prod"
    assert refused["name"] == "web"
    # Every request is a dry run, and the preview stores nothing.
    assert len(cluster.requests) == 3
    assert all(request["dry_run"] == "All" for request in cluster.requests)
    assert _counts() == before
    # The approval rules do not change.
    assert answer["safety_level"] == "dangerous"
    assert answer["policy_verdict"] == "needs-approval"


def test_the_preview_holds_no_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    value = "made-up-value-7Qx2Lp9Vw"
    manifest = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "db", "namespace": "prod"},
            "stringData": {"password": value, "note": value},
        }
    )

    answer = _preview(manifest)

    assert answer["plan_shown"] is True
    assert _changes(answer) == [(1, "Secret", "create")]
    assert value not in json.dumps(answer["proposed_effect"])


def test_a_cluster_that_does_not_answer_gives_the_time_out_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    monkeypatch.setattr(_composite_preview, "TARGET_READ_TIMEOUT_SECONDS", 0.2)
    cluster = _StandInCluster(hang=True)
    _setup(monkeypatch, cluster)

    answer = _preview(_THREE_OBJECTS)

    assert answer["status"] == "ok"
    assert answer["plan_shown"] is False
    assert answer["plan_note"] == _composite_preview.TARGET_READ_TIMEOUT_NOTE
    assert "proposed_effect" not in answer
    assert answer["preview_hash"]
    assert answer["policy_verdict"] == "needs-approval"


def test_a_namespace_from_the_same_manifest_gets_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster(missing_namespaces=frozenset({"web"}))
    _setup(monkeypatch, cluster)
    manifest = (
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: web}\n"
        "---\n"
        "apiVersion: v1\nkind: Service\nmetadata: {name: api, namespace: web}\n"
        "spec: {ports: [{port: 80}]}\n"
    )

    answer = _preview(manifest)

    assert _changes(answer) == [(1, "Namespace", "create"), (2, "Service", "refused")]
    assert "same manifest" in answer["proposed_effect"]["preview"]["documents"][1]["note"]


def test_a_kind_the_cluster_does_not_know_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()

    async def _discover(*, api_version: str, kind: str) -> Any:
        if kind == "Widget":
            raise LookupError("no kind Widget is known")
        return SimpleNamespace(kind=kind, namespaced=True)

    cluster.resources = SimpleNamespace(get=_discover)
    _setup(monkeypatch, cluster)
    manifest = _THREE_OBJECTS.replace("kind: Ingress", "kind: Widget")

    answer = _preview(manifest)

    assert answer["plan_shown"] is False
    assert "every part" in answer["plan_note"]
    preview = answer["proposed_effect"]["preview"]
    assert preview["not_checked"] == [3]
    assert "no kind Widget is known" in preview["documents"][2]["reason"]


def test_a_read_only_login_gets_no_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)

    answer = _preview(_THREE_OBJECTS, _login("reader", role=TenantRole.READ_ONLY))

    assert cluster.requests == []
    assert answer["plan_shown"] is False
    assert answer["plan_note"] == _composite_preview.TARGET_READ_NOT_ALLOWED_NOTE


def test_with_the_switch_off_the_preview_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)

    answer = _preview(_THREE_OBJECTS)

    assert cluster.requests == []
    assert answer["plan_shown"] is False
    assert answer["plan_note"] == _composite_preview.PLAN_NOT_SHOWN_NOTE
    assert "proposed_effect" not in answer


@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
def test_the_tool_text_names_the_dry_run_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "false")
    if on:
        _switch_on(monkeypatch)
    get_settings.cache_clear()
    definition = mcp_registry._TOOLS["preview_operation"][0]
    old, new = K8S_APPLY_DRY_RUN_TEXT_SWAP

    description = tool_for_settings(definition, build_operator()).to_wire()["description"]

    assert old in definition.description
    if on:
        assert new in description
    else:
        assert description == definition.description


def test_a_failed_cluster_login_says_the_dry_run_did_not_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    monkeypatch.setattr(
        KubernetesConnector,
        "_get_api_client",
        AsyncMock(side_effect=RuntimeError("the cluster login was refused")),
    )

    answer = _preview(_THREE_OBJECTS)

    assert answer["status"] == "ok"
    assert answer["plan_shown"] is False
    assert "did not work" in answer["plan_note"]
    assert "the cluster login was refused" in answer["plan_note"]
    assert "proposed_effect" not in answer
    assert cluster.requests == []
