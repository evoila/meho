# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the K8s read paths added by #3840.

* ``k8s.cr.list`` / ``k8s.cr.info`` with ``group=""`` read core-group
  objects under ``/api/{version}`` (:mod:`ops_core_objects`).
* ``k8s.auth.can_i`` / ``k8s.auth.rules`` -- self access reviews
  (:mod:`ops_access`).
* ``k8s.admission_webhook.list`` -- validating + mutating webhooks
  (:mod:`ops_admission`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from kubernetes_asyncio import client

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector, KubernetesTargetLike
from meho_backplane.connectors.kubernetes.connector import _WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_access import (
    K8S_AUTH_CAN_I_PARAMETER_SCHEMA,
    access_review_body,
    rules_review_result,
)
from meho_backplane.connectors.kubernetes.ops_admission import admission_webhook_rows
from meho_backplane.connectors.kubernetes.ops_core_objects import list_items
from meho_backplane.connectors.kubernetes.ops_customresource import (
    K8S_CR_INFO_PARAMETER_SCHEMA,
    K8S_CR_LIST_PARAMETER_SCHEMA,
)
from meho_backplane.settings import get_settings

_NEW_OP_IDS = ("k8s.auth.can_i", "k8s.auth.rules", "k8s.admission_webhook.list")


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
    name: str = "k3s-unit"
    host: str = "k3s-unit.test.invalid"
    port: int = 6443
    secret_ref: str = "k8s/k3s-unit"
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget()


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    return Operator(
        sub="op-access-test",
        name="Access Test Operator",
        email=None,
        raw_jwt="op.access.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-000000003840"),
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_client(api_client: MagicMock) -> Any:
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=api_client,
    )


# ---------------------------------------------------------------------------
# Core group through k8s.cr.list / k8s.cr.info
# ---------------------------------------------------------------------------


def test_cr_param_schemas_accept_the_empty_core_group() -> None:
    params = {"group": "", "version": "v1", "plural": "namespaces"}
    Draft202012Validator(K8S_CR_LIST_PARAMETER_SCHEMA).validate(params)
    Draft202012Validator(K8S_CR_INFO_PARAMETER_SCHEMA).validate({**params, "name": "default"})


@pytest.mark.asyncio
async def test_cr_list_core_group_cluster_wide_uses_api_root() -> None:
    list_resp = {
        "apiVersion": "v1",
        "kind": "NamespaceList",
        "items": [{"metadata": {"name": "default"}, "spec": {"finalizers": ["kubernetes"]}}],
    }
    api_client = MagicMock(close=AsyncMock(), call_api=AsyncMock(return_value=list_resp))
    with (
        _patch_client(api_client),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        result = await _make_connector().k8s_cr_list(
            _make_operator(), _TARGET, {"group": "", "version": "v1", "plural": "namespaces"}
        )
        co_cls.return_value.list_cluster_custom_object.assert_not_called()

    args, kwargs = api_client.call_api.call_args
    assert args == ("/api/{version}/{plural}", "GET")
    assert kwargs["path_params"] == {"version": "v1", "plural": "namespaces"}
    assert kwargs["auth_settings"] == ["BearerToken"]
    assert kwargs["_return_http_data_only"] is True
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["name"] == "default"
    assert row["kind"] == "Namespace"
    assert row["api_version"] == "v1"


@pytest.mark.asyncio
async def test_cr_list_core_group_namespaced_path() -> None:
    api_client = MagicMock(
        close=AsyncMock(), call_api=AsyncMock(return_value={"kind": "ServiceList", "items": []})
    )
    with _patch_client(api_client):
        result = await _make_connector().k8s_cr_list(
            _make_operator(),
            _TARGET,
            {"group": "", "version": "v1", "plural": "services", "namespace": "shop"},
        )

    args, kwargs = api_client.call_api.call_args
    assert args[0] == "/api/{version}/namespaces/{namespace}/{plural}"
    assert kwargs["path_params"] == {"version": "v1", "namespace": "shop", "plural": "services"}
    assert result == {"rows": [], "total": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("namespace", "path", "path_params"),
    [
        (None, "/api/{version}/{plural}/{name}", {"version": "v1", "plural": "namespaces"}),
        (
            "shop",
            "/api/{version}/namespaces/{namespace}/{plural}/{name}",
            {"version": "v1", "plural": "namespaces", "namespace": "shop"},
        ),
    ],
)
async def test_cr_info_core_group_paths(
    namespace: str | None, path: str, path_params: dict[str, str]
) -> None:
    obj = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "default"}}
    api_client = MagicMock(close=AsyncMock(), call_api=AsyncMock(return_value=obj))
    params: dict[str, Any] = {
        "group": "",
        "version": "v1",
        "plural": "namespaces",
        "name": "default",
    }
    if namespace:
        params["namespace"] = namespace
    with _patch_client(api_client):
        result = await _make_connector().k8s_cr_info(_make_operator(), _TARGET, params)

    args, kwargs = api_client.call_api.call_args
    assert args[0] == path
    assert kwargs["path_params"] == {**path_params, "name": "default"}
    assert result["kind"] == "Namespace"
    assert "rows" not in result


