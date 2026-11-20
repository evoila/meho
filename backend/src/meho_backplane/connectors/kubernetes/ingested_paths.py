# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Kubernetes API paths that make an ingested operation a credential call.

An operator can ingest a Kubernetes API file as a generic connector. Each
operation then has the op id ``<METHOD>:<path>``, for example
``POST:/api/v1/namespaces/{namespace}/pods/{name}/exec``. These operations
get the same rule as ``k8s.exec`` and as a ``k8s.apply`` that waits
(:mod:`~meho_backplane.connectors.kubernetes.op_classes`): a person approves
each call, and no standing grant, no agent grant and no run approval covers
it (:func:`~meho_backplane.op_classes.declare_credential_capable_match`).

* Every method: the Secrets (read, list, watch and write), a command in a
  pod (``exec``, ``attach``), a port forward and a proxy to a pod, a service
  or a node, and a new ServiceAccount token.
* A write (``POST``, ``PUT``, ``PATCH``): a pod or an object that runs one,
  an ephemeral container, a role or a binding, an object that makes or
  reads a Secret (External Secrets, Sealed Secrets, cert-manager) and a
  certificate signing request.

A path segment in braces or with a value counts the same, so the names of
the path params in the API file do not matter. A ``/status`` or ``/scale``
subresource of a workload keeps its rules: it does not change what runs.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.op_classes import declare_credential_capable_match

__all__ = ["ingested_op_is_credential_call"]

_SEG = r"[^/]+"
_NS = rf"(?:/namespaces/{_SEG})?"
_GROUP = rf"/apis/{_SEG}/{_SEG}"

#: Paths that make a call of every method a credential call.
_ANY_METHOD: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(rf"/api/v1(?:/watch)?{_NS}/secrets(?:/{_SEG})?"),
    re.compile(rf"/api/v1/namespaces/{_SEG}/pods/{_SEG}/(?:exec|attach|portforward|proxy)(?:/.*)?"),
    re.compile(rf"/api/v1{_NS}/(?:pods|services|nodes)/{_SEG}/proxy(?:/.*)?"),
    re.compile(rf"/api/v1/namespaces/{_SEG}/serviceaccounts/{_SEG}/token"),
)

#: Paths that make a write a credential call.
_WRITE: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        rf"/api/v1/namespaces/{_SEG}/(?:pods|podtemplates|replicationcontrollers)(?:/{_SEG})?"
    ),
    re.compile(rf"/api/v1/namespaces/{_SEG}/pods/{_SEG}/ephemeralcontainers"),
    re.compile(
        rf"/apis/(?:apps|batch|extensions)/{_SEG}/namespaces/{_SEG}/"
        rf"(?:cronjobs|daemonsets|deployments|jobs|replicasets|statefulsets)(?:/{_SEG})?"
    ),
    re.compile(
        rf"/apis/rbac\.authorization\.k8s\.io/{_SEG}{_NS}/"
        rf"(?:clusterrolebindings|clusterroles|rolebindings|roles)(?:/{_SEG})?"
    ),
    re.compile(
        rf"{_GROUP}{_NS}/(?:certificates|clusterexternalsecrets|clustersecretstores|"
        rf"externalsecrets|pushsecrets|sealedsecrets|secretstores)(?:/{_SEG})?"
    ),
    re.compile(
        rf"/apis/certificates\.k8s\.io/{_SEG}/certificatesigningrequests"
        rf"(?:/{_SEG}(?:/approval)?)?"
    ),
)

_WRITE_METHODS: Final[frozenset[str]] = frozenset({"PATCH", "POST", "PUT"})


def ingested_op_is_credential_call(op_id: str) -> bool:
    """True when the ingested op id *op_id* is a Kubernetes credential call.

    *op_id* is ``<METHOD>:<path>``. An op id without a ``:`` is not an
    ingested operation and does not count. A trailing ``/`` counts the same
    as none.
    """
    method, sep, path = op_id.partition(":")
    if not sep or not path.startswith("/"):
        return False
    path = path.rstrip("/") or "/"
    if any(pattern.fullmatch(path) for pattern in _ANY_METHOD):
        return True
    return method.upper() in _WRITE_METHODS and any(pattern.fullmatch(path) for pattern in _WRITE)


declare_credential_capable_match(ingested_op_is_credential_call)
