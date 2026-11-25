# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of ``k8s.apply`` summarises each object.

For each object of the manifest the card says whether it is new or exists
already, who else owns fields of it, and which risky fields it holds after
the apply. The summary is for a person only and changes no approval rule.

The dry run and the read of the live object are stubbed with raw answers
in the shape of the API server.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from multidict import CIMultiDict, CIMultiDictProxy

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)

# Made-up values, built from parts so no secret scanner reads them as real.
_SECRET_VALUE = "not-a-real" + "-value-s1"
_ENV_VALUE = "not-a-real" + "-value-s2"
_LIVE_VALUE = "not-a-real" + "-value-s3"


@dataclass
class _Descriptor:
    op_id: str


class _Target:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "cluster-a"


class _Response:
    reason = "OK"

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._text = body if isinstance(body, str) else json.dumps(body)
        self.headers = CIMultiDictProxy(CIMultiDict())

    async def text(self) -> str:
        return self._text


def _operator() -> Operator:
    return Operator(
        sub="op-summary-test",
        name="Summary Test Operator",
        email=None,
        raw_jwt="op.summary.test.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _resource(*, namespaced: bool = True) -> Any:
    resource = MagicMock()
    resource.namespaced = namespaced
    return resource


def _body(doc: dict[str, Any]) -> dict[str, Any]:
    """The object a dry run returns: the manifest plus server-set fields."""
    return {**doc, "metadata": {**doc["metadata"], "managedFields": [{"manager": "meho"}]}}


async def _card(
    monkeypatch: pytest.MonkeyPatch,
    manifest: str,
    *,
    dry_run: Callable[[dict[str, Any]], _Response] | None = None,
    live: Callable[[str], _Response] | None = None,
    namespaced: bool = True,
    agent: bool = False,
) -> tuple[dict[str, Any], MagicMock]:
    """Run the real ``k8s.apply`` card builder against a stubbed cluster."""
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=_resource(namespaced=namespaced))

    async def _apply(_resource: Any, **kwargs: Any) -> _Response:
        doc = kwargs["body"]
        return dry_run(doc) if dry_run else _Response(200, _body(doc))

    async def _get(_resource: Any, **kwargs: Any) -> _Response:
        return live(kwargs["name"]) if live else _Response(404, {"kind": "Status", "code": 404})

    dyn.server_side_apply = AsyncMock(side_effect=_apply)
    dyn.get = AsyncMock(side_effect=_get)

    async def _dynamic_client(_api_client: Any) -> Any:
        return dyn

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient", _dynamic_client
    )
    connector = KubernetesConnector(kubeconfig_loader=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "_get_api_client", AsyncMock(return_value=MagicMock()))
    ctx = PreviewContext(
        descriptor=_Descriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_Target(),
        params={"manifest": manifest, "dry_run": "none"},
        connector_id="kubernetes-1.x",
        agent_preview=agent,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect, dyn


_NEW_PAIR = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: prod}
spec:
  template:
    spec:
      containers:
        - {name: app, image: registry.example.test/web:2}
---
apiVersion: v1
kind: Service
metadata: {name: web, namespace: prod}
spec:
  type: ClusterIP
  ports: [{port: 80}]
"""


@pytest.mark.asyncio
async def test_a_new_deployment_and_service_are_two_new_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, _ = await _card(monkeypatch, _NEW_PAIR)

    summary = effect["preview"]["object_summary"]
    assert [(e["api_group"], e["kind"], e["name"], e["state"]) for e in summary] == [
        ("apps", "Deployment", "web", "new"),
        ("core", "Service", "web", "new"),
    ]
    assert [e["risky_fields"] for e in summary] == [[], []]
    assert all(e["other_writers"] == [] for e in summary)
    assert all(e["built_in"] is True for e in summary)


_IMAGE_ONLY = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: prod}
spec:
  template:
    spec:
      containers:
        - {name: app, image: registry.example.test/web:3}
"""


def _after_state(doc: dict[str, Any]) -> _Response:
    """The dry-run answer for an existing Deployment: it keeps what was there."""
    after = _body(doc)
    pod = after["spec"]["template"]["spec"]
    pod["serviceAccountName"] = "web-runner"
    pod["hostNetwork"] = True
    pod["volumes"] = [
        {"name": "creds", "secret": {"secretName": "web-creds"}},
        {"name": "host", "hostPath": {"path": "/var/run"}},
    ]
    pod["containers"][0]["env"] = [
        {"name": "TOKEN", "value": _ENV_VALUE},
        {"name": "DB", "valueFrom": {"secretKeyRef": {"name": "db-auth", "key": "pw"}}},
    ]
    pod["containers"][0]["securityContext"] = {"privileged": True}
    return _Response(200, after)


