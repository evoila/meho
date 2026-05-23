# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.logs`` op -- non-streaming pod log fetch with body-size cap.

The op is the v0.2 K8s connector's answer to ``kubectl logs <pod>``: a
single-response, chunked fetch backed by
:meth:`kubernetes_asyncio.client.CoreV1Api.read_namespaced_pod_log`.
Streaming (``kubectl logs -f``) is out of scope -- the connector
returns a flat dict the dispatcher wraps into one
:class:`~meho_backplane.connectors.schemas.OperationResult`. v0.2.next
adds the streaming transport once MCP's ``tools/call`` envelope grows a
streaming shape.

The handler resolves prefix-style pod names (the same shape G3.2-T3
will introduce on ``k8s.pod.info``) before issuing the log fetch. The
resolver currently does the dumb thing -- ``list_namespaced_pod`` +
client-side filter; the server-side field selector route lands when
G3.2-T3 ships its production-grade resolver.

Multi-container pods without ``--container`` short-circuit to a
structured error carrying the available container names so the caller
can retry with an explicit container. Single-container pods auto-pick
the only container.

The 1MB body cap is applied client-side. The k8s API exposes
``limit_bytes`` for a server-side cap, but it terminates the response
mid-line (the k8s docs explicitly say "may return slightly more or
slightly less than the specified limit"). v0.2 wants line-boundary
truncation -- we fetch the raw body, count UTF-8 bytes line by line
from the tail, and drop the leading lines that push the response past
1 MiB. ``truncated_byte_count`` in extras records how much was dropped
so the operator can decide whether to retry with a smaller ``--tail``
or a tighter ``--since``.

References
----------
* Parent task: G3.2-T5 (#325).
* Parent Initiative: G3.2 (#320), kubernetes-asyncio typed connector.
* k8s API: https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.32/#read-log-pod-v1-core
* ``kubernetes_asyncio.CoreV1Api.read_namespaced_pod_log``:
  https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/CoreV1Api.md
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "K8S_LOGS_LLM_INSTRUCTIONS",
    "K8S_LOGS_PARAMETER_SCHEMA",
    "K8S_LOGS_RESPONSE_SCHEMA",
    "MAX_BODY_BYTES",
    "MAX_TAIL_LINES",
    "MultiContainerAmbiguityError",
    "PodNotFoundError",
    "k8s_logs",
    "parse_duration",
    "resolve_pod_and_container",
    "truncate_lines_to_byte_cap",
]

#: Hard cap on ``--tail``. The k8s API itself has no upper bound, but
#: 5000 lines is the operator-facing ergonomics cap -- above this the
#: caller almost certainly wants ``--since`` instead, and pulling more
#: than 5000 lines blows past the 1 MiB body cap for any non-trivial
#: log line length.
MAX_TAIL_LINES = 5000

#: 1 MiB serialised body cap. The dispatcher's default
#: :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
#: materializes the returned ``lines`` collection into a
#: :class:`~meho_backplane.connectors.schemas.ResultHandle` once it
#: crosses the threshold; capping the body keeps reduction overhead
#: bounded and the payload below the audit row's display size. 1 MiB
#: matches the operator's typical
#: terminal scrollback and the MCP envelope's practical payload
#: ceiling (no hard limit, but >1 MB choices stall clients).
MAX_BODY_BYTES = 1024 * 1024


#: JSON Schema 2020-12 for ``k8s.logs`` ``params``. The dispatcher
#: validates against this before the handler runs; the handler then
#: only re-reads validated values.
K8S_LOGS_PARAMETER_SCHEMA: dict[str, Any] = {
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
        "container": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Container name within the pod. Required when the pod "
                "has more than one container; auto-selected when there "
                "is only one."
            ),
        },
        "tail": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TAIL_LINES,
            "default": 100,
            "description": (
                "Lines from the end of the log. Capped at "
                f"{MAX_TAIL_LINES}; pass --since for time-bounded slices "
                "instead of larger tails."
            ),
        },
        "since": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^\s*\d+\s*[smhd]\s*$",
            "description": (
                "Duration string -- '5m', '1h', '24h', '7d'. Resolves "
                "to ``since_seconds`` on the wire. Server-side filter; "
                "logs older than the cutoff are dropped before transit."
            ),
        },
        "previous": {
            "type": "boolean",
            "default": False,
            "description": (
                "Fetch logs from the previous container instance (i.e. "
                "before the most-recent restart). Maps to the API's "
                "``previous=true`` flag."
            ),
        },
    },
    "required": ["pod_name", "namespace"],
    "additionalProperties": False,
}


