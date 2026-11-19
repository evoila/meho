# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.cr.list`` / ``k8s.cr.info`` hide credential fields of every kind.

* A ``HelmChartConfig`` (k3s / RKE2) gets its Helm values masked like a
  ``HelmChart``, also as a kind-less item of a ``HelmChartConfigList``.
* Any other kind gets the values under secret-shaped key names in ``spec``
  and ``status`` hidden before ``spec_excerpt``, ``status_excerpt`` and
  ``fields`` are built, also a secret-named field inside a text value, and
  also when the excerpt is cut.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector, KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_customresource import (
    CR_SPEC_EXCERPT_MAX_BYTES,
    custom_resource_row,
)
from meho_backplane.connectors.kubernetes.redaction import (
    LAST_APPLIED_ANNOTATION,
    REDACTED_PREFIX,
)
from meho_backplane.settings import get_settings

# Made-up values, built from parts so secret scanners do not flag the source.
_FAKE = "FAKE" + "-made-up-cr-5520"
_FAKE_STATUS = "FAKE" + "-made-up-status-6631"
_FAKE_TEXT = "FAKE" + "-made-up-text-7742"
_MARKER = "[REDACTED:param_name]"
_FAKES = (_FAKE, _FAKE_STATUS, _FAKE_TEXT)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str = "cluster-a"
    host: str = "cluster-a.test.invalid"
    port: int | None = 6443
    secret_ref: str = "k8s/cluster-a"
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget()


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "clusters": [{"name": "c", "cluster": {"server": "https://cluster-a.test.invalid"}}],
            "contexts": [{"name": "default", "context": {"cluster": "c", "user": "u"}}],
            "users": [{"name": "u", "user": {"token": "kubeconfig-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    return Operator(
        sub="op-k8s-cr-secret-fields",
        name="Test Operator",
        email=None,
        raw_jwt="op.k8s.cr.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000b0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


def _helm_chart_config(*, with_kind: bool = True) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "apiVersion": "helm.cattle.io/v1",
        "metadata": {
            "name": "rke2-ingress-nginx",
            "namespace": "kube-system",
            "annotations": {"owner": "platform"},
        },
        "spec": {"valuesContent": f"controller:\n  adminPassword: {_FAKE}\n"},
    }
    if with_kind:
        obj["kind"] = "HelmChartConfig"
    obj["metadata"]["annotations"][LAST_APPLIED_ANNOTATION] = json.dumps(
        {**obj, "kind": "HelmChartConfig"}
    )
    return obj


def _database() -> dict[str, Any]:
    """A custom resource of a kind MEHO has no rule for, with credentials in it."""
    spec: dict[str, Any] = {
        "engine": "postgres",
        "auth": {"user": "app", "password": _FAKE, "pin": 12345678},
        "bootstrap": f'{{"admin": {{"user": "root", "secret": "{_FAKE_TEXT}"}}}}',
        "enabled": True,
    }
    return {
        "apiVersion": "db.example.test/v1",
        "kind": "Database",
        "metadata": {
            "name": "orders",
            "namespace": "team-a",
            "annotations": {"note": f"password: {_FAKE_TEXT}", "owner": "team-a"},
        },
        "spec": spec,
        "status": {"phase": "Ready", "connection": {"token": _FAKE_STATUS}},
    }


def _shown_fakes(row: Any) -> list[str]:
    blob = json.dumps(row)
    return [value for value in _FAKES if value in blob]


def _api_client_patches() -> Any:
    return (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi"),
    )


# ---------------------------------------------------------------------------
# HelmChartConfig
# ---------------------------------------------------------------------------


async def test_cr_info_of_helm_chart_config_returns_no_values_text() -> None:
    client_patch, co_patch = _api_client_patches()
    with client_patch, co_patch as co_cls:
        co_cls.return_value.get_namespaced_custom_object = AsyncMock(
            return_value=_helm_chart_config()
        )
        row = await _make_connector().k8s_cr_info(
            _make_operator(),
            _TARGET,
            {
                "group": "helm.cattle.io",
                "version": "v1",
                "plural": "helmchartconfigs",
                "name": "rke2-ingress-nginx",
                "namespace": "kube-system",
                "fields": ["spec.valuesContent"],
            },
        )

    assert _shown_fakes(row) == []
    assert row["kind"] == "HelmChartConfig"
    assert json.loads(row["spec_excerpt"])["valuesContent"].startswith(REDACTED_PREFIX)
    assert row["fields"]["spec.valuesContent"].startswith(REDACTED_PREFIX)
    assert row["annotations"]["owner"].startswith(REDACTED_PREFIX)


async def test_cr_list_of_helm_chart_configs_masks_kind_less_items() -> None:
    envelope = {
        "apiVersion": "helm.cattle.io/v1",
        "kind": "HelmChartConfigList",
        "metadata": {"continue": ""},
        "items": [_helm_chart_config(with_kind=False), _helm_chart_config()],
    }
    client_patch, co_patch = _api_client_patches()
    with client_patch, co_patch as co_cls:
        co_cls.return_value.list_cluster_custom_object = AsyncMock(return_value=envelope)
        result = await _make_connector().k8s_cr_list(
            _make_operator(),
            _TARGET,
            {"group": "helm.cattle.io", "version": "v1", "plural": "helmchartconfigs"},
        )

    assert result["total"] == 2
    assert _shown_fakes(result) == []
    for row in result["rows"]:
        assert row["kind"] == "HelmChartConfig"
        assert json.loads(row["spec_excerpt"])["valuesContent"].startswith(REDACTED_PREFIX)


# ---------------------------------------------------------------------------
# Any other kind
# ---------------------------------------------------------------------------


async def test_cr_list_of_another_kind_hides_credential_fields_in_excerpt_and_fields() -> None:
    client_patch, co_patch = _api_client_patches()
    with client_patch, co_patch as co_cls:
        co_cls.return_value.list_namespaced_custom_object = AsyncMock(
            return_value={"kind": "DatabaseList", "items": [_database()]}
        )
        result = await _make_connector().k8s_cr_list(
            _make_operator(),
            _TARGET,
            {
                "group": "db.example.test",
                "version": "v1",
                "plural": "databases",
                "namespace": "team-a",
                "fields": ["spec.auth", "spec.bootstrap", "status.connection"],
            },
        )

    assert _shown_fakes(result) == []
    (row,) = result["rows"]
    spec = json.loads(row["spec_excerpt"])
    assert spec["auth"] == {"user": "app", "password": _MARKER, "pin": 12345678}
    assert spec["engine"] == "postgres"
    assert spec["enabled"] is True
    assert json.loads(spec["bootstrap"]) == {"admin": {"user": "root", "secret": _MARKER}}
    assert json.loads(row["status_excerpt"]) == {
        "phase": "Ready",
        "connection": {"token": _MARKER},
    }
    assert row["fields"]["spec.auth"]["password"] == _MARKER
    assert row["fields"]["status.connection"] == {"token": _MARKER}
    assert row["annotations"] == {"note": f'password: "{_MARKER}"', "owner": "team-a"}


def test_cut_excerpt_hides_the_secret_before_the_cut() -> None:
    """The cut falls inside the secret value: no part of it is shown."""
    obj = _database()
    obj["spec"] = {"aaa_filler": "", "bootstrap": obj["spec"]["bootstrap"]}
    # Grow the filler until the excerpt cut lands six bytes into the value.
    at = json.dumps(obj["spec"], sort_keys=True).index(_FAKE_TEXT)
    obj["spec"]["aaa_filler"] = "x" * (CR_SPEC_EXCERPT_MAX_BYTES - at - 6)

    row = custom_resource_row(obj)

    assert row["spec_truncated"] is True
    assert row["spec_excerpt"].startswith('{"aaa_filler": "xxx')
    assert _FAKE_TEXT[:6] not in row["spec_excerpt"]
    assert _shown_fakes(row) == []


def test_another_kind_without_secret_fields_is_unchanged() -> None:
    obj = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "nightly"},
        "spec": {"schedule": "0 1 * * *", "suspend": False},
        "status": {"lastScheduleTime": "2026-11-18T01:00:00Z"},
    }
    row = custom_resource_row(obj, ["spec.schedule"])

    assert json.loads(row["spec_excerpt"]) == obj["spec"]
    assert json.loads(row["status_excerpt"]) == obj["status"]
    assert row["fields"] == {"spec.schedule": "0 1 * * *"}


def test_input_object_is_not_changed() -> None:
    obj = _database()
    custom_resource_row(obj, ["spec.auth"])
    assert obj["spec"]["auth"]["password"] == _FAKE
    assert obj["status"]["connection"]["token"] == _FAKE_STATUS
