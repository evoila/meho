# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Paths that look like Kubernetes API paths without the usual start.

MEHO reads a Kubernetes call of an ingested API file by the start of its
path: ``/api/v1`` or ``/apis/<group>/<version>``
(:mod:`~meho_backplane.connectors.kubernetes.ingested_paths`).
:func:`full_kubernetes_path` is the one place that knows the paths that
look like Kubernetes paths without that start, and the full path each one
stands for. Three rules use it:

* the parser refuses an API file with such a path
  (:func:`refuse_short_kubernetes_paths`, registered with
  :func:`~meho_backplane.operations.ingest.openapi.register_spec_path_refusal`);
* a stored operation with such a path gets the answer of the same call on
  the full path (``ingested_op_is_credential_call``);
* MEHO writes a warning at start for each connector with such operations
  (:mod:`~meho_backplane.connectors.kubernetes.short_path_warning`).

The shapes, where ``{x}`` is one path segment:

* A, matched from any ``/`` of the path: ``/namespaces/{x}/secrets``
  (also with ``/{x}`` after it, and with ``/watch`` in front), a pod's
  ``exec``, ``attach``, ``portforward`` or ``proxy``, and a ServiceAccount
  ``token``.
* B, matched only at the start of the path or after one leading ``/v1``:
  ``/namespaces/{x}/<core plural>`` and
  ``/<group>/<version>/namespaces/{x}/{x}``, where the group is ``apps``,
  ``batch``, ``autoscaling``, ``policy`` or a name that ends in
  ``.k8s.io``.

Cluster-wide paths (``/nodes/{x}``), other plurals under ``/namespaces/{x}``
and custom API groups do not match: APIs of other products use them too.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from meho_backplane.operations.ingest.openapi import register_spec_path_refusal

__all__ = ["full_kubernetes_path", "refuse_short_kubernetes_paths"]

_SEG = r"[^/]+"

#: The full start, read from any ``/`` in front of the Kubernetes part.
_FULL_START: Final[re.Pattern[str]] = re.compile(rf"/api/v1(?:/.*)?|/apis/{_SEG}/{_SEG}/.*")

#: Shape A: matched from any ``/`` of the path.
_SHAPE_A: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(rf"(?:/watch)?/namespaces/{_SEG}/secrets(?:/{_SEG})?"),
    re.compile(rf"/namespaces/{_SEG}/pods/{_SEG}/(?:exec|attach|portforward|proxy)(?:/.*)?"),
    re.compile(rf"/namespaces/{_SEG}/serviceaccounts/{_SEG}/token"),
)

_CORE_PLURALS = (
    "pods|services|configmaps|secrets|serviceaccounts|persistentvolumeclaims|endpoints"
    "|events|limitranges|resourcequotas|replicationcontrollers|podtemplates|bindings"
)

#: Shape B with a core plural, after the leading ``/v1`` is dropped.
_SHAPE_B_CORE: Final[re.Pattern[str]] = re.compile(
    rf"/namespaces/{_SEG}/(?:{_CORE_PLURALS})(?:/.*)?"
)

#: Shape B with a built-in group, after the leading ``/v1`` is dropped.
_SHAPE_B_GROUP: Final[re.Pattern[str]] = re.compile(
    rf"/(?:apps|batch|autoscaling|policy|[^/]+\.k8s\.io)/v[0-9]+(?:(?:alpha|beta)[0-9]+)?"
    rf"/namespaces/{_SEG}/{_SEG}(?:/.*)?"
)

#: How many matched paths the refusal text names.
_SHOWN_PATHS: Final[int] = 3


def _tails(path: str) -> list[str]:
    """*path* and each part of it that starts at a later ``/``."""
    return [path[index:] for index, char in enumerate(path) if char == "/"]


def full_kubernetes_path(path: str) -> str | None:
    """Return the full Kubernetes path that the short *path* stands for.

    Returns ``None`` when the full start comes before the Kubernetes part
    of *path*, or when *path* looks like no Kubernetes path. A full start
    after that part, for example in the rest of a pod ``proxy`` path, does
    not count. A trailing ``/`` counts the same as none. Examples:
    ``/x/namespaces/{n}/secrets`` gives
    ``/api/v1/namespaces/{n}/secrets``, ``/v1/namespaces/{n}/pods`` gives
    ``/api/v1/namespaces/{n}/pods`` and
    ``/apps/v1/namespaces/{n}/deployments`` gives
    ``/apis/apps/v1/namespaces/{n}/deployments``.
    """
    path = path.rstrip("/")
    if not path.startswith("/"):
        return None
    start = path.removeprefix("/v1") if path.startswith("/v1/") else path
    if _SHAPE_B_CORE.fullmatch(start):
        return "/api/v1" + start
    if _SHAPE_B_GROUP.fullmatch(start):
        return "/apis" + start
    for tail in _tails(path):
        if _FULL_START.fullmatch(tail):
            return None
        if any(pattern.fullmatch(tail) for pattern in _SHAPE_A):
            return "/api/v1" + tail
    return None


def refuse_short_kubernetes_paths(paths: Sequence[str]) -> str | None:
    """Return the refusal text when one of *paths* is a short Kubernetes path."""
    matched = [path for path in paths if full_kubernetes_path(path) is not None]
    if not matched:
        return None
    examples = ", ".join(matched[:_SHOWN_PATHS])
    return (
        "MEHO does not load this API file. Some of its paths look like Kubernetes "
        "paths without the usual start (/api/v1 or /apis/<group>/<version>), for "
        f"example: {examples}. MEHO checks Kubernetes calls by that start, so it "
        "could not check these calls. Use the standard API file that the cluster "
        "serves: it has full paths. If the start is in the file's server address, "
        "write it there as a relative path (for example /api/v1) and put the host "
        "in the target address. MEHO then adds the start to each path."
    )


register_spec_path_refusal(refuse_short_kubernetes_paths)
