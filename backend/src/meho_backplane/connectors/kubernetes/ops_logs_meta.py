# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``k8s.logs`` row: a non-streaming pod-log fetch (G3.2-T5 #325).

The schemas and the handler live in
:mod:`~meho_backplane.connectors.kubernetes.ops_logs`. The row lives here
because that module is already at the code-quality size cap.
:func:`~meho_backplane.connectors.kubernetes.ops.collect_ops` reads
:data:`OPS` at start-up.
"""

from __future__ import annotations

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_logs import (
    K8S_LOGS_LLM_INSTRUCTIONS,
    K8S_LOGS_PARAMETER_SCHEMA,
    K8S_LOGS_RESPONSE_SCHEMA,
)

__all__ = ["LOGS_OP", "OPS"]

LOGS_OP = KubernetesOp(
    op_id="k8s.logs",
    handler_attr="logs",
    summary="Fetch a chunk of pod logs as a single non-streaming response.",
    description=(
        "Reads container stdout/stderr via the Kubernetes API's "
        "``GET /api/v1/namespaces/{namespace}/pods/{name}/log`` route "
        "and returns the lines in one response. Honours the standard "
        "kubectl-style knobs -- ``--tail`` (default 100, capped at "
        "5000), ``--container`` (required for multi-container pods; "
        "an init container name is accepted too), "
        "``--since`` (duration string -- '5m', '1h', '24h', '7d'), "
        "and ``--previous`` (logs from the prior container "
        "instance after a restart). Pod name resolution accepts an "
        "exact match or a unique prefix within the namespace. The "
        "response body is capped at 1 MiB serialised; oversize "
        "payloads are truncated line-boundary from the front (most-"
        "recent lines kept) and ``truncated=true`` is set on the "
        "result with ``truncated_byte_count`` carrying the dropped "
        "byte count. Streaming (``kubectl logs -f``) is out of "
        "scope for v0.2 -- operators following live logs continue "
        "using the kubectl-vcf.sh fallback."
    ),
    parameter_schema=K8S_LOGS_PARAMETER_SCHEMA,
    response_schema=K8S_LOGS_RESPONSE_SCHEMA,
    group_key="logs",
    tags=("read-only", "pod", "logs"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=K8S_LOGS_LLM_INSTRUCTIONS,
)


#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = (LOGS_OP,)
