# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``k8s.logs`` op (G3.2-T5, #325).

The k3d integration shape lives in
:mod:`tests.integration.test_connectors_k8s_k3d` (extended under
T6 / #326 with the live cluster). This module exercises the same
contract with mocked ``kubernetes_asyncio.client.CoreV1Api`` so the
gate runs in every CI lane regardless of Docker availability.

Coverage matrix (per #325 acceptance criteria):

* ``k8s.logs <pod> --namespace argocd`` returns last 100 lines (tail
  default) from a stub cluster.
* ``--tail 500`` flows through to ``tail_lines=500``.
* ``--tail 99999`` clamps to 5000 in the handler.
* ``--container <name>`` selects the named container in a
  multi-container pod.
* Multi-container pod without ``--container`` -> structured error
  whose ``args[1]`` lists available containers.
* ``--since 5m`` resolves to ``since_seconds=300``.
* ``--previous=true`` flows through to ``previous=True``.
* 1 MiB cap: a pod with > 1 MiB of logs returns ``truncated=true`` and
  ``truncated_byte_count`` in the result extras.
* Audit shape (extras vs payload): the handler returns ``lines`` in
  the result dict only; the dispatcher writes ``params_hash`` against
  the input params, never the log content. The unit test asserts the
  handler's contract; the audit row shape is covered by the
  dispatcher's contract tests.
* Prefix resolution: unique prefix resolves to one pod; ambiguous
  prefix raises :class:`PodNotFoundError`.
* Empty namespace / missing pod: raises :class:`PodNotFoundError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_logs import (
    MAX_BODY_BYTES,
    MAX_TAIL_LINES,
    MultiContainerAmbiguityError,
    PodNotFoundError,
    k8s_logs,
    parse_duration,
    truncate_lines_to_byte_cap,
)

# ---------------------------------------------------------------------------
# Target stub + kubeconfig
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="rke2-meho",
    host="rke2-meho.test.invalid",
    port=6443,
    secret_ref="kv/data/k8s/rke2-meho",
)


def _stub_kubeconfig_dict() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
        "users": [{"name": "u1", "user": {"token": "stub-token"}}],
    }


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _pod_obj(name: str, containers: list[str]) -> MagicMock:
    """Stub for ``V1Pod`` -- just the attrs the handler reads."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.spec.containers = [MagicMock(name=c) for c in containers]
    # ``MagicMock(name=...)`` sets the mock's repr-name, not its
    # ``.name`` attribute; assign explicitly.
    for mock_container, cname in zip(pod.spec.containers, containers, strict=True):
        mock_container.name = cname
    return pod


def _pod_list(pods: list[Any]) -> MagicMock:
    pod_list = MagicMock()
    pod_list.items = pods
    return pod_list


def _patch_api(
    *,
    list_pods: Any = None,
    log_body: str = "",
    log_side_effect: Exception | None = None,
) -> Any:
    """Patch the kubeconfig client + the CoreV1Api factory in one go.

    Returns the patched ``CoreV1Api`` instance mock so the test can
    assert against ``read_namespaced_pod_log.call_args``.
    """
    api_client_mock = MagicMock(close=AsyncMock())
    core_v1 = MagicMock()
    if list_pods is None:
        list_pods = _pod_list([_pod_obj("argocd-server-7c4b8d-x7r2k", ["argocd-server"])])
    core_v1.list_namespaced_pod = AsyncMock(return_value=list_pods)
    if log_side_effect is not None:
        core_v1.read_namespaced_pod_log = AsyncMock(side_effect=log_side_effect)
    else:
        core_v1.read_namespaced_pod_log = AsyncMock(return_value=log_body)
    config_patch = patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=api_client_mock,
    )
    core_v1_patch = patch(
        "meho_backplane.connectors.kubernetes.ops_logs.client.CoreV1Api",
        return_value=core_v1,
    )
    return config_patch, core_v1_patch, core_v1


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5s", 5),
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("24h", 86400),
        ("7d", 604800),
        (" 5m ", 300),
        ("5 m", 300),
    ],
)
def test_parse_duration_valid(value: str, expected: int) -> None:
    assert parse_duration(value) == expected


def test_parse_duration_none_passes_through() -> None:
    assert parse_duration(None) is None


@pytest.mark.parametrize("value", ["", "5", "5x", "abc", "-5m", "5.5m"])
def test_parse_duration_invalid_raises(value: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(value)


# ---------------------------------------------------------------------------
# truncate_lines_to_byte_cap
# ---------------------------------------------------------------------------


def test_truncate_lines_under_cap_returns_full_input() -> None:
    lines = ["line-a", "line-b", "line-c"]
    kept, dropped = truncate_lines_to_byte_cap(lines, cap_bytes=1024)
    assert kept == lines
    assert dropped == 0


def test_truncate_lines_drops_from_front_at_line_boundary() -> None:
    """Keep tail; drop leading lines that push the response past the cap."""
    lines = ["a" * 100, "b" * 100, "c" * 100]
    # cap = 220 bytes -> fits 'c'*100 + newline + 'b'*100 = 201 bytes; adding
    # 'a'*100 needs another 101 bytes which exceeds 220. Keep [b, c].
    kept, dropped = truncate_lines_to_byte_cap(lines, cap_bytes=220)
    assert kept == ["b" * 100, "c" * 100]
    # Dropped one line of 100 bytes + the joining newline -> 101.
    assert dropped == 101


def test_truncate_lines_empty_input() -> None:
    kept, dropped = truncate_lines_to_byte_cap([], cap_bytes=1024)
    assert kept == []
    assert dropped == 0


def test_truncate_lines_keeps_single_oversize_line() -> None:
    """When even the tail line by itself exceeds the cap, keep it intact.

    Line-boundary truncation contract: we never slice inside a line.
    """
    lines = ["x" * 200]
    kept, dropped = truncate_lines_to_byte_cap(lines, cap_bytes=100)
    assert kept == ["x" * 200]
    assert dropped == 0


# ---------------------------------------------------------------------------
# k8s_logs -- happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_logs_default_tail_100_returns_lines() -> None:
    connector = _make_connector()
    body = "\n".join(f"line-{i}" for i in range(100)) + "\n"
    config_patch, core_v1_patch, core_v1 = _patch_api(log_body=body)
    with config_patch, core_v1_patch:
        result = await k8s_logs(
            connector, _TARGET, {"pod_name": "argocd-server", "namespace": "argocd"}
        )

    assert result["pod"] == "argocd-server-7c4b8d-x7r2k"
    assert result["namespace"] == "argocd"
    assert result["container"] == "argocd-server"
    assert result["truncated"] is False
    assert result["lines"][0] == "line-0"
    assert result["lines"][-1] == "line-99"
    assert len(result["lines"]) == 100
    # ``--tail`` defaults to 100.
    kwargs = core_v1.read_namespaced_pod_log.call_args.kwargs
    assert kwargs["tail_lines"] == 100
    assert kwargs["previous"] is False
    assert "since_seconds" not in kwargs


@pytest.mark.asyncio
async def test_k8s_logs_explicit_tail_flows_to_api() -> None:
    connector = _make_connector()
    config_patch, core_v1_patch, core_v1 = _patch_api(log_body="x\n")
    with config_patch, core_v1_patch:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd", "tail": 500},
        )
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["tail_lines"] == 500


@pytest.mark.asyncio
async def test_k8s_logs_tail_clamps_at_max() -> None:
    """Clamp at MAX_TAIL_LINES even when schema lets a larger value through."""
    connector = _make_connector()
    config_patch, core_v1_patch, core_v1 = _patch_api(log_body="x\n")
    with config_patch, core_v1_patch:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd", "tail": 99999},
        )
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["tail_lines"] == MAX_TAIL_LINES


@pytest.mark.asyncio
async def test_k8s_logs_since_resolves_to_seconds() -> None:
    connector = _make_connector()
    config_patch, core_v1_patch, core_v1 = _patch_api(log_body="x\n")
    with config_patch, core_v1_patch:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd", "since": "5m"},
        )
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["since_seconds"] == 300


@pytest.mark.asyncio
async def test_k8s_logs_previous_flows_to_api() -> None:
    connector = _make_connector()
    config_patch, core_v1_patch, core_v1 = _patch_api(log_body="x\n")
    with config_patch, core_v1_patch:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd", "previous": True},
        )
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["previous"] is True


# ---------------------------------------------------------------------------
# k8s_logs -- multi-container pods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_logs_explicit_container_in_multi_container_pod() -> None:
    connector = _make_connector()
    multi = _pod_list([_pod_obj("istio-pilot-1", ["discovery", "istio-proxy"])])
    config_patch, core_v1_patch, core_v1 = _patch_api(list_pods=multi, log_body="x\n")
    with config_patch, core_v1_patch:
        result = await k8s_logs(
            connector,
            _TARGET,
            {
                "pod_name": "istio-pilot-1",
                "namespace": "istio",
                "container": "istio-proxy",
            },
        )
    assert result["container"] == "istio-proxy"
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["container"] == "istio-proxy"


@pytest.mark.asyncio
async def test_k8s_logs_multi_container_without_container_param_errors() -> None:
    connector = _make_connector()
    multi = _pod_list([_pod_obj("istio-pilot-1", ["discovery", "istio-proxy"])])
    config_patch, core_v1_patch, _core_v1 = _patch_api(list_pods=multi, log_body="x\n")
    with config_patch, core_v1_patch, pytest.raises(MultiContainerAmbiguityError) as exc:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "istio-pilot-1", "namespace": "istio"},
        )
    assert exc.value.containers == ["discovery", "istio-proxy"]


@pytest.mark.asyncio
async def test_k8s_logs_unknown_container_in_multi_container_pod_errors() -> None:
    connector = _make_connector()
    multi = _pod_list([_pod_obj("istio-pilot-1", ["discovery", "istio-proxy"])])
    config_patch, core_v1_patch, _core_v1 = _patch_api(list_pods=multi, log_body="x\n")
    with config_patch, core_v1_patch, pytest.raises(MultiContainerAmbiguityError) as exc:
        await k8s_logs(
            connector,
            _TARGET,
            {
                "pod_name": "istio-pilot-1",
                "namespace": "istio",
                "container": "nope",
            },
        )
    assert exc.value.containers == ["discovery", "istio-proxy"]


# ---------------------------------------------------------------------------
# k8s_logs -- pod resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_logs_pod_prefix_resolves_to_exact_name() -> None:
    connector = _make_connector()
    pods = _pod_list(
        [
            _pod_obj("argocd-server-7c4b8d-x7r2k", ["argocd-server"]),
            _pod_obj("argocd-repo-server-abc123", ["argocd-repo-server"]),
        ]
    )
    config_patch, core_v1_patch, core_v1 = _patch_api(list_pods=pods, log_body="x\n")
    with config_patch, core_v1_patch:
        result = await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd"},
        )
    assert result["pod"] == "argocd-server-7c4b8d-x7r2k"
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["name"] == "argocd-server-7c4b8d-x7r2k"


@pytest.mark.asyncio
async def test_k8s_logs_pod_prefix_ambiguous_raises() -> None:
    connector = _make_connector()
    pods = _pod_list(
        [
            _pod_obj("api-1", ["api"]),
            _pod_obj("api-2", ["api"]),
        ]
    )
    config_patch, core_v1_patch, _core_v1 = _patch_api(list_pods=pods, log_body="x\n")
    with config_patch, core_v1_patch, pytest.raises(PodNotFoundError) as exc:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "api", "namespace": "default"},
        )
    assert exc.value.candidates == ["api-1", "api-2"]


@pytest.mark.asyncio
async def test_k8s_logs_pod_not_found_raises() -> None:
    connector = _make_connector()
    pods = _pod_list([])
    config_patch, core_v1_patch, _core_v1 = _patch_api(list_pods=pods, log_body="x\n")
    with config_patch, core_v1_patch, pytest.raises(PodNotFoundError) as exc:
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "ghost", "namespace": "default"},
        )
    assert exc.value.candidates == []


@pytest.mark.asyncio
async def test_k8s_logs_exact_match_wins_over_prefix_match() -> None:
    """Exact ``foo-bar`` match wins over prefix when ``foo-bar-x`` also exists."""
    connector = _make_connector()
    pods = _pod_list(
        [
            _pod_obj("api-1", ["api"]),
            _pod_obj("api-1-replica", ["api"]),
        ]
    )
    config_patch, core_v1_patch, core_v1 = _patch_api(list_pods=pods, log_body="x\n")
    with config_patch, core_v1_patch:
        result = await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "api-1", "namespace": "default"},
        )
    assert result["pod"] == "api-1"
    assert core_v1.read_namespaced_pod_log.call_args.kwargs["name"] == "api-1"


# ---------------------------------------------------------------------------
# k8s_logs -- 1MB cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_logs_body_under_cap_returns_untruncated() -> None:
    connector = _make_connector()
    body = "small body\nsecond line\n"
    config_patch, core_v1_patch, _core_v1 = _patch_api(log_body=body)
    with config_patch, core_v1_patch:
        result = await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd"},
        )
    assert result["truncated"] is False
    assert "truncated_byte_count" not in result
    assert "byte_count" not in result


@pytest.mark.asyncio
async def test_k8s_logs_body_over_cap_truncates_from_front() -> None:
    connector = _make_connector()
    # Build a body just over 1 MiB. Each line is 1 KiB + 1 byte newline ->
    # 1025 bytes; 1100 lines = ~1.10 MiB > 1 MiB cap.
    lines = [f"L{i:04d}-{'x' * (1024 - 6)}" for i in range(1100)]
    body = "\n".join(lines) + "\n"
    config_patch, core_v1_patch, _core_v1 = _patch_api(log_body=body)
    with config_patch, core_v1_patch:
        result = await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd"},
        )

    assert result["truncated"] is True
    assert result["truncated_byte_count"] > 0
    assert result["byte_count"] == len(body.encode("utf-8"))
    # Most-recent lines kept: the last line is preserved.
    assert result["lines"][-1].startswith("L1099-")
    # Kept body fits in the cap.
    kept_bytes = sum(len(line.encode("utf-8")) for line in result["lines"]) + max(
        len(result["lines"]) - 1, 0
    )
    assert kept_bytes <= MAX_BODY_BYTES


# ---------------------------------------------------------------------------
# k8s_logs -- API exceptions propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_logs_api_exception_propagates() -> None:
    """An ``ApiException`` from the API call propagates so the dispatcher's
    ``connector_error`` envelope can surface ``extras.exception_class``.
    """
    from kubernetes_asyncio.client.exceptions import ApiException

    connector = _make_connector()
    config_patch, core_v1_patch, _core_v1 = _patch_api(
        log_side_effect=ApiException(status=403, reason="Forbidden")
    )
    with config_patch, core_v1_patch, pytest.raises(ApiException):
        await k8s_logs(
            connector,
            _TARGET,
            {"pod_name": "argocd-server", "namespace": "argocd"},
        )


# ---------------------------------------------------------------------------
# Bound-method shim on KubernetesConnector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kubernetes_connector_logs_method_delegates_to_handler() -> None:
    connector = _make_connector()
    body = "delegated\n"
    config_patch, core_v1_patch, _core_v1 = _patch_api(log_body=body)
    with config_patch, core_v1_patch:
        result = await connector.logs(_TARGET, {"pod_name": "argocd-server", "namespace": "argocd"})
    assert result["lines"] == ["delegated"]
