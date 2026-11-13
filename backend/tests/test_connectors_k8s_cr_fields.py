# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.cr.list`` / ``k8s.cr.info``: cut reports and the ``fields`` read.

A CronJob's ``schedule`` and a node's ``nodeInfo`` sort after a long
block, so the 2,048-byte excerpt lost them. These tests pin the two
fixes: the excerpt names the keys it lost, and ``fields`` returns chosen
paths uncut (after redaction).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import jsonschema
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector, KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_customresource import (
    CR_SPEC_EXCERPT_MAX_BYTES,
    K8S_CR_INFO_PARAMETER_SCHEMA,
    K8S_CR_INFO_RESPONSE_SCHEMA,
    K8S_CR_LIST_RESPONSE_SCHEMA,
    custom_resource_row,
)
from meho_backplane.connectors.kubernetes.ops_customresource_fields import (
    omitted_top_level_keys,
    select_fields,
)
from meho_backplane.connectors.kubernetes.redaction import REDACTED_PREFIX
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _Target:
    name: str = "cluster-a"
    host: str = "cluster-a.test.invalid"
    port: int = 6443
    secret_ref: str = "k8s/cluster-a"
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


def _connector() -> KubernetesConnector:
    async def _loader(_t: KubernetesTargetLike, _o: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _operator() -> Operator:
    return Operator(
        sub="op-cr-fields",
        name="CR Fields Operator",
        email=None,
        raw_jwt="op.cr.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _cronjob() -> dict[str, Any]:
    script = "echo step; " * 300
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "nightly", "namespace": "ops"},
        "spec": {
            "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"args": [script]}]}}}},
            "schedule": "*/5 * * * *",
            "suspend": False,
            "timeZone": "Europe/Berlin",
        },
    }


def _node() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": "node-1"},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}, {"type": "DiskPressure"}],
            "images": [{"names": [f"registry.test.invalid/app-{i}:1"]} for i in range(200)],
            "nodeInfo": {"kubeletVersion": "v1.31.0"},
            "volumesInUse": ["vol-1"],
        },
    }


def test_cut_spec_names_the_keys_it_lost() -> None:
    row = custom_resource_row(_cronjob())
    assert row["spec_truncated"] is True
    assert row["spec_omitted_keys"] == ["jobTemplate", "schedule", "suspend", "timeZone"]
    assert "status_omitted_keys" not in row


def test_cut_status_names_the_keys_it_lost() -> None:
    row = custom_resource_row(_node())
    assert row["status_truncated"] is True
    assert row["status_omitted_keys"] == ["images", "nodeInfo", "volumesInUse"]


def test_uncut_block_has_no_omitted_keys_and_no_fields() -> None:
    row = custom_resource_row({"kind": "Thing", "metadata": {}, "spec": {"a": 1}})
    assert "spec_omitted_keys" not in row
    assert "fields" not in row
    assert "fields_missing" not in row


def test_omitted_keys_kept_keys_fit_in_the_cut_text() -> None:
    value = {"a": "x" * 100, "b": "y" * 100, "c": "z" * 100}
    cap = 150
    text = json.dumps(value, sort_keys=True)[:cap]
    omitted = omitted_top_level_keys(value, cap)
    assert omitted == ["b", "c"]
    assert '"a": "' + "x" * 100 + '"' in text


def test_omitted_keys_edge_inputs() -> None:
    assert omitted_top_level_keys(None, 10) == []
    assert omitted_top_level_keys(["x" * 100], 10) == []
    assert omitted_top_level_keys({}, 10) == []
    assert omitted_top_level_keys({"k": "é" * 100}, 20) == ["k"]


def test_fields_read_cronjob_values_past_the_cut() -> None:
    row = custom_resource_row(_cronjob(), ["spec.schedule", "spec.timeZone", "spec.suspend"])
    assert row["fields"] == {
        "spec.schedule": "*/5 * * * *",
        "spec.timeZone": "Europe/Berlin",
        "spec.suspend": False,
    }
    assert row["fields_missing"] == []