#: Informational response schema for the meta-tools. The dispatcher's
#: default reducer does not validate outbound payloads against this
#: schema; it is descriptive metadata for ``describe_operation``.
K8S_LOGS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pod": {"type": "string"},
        "namespace": {"type": "string"},
        "container": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "string"}},
        "truncated": {"type": "boolean"},
        "line_count": {"type": "integer", "minimum": 0},
        "byte_count": {"type": "integer", "minimum": 0},
        "truncated_byte_count": {"type": "integer", "minimum": 0},
    },
    "required": ["pod", "namespace", "container", "lines", "truncated"],
    "additionalProperties": False,
}


#: ``llm_instructions`` blob the meta-tools (G0.6-T8 #399) inline into
#: ``describe_operation`` responses. Same discipline G3.3 will follow
#: for the full Vault op surface: when-to-use prose + parameter hints +
#: output-shape sketch.
K8S_LOGS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Fetch a chunk of pod logs as a single response. Use when the "
        "operator names a specific pod and wants its recent log lines "
        "(e.g. 'show me the last 200 lines from argocd-server'). "
        "Non-streaming -- for live tailing, the operator should keep "
        "using kubectl-vcf.sh -f until v0.2.next ships the streaming "
        "transport. Read-only; the log content is never written to "
        "the audit_log payload (which records only the request "
        "params, not the bytes returned)."
    ),
    "parameter_hints": {
        "pod_name": (
            "Exact pod name or unique prefix within the namespace. "
            "Prefer the full name when the agent already knows it "
            "(e.g. after a prior k8s.pod.list); fall back to a prefix "
            "for ad-hoc operator-driven queries."
        ),
        "namespace": "Required.",
        "container": (
            "Required for multi-container pods. The handler returns a "
            "structured error listing the pod's containers when this "
            "is omitted and the pod has more than one."
        ),
        "tail": (
            f"Defaults to 100; capped at {MAX_TAIL_LINES}. Prefer "
            "'since' over very large tails -- the response is capped "
            f"at {MAX_BODY_BYTES // (1024 * 1024)} MiB and oversize "
            "responses truncate from the front."
        ),
        "since": (
            "Duration string (e.g. '5m', '1h', '24h', '7d'). Server-"
            "side filter. Combine with a moderate tail for time-"
            "bounded slices."
        ),
        "previous": (
            "Set true to fetch the previous container instance's logs "
            "(after a crash + restart). Defaults to false."
        ),
    },
    "output_shape": (
        "Flat dict: {'pod': <resolved-name>, 'namespace', 'container', "
        "'lines': [<str>], 'truncated': <bool>}. When truncated is "
        "true, extras carries 'line_count', 'byte_count', and "
        "'truncated_byte_count' so callers can render an 'X KiB "
        "dropped from the front' hint."
    ),
}


#: Duration suffix to seconds multiplier. The schema's regex
#: (``\d+[smhd]``) constrains the suffix to one of these characters;
#: this table is the parser's lookup.
_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


def parse_duration(value: str | None) -> int | None:
    """Parse a duration string into seconds; ``None`` passes through.

    Accepts the same shape as the schema's ``pattern`` -- a positive
    integer followed by ``s`` / ``m`` / ``h`` / ``d``. Whitespace at
    the edges is tolerated; the schema already accepts it so the
    parser must too. Returns ``None`` for ``None`` input so the
    handler can splat the result into ``since_seconds`` unconditionally
    (``kubernetes_asyncio`` ignores ``None`` kwargs).

    Raises :class:`ValueError` for any string the regex doesn't match
    -- the schema rejects malformed input before the handler runs, so
    a raise here means schema/parser drift and should fail loud.
    """
    if value is None:
        return None
    match = _DURATION_RE.match(value)
    if match is None:
        # Schema-enforced unreachable in production; raise so the
        # dispatcher's connector_error envelope surfaces the bug if a
        # schema relaxation lets a bad value through.
        raise ValueError(f"invalid duration: {value!r}")
    quantity = int(match.group(1))
    unit = match.group(2)
    return quantity * _DURATION_UNITS[unit]