@pytest.mark.asyncio
async def test_cr_list_named_group_still_uses_custom_objects_api() -> None:
    api_client = MagicMock(close=AsyncMock(), call_api=AsyncMock())
    with (
        _patch_client(api_client),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.list_cluster_custom_object = AsyncMock(
            return_value={
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingWebhookConfigurationList",
                "items": [{"metadata": {"name": "policy"}}],
            }
        )
        result = await _make_connector().k8s_cr_list(
            _make_operator(),
            _TARGET,
            {
                "group": "admissionregistration.k8s.io",
                "version": "v1",
                "plural": "validatingwebhookconfigurations",
            },
        )

    api_client.call_api.assert_not_called()
    row = result["rows"][0]
    assert row["kind"] == "ValidatingWebhookConfiguration"
    assert row["api_version"] == "admissionregistration.k8s.io/v1"


def test_list_items_keeps_an_item_kind_and_does_not_mutate() -> None:
    item = {"apiVersion": "example.io/v1", "kind": "Widget", "metadata": {"name": "a"}}
    bare = {"metadata": {"name": "b"}}
    resp = {"apiVersion": "example.io/v1", "kind": "WidgetList", "items": [item, bare]}
    out = list_items(resp, group="example.io", plural="widgets")
    assert [o["kind"] for o in out] == ["Widget", "Widget"]
    assert "kind" not in bare


def test_list_items_core_secrets_without_list_kind_still_count_as_secret() -> None:
    out = list_items({"items": [{"metadata": {"name": "s"}}]}, group="", plural="secrets")
    assert out[0]["kind"] == "Secret"


def test_list_items_unknown_kind_is_left_alone() -> None:
    out = list_items({"items": [{"metadata": {"name": "x"}}]}, group="", plural="pods")
    assert "kind" not in out[0]


# ---------------------------------------------------------------------------
# k8s.auth.can_i / k8s.auth.rules
# ---------------------------------------------------------------------------


def test_can_i_schema_needs_exactly_one_of_resource_or_path() -> None:
    validator = Draft202012Validator(K8S_AUTH_CAN_I_PARAMETER_SCHEMA)
    assert validator.is_valid({"verb": "get", "resource": "pods"})
    assert validator.is_valid({"verb": "get", "non_resource_path": "/healthz"})
    assert not validator.is_valid({"verb": "get"})
    assert not validator.is_valid({"verb": "get", "resource": "pods", "non_resource_path": "/x"})
    assert not validator.is_valid({"verb": "get", "non_resource_path": "healthz"})


def test_access_review_body_resource_form() -> None:
    body = access_review_body(
        {"verb": "patch", "group": "apps", "resource": "deployments", "namespace": "shop"}
    )
    attrs = body.spec.resource_attributes
    assert body.kind == "SelfSubjectAccessReview"
    assert (attrs.verb, attrs.group, attrs.resource, attrs.namespace) == (
        "patch",
        "apps",
        "deployments",
        "shop",
    )
    assert attrs.name is None
    assert body.spec.non_resource_attributes is None


def test_access_review_body_non_resource_form() -> None:
    body = access_review_body({"verb": "get", "non_resource_path": "/healthz"})
    assert body.spec.resource_attributes is None
    assert body.spec.non_resource_attributes.path == "/healthz"
    assert body.spec.non_resource_attributes.verb == "get"


@pytest.mark.asyncio
async def test_can_i_handler_returns_the_answer() -> None:
    answered = client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(),
        status=client.V1SubjectAccessReviewStatus(allowed=False, reason="no RBAC rule"),
    )
    with (
        _patch_client(MagicMock(close=AsyncMock())),
        patch("meho_backplane.connectors.kubernetes.ops_access.client.AuthorizationV1Api") as cls,
    ):
        cls.return_value.create_self_subject_access_review = AsyncMock(return_value=answered)
        result = await _make_connector().k8s_auth_can_i(
            _make_operator(), _TARGET, {"verb": "delete", "resource": "secrets"}
        )
        sent = cls.return_value.create_self_subject_access_review.call_args.kwargs["body"]

    assert sent.spec.resource_attributes.resource == "secrets"
    assert result["allowed"] is False
    assert result["denied"] is False
    assert result["reason"] == "no RBAC rule"
    assert result["verb"] == "delete"
    assert result["resource"] == "secrets"