def _live_deployment(name: str) -> _Response:
    return _Response(
        200,
        {
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "managedFields": [
                    {"manager": "meho", "operation": "Apply"},
                    {"manager": "helm", "operation": "Update"},
                    {"manager": "argocd-controller", "operation": "Apply"},
                    {"manager": "helm", "operation": "Update"},
                ],
            },
            "spec": {"template": {"spec": {"containers": [{"env": [{"value": _LIVE_VALUE}]}]}}},
        },
    )


@pytest.mark.asyncio
async def test_an_image_only_apply_names_what_the_workload_already_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, dyn = await _card(monkeypatch, _IMAGE_ONLY, dry_run=_after_state, live=_live_deployment)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["state"] == "exists"
    risks = entry["risky_fields"]
    assert "uses Secret web-creds" in risks
    assert "uses Secret db-auth" in risks
    assert "runs as service account web-runner" in risks
    assert "hostNetwork is on" in risks
    assert "hostPath volume /var/run" in risks
    assert "a container is privileged" in risks
    # The sent manifest holds none of these.
    assert "web-creds" not in _IMAGE_ONLY
    # One read of the live object, and no write other than the dry run.
    assert dyn.get.await_count == 1
    assert dyn.server_side_apply.await_args.kwargs["dry_run"] == "All"


