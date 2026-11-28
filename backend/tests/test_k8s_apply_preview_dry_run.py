# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``preview_operation`` of ``k8s.apply`` shows a dry run on the cluster.

Each test goes through the real ``preview_dispatch`` over a stand-in
dynamic client that records every apply. The tests with the real
Kubernetes client over an HTTP stand-in server are in
``test_k8s_apply_preview_dry_run_wire.py``.

* With ``K8S_APPLY_PREVIEW_DRY_RUN`` on, the preview shows for each document
  whether the apply creates it, changes it or is refused, with
  ``plan_shown: true``. Every apply carries ``dryRun=All``, and the preview
  makes no approval request and one audit row of the class read.
* A Secret value of the manifest is not in the dry-run result of the
  preview. The fixed sentence for the texts of a document with hidden
  values is tested in ``test_k8s_apply_preview_dry_run_hidden.py``.
* ``document`` is the place in the manifest, also across empty documents.
* Each entry is made for its own document; a document the preview cannot
  name safely is not sent and is ``not_checked``.
* A long list of documents is stored behind a result handle, and every
  entry is read back; a short list stays in the answer.
* A cluster that does not answer in time gives the time-out note and the
  rest of the preview. A failed sign-in gives a plain note.
* A login with the Read-only role, and an Operator whom the policy refuses
  the apply, get no dry run.
* With the switch off, the preview sends nothing and answers as before.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from multidict import CIMultiDict, CIMultiDictProxy
from sqlalchemy import func, select

import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes import ops_write_apply_dry_run as dry_run_module
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import _composite_preview, dispatch
from meho_backplane.operations._preview import _k8s_apply_redactor
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
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


class _WarningResponse(_Response):
    """A stand-in answer that carries ``Warning`` headers."""

    def __init__(self, status: int, body: Any, warnings: list[str]) -> None:
        super().__init__(status, body)
        self.headers = CIMultiDictProxy(
            CIMultiDict(("Warning", f'299 - "{text}"') for text in warnings)
        )


class _StandInCluster:
    """A stand-in API server: 201 for a new object, 200 for one in *live*.

    A kind of *refused_kinds* is refused with 422 and *refusal*, as an
    admission check refuses it. A namespace of *missing_namespaces* gives
    404. Each answer carries *warnings*. With *hang* each apply waits until
    it is cancelled.
    """

    def __init__(
        self,
        *,
        live: frozenset[str] = frozenset(),
        refused_kinds: frozenset[str] = frozenset(),
        missing_namespaces: frozenset[str] = frozenset(),
        hang: bool = False,
        refusal: str = "refused by an admission check",
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.live = live
        self.refused_kinds = refused_kinds
        self.missing_namespaces = missing_namespaces
        self.hang = hang
        self.refusal = refusal
        self.warnings = list(warnings)
        self.requests: list[dict[str, Any]] = []
        self.resources = SimpleNamespace(get=self._discover)

    async def _discover(self, *, api_version: str, kind: str) -> Any:
        return SimpleNamespace(kind=kind, namespaced=kind != "Namespace")

    async def server_side_apply(self, resource: Any, **kwargs: Any) -> _Response:
        self.requests.append({"kind": resource.kind, **kwargs})
        if self.hang:
            await asyncio.Event().wait()
        if kwargs.get("namespace") in self.missing_namespaces:
            body: Any = {"kind": "Status", "message": "namespaces web not found"}
            return _WarningResponse(404, body, self.warnings)
        if resource.kind in self.refused_kinds:
            body = {"kind": "Status", "message": self.refusal}
            return _WarningResponse(422, body, self.warnings)
        status = 200 if kwargs["name"] in self.live else 201
        return _WarningResponse(status, kwargs["body"], self.warnings)


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


def _preview_rows() -> list[AuditLog]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.method == "PREVIEW").order_by(AuditLog.occurred_at)
            )
            return list(result.scalars().all())

    return asyncio.run(_do())


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
    # Every request is a dry run. The preview makes no approval request,
    # and one audit row of the class read.
    assert len(cluster.requests) == 3
    assert all(request["dry_run"] == "All" for request in cluster.requests)
    assert _counts() == (before[0], before[1] + 1)
    row = _preview_rows()[-1]
    assert (row.method, row.path, row.payload["op_class"]) == ("PREVIEW", "k8s.apply", "read")
    assert row.payload["documents"] == 3
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
    assert "RuntimeError" not in answer["plan_note"]
    assert "proposed_effect" not in answer
    assert cluster.requests == []


