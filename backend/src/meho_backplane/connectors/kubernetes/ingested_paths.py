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
* A write (``POST``, ``PUT``, ``PATCH``) of an object that is not one of
  the plain objects of ``k8s.apply`` that need no check of their body
  (:data:`~meho_backplane.connectors.kubernetes.op_classes.PLAIN_PLURALS`),
  read by API group and plural: a pod or a workload, a role or a binding, a
  webhook, an Ingress, a custom resource and every other kind. Also an
  ephemeral container and the approval of a certificate signing request.

A path segment in braces or with a value counts the same, so the names of
the path params in the API file do not matter. A path can have any prefix
in front, for example the base path of a proxy: each rule is read from
every ``/`` of the path on. A ``/status`` or ``/scale`` subresource keeps
its rules: it does not change what runs. A write of a cluster-wide core
object (``/api/v1/<plural>``: a node, a namespace, a volume) keeps its
rules too: such an object runs no pod by itself.
"""

from __future__ import annotations

import re
from typing import Final

from meho_backplane.connectors.kubernetes.op_classes import PLAIN_PLURALS
from meho_backplane.op_classes import declare_credential_capable_match

__all__ = ["ingested_op_is_credential_call"]

_SEG = r"[^/]+"
_NS = rf"(?:/namespaces/{_SEG})?"

#: Paths that make a call of every method a credential call.
_ANY_METHOD: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(rf"/api/v1(?:/watch)?{_NS}/secrets(?:/{_SEG})?"),
    re.compile(rf"/api/v1/namespaces/{_SEG}/pods/{_SEG}/(?:exec|attach|portforward|proxy)(?:/.*)?"),
    re.compile(rf"/api/v1{_NS}/(?:pods|services|nodes)/{_SEG}/proxy(?:/.*)?"),
    re.compile(rf"/api/v1/namespaces/{_SEG}/serviceaccounts/{_SEG}/token"),
)

#: Write subresources that make a write a credential call.
_WRITE_SUBRESOURCES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(rf"/api/v1/namespaces/{_SEG}/pods/{_SEG}/ephemeralcontainers"),
    re.compile(rf"/apis/certificates\.k8s\.io/{_SEG}/certificatesigningrequests/{_SEG}/approval"),
)

#: The ``status`` and ``finalize`` subresources of a Namespace (plain).
_NAMESPACE_SUBRESOURCE: Final[re.Pattern[str]] = re.compile(
    rf"/api/v1/namespaces/{_SEG}/(?:status|finalize)"
)

#: A namespaced core object or a list of them.
_CORE_OBJECT: Final[re.Pattern[str]] = re.compile(
    rf"/api/v1/namespaces/{_SEG}/(?P<plural>{_SEG})(?:/{_SEG})?"
)

#: An object of an API group, namespaced or cluster-wide, or a list of them.
_GROUP_OBJECT: Final[re.Pattern[str]] = re.compile(
    rf"/apis/(?P<group>{_SEG})/{_SEG}{_NS}/(?P<plural>{_SEG})(?:/{_SEG})?"
)

_WRITE_METHODS: Final[frozenset[str]] = frozenset({"PATCH", "POST", "PUT"})


def _tails(path: str) -> list[str]:
    """*path* and each part of it that starts at a later ``/``."""
    return [path[index:] for index, char in enumerate(path) if char == "/"]


def _write_waits(tail: str) -> bool:
    """True when a write to the path *tail* is a credential call."""
    if any(pattern.fullmatch(tail) for pattern in _WRITE_SUBRESOURCES):
        return True
    if _NAMESPACE_SUBRESOURCE.fullmatch(tail):
        return False
    core = _CORE_OBJECT.fullmatch(tail)
    if core is not None:
        return ("", core["plural"]) not in PLAIN_PLURALS
    grouped = _GROUP_OBJECT.fullmatch(tail)
    if grouped is not None:
        return (grouped["group"], grouped["plural"]) not in PLAIN_PLURALS
    return False


def ingested_op_is_credential_call(op_id: str) -> bool:
    """True when the ingested op id *op_id* is a Kubernetes credential call.

    *op_id* is ``<METHOD>:<path>``. An op id without a ``:`` is not an
    ingested operation and does not count. A trailing ``/`` counts the same
    as none. Each rule is read from every ``/`` of the path on.
    """
    method, sep, path = op_id.partition(":")
    if not sep or not path.startswith("/"):
        return False
    tails = _tails(path.rstrip("/") or "/")
    if any(pattern.fullmatch(tail) for tail in tails for pattern in _ANY_METHOD):
        return True
    return method.upper() in _WRITE_METHODS and any(_write_waits(tail) for tail in tails)


declare_credential_capable_match(ingested_op_is_credential_call)