@pytest.mark.asyncio
async def test_the_card_names_the_other_writers_without_meho(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, _ = await _card(monkeypatch, _IMAGE_ONLY, live=_live_deployment)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["other_writers"] == [
        {"manager": "helm", "operation": "Update"},
        {"manager": "argocd-controller", "operation": "Apply"},
    ]


_CUSTOM = """
apiVersion: example.test/v1
kind: Deployment
metadata: {name: look-alike, namespace: prod}
spec: {}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: admins}
roleRef: {kind: ClusterRole, name: cluster-admin}
subjects: [{kind: User, name: someone}]
"""


@pytest.mark.asyncio
async def test_a_kind_is_read_together_with_its_group_and_a_cluster_kind_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, _ = await _card(monkeypatch, _CUSTOM)

    custom, binding = effect["preview"]["object_summary"]
    assert (custom["api_group"], custom["kind"], custom["built_in"]) == (
        "example.test",
        "Deployment",
        False,
    )
    assert binding["built_in"] is True
    assert binding["risky_fields"] == ["a ClusterRoleBinding that grants ClusterRole cluster-admin"]


@pytest.mark.asyncio
async def test_a_cluster_wide_kind_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = "apiVersion: v1\nkind: Namespace\nmetadata: {name: team-a}\n"
    effect, _ = await _card(monkeypatch, manifest, namespaced=False)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["cluster_wide"] is True
    assert entry["namespace"] is None


@pytest.mark.asyncio
async def test_a_service_that_is_reachable_from_outside_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = (
        "apiVersion: v1\nkind: Service\nmetadata: {name: web, namespace: prod}\n"
        "spec: {type: LoadBalancer, externalIPs: ['192.0.2.1']}\n"
    )
    effect, _ = await _card(monkeypatch, manifest)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["risky_fields"] == [
        "a Service of type LoadBalancer (reachable from outside)",
        "a Service with external IPs (reachable from outside)",
    ]


_SECRET_DOC = f"""
apiVersion: v1
kind: Secret
metadata: {{name: creds, namespace: prod}}
stringData: {{password: {_SECRET_VALUE}}}
"""


@pytest.mark.asyncio
async def test_no_secret_value_reaches_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    def _dry_run(doc: dict[str, Any]) -> _Response:
        return _Response(200, {**_body(doc), "data": {"password": _SECRET_VALUE}})

    def _live(name: str) -> _Response:
        return _Response(
            200,
            {
                "metadata": {"name": name, "managedFields": [{"manager": "kubectl"}]},
                "data": {"password": _LIVE_VALUE},
            },
        )

    effect, _ = await _card(monkeypatch, _SECRET_DOC, dry_run=_dry_run, live=_live)
    effect2, _ = await _card(monkeypatch, _IMAGE_ONLY, dry_run=_after_state, live=_live_deployment)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["risky_fields"] == ["a Secret object (its values are not shown)"]
    for card in (effect, effect2):
        text = json.dumps(card["preview"]["object_summary"])
        for value in (_SECRET_VALUE, _ENV_VALUE, _LIVE_VALUE):
            assert value not in text
    # The part of the stored request that holds the summary is what the
    # audit row and the approval row keep.
    assert _ENV_VALUE not in json.dumps(effect2)
    assert _SECRET_VALUE not in json.dumps(effect)


@pytest.mark.asyncio
async def test_an_odd_name_is_not_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _dry_run(doc: dict[str, Any]) -> _Response:
        after = _body(doc)
        after["spec"]["template"]["spec"]["serviceAccountName"] = _SECRET_VALUE.upper()
        return _Response(200, after)

    effect, _ = await _card(monkeypatch, _IMAGE_ONLY, dry_run=_dry_run)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["risky_fields"] == ["runs as service account (name not shown)"]
    assert _SECRET_VALUE.upper() not in json.dumps(effect)


@pytest.mark.asyncio
async def test_a_failed_read_of_the_live_object_is_not_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, _ = await _card(
        monkeypatch, _IMAGE_ONLY, live=lambda _name: _Response(500, "internal error")
    )

    (entry,) = effect["preview"]["object_summary"]
    assert entry["state"] == "unknown"
    assert entry["other_writers"] is None
    assert "could not be read" in entry["state_note"]
    # The risky fields still come from the dry run.
    assert entry["risky_fields"] == []


@pytest.mark.asyncio
async def test_a_live_read_that_raises_is_not_new(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str) -> _Response:
        raise ConnectionError("no route")

    effect, _ = await _card(monkeypatch, _IMAGE_ONLY, live=_boom)

    (entry,) = effect["preview"]["object_summary"]
    assert entry["state"] == "unknown"
    assert "ConnectionError" in entry["state_note"]


@pytest.mark.asyncio
async def test_a_dry_run_with_no_object_says_the_summary_could_not_be_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, _ = await _card(monkeypatch, _IMAGE_ONLY, dry_run=lambda _doc: _Response(200, ""))

    (entry,) = effect["preview"]["object_summary"]
    assert entry["risky_fields"] is None
    assert entry["summary_unavailable"] == "the dry run returned no object"


@pytest.mark.asyncio
async def test_a_failed_dry_run_says_the_summary_could_not_be_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, _ = await _card(
        monkeypatch,
        _IMAGE_ONLY,
        dry_run=lambda _doc: _Response(403, {"kind": "Status", "message": "denied"}),
    )

    assert effect["preview_unavailable"] is True
    (entry,) = effect["object_summary"]
    assert "could not be made" in entry["summary_unavailable"]


@pytest.mark.asyncio
async def test_an_agent_preview_has_no_summary_and_makes_no_extra_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect, dyn = await _card(monkeypatch, _NEW_PAIR, agent=True)

    assert "object_summary" not in effect["preview"]
    dyn.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_stored_request_an_agent_reads_has_no_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok, _ = await _card(monkeypatch, _NEW_PAIR)
    failed, _ = await _card(
        monkeypatch, _NEW_PAIR, dry_run=lambda _doc: _Response(403, {"message": "denied"})
    )
    assert "object_summary" in ok["preview"]
    assert "object_summary" in failed

    for effect in (ok, failed):
        shown = proposed_effect_for_agent(
            "k8s.apply",
            {"op_id": "k8s.apply", **effect},
            params={"manifest": _NEW_PAIR},
            connector_id="kubernetes-1.x",
            tenant=None,
        )
        assert shown is not None
        assert "object_summary" not in json.dumps(shown)


@pytest.mark.asyncio
async def test_a_second_document_that_fails_leaves_no_partial_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _dry_run(doc: dict[str, Any]) -> _Response:
        if doc["kind"] == "Service":
            return _Response(403, {"message": "denied"})
        return _Response(200, _body(doc))

    effect, _ = await _card(monkeypatch, _NEW_PAIR, dry_run=_dry_run)

    assert effect["preview_unavailable"] is True
    assert [e.get("kind") for e in effect["object_summary"]] == [None]


def test_the_manifest_still_parses_as_the_tests_assume() -> None:
    assert len([doc for doc in yaml.safe_load_all(_NEW_PAIR) if doc]) == 2