class PodNotFoundError(LookupError):
    """Pod prefix matched zero or many pods.

    Distinct from a generic ``ApiException`` so the dispatcher's
    ``connector_error`` envelope carries
    ``extras.exception_class="PodNotFoundError"`` and callers can
    render a "did-you-mean" hint listing the resolved candidates (if
    any) from ``args[1]``.
    """

    def __init__(self, message: str, candidates: list[str] | None = None) -> None:
        super().__init__(message, candidates or [])

    @property
    def candidates(self) -> list[str]:
        return list(self.args[1])


class MultiContainerAmbiguityError(ValueError):
    """Pod has multiple containers and the caller didn't pick one.

    The structured response includes the container list in
    ``args[1]`` so the dispatcher's ``connector_error`` envelope can
    surface them to the operator without a second round-trip.
    """

    def __init__(self, pod: str, containers: list[str]) -> None:
        super().__init__(
            f"pod {pod!r} has multiple containers; specify --container",
            containers,
        )

    @property
    def containers(self) -> list[str]:
        return list(self.args[1])


async def resolve_pod_and_container(
    v1: client.CoreV1Api,
    namespace: str,
    pod_name: str,
    container: str | None,
) -> tuple[str, str]:
    """Resolve ``pod_name`` prefix + ``container`` against the cluster.

    Returns the exact pod name and the container name to pass to
    :meth:`read_namespaced_pod_log`. Raises:

    * :class:`PodNotFoundError` -- zero or multiple pods match the
      prefix. ``candidates`` carries the matched names so the caller
      can surface a "did-you-mean".
    * :class:`MultiContainerAmbiguityError` -- pod has multiple
      containers and ``container`` is ``None``. ``containers`` carries
      the candidate list.
    * :class:`kubernetes_asyncio.client.ApiException` -- API-side
      errors (RBAC denial, namespace missing). Propagated unchanged.

    Implementation note: ``list_namespaced_pod`` returns every pod in
    the namespace. Once G3.2-T3 (#323) ships a paginated resolver
    against ``read_namespaced_pod`` + ``field_selector``, switch to
    that path -- it's strictly faster and avoids the full-namespace
    list when the caller already has the exact name.
    """
    pod_list = await v1.list_namespaced_pod(namespace=namespace)
    matches: list[Any] = []
    for pod in pod_list.items:
        if pod.metadata.name == pod_name:
            matches = [pod]
            break
        if pod.metadata.name.startswith(pod_name):
            matches.append(pod)

    if not matches:
        raise PodNotFoundError(f"no pod in namespace {namespace!r} matches {pod_name!r}")
    if len(matches) > 1:
        names = sorted(p.metadata.name for p in matches)
        raise PodNotFoundError(
            f"pod prefix {pod_name!r} in namespace {namespace!r} matched {len(names)} pods",
            names,
        )

    pod = matches[0]
    container_names: list[str] = [c.name for c in (pod.spec.containers or [])]
    if container is not None:
        if container not in container_names:
            raise MultiContainerAmbiguityError(pod.metadata.name, container_names)
        return pod.metadata.name, container

    if len(container_names) == 1:
        return pod.metadata.name, container_names[0]
    raise MultiContainerAmbiguityError(pod.metadata.name, container_names)