def test_fields_read_node_blocks_uncut() -> None:
    row = custom_resource_row(_node(), ["status.conditions", "status.nodeInfo"])
    assert len(row["fields"]["status.conditions"]) == 2
    assert row["fields"]["status.nodeInfo"] == {"kubeletVersion": "v1.31.0"}


def test_fields_value_is_not_cut_at_the_excerpt_limit() -> None:
    row = custom_resource_row(_node(), ["status.images"])
    assert len(json.dumps(row["fields"]["status.images"])) > CR_SPEC_EXCERPT_MAX_BYTES * 2


def test_fields_missing_is_listed_not_null() -> None:
    row = custom_resource_row(_cronjob(), ["spec.nothing", "spec.schedule.deeper", "status.phase"])
    assert row["fields"] == {}
    assert row["fields_missing"] == ["spec.nothing", "spec.schedule.deeper", "status.phase"]


def test_fields_can_index_a_list_and_a_bad_index_is_missing() -> None:
    row = custom_resource_row(_node(), ["status.conditions.1.type", "status.conditions.9"])
    assert row["fields"] == {"status.conditions.1.type": "DiskPressure"}
    assert row["fields_missing"] == ["status.conditions.9"]


def test_fields_outside_spec_and_status_are_never_read() -> None:
    found, missing = select_fields(_cronjob(), ["metadata.name", "kind", "spec"])
    assert list(found) == ["spec"]
    assert missing == ["metadata.name", "kind"]


def test_fields_on_a_helm_chart_return_placeholders_not_values() -> None:
    chart = {
        "apiVersion": "helm.cattle.io/v1",
        "kind": "HelmChart",
        "metadata": {"name": "c"},
        "spec": {
            "chart": "app",
            "valuesContent": "password: made-up-chart-pass",
            "set": {"db.password": "made-up-set-pass"},
        },
    }
    row = custom_resource_row(chart, ["spec.valuesContent", "spec.set", "spec.chart"])
    text = json.dumps(row)
    assert "made-up-chart-pass" not in text
    assert "made-up-set-pass" not in text
    assert row["fields"]["spec.valuesContent"].startswith(REDACTED_PREFIX)
    assert row["fields"]["spec.chart"] == "app"


def test_fields_on_a_secret_cannot_read_data() -> None:
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "s"},
        "data": {"password": "fake-value"},
    }
    row = custom_resource_row(secret, ["data.password", "spec"])
    assert "fake-value" not in json.dumps(row)
    assert row["fields"] == {}


def test_row_with_new_keys_matches_both_response_schemas() -> None:
    row = custom_resource_row(_cronjob(), ["spec.schedule", "spec.nope"])
    jsonschema.validate(row, K8S_CR_INFO_RESPONSE_SCHEMA)
    jsonschema.validate({"rows": [row], "total": 1}, K8S_CR_LIST_RESPONSE_SCHEMA)


def test_fields_param_schema_rejects_bad_paths() -> None:
    def ok(fields: Any) -> bool:
        params = {"group": "", "version": "v1", "plural": "nodes", "name": "n", "fields": fields}
        return jsonschema.Draft202012Validator(K8S_CR_INFO_PARAMETER_SCHEMA).is_valid(params)

    assert ok(["spec.schedule", "status.conditions.0"])
    assert not ok(["metadata.name"])
    assert not ok(["spec..x"])
    assert not ok([])
    assert not ok(["spec"] * 21)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["k8s_cr_info", "k8s_cr_list"])
async def test_handlers_pass_fields_through(op: str) -> None:
    connector = _connector()
    cj = _cronjob()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.get_namespaced_custom_object = AsyncMock(return_value=cj)
        co_cls.return_value.list_namespaced_custom_object = AsyncMock(return_value={"items": [cj]})
        params = {
            "group": "batch",
            "version": "v1",
            "plural": "cronjobs",
            "namespace": "ops",
            "fields": ["spec.schedule"],
        }
        if op == "k8s_cr_info":
            params["name"] = "nightly"
        result = await getattr(connector, op)(_operator(), _Target(), params)

    row = result if op == "k8s_cr_info" else result["rows"][0]
    assert row["fields"] == {"spec.schedule": "*/5 * * * *"}
    assert row["spec_truncated"] is True
