# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.exec`` op -- bounded command-and-capture over a websocket transport.

The op is the v0.x K8s connector's answer to ``kubectl exec <pod> --
<cmd>``: run an explicit ``argv`` command inside a container, capture its
stdout / stderr, and surface the process exit code -- all in a single
:class:`~meho_backplane.connectors.schemas.OperationResult`. It is the
first (and so far only) K8s op that cannot ride the cached
:class:`kubernetes_asyncio.client.ApiClient`: pod exec is an HTTP
``Upgrade`` to a websocket carrying the multiplexed ``v4.channel.k8s.io``
sub-protocol, which `kubernetes_asyncio` only speaks through its
:class:`kubernetes_asyncio.stream.WsApiClient` subclass. The connector
holds a *parallel* per-target cache of those websocket clients
(``KubernetesConnector._ws_api_clients``), built from the same
operator-identity kubeconfig as the read clients and closed alongside
them on connector teardown.

Why we drive the socket ourselves rather than calling the op with
``_preload_content=True``
-------------------------------------------------------------------------
``WsApiClient.request`` with ``_preload_content=True`` (the library
default) concatenates STDOUT and STDERR into one blob *and discards the
ERROR_CHANNEL status frame* -- so the exit code is lost and the two
streams can no longer be told apart. To satisfy the op's contract
(separate stdout / stderr + parsed exit code + bounded timeout + 1 MiB
cap) we pass ``_preload_content=False`` to
``connect_get_namespaced_pod_exec``. That returns the raw aiohttp
websocket context manager; we ``async with`` it, iterate the frames,
demultiplex the leading channel byte ourselves, and parse the
``ERROR_CHANNEL`` (channel 3) status frame for the exit code via
:meth:`kubernetes_asyncio.stream.WsApiClient.parse_error_data`.

Interactive exec (``kubectl exec -it``) is explicitly **out of scope**:
the dispatcher returns a single ``OperationResult`` and there is no
incremental-output / stdin envelope to carry a live PTY. ``stdin`` and
``tty`` are pinned to ``False`` on the wire and there is no code path
that flips them. This is the same deferral ``k8s.logs -f`` took; both
land once the MCP ``tools/call`` envelope grows a streaming shape.

Never-log-content posture
-------------------------
The captured stdout / stderr is returned in the result dict only. The
dispatcher's audit row records ``params_hash`` over the input params
(pod / namespace / command / container) -- never the bytes the command
emitted. The 1 MiB cap mirrors :mod:`ops_logs`: stdout and stderr each
get an independent byte budget, and a stream that exceeds it is
truncated **from the front** (most-recent bytes kept) with a
``*_truncated_byte_count`` recorded so the caller can render a "X KiB
dropped" hint.