def truncate_lines_to_byte_cap(lines: list[str], cap_bytes: int) -> tuple[list[str], int]:
    """Keep the most-recent lines that fit in ``cap_bytes`` UTF-8 bytes.

    Returns ``(kept_lines, dropped_byte_count)``. The cap accounts for
    the joining newline between lines (one byte per join, no trailing
    newline) so the consumer's serialised view matches the byte
    budget. When even the last line by itself exceeds ``cap_bytes``,
    the function returns it alone without slicing inside the line --
    line-boundary truncation is the contract.
    """
    if not lines:
        return [], 0

    kept_reversed: list[str] = []
    running_bytes = 0
    # Iterate tail-first: the most-recent lines are at the end of the
    # source list, and the body cap evicts the oldest lines first.
    for line in reversed(lines):
        line_bytes = len(line.encode("utf-8"))
        join_byte = 1 if kept_reversed else 0
        next_bytes = running_bytes + line_bytes + join_byte
        if next_bytes > cap_bytes and kept_reversed:
            break
        kept_reversed.append(line)
        running_bytes = next_bytes

    kept = list(reversed(kept_reversed))
    dropped = sum(len(line.encode("utf-8")) for line in lines[: len(lines) - len(kept)])
    # Account for the joining newlines we conceptually dropped along
    # with the lines themselves (one per dropped line that was joined
    # to something).
    if len(lines) - len(kept) > 0:
        dropped += len(lines) - len(kept)
    return kept, dropped


async def k8s_logs(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.logs``.

    Module-level free function so a future migration to per-op
    handler files keeps the API stable; the bound-method shim on
    :class:`KubernetesConnector` (``logs``) delegates here. Schema
    validation runs in the dispatcher, so ``params`` arrives
    pre-checked against :data:`K8S_LOGS_PARAMETER_SCHEMA` -- the
    handler only re-reads validated values.

    ``operator`` is forwarded to
    :meth:`KubernetesConnector._get_api_client` so a cold-cache
    kubeconfig load runs under the operator's identity.

    Raises propagate to the dispatcher's ``connector_error`` branch:

    * :class:`PodNotFoundError` -- prefix matched zero or many pods.
    * :class:`MultiContainerAmbiguityError` -- multi-container pod
      missing ``--container``.
    * :class:`kubernetes_asyncio.client.ApiException` -- API errors
      (RBAC, missing namespace, container not running yet for
      ``previous=true``).
    """
    pod_name = params["pod_name"]
    namespace = params["namespace"]
    # ``tail`` is schema-defaulted to 100 but the dispatcher passes
    # raw ``params`` through without injecting defaults -- the
    # ``Draft202012Validator`` validates only, it does not coerce.
    tail = int(params.get("tail", 100))
    if tail > MAX_TAIL_LINES:
        # Defence in depth: the schema's ``maximum`` already enforces
        # this. Keep the explicit clamp so a future schema relaxation
        # cannot exceed the cap silently.
        tail = MAX_TAIL_LINES
    container_param: str | None = params.get("container")
    since_seconds = parse_duration(params.get("since"))
    previous = bool(params.get("previous", False))

    api_client = await connector._get_api_client(target, operator)
    v1 = client.CoreV1Api(api_client)

    exact_name, container_name = await resolve_pod_and_container(
        v1, namespace, pod_name, container_param
    )

    # ``kubernetes_asyncio`` accepts every parameter as a kwarg; pass
    # only the ones with non-default values so the wire request stays
    # minimal and the server doesn't have to short-circuit None
    # filters. ``timestamps=False`` is the API default but log lines
    # often carry their own timestamp via the application -- exposing
    # the API's RFC3339 prefix is a v0.2.next ergonomics knob.
    kwargs: dict[str, Any] = {
        "name": exact_name,
        "namespace": namespace,
        "container": container_name,
        "tail_lines": tail,
        "previous": previous,
    }
    if since_seconds is not None:
        kwargs["since_seconds"] = since_seconds

    body: str = await v1.read_namespaced_pod_log(**kwargs)

    # ``read_namespaced_pod_log`` returns the body as a single string;
    # an empty cluster log returns "" (no trailing newline). Split on
    # \n and drop the trailing empty token the split produces when
    # the body ends with \n (the common case -- container stdout
    # always terminates the last line).
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    byte_count = len(body.encode("utf-8"))
    truncated = False
    truncated_byte_count = 0
    if byte_count > MAX_BODY_BYTES:
        lines, truncated_byte_count = truncate_lines_to_byte_cap(lines, MAX_BODY_BYTES)
        truncated = True

    result: dict[str, Any] = {
        "pod": exact_name,
        "namespace": namespace,
        "container": container_name,
        "lines": lines,
        "truncated": truncated,
    }
    if truncated:
        result["line_count"] = len(lines)
        result["byte_count"] = byte_count
        result["truncated_byte_count"] = truncated_byte_count
    return result
