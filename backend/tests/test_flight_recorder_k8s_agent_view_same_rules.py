# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent's trace copy and the direct answer hide the same env values.

The same Pod and Deployment run two ways:

* the direct answer: ``k8s.pod.info`` / ``k8s.deployment.info``
  (:func:`pod_info`, :func:`deployment_info`), then the default view
  (:func:`hide_secret_values`);
* the agent's copy of the stored trace: the Kubernetes API object as the
  trace stores it, then :func:`agent_view_attributes`.

For every env entry, a value hidden in one path is hidden in the other, and
a value shown in one path is shown in the other.
"""

from __future__ import annotations

from typing import Any

import pytest
from kubernetes_asyncio.client import ApiClient
from kubernetes_asyncio.client.models import (
    V1Container,
    V1Deployment,
    V1DeploymentSpec,
    V1EnvVar,
    V1EnvVarSource,
    V1LabelSelector,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1PodTemplateSpec,
    V1SecretKeySelector,
)

from meho_backplane.connectors.kubernetes.ops_workload import deployment_info, pod_info
from meho_backplane.flight_recorder.agent_view import agent_view_attributes
from meho_backplane.operations._secret_values import hide_secret_values

_FAKE = "made-up-value-not-real-4471"

_ENV: list[V1EnvVar] = [
    V1EnvVar(name="LOG_LEVEL", value="debug"),
    V1EnvVar(name="EMPTY", value=""),
    V1EnvVar(name="DB_PASSWORD", value=_FAKE),
    V1EnvVar(name="API_TOKEN", value=_FAKE),
    V1EnvVar(name="AWS_SECRET_ACCESS_KEY", value=_FAKE),
    V1EnvVar(name="CONFIG", value=f"password: {_FAKE}"),
    V1EnvVar(name="DATABASE_URL", value=f"postgres://app:{_FAKE}@db.example.invalid/app"),
    V1EnvVar(name="GREETING", value="hello"),
    V1EnvVar(
        name="API_KEY",
        value_from=V1EnvVarSource(
            secret_key_ref=V1SecretKeySelector(name="app-secrets", key="api-key")
        ),
    ),
]


def _container() -> V1Container:
    return V1Container(name="app", image="registry.example.invalid/app:1.0", env=list(_ENV))


def _pod() -> V1Pod:
    return V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=V1ObjectMeta(name="app-0", namespace="default"),
        spec=V1PodSpec(containers=[_container()]),
    )


def _deployment() -> V1Deployment:
    return V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(name="app", namespace="default"),
        spec=V1DeploymentSpec(
            selector=V1LabelSelector(match_labels={"app": "demo"}),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels={"app": "demo"}),
                spec=V1PodSpec(containers=[_container()]),
            ),
        ),
    )


def _hidden_by_name(entries: list[dict[str, Any]]) -> dict[str, bool]:
    """Per env name: True when the shown value is not the value set on the object."""
    raw = {var.name: var.value for var in _ENV}
    return {entry["name"]: entry.get("value") != raw[entry["name"]] for entry in entries}


def _direct_env(op_id: str, info: dict[str, Any]) -> list[dict[str, Any]]:
    shown = hide_secret_values(op_id, info)
    env: list[dict[str, Any]] = shown["containers"][0]["env"]
    return env


async def _trace_env(op_id: str, obj: Any, path: tuple[str, ...]) -> list[dict[str, Any]]:
    # The trace stores the object as the Kubernetes API sends it (camelCase JSON).
    async with ApiClient() as api:
        body = api.sanitize_for_serialization(obj)
    attributes = agent_view_attributes(
        {"connector_id": "k8s-1.x", "op_id": op_id, "method": "GET", "response_body": body}
    )
    node: Any = attributes["response_body"]
    for step in path:
        node = node[step]
    env: list[dict[str, Any]] = node["containers"][0]["env"]
    return env


@pytest.mark.parametrize(
    ("op_id", "obj", "info", "path"),
    [
        ("k8s.pod.info", _pod(), pod_info, ("spec",)),
        ("k8s.deployment.info", _deployment(), deployment_info, ("spec", "template", "spec")),
    ],
    ids=["pod", "deployment"],
)
async def test_direct_answer_and_agent_trace_copy_hide_the_same_env_values(
    op_id: str, obj: Any, info: Any, path: tuple[str, ...]
) -> None:
    direct = _hidden_by_name(_direct_env(op_id, info(obj)))
    trace = _hidden_by_name(await _trace_env(op_id, obj, path))

    assert direct == trace
    # Both kinds of entry are in the set, so the check compares both ways.
    assert direct["DB_PASSWORD"] is True
    assert direct["LOG_LEVEL"] is False