References
----------
* Parent task: G3.14-T2 (#1404).
* Parent Initiative: G3.14 (#1398), kubernetes write/exec op surface.
* k8s exec sub-protocol (``v4.channel.k8s.io``):
  https://kubernetes.io/docs/reference/using-api/api-concepts/
* ``kubernetes_asyncio.stream.WsApiClient`` /
  ``CoreV1Api.connect_get_namespaced_pod_exec``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/stream/ws_client.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client
from kubernetes_asyncio.stream.ws_client import (
    ERROR_CHANNEL,
    STDERR_CHANNEL,
    STDOUT_CHANNEL,
    WsApiClient,
)

from meho_backplane.connectors.kubernetes.ops_logs import resolve_pod_and_container

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "K8S_EXEC_LLM_INSTRUCTIONS",
    "K8S_EXEC_PARAMETER_SCHEMA",
    "K8S_EXEC_RESPONSE_SCHEMA",
    "MAX_COMMAND_ARGS",
    "MAX_STREAM_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "k8s_exec",
    "truncate_bytes_to_cap",
]

#: 1 MiB cap per captured stream (stdout and stderr each get their own
#: budget). Same rationale as :data:`ops_logs.MAX_BODY_BYTES`: keep the
#: reducer / audit-row payload bounded. A command that floods more than
#: this is almost certainly the wrong tool -- prefer ``k8s.logs`` for
#: bulk output.
MAX_STREAM_BYTES = 1024 * 1024

#: Hard cap on ``argv`` length. A command with more than this many
#: tokens is a smell (shell-string-splatting, generated argv); the
#: schema's ``maxItems`` enforces the same bound, this is defence in
#: depth.
MAX_COMMAND_ARGS = 64

#: Default run-to-completion timeout. Most operator-driven exec calls
#: (``cat`` a file, ``ps``, ``env``) finish in well under a second; the
#: default is generous enough for a slow ``apt-get`` query without
#: letting a runaway command hold the socket open indefinitely.
DEFAULT_TIMEOUT_SECONDS = 30

#: Upper bound on the caller-supplied timeout. Above this the caller
#: almost certainly wants a Job or a different tool -- exec is for
#: short, bounded probes, not long-running work.
MAX_TIMEOUT_SECONDS = 300


#: JSON Schema 2020-12 for ``k8s.exec`` ``params``. Validated by the
#: dispatcher before the handler runs; the handler re-reads only
#: validated values.
K8S_EXEC_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pod_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Pod name. Exact match or unique prefix; the handler "
                "resolves prefixes against the namespace's pod list. "
                "Ambiguous prefixes return a structured error listing "
                "the matching pods."
            ),
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Namespace the pod lives in.",
        },
        "command": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": MAX_COMMAND_ARGS,
            "description": (
                "Command as an explicit argv array -- NOT a shell "
                "string. ['ls', '-la', '/etc'] runs ls directly; it is "
                "not executed through a shell, so glob / pipe / "
                "redirection metacharacters are passed literally. To run "
                "a shell pipeline, invoke the shell explicitly: "
                "['sh', '-c', 'ps aux | grep nginx']."
            ),
        },
        "container": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Container name within the pod. Required when the pod "
                "has more than one container; auto-selected when there "
                "is only one."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TIMEOUT_SECONDS,
            "default": DEFAULT_TIMEOUT_SECONDS,
            "description": (
                "Run-to-completion deadline. On expiry the socket is "
                f"closed, partial output is returned, and timed_out is "
                f"set. Defaults to {DEFAULT_TIMEOUT_SECONDS}s; capped at "
                f"{MAX_TIMEOUT_SECONDS}s."
            ),
        },
    },
    "required": ["pod_name", "namespace", "command"],
    "additionalProperties": False,
}


#: Informational response schema for the meta-tools. Descriptive only;
#: the dispatcher's default reducer does not validate outbound payloads
#: against it.
K8S_EXEC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pod": {"type": "string"},
        "namespace": {"type": "string"},
        "container": {"type": "string"},
        "command": {"type": "array", "items": {"type": "string"}},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
        "timed_out": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "stdout_truncated_byte_count": {"type": "integer", "minimum": 0},
        "stderr_truncated_byte_count": {"type": "integer", "minimum": 0},
    },
    "required": [
        "pod",
        "namespace",
        "container",
        "command",
        "stdout",
        "stderr",
        "exit_code",
        "timed_out",
        "truncated",
    ],
    "additionalProperties": False,
}