def test_rules_review_result_one_row_per_rule() -> None:
    review = client.V1SelfSubjectRulesReview(
        spec=client.V1SelfSubjectRulesReviewSpec(namespace="shop"),
        status=client.V1SubjectRulesReviewStatus(
            incomplete=True,
            resource_rules=[
                client.V1ResourceRule(verbs=["get", "list"], api_groups=[""], resources=["pods"])
            ],
            non_resource_rules=[
                client.V1NonResourceRule(verbs=["get"], non_resource_urls=["/healthz"])
            ],
        ),
    )
    out = rules_review_result(review, "shop")
    assert out["total"] == 2
    assert out["incomplete"] is True
    assert out["rows"][0] == {
        "kind": "resource",
        "verbs": ["get", "list"],
        "api_groups": [""],
        "resources": ["pods"],
        "resource_names": [],
        "non_resource_urls": [],
    }
    assert out["rows"][1]["kind"] == "non_resource"
    assert out["rows"][1]["non_resource_urls"] == ["/healthz"]


@pytest.mark.asyncio
async def test_rules_handler_sends_the_namespace() -> None:
    answered = client.V1SelfSubjectRulesReview(
        spec=client.V1SelfSubjectRulesReviewSpec(namespace="shop"),
        status=client.V1SubjectRulesReviewStatus(
            incomplete=False, resource_rules=[], non_resource_rules=[]
        ),
    )
    with (
        _patch_client(MagicMock(close=AsyncMock())),
        patch("meho_backplane.connectors.kubernetes.ops_access.client.AuthorizationV1Api") as cls,
    ):
        cls.return_value.create_self_subject_rules_review = AsyncMock(return_value=answered)
        result = await _make_connector().k8s_auth_rules(
            _make_operator(), _TARGET, {"namespace": "shop"}
        )
        sent = cls.return_value.create_self_subject_rules_review.call_args.kwargs["body"]

    assert sent.spec.namespace == "shop"
    assert result == {
        "namespace": "shop",
        "rows": [],
        "total": 0,
        "incomplete": False,
        "evaluation_error": None,
    }


# ---------------------------------------------------------------------------
# k8s.admission_webhook.list
# ---------------------------------------------------------------------------


