# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Kubernetes connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

import yaml

from meho_backplane.op_classes import (
    declare_credential_capable,
    declare_credential_check,
    declare_hidden_stored_param,
    declare_no_secret_value,
    declare_op_classes,
)

declare_op_classes(
    credential_read=(
        # The governed guest-cluster kubeconfig read (#3496). Reads a Secret's
        # data value and stages it to a tenant-scoped Vault secret_ref,
        # returning only the ref, never the value. The class makes audit and
        # broadcast collapse to aggregate-only on top of its approval gate.
        "k8s.secret.read_to_ref",
        # #5230 -- the compare read. The backplane reads the secret values
        # itself to compare them and returns only same/different. The class
        # makes the flight recorder drop the vendor bodies (they carry the
        # values) and collapses the broadcast to aggregate-only.
        "k8s.secret.compare_to_ref",
    ),
    credential_write=(
        # The Secret ``data`` / ``stringData`` is in ``params``.
        "k8s.secret.create",
        # The values of an existing Secret ride in the same params shape as
        # k8s.secret.create (inline maps or store refs), so the same class
        # keeps them off the approval row and the broadcast.
        "k8s.secret.update",
        # The Job ``spec`` carries a pod template whose inline ``env`` entries
        # can hold credential material in ``params`` (G3.14-T1 #1403).
        "k8s.job.create",
    ),
)

# The compare read returns only "same" or "different", never a value. So it
# runs without waiting for approval (meho_backplane.operations.credential_read).
declare_no_secret_value(("k8s.secret.compare_to_ref",))

# A command in a container can read or write any credential the container
# holds. So every call waits for a person, and no standing grant, agent
# grant or run approval covers it.
declare_credential_capable(("k8s.exec",))

#: Kinds whose objects hold a credential, in lower case. A ServiceAccount
#: token is a Secret too.
CREDENTIAL_KINDS: Final[frozenset[str]] = frozenset({"secret"})

#: The objects whose ``k8s.apply`` keeps the rules of ``k8s.apply``: one
#: ``(API group, kind, plural)`` per entry, the core group as ``""``. An apply
#: of any other object waits for a person, also a custom kind of a CRD. Each
#: entry says why its objects cannot run a pod, give rights, make or read a
#: Secret, or change how the cluster checks requests. Add no kind without
#: such a reason.
PLAIN_OBJECTS: Final[tuple[tuple[str, str, str], ...]] = (
    # Plain settings as text. A pod reads it only when its own manifest
    # mounts it, and that manifest waits.
    ("", "ConfigMap", "configmaps"),
    # Picks pods by label and gives them one address. It starts no pod and
    # reads no Secret.
    ("", "Service", "services"),
    # A name for a group of objects. It holds no workload and no rights.
    ("", "Namespace", "namespaces"),
    # A name a pod can run as. Its rights come only from a role binding,
    # and a binding waits.
    ("", "ServiceAccount", "serviceaccounts"),
    # A request for storage. It runs nothing and reads no Secret.
    ("", "PersistentVolumeClaim", "persistentvolumeclaims"),
    # Limits and quotas only cap what the pods of a namespace may use.
    ("", "LimitRange", "limitranges"),
    ("", "ResourceQuota", "resourcequotas"),
    # Routes outside traffic to a Service. It starts no pod and gives no
    # rights in the cluster.
    ("networking.k8s.io", "Ingress", "ingresses"),
    # Names the controller that serves an Ingress. It starts no controller.
    ("networking.k8s.io", "IngressClass", "ingressclasses"),
    # Limits which pods may talk to each other.
    ("networking.k8s.io", "NetworkPolicy", "networkpolicies"),
    # Limits how many pods a voluntary stop may take down at once.
    ("policy", "PodDisruptionBudget", "poddisruptionbudgets"),
    # Changes the replica count of a workload that already exists. What the
    # pods run stays as the workload says.
    ("autoscaling", "HorizontalPodAutoscaler", "horizontalpodautoscalers"),
    # A number that orders pods when the cluster is full.
    ("scheduling.k8s.io", "PriorityClass", "priorityclasses"),
)

#: ``(API group, kind in lower case)`` of :data:`PLAIN_OBJECTS`.
_PLAIN_KINDS: Final[frozenset[tuple[str, str]]] = frozenset(
    (group, kind.lower()) for group, kind, _ in PLAIN_OBJECTS
)

#: ``(API group, plural)`` of :data:`PLAIN_OBJECTS`, for the paths of an
#: ingested Kubernetes API file (:mod:`.ingested_paths`).
PLAIN_PLURALS: Final[frozenset[tuple[str, str]]] = frozenset(
    (group, plural) for group, _, plural in PLAIN_OBJECTS
)


#: The most objects the check reads in one manifest: each document and each
#: entry of an ``items`` array counts. A manifest with more counts as one MEHO
#: cannot read. YAML aliases let a small text name the same object or the same
#: ``items`` array many times; the check reads each of them once.
MAX_CHECKED_OBJECTS: Final[int] = 10_000