def test_document_counts_the_place_across_empty_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster(refused_kinds=frozenset({"Deployment"}))
    _setup(monkeypatch, cluster)
    manifest = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: prod}\n"
        "---\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: prod}\n"
        "spec: {template: {spec: {containers: [{name: app, image: web:2}]}}}\n"
    )

    answer = _preview(manifest)

    assert _changes(answer) == [(1, "ConfigMap", "create"), (3, "Deployment", "refused")]


def test_a_document_the_preview_cannot_name_is_not_counted_as_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster(live=frozenset({"web"}))
    _setup(monkeypatch, cluster)

    def _drops_deployments(ctx: Any) -> Any:
        redact = _k8s_apply_redactor(ctx)

        def _redact(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [doc for doc in redact(docs) if doc.get("kind") != "Deployment"]

        return _redact

    # The shown list is one entry shorter than the manifest.
    monkeypatch.setattr(dry_run_module, "_k8s_apply_redactor", _drops_deployments)

    answer = _preview(_THREE_OBJECTS)

    assert answer["plan_shown"] is False
    preview = answer["proposed_effect"]["preview"]
    documents = preview["documents"]
    assert [doc["document"] for doc in documents] == [1, 2, 3]
    assert documents[1]["change"] == "not_checked"
    assert "kind" not in documents[1]
    assert preview["not_checked"] == [2]
    # Each checked entry names the document it was checked for.
    assert [(r["kind"], r["name"]) for r in cluster.requests] == [
        ("ConfigMap", "settings"),
        ("Ingress", "web"),
    ]
    assert (documents[0]["kind"], documents[0]["name"]) == ("ConfigMap", "settings")
    assert (documents[2]["kind"], documents[2]["name"]) == ("Ingress", "web")


class _Valkey:
    """A minimal async Valkey stand-in, so the real result store keeps rows."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


def _config_maps(count: int) -> str:
    return "---\n".join(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: cm-{n}, namespace: prod}}\n"
        for n in range(count)
    )


def test_a_long_list_of_documents_comes_back_as_a_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    monkeypatch.setattr(dry_run_module, "_DOCUMENTS_REDUCER", JsonFluxReducer(store=store))
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    operator = _login("writer")

    answer = _preview(_config_maps(400), operator)

    assert answer["plan_shown"] is True
    documents = answer["proposed_effect"]["preview"]["documents"]
    assert isinstance(documents, dict)
    handle_id = uuid.UUID(documents["handle"]["handle_id"])

    async def _read_all() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            window = await result_query_core.read_result_window(
                operator, handle_id, offset=len(rows), limit=100
            )
            rows.extend(window["rows"])
            if not window["rows"]:
                return rows

    rows = asyncio.run(_read_all())
    assert len(rows) == 400
    assert [row["document"] for row in rows] == list(range(1, 401))
    assert {row["change"] for row in rows} == {"create"}
    assert {row["kind"] for row in rows} == {"ConfigMap"}
    assert {row["namespace"] for row in rows} == {"prod"}
    assert [row["name"] for row in rows] == [f"cm-{n}" for n in range(400)]


def test_a_short_list_of_documents_stays_in_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)

    answer = _preview(_config_maps(3))

    documents = answer["proposed_effect"]["preview"]["documents"]
    assert isinstance(documents, list)
    assert [doc["name"] for doc in documents] == ["cm-0", "cm-1", "cm-2"]


def test_an_operator_whom_the_policy_refuses_gets_no_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true")
    monkeypatch.setenv(
        "MCP_CONNECTOR_PROFILES",
        json.dumps({"k8s-read": {"tools": "operate", "connectors": ["k8s"]}}),
    )
    get_settings.cache_clear()
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    # A read-only profile: the policy refuses every call that changes something.
    caller = Operator(
        sub="profile-writer",
        name="profile-writer",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        mcp_tool_profile="k8s-read",
    )

    answer = _preview(_THREE_OBJECTS, caller)

    assert cluster.requests == []
    assert answer["plan_shown"] is False
    assert answer["plan_note"] == _composite_preview.TARGET_READ_NOT_ALLOWED_NOTE
    assert answer["policy_verdict"] == "deny"
    assert _preview_rows() == []
    # The real call of this caller is refused too.
    result = asyncio.run(
        dispatch(
            operator=caller,
            connector_id=_CONNECTOR_ID,
            op_id="k8s.apply",
            target=_Target(),
            params={"manifest": _THREE_OBJECTS},
        )
    )
    assert result.status == "denied", result
    assert cluster.requests == []