#: ``llm_instructions`` blob the meta-tools inline into
#: ``describe_operation`` responses.
K8S_EXEC_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Run a short, bounded command inside a running container and "
        "capture its stdout / stderr + exit code -- e.g. 'cat "
        "/etc/nginx/nginx.conf in the web pod', 'check the running "
        "processes', 'read an env var the app sees'. DANGEROUS and "
        "approval-gated: a command can mutate container state. Prefer "
        "the read-only ops (k8s.logs, k8s.pod.info, k8s.configmap.info) "
        "whenever they answer the question. Interactive exec (a live "
        "shell, kubectl exec -it) is NOT available -- this op is "
        "command-and-capture only; the command runs to completion (or "
        "the timeout) and the full output is returned in one response. "
        "Output bytes are never written to the audit log."
    ),
    "parameter_hints": {
        "pod_name": (
            "Exact pod name or unique prefix within the namespace. "
            "Prefer the full name when known (e.g. from a prior "
            "k8s.pod.list)."
        ),
        "namespace": "Required.",
        "command": (
            "Explicit argv array, NOT a shell string. ['ls', '-la'] -- "
            "not 'ls -la'. The command is exec'd directly; for a shell "
            "pipeline use ['sh', '-c', '<pipeline>']."
        ),
        "container": (
            "Required for multi-container pods. The handler returns a "
            "structured error listing the pod's containers when this is "
            "omitted and the pod has more than one."
        ),
        "timeout_seconds": (
            f"Defaults to {DEFAULT_TIMEOUT_SECONDS}; capped at "
            f"{MAX_TIMEOUT_SECONDS}. On expiry the socket is closed and "
            "timed_out=true is set with whatever output arrived."
        ),
    },
    "output_shape": (
        "Flat dict: {'pod', 'namespace', 'container', 'command', "
        "'stdout': <str>, 'stderr': <str>, 'exit_code': <int|null>, "
        "'timed_out': <bool>, 'truncated': <bool>}. exit_code is null "
        "when the command timed out before the status frame arrived. "
        "Each stream is capped at 1 MiB and truncated from the front "
        "when oversize; *_truncated_byte_count records the dropped "
        "bytes."
    ),
}


def truncate_bytes_to_cap(data: bytes, cap_bytes: int) -> tuple[bytes, int]:
    """Keep the most-recent ``cap_bytes`` of ``data``; drop from the front.

    Returns ``(kept, dropped_byte_count)``. Mirrors the front-truncation
    posture of :func:`ops_logs.truncate_lines_to_byte_cap` but operates
    on the raw byte stream (exec output has no line-structure contract).
    """
    if len(data) <= cap_bytes:
        return data, 0
    dropped = len(data) - cap_bytes
    return data[dropped:], dropped


@dataclass
class _ExecCapture:
    """Mutable accumulator for the demuxed exec channels.

    The caller owns the instance and passes it into
    :func:`_drain_exec_socket`. That ownership is load-bearing for the
    timeout path: when :func:`asyncio.wait_for` cancels the drain
    coroutine mid-iteration, the bytes already written here survive the
    cancellation (a tuple returned from the coroutine would be lost),
    so partial output is still surfaced with ``timed_out=true``.
    """

    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    error_frame: str | None = None


async def _drain_exec_socket(
    ws: Any,
    capture: _ExecCapture,
    *,
    cap_bytes: int,
) -> None:
    """Read exec websocket frames until close; demux into ``capture``.

    Each frame's first byte selects the channel; the remainder is the
    payload. Channel 1 = stdout, 2 = stderr, 3 = error (the
    ``v4.channel.k8s.io`` status frame carrying the exit code). Channels
    other than these (e.g. resize) are ignored -- we never request a
    TTY, so they should not appear.

    Stdout / stderr accumulate up to a soft limit (twice ``cap_bytes``)
    each; bytes past that are dropped here so an over-emitting command
    cannot exhaust memory, while the headroom lets the caller report an
    accurate dropped-byte hint. The cap is enforced *within* each frame
    (only the remaining-budget slice of an incoming frame is appended),
    so a single oversized frame cannot push the accumulator past the
    soft limit regardless of frame size. Front-truncation to
    ``cap_bytes`` is applied by the caller.

    Writes into the caller-owned ``capture`` rather than returning a
    value so a mid-drain :func:`asyncio.wait_for` cancellation (timeout)
    preserves whatever output arrived.
    """
    soft_limit = cap_bytes * 2
    async for wsmsg in ws:
        raw = wsmsg.data
        if not isinstance(raw, (bytes, bytearray)):
            # aiohttp surfaces TEXT frames as str; the exec protocol is
            # binary, but decode defensively so a stray text frame does
            # not crash the demux.
            raw = str(raw).encode("utf-8")
        if len(raw) < 1:
            continue
        channel = raw[0]
        payload = raw[1:]
        if not payload:
            continue
        if channel == STDOUT_CHANNEL:
            _append_bounded(capture.stdout, payload, soft_limit)
        elif channel == STDERR_CHANNEL:
            _append_bounded(capture.stderr, payload, soft_limit)
        elif channel == ERROR_CHANNEL:
            capture.error_frame = payload.decode("utf-8", errors="replace")