#: Tells whether one manifest object counts. It gets a mapping with a
#: non-empty text ``kind``.
_Counts = Callable[[Mapping[str, Any]], bool]


def _is_list_kind(kind: str) -> bool:
    """True for a Kubernetes list object: ``List`` in any case, or ``<Kind>List``.

    Only the bare ``List`` is read without case: a kind such as ``Playlist``
    is not a list.
    """
    return kind.lower() == "list" or kind.endswith("List")


def _items_to_read(doc: Any, counts: _Counts) -> list[Any] | None:
    """The ``items`` the check reads next, or ``None`` when *doc* counts.

    *doc* counts when *counts* says so or when it cannot be read. Only a list
    object (``kind: List`` or ``<Kind>List``) has its ``items`` read. The
    ``items`` field of any other kind is part of its spec.
    """
    if not isinstance(doc, Mapping):
        return None
    kind = doc.get("kind")
    if not isinstance(kind, str) or not kind or counts(doc):
        return None
    items = doc.get("items") if _is_list_kind(kind) else []
    if items is None:
        return []
    return items if isinstance(items, list) else None


def _any_object_counts(docs: list[Any], counts: _Counts) -> bool:
    """True when a doc counts, a list holds one that counts, or one cannot be read.

    Each object and each ``items`` array is read once. Each document and each
    ``items`` entry counts toward :data:`MAX_CHECKED_OBJECTS`.
    """
    budget = len(docs)
    stack = list(docs)
    seen: set[int] = set()
    while stack and budget <= MAX_CHECKED_OBJECTS:
        doc = stack.pop()
        if id(doc) in seen:
            continue
        seen.add(id(doc))
        items = _items_to_read(doc, counts)
        if items is None:
            return True
        if not items or id(items) in seen:
            continue
        seen.add(id(items))
        budget += len(items)
        stack.extend(items)
    return budget > MAX_CHECKED_OBJECTS


def _manifest_has(params: Mapping[str, Any], counts: _Counts) -> bool:
    manifest = params.get("manifest")
    if not isinstance(manifest, str):
        return True
    try:
        docs = [doc for doc in yaml.safe_load_all(manifest) if doc is not None]
    except (yaml.YAMLError, RecursionError):
        return True
    if not docs:
        return True
    return _any_object_counts(docs, counts)


def _is_credential_kind(doc: Mapping[str, Any]) -> bool:
    return str(doc["kind"]).lower() in CREDENTIAL_KINDS


def _api_group(api_version: Any) -> str | None:
    """The API group of an ``apiVersion``: ``""`` for ``v1``, ``None`` when unreadable."""
    if api_version == "v1":
        return ""
    if not isinstance(api_version, str):
        return None
    group, sep, version = api_version.partition("/")
    if not sep or not group or not version or "/" in version:
        return None
    return group


def _is_not_plain(doc: Mapping[str, Any]) -> bool:
    """True when *doc* is not one of :data:`PLAIN_OBJECTS`.

    The kind is read without case, the group as written. A list object is
    plain when it is a ``List`` (in any case) or a ``<Kind>List`` of a plain kind; its
    items are then read one by one.
    """
    group = _api_group(doc.get("apiVersion"))
    if group is None:
        return True
    kind = str(doc["kind"])
    if _is_list_kind(kind):
        kind = kind[: -len("List")]
        if not kind:
            return False
    return (group, kind.lower()) not in _PLAIN_KINDS


def manifest_holds_credential(params: Mapping[str, Any]) -> bool:
    """True when the ``k8s.apply`` manifest in *params* holds a credential kind.

    Any document of a multi-document manifest counts, and any item of a
    list object. A manifest MEHO cannot read counts too: it is not text, it is not
    YAML or JSON, a document has no kind, or it has more than
    :data:`MAX_CHECKED_OBJECTS` objects.
    """
    return _manifest_has(params, _is_credential_kind)


def manifest_needs_a_person(params: Mapping[str, Any]) -> bool:
    """True when an object of the ``k8s.apply`` manifest in *params* is not plain.

    Each document and each item of a list object must be one of
    :data:`PLAIN_OBJECTS`, by API group and kind. An object without a
    readable ``apiVersion`` or ``kind`` counts, and so does a manifest MEHO
    cannot read, as in :func:`manifest_holds_credential`.
    """
    return _manifest_has(params, _is_not_plain)


# Only an apply of plain objects keeps the rules of k8s.apply. Any other
# object can run a pod, give rights, make or read a Secret, or change how
# the cluster checks requests, so its apply waits for a person on every call.
declare_credential_check("k8s.apply", manifest_needs_a_person)

# The manifest of an apply of a Secret holds the Secret's values. A stored
# request keeps it only in the encrypted handoff of the run
# (meho_backplane.operations.approval_queue.create_pending_request).
declare_hidden_stored_param("k8s.apply", "manifest", manifest_holds_credential)
