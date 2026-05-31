# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``k8s.exec`` op (G3.14-T2, #1404).

The live k3s exercise lives in
:mod:`tests.integration.test_connectors_k8s_k3d`; this module exercises
the demux / exit-code / timeout / cap contract with a fake websocket so
the gate runs in every CI lane regardless of Docker availability.

Coverage matrix (per #1404 acceptance criteria):

* Happy path: argv command runs, stdout / stderr demuxed from the
  channel-prefixed frames, exit code 0 parsed from the Success status
  frame.
* Non-zero exit: the ERROR_CHANNEL status frame carries the code in
  ``details.causes[0].message``; the handler surfaces it as
  ``exit_code``.
* Timeout: a socket that never closes is torn down at the deadline; the
  handler returns partial output + ``timed_out=true`` + ``exit_code=None``.
* ``self._ws_api_clients`` is cached per ``secret_ref`` and closed on
  ``aclose`` (no leaked sockets).
* 1 MiB cap: a stream over the cap is truncated from the front and
  ``truncated_byte_count`` is recorded.
* ``stdin`` / ``tty`` are pinned ``False`` on the wire (no interactive
  path).
* Pod / container resolution reuses the logs resolver: ambiguous prefix
  and multi-container-without-container both raise the structured
  errors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.stream.ws_client import (
    ERROR_CHANNEL,
    STDERR_CHANNEL,
    STDOUT_CHANNEL,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_exec import (
    MAX_STREAM_BYTES,
    k8s_exec,
    truncate_bytes_to_cap,
)
from meho_backplane.connectors.kubernetes.ops_logs import (
    MultiContainerAmbiguityError,
    PodNotFoundError,
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
    secret_ref="k8s/rke2-meho",
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
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return _stub_kubeconfig_dict()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    return Operator(
        sub="op-exec-test",
        name="Exec Test Operator",
        email=None,
        raw_jwt="op.exec.jwt",
        tenant_id=__import__("uuid").UUID("00000000-0000-0000-0000-00000000e0e0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _pod_obj(name: str, containers: list[str]) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.spec.containers = [MagicMock() for _ in containers]
    for mock_container, cname in zip(pod.spec.containers, containers, strict=True):
        mock_container.name = cname
    return pod


def _pod_list(pods: list[Any]) -> MagicMock:
    pod_list = MagicMock()
    pod_list.items = pods
    return pod_list


# ---------------------------------------------------------------------------
# Fake websocket: yields channel-prefixed binary frames, then closes.
# ---------------------------------------------------------------------------


class _FakeWsMsg:
    """Mimics an aiohttp WSMessage -- only ``.data`` is read by the demux."""

    def __init__(self, data: bytes) -> None:
        self.data = data


def _frame(channel: int, payload: bytes) -> _FakeWsMsg:
    return _FakeWsMsg(bytes([channel]) + payload)


class _FakeWebSocket:
    """Async-iterable fake websocket.

    ``frames`` are yielded in order; when ``hang`` is True the iterator
    awaits an event that is never set, simulating a command that never
    completes (drives the timeout path).
    """

    def __init__(self, frames: list[_FakeWsMsg], *, hang: bool = False) -> None:
        self._frames = frames
        self._hang = hang
        self.closed = False

    def __aiter__(self) -> _FakeWebSocket:
        self._iter = iter(self._frames)
        return self

    async def __anext__(self) -> _FakeWsMsg:
        try:
            return next(self._iter)
        except StopIteration:
            if self._hang:
                import asyncio

                await asyncio.Event().wait()  # never returns
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class _FakeWsCtx:
    """Async context manager wrapping a :class:`_FakeWebSocket`."""

    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self._ws

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _success_status_frame() -> bytes:
    return json.dumps({"status": "Success"}).encode("utf-8")


def _failure_status_frame(code: int) -> bytes:
    return json.dumps(
        {
            "status": "Failure",
            "reason": "NonZeroExitCode",
            "details": {"causes": [{"reason": "ExitCode", "message": str(code)}]},
        }
    ).encode("utf-8")


def _patch_exec(
    *,
    ws: _FakeWebSocket,
    list_pods: Any = None,
) -> tuple[Any, Any, MagicMock]:
    """Patch the kubeconfig clients + CoreV1Api for the exec + resolve path.

    Returns ``(config_patch, ws_client_patch, core_v1)``. ``core_v1`` is
    the shared CoreV1Api mock so tests can assert on
    ``connect_get_namespaced_pod_exec.call_args``.
    """
    api_client_mock = MagicMock(close=AsyncMock())
    core_v1 = MagicMock()
    if list_pods is None:
        list_pods = _pod_list([_pod_obj("web-7c4b8d-x7r2k", ["web"])])
    core_v1.list_namespaced_pod = AsyncMock(return_value=list_pods)
    core_v1.connect_get_namespaced_pod_exec = AsyncMock(return_value=_FakeWsCtx(ws))

    config_patch = patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=api_client_mock,
    )
    # _get_ws_api_client builds a WsApiClient via load_kube_config_from_dict;
    # patch the connector method to hand back a sentinel so CoreV1Api wraps it.
    ws_client_sentinel = MagicMock(close=AsyncMock())
    ws_client_patch = patch.object(
        KubernetesConnector,
        "_get_ws_api_client",
        new_callable=AsyncMock,
        return_value=ws_client_sentinel,
    )
    core_v1_patch = patch(
        "meho_backplane.connectors.kubernetes.ops_exec.client.CoreV1Api",
        return_value=core_v1,
    )
    return (config_patch, ws_client_patch, core_v1_patch), core_v1, ws_client_sentinel


# ---------------------------------------------------------------------------
# truncate_bytes_to_cap
# ---------------------------------------------------------------------------


def test_truncate_under_cap_returns_full() -> None:
    data = b"hello world"
    kept, dropped = truncate_bytes_to_cap(data, 1024)
    assert kept == data
    assert dropped == 0


def test_truncate_over_cap_drops_from_front() -> None:
    data = b"0123456789"
    kept, dropped = truncate_bytes_to_cap(data, 4)
    assert kept == b"6789"  # most-recent kept
    assert dropped == 6


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_happy_path_demuxes_streams_and_parses_exit_zero() -> None:
    ws = _FakeWebSocket(
        [
            _frame(STDOUT_CHANNEL, b"line-out-1\n"),
            _frame(STDERR_CHANNEL, b"warn-1\n"),
            _frame(STDOUT_CHANNEL, b"line-out-2\n"),
            _frame(ERROR_CHANNEL, _success_status_frame()),
        ]
    )
    patches, core_v1, _ = _patch_exec(ws=ws)
    connector = _make_connector()
    with patches[0], patches[1], patches[2]:
        result = await k8s_exec(
            connector,
            _TARGET,
            _make_operator(),
            {"pod_name": "web", "namespace": "default", "command": ["echo", "hi"]},
        )

    assert result["pod"] == "web-7c4b8d-x7r2k"
    assert result["namespace"] == "default"
    assert result["container"] == "web"
    assert result["command"] == ["echo", "hi"]
    assert result["stdout"] == "line-out-1\nline-out-2\n"
    assert result["stderr"] == "warn-1\n"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["truncated"] is False

    # stdin / tty pinned False on the wire; no interactive path.
    kwargs = core_v1.connect_get_namespaced_pod_exec.call_args.kwargs
    assert kwargs["stdin"] is False
    assert kwargs["tty"] is False
    assert kwargs["stdout"] is True
    assert kwargs["stderr"] is True
    assert kwargs["command"] == ["echo", "hi"]
    assert kwargs["_preload_content"] is False


# ---------------------------------------------------------------------------
# Non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_non_zero_exit_parses_code_from_status_frame() -> None:
    ws = _FakeWebSocket(
        [
            _frame(STDERR_CHANNEL, b"command failed\n"),
            _frame(ERROR_CHANNEL, _failure_status_frame(2)),
        ]
    )
    patches, _, _ = _patch_exec(ws=ws)
    connector = _make_connector()
    with patches[0], patches[1], patches[2]:
        result = await k8s_exec(
            connector,
            _TARGET,
            _make_operator(),
            {"pod_name": "web", "namespace": "default", "command": ["false"]},
        )
    assert result["exit_code"] == 2
    assert result["stderr"] == "command failed\n"
    assert result["stdout"] == ""
    assert result["timed_out"] is False


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_timeout_closes_socket_returns_partial() -> None:
    ws = _FakeWebSocket(
        [_frame(STDOUT_CHANNEL, b"partial-output\n")],
        hang=True,  # no status frame, iterator never completes
    )
    patches, _, _ = _patch_exec(ws=ws)
    connector = _make_connector()
    with patches[0], patches[1], patches[2]:
        result = await k8s_exec(
            connector,
            _TARGET,
            _make_operator(),
            {
                "pod_name": "web",
                "namespace": "default",
                "command": ["sleep", "9999"],
                "timeout_seconds": 1,
            },
        )
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert result["stdout"] == "partial-output\n"
    assert ws.closed is True  # socket torn down on timeout


# ---------------------------------------------------------------------------
# 1 MiB cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_oversize_stdout_truncated_from_front() -> None:
    big = b"A" * (MAX_STREAM_BYTES + 4096)
    ws = _FakeWebSocket(
        [
            _frame(STDOUT_CHANNEL, big),
            _frame(ERROR_CHANNEL, _success_status_frame()),
        ]
    )
    patches, _, _ = _patch_exec(ws=ws)
    connector = _make_connector()
    with patches[0], patches[1], patches[2]:
        result = await k8s_exec(
            connector,
            _TARGET,
            _make_operator(),
            {"pod_name": "web", "namespace": "default", "command": ["cat", "/big"]},
        )
    assert result["truncated"] is True
    assert result["stdout_truncated_byte_count"] == 4096
    assert len(result["stdout"].encode("utf-8")) == MAX_STREAM_BYTES
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# WsApiClient cache + teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_client_cached_per_secret_ref_and_closed_on_aclose() -> None:
    connector = _make_connector()
    operator = _make_operator()

    built: list[MagicMock] = []

    async def _fake_load(*, config_dict: Any, client_configuration: Any) -> None:
        return None

    def _fake_ws_ctor(configuration: Any = None) -> MagicMock:
        m = MagicMock(close=AsyncMock())
        built.append(m)
        return m

    with (
        patch(
            "kubernetes_asyncio.config.load_kube_config_from_dict",
            new=AsyncMock(side_effect=_fake_load),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.WsApiClient",
            side_effect=_fake_ws_ctor,
        ),
    ):
        c1 = await connector._get_ws_api_client(_TARGET, operator)
        c2 = await connector._get_ws_api_client(_TARGET, operator)

    assert c1 is c2, "second call must hit the per-secret_ref cache"
    assert len(built) == 1, "only one WsApiClient built for one secret_ref"
    assert connector._cache_key(_TARGET) in connector._ws_api_clients

    await connector.aclose()
    c1.close.assert_awaited_once()
    assert connector._ws_api_clients == {}


# ---------------------------------------------------------------------------
# Pod / container resolution reuse (logs resolver)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_ambiguous_prefix_raises_pod_not_found() -> None:
    ws = _FakeWebSocket([])
    pods = _pod_list([_pod_obj("web-a", ["c"]), _pod_obj("web-b", ["c"])])
    patches, _, _ = _patch_exec(ws=ws, list_pods=pods)
    connector = _make_connector()
    with patches[0], patches[1], patches[2], pytest.raises(PodNotFoundError):
        await k8s_exec(
            connector,
            _TARGET,
            _make_operator(),
            {"pod_name": "web", "namespace": "default", "command": ["ls"]},
        )


@pytest.mark.asyncio
async def test_exec_multi_container_without_container_raises() -> None:
    ws = _FakeWebSocket([])
    pods = _pod_list([_pod_obj("web-1", ["app", "sidecar"])])
    patches, _, _ = _patch_exec(ws=ws, list_pods=pods)
    connector = _make_connector()
    with patches[0], patches[1], patches[2], pytest.raises(MultiContainerAmbiguityError):
        await k8s_exec(
            connector,
            _TARGET,
            _make_operator(),
            {"pod_name": "web-1", "namespace": "default", "command": ["ls"]},
        )