def _validating_config() -> client.V1ValidatingWebhookConfiguration:
    return client.V1ValidatingWebhookConfiguration(
        metadata=client.V1ObjectMeta(name="policy-engine"),
        webhooks=[
            client.V1ValidatingWebhook(
                name="validate.policy.example.io",
                admission_review_versions=["v1"],
                side_effects="None",
                failure_policy="Fail",
                timeout_seconds=10,
                client_config=client.AdmissionregistrationV1WebhookClientConfig(
                    ca_bundle="Y2EtYnVuZGxl",
                    service=client.AdmissionregistrationV1ServiceReference(
                        namespace="policy", name="webhook", path="/validate", port=443
                    ),
                ),
                rules=[
                    client.V1RuleWithOperations(
                        operations=["CREATE", "UPDATE"],
                        api_groups=["apps"],
                        api_versions=["v1"],
                        resources=["deployments"],
                        scope="Namespaced",
                    )
                ],
                namespace_selector=client.V1LabelSelector(
                    match_expressions=[
                        client.V1LabelSelectorRequirement(
                            key="policy", operator="NotIn", values=["off"]
                        )
                    ]
                ),
                match_conditions=[client.V1MatchCondition(name="skip-system", expression="true")],
            )
        ],
    )


def _mutating_config() -> client.V1MutatingWebhookConfiguration:
    return client.V1MutatingWebhookConfiguration(
        metadata=client.V1ObjectMeta(name="injector"),
        webhooks=[
            client.V1MutatingWebhook(
                name="inject.example.io",
                admission_review_versions=["v1"],
                side_effects="None",
                failure_policy="Ignore",
                reinvocation_policy="IfNeeded",
                client_config=client.AdmissionregistrationV1WebhookClientConfig(
                    url="https://injector.example.invalid/mutate"
                ),
            )
        ],
    )


def test_admission_webhook_rows_shape_and_no_ca_bundle() -> None:
    rows = admission_webhook_rows([_validating_config()], "validating")
    assert len(rows) == 1
    row = rows[0]
    assert row["configuration"] == "policy-engine"
    assert row["type"] == "validating"
    assert row["failure_policy"] == "Fail"
    assert row["service"] == {
        "namespace": "policy",
        "name": "webhook",
        "path": "/validate",
        "port": 443,
    }
    assert row["rules"][0]["operations"] == ["CREATE", "UPDATE"]
    assert row["namespace_selector"] == "policy notin (off)"
    assert row["object_selector"] is None
    assert row["match_conditions"] == [{"name": "skip-system", "expression": "true"}]
    assert row["reinvocation_policy"] is None
    assert "Y2EtYnVuZGxl" not in str(row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("webhook_type", "expected"),
    [
        (None, ["validating", "mutating"]),
        ("validating", ["validating"]),
        ("mutating", ["mutating"]),
    ],
)
async def test_admission_webhook_list_handler_type_filter(
    webhook_type: str | None, expected: list[str]
) -> None:
    params = {} if webhook_type is None else {"type": webhook_type}
    with (
        _patch_client(MagicMock(close=AsyncMock())),
        patch(
            "meho_backplane.connectors.kubernetes.ops_admission.client.AdmissionregistrationV1Api"
        ) as cls,
    ):
        cls.return_value.list_validating_webhook_configuration = AsyncMock(
            return_value=MagicMock(items=[_validating_config()])
        )
        cls.return_value.list_mutating_webhook_configuration = AsyncMock(
            return_value=MagicMock(items=[_mutating_config()])
        )
        result = await _make_connector().k8s_admission_webhook_list(
            _make_operator(), _TARGET, params
        )

    assert [row["type"] for row in result["rows"]] == expected
    assert result["total"] == len(expected)
    if "mutating" in expected:
        mutating = result["rows"][-1]
        assert mutating["reinvocation_policy"] == "IfNeeded"
        assert mutating["url"] == "https://injector.example.invalid/mutate"
        assert mutating["service"] is None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_new_ops_are_registered_safe_reads_in_the_access_group() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    for op_id in _NEW_OP_IDS:
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert op.group_key == "access"
        assert callable(getattr(KubernetesConnector, op.handler_attr))
    assert "access" in _WHEN_TO_USE_BY_GROUP