def _append_bounded(buf: bytearray, payload: bytes, soft_limit: int) -> None:
    """Append only as many ``payload`` bytes as fit under ``soft_limit``.

    Slicing the incoming frame to the remaining budget (rather than
    gating on ``len(buf) < soft_limit`` before an unbounded ``extend``)
    means one oversized frame can add at most ``soft_limit - len(buf)``
    bytes -- the accumulator can never exceed ``soft_limit`` even when a
    single frame is larger than the whole budget.
    """
    remaining = soft_limit - len(buf)
    if remaining > 0:
        buf.extend(payload[:remaining])


def _parse_exit_code(error_frame: str | None) -> int | None:
    """Parse the exit code from the ERROR_CHANNEL status frame.

    The ``v4.channel.k8s.io`` status frame is a serialised
    ``metav1.Status``: ``status == "Success"`` means exit 0;
    a non-zero exit carries the code in
    ``details.causes[0].message``. :meth:`WsApiClient.parse_error_data`
    implements exactly that mapping, so we delegate to it.

    Returns ``None`` when no status frame arrived (the command timed out
    before completion) or the frame is malformed -- the caller surfaces
    ``exit_code=null`` rather than guessing.
    """
    if not error_frame:
        return None
    try:
        return WsApiClient.parse_error_data(error_frame)
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


