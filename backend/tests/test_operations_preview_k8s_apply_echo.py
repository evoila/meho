# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``k8s.apply`` approval preview shows the manifest it applies (#3877).

Before this, the preview listed only each object's identity, so the
approver could not see which fields the apply sets. These tests pin:

* a single-document and a multi-document manifest are echoed under
  ``preview.manifest``, one dict per document, in order;
* ``preview.subject`` names the objects (``<kind>/<namespace>/<name>``);
* Secret ``data`` / ``stringData`` values, ``env`` values and the
  last-applied annotation never reach the echo; key and variable names do;
* a manifest larger than the echo budget keeps only the identity of the
  documents that do not fit.

The dry-run itself is stubbed: the echo is built from the parked params.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import yaml

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.ops_write_preview import (
    MANIFEST_ECHO_MAX_BYTES,
    apply_subject,
)
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect

# Fake values built from parts, so no secret scanner reads them as real.
_SECRET_VALUE = "not-a-real" + "-value-1"
_ENV_VALUE = "not-a-real" + "-value-2"
_STRING_DATA_VALUE = "not-a-real" + "-value-3"


@dataclass
class _FakeDescriptor:
    op_id: str


class _FakeTarget:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "cluster-a"


def _operator() -> Operator:
    return Operator(
        sub="op-apply-echo-test",
        name="Apply Echo Test Operator",
        email=None,
        raw_jwt="op.apply.echo.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _summary(doc: dict[str, Any]) -> dict[str, Any]:
    metadata = doc.get("metadata") or {}
    return {
        "api_version": doc["apiVersion"],
        "kind": doc["kind"],
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "resource_version": None,
        "uid": "u-1",
    }


async def _preview(monkeypatch: pytest.MonkeyPatch, manifest: str) -> dict[str, Any]:
    """Run the real ``k8s.apply`` builder with the dry-run stubbed out."""

    async def _fake_apply(
        connector: KubernetesConnector,
        target: Any,
        operator: Operator,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        docs = [doc for doc in yaml.safe_load_all(params["manifest"]) if doc]
        applied = [_summary(doc) for doc in docs]
        return {"dry_run": True, "field_manager": "meho", "applied": applied, "total": len(applied)}

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.k8s_apply",
        _fake_apply,
    )
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=KubernetesConnector(kubeconfig_loader=None),  # type: ignore[arg-type]
        operator=_operator(),
        target=_FakeTarget(),
        params={"manifest": manifest, "dry_run": "none"},
        connector_id="kubernetes-1.x",
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview: dict[str, Any] = effect["preview"]
    return preview


_SINGLE = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: prod
  annotations:
    team: payments
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: registry.example.test/web:1.2
          env:
            - name: LOG_LEVEL
              value: {_ENV_VALUE}
            - name: DB_HOST
              valueFrom:
                configMapKeyRef: {{name: db, key: host}}
"""


@pytest.mark.asyncio
async def test_single_document_manifest_is_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    preview = await _preview(monkeypatch, _SINGLE)

    # The dry-run summary is still there.
    assert preview["applied"][0]["name"] == "web"
    assert preview["subject"] == "Deployment/prod/web"

    [doc] = preview["manifest"]
    assert doc["kind"] == "Deployment"
    assert doc["metadata"]["annotations"] == {"team": "payments"}
    assert doc["spec"]["replicas"] == 3
    container = doc["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.example.test/web:1.2"
    # The env variable names stay, the value does not.
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["LOG_LEVEL"]["value"] == "***REDACTED***"
    assert env["DB_HOST"]["valueFrom"] == {"configMapKeyRef": {"name": "db", "key": "host"}}
    assert _ENV_VALUE not in json.dumps(preview)


_MULTI = f"""
apiVersion: v1
kind: Namespace
metadata:
  name: prod
---
apiVersion: v1
kind: Secret
metadata:
  name: db
  namespace: prod
  annotations:
    kubectl.kubernetes.io/last-applied-configuration: '{{"data": {{"pw": "{_SECRET_VALUE}"}}}}'
type: Opaque
data:
  pw: {_SECRET_VALUE}
stringData:
  url: {_STRING_DATA_VALUE}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: prod
data:
  mode: fast
"""


@pytest.mark.asyncio
async def test_multi_document_manifest_is_echoed_with_secret_values_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = await _preview(monkeypatch, _MULTI)

    assert preview["subject"] == "Namespace/prod, Secret/prod/db, ConfigMap/prod/settings"
    namespace, secret, config_map = preview["manifest"]
    assert namespace["kind"] == "Namespace"
    # A Secret keeps its key names; every value is hidden.
    assert secret["data"] == {"pw": "***REDACTED***"}
    assert secret["stringData"] == {"url": "***REDACTED***"}
    assert secret["type"] == "Opaque"
    annotations = secret["metadata"]["annotations"]
    assert annotations["kubectl.kubernetes.io/last-applied-configuration"] == "***REDACTED***"
    # A ConfigMap is not secret: its data is shown.
    assert config_map["data"] == {"mode": "fast"}

    text = json.dumps(preview)
    assert _SECRET_VALUE not in text
    assert _STRING_DATA_VALUE not in text


@pytest.mark.asyncio
async def test_documents_past_the_echo_budget_keep_only_their_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = "x" * (MANIFEST_ECHO_MAX_BYTES // 2 + 1)
    manifest = "\n---\n".join(
        f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm-{i}\n  namespace: prod\n"
        f"data:\n  blob: {big}\n"
        for i in range(3)
    )
    preview = await _preview(monkeypatch, manifest)

    first, second, third = preview["manifest"]
    assert first["data"] == {"blob": big}
    for doc, name in ((second, "cm-1"), (third, "cm-2")):
        assert doc["metadata"] == {"name": name, "namespace": "prod"}
        assert "data" not in doc
        assert "echo_omitted" in doc
    assert len(json.dumps(preview["manifest"])) < MANIFEST_ECHO_MAX_BYTES + 2048


def test_subject_names_five_objects_and_counts_the_rest() -> None:
    applied = [{"kind": "ConfigMap", "namespace": "prod", "name": f"cm-{i}"} for i in range(7)]
    subject = apply_subject(applied)
    assert subject is not None
    assert subject.startswith("ConfigMap/prod/cm-0, ")
    assert subject.endswith("ConfigMap/prod/cm-4 and 2 more")
    assert apply_subject([]) is None
    assert apply_subject(None) is None