async def _run_exec_over_ws(
    v1_ws: client.CoreV1Api,
    *,
    pod: str,
    namespace: str,
    command: list[str],
    container: str,
    timeout_seconds: int,
) -> tuple[_ExecCapture, bool]:
    """Open the exec websocket, drain it under a deadline, demux channels.

    Returns ``(capture, timed_out)``. The :class:`_ExecCapture` is
    populated in place so partial output survives a timeout cancellation
    (see :func:`_drain_exec_socket`).

    ``_preload_content=False`` returns the raw aiohttp websocket context
    manager (see module docstring) so we can demux the channels and read
    the ``v4.channel.k8s.io`` status frame ourselves. ``stdin``/``tty``
    are pinned ``False`` -- command-and-capture only.

    The whole socket lifecycle -- the connect/upgrade handshake at
    ``__aenter__`` *and* the frame drain -- runs under a single
    :func:`asyncio.wait_for` deadline, so a stalled connect/upgrade is
    bounded by ``timeout_seconds`` just like a runaway command. On
    timeout the socket (if it opened) is closed explicitly so the
    server-side exec is torn down and the connection is not leaked;
    whatever output arrived is returned.
    """
    capture = _ExecCapture()
    timed_out = False

    # The generated CoreV1Api stub types ``command`` as ``str`` and the
    # return as ``str`` -- both are wrong for the websocket exec path.
    # ``WsApiClient.request`` expands a ``command`` *list* into repeated
    # query params, and with ``_preload_content=False`` the call returns
    # the raw aiohttp websocket context manager (an ``async with``-able),
    # not a decoded string. Cast to ``Any`` so we can drive it.
    ws_ctx: Any = await v1_ws.connect_get_namespaced_pod_exec(
        name=pod,
        namespace=namespace,
        command=command,  # type: ignore[arg-type]  # list expanded by WsApiClient.request
        container=container,
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=False,
    )

    # Hold a reference to the opened socket so the timeout branch can tear
    # it down even when the deadline fires *during* the drain (the
    # ``async with`` block would otherwise have already exited).
    opened_ws: Any = None

    async def _open_and_drain() -> None:
        nonlocal opened_ws
        async with ws_ctx as ws:
            opened_ws = ws
            await _drain_exec_socket(ws, capture, cap_bytes=MAX_STREAM_BYTES)

    try:
        # A single budget covers the connect/upgrade handshake performed
        # at ``__aenter__`` and the frame drain that follows, so a
        # connection-upgrade stall is bounded, not just a hung command.
        await asyncio.wait_for(_open_and_drain(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        if opened_ws is not None:
            await opened_ws.close()

    return capture, timed_out


async def k8s_exec(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.exec``.

    Module-level free function so a future migration to per-op handler
    files keeps the API stable; the bound-method shim on
    :class:`KubernetesConnector` (``exec_command``) delegates here.
    Schema validation runs in the dispatcher, so ``params`` arrives
    pre-checked against :data:`K8S_EXEC_PARAMETER_SCHEMA`.

    Pod / container resolution reuses
    :func:`ops_logs.resolve_pod_and_container` (the read client suffices
    for the resolve round-trip -- only the exec itself needs the
    websocket transport). The exec then runs over the per-target
    :class:`~kubernetes_asyncio.stream.WsApiClient` from
    :meth:`KubernetesConnector._get_ws_api_client`.

    ``stdin`` and ``tty`` are pinned ``False`` -- this is
    command-and-capture only; interactive exec is out of scope (see the
    module docstring).

    Raises propagate to the dispatcher's ``connector_error`` branch:

    * :class:`ops_logs.PodNotFoundError` -- prefix matched zero/many pods.
    * :class:`ops_logs.MultiContainerAmbiguityError` -- multi-container
      pod missing ``--container``.
    * :class:`kubernetes_asyncio.client.ApiException` -- API errors
      (RBAC denial, pod not running, container not found).
    """
    pod_name = params["pod_name"]
    namespace = params["namespace"]
    command: list[str] = list(params["command"])
    container_param: str | None = params.get("container")
    timeout_seconds = int(params.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if timeout_seconds > MAX_TIMEOUT_SECONDS:
        # Defence in depth; the schema's ``maximum`` already enforces it.
        timeout_seconds = MAX_TIMEOUT_SECONDS

    # Resolve the pod + container via the cached read client -- the
    # resolve round-trip is an ordinary REST list, not a websocket exec.
    read_client = await connector._get_api_client(target, operator)
    v1_read = client.CoreV1Api(read_client)
    exact_name, container_name = await resolve_pod_and_container(
        v1_read, namespace, pod_name, container_param
    )

    ws_client = await connector._get_ws_api_client(target, operator)
    v1_ws = client.CoreV1Api(ws_client)

    capture, timed_out = await _run_exec_over_ws(
        v1_ws,
        pod=exact_name,
        namespace=namespace,
        command=command,
        container=container_name,
        timeout_seconds=timeout_seconds,
    )

    stdout_bytes, stdout_dropped = truncate_bytes_to_cap(bytes(capture.stdout), MAX_STREAM_BYTES)
    stderr_bytes, stderr_dropped = truncate_bytes_to_cap(bytes(capture.stderr), MAX_STREAM_BYTES)
    truncated = stdout_dropped > 0 or stderr_dropped > 0

    exit_code = None if timed_out else _parse_exit_code(capture.error_frame)

    result: dict[str, Any] = {
        "pod": exact_name,
        "namespace": namespace,
        "container": container_name,
        "command": command,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": truncated,
    }
    if truncated:
        result["stdout_truncated_byte_count"] = stdout_dropped
        result["stderr_truncated_byte_count"] = stderr_dropped
    return result
