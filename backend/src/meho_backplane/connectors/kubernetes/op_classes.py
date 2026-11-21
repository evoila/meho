# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Kubernetes connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from collections.abc import Mapping
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

#: Kinds whose objects run a pod, in lower case. A pod runs any command the
#: manifest names, with every Secret of its namespace it mounts.
COMMAND_KINDS: Final[frozenset[str]] = frozenset(
    {
        "cronjob",
        "daemonset",
        "deployment",
        "job",
        "pod",
        "podtemplate",
        "replicaset",
        "replicationcontroller",
        "statefulset",
    }
)

#: Kinds whose objects give rights in the cluster, in lower case.
RIGHTS_KINDS: Final[frozenset[str]] = frozenset(
    {"clusterrole", "clusterrolebinding", "role", "rolebinding"}
)

#: Kinds whose objects make a Secret, read one out of the cluster or reach a
#: secret store, in lower case: External Secrets, Sealed Secrets and
#: cert-manager. MEHO reads the kind only, not the API group, so a kind of
#: another group with the same name counts too.
SECRET_MAKING_KINDS: Final[frozenset[str]] = frozenset(
    {
        "certificate",
        "clusterexternalsecret",
        "clustersecretstore",
        "externalsecret",
        "pushsecret",
        "sealedsecret",
        "secretstore",
    }
)

#: Every kind that makes a ``k8s.apply`` a credential call.
PERSON_KINDS: Final[frozenset[str]] = (
    CREDENTIAL_KINDS | COMMAND_KINDS | RIGHTS_KINDS | SECRET_MAKING_KINDS
)


#: The most objects the check reads in one manifest: each document and each
#: entry of an ``items`` array counts. A manifest with more counts as one MEHO
#: cannot read. YAML aliases let a small text name the same object or the same
#: ``items`` array many times; the check reads each of them once.
MAX_CHECKED_OBJECTS: Final[int] = 10_000


def _is_list_kind(kind: str) -> bool:
    """True for a Kubernetes list object: ``List`` or ``<Kind>List``."""
    return kind.endswith("List")


def _items_to_read(doc: Any, kinds: frozenset[str]) -> list[Any] | None:
    """The ``items`` the check reads next, or ``None`` when *doc* counts.

    *doc* counts when it is one of *kinds* or cannot be read. Only a list
    object (``kind: List`` or ``<Kind>List``) has its ``items`` read. The
    ``items`` field of any other kind is part of its spec.
    """
    if not isinstance(doc, Mapping):
        return None
    kind = doc.get("kind")
    if not isinstance(kind, str) or not kind or kind.lower() in kinds:
        return None
    items = doc.get("items") if _is_list_kind(kind) else []
    if items is None:
        return []
    return items if isinstance(items, list) else None


def _holds_credential(docs: list[Any], kinds: frozenset[str] = CREDENTIAL_KINDS) -> bool:
    """True when a doc is one of *kinds*, a list with one, or cannot be read.

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
        items = _items_to_read(doc, kinds)
        if items is None:
            return True
        if not items or id(items) in seen:
            continue
        seen.add(id(items))
        budget += len(items)
        stack.extend(items)
    return budget > MAX_CHECKED_OBJECTS


def _manifest_holds(params: Mapping[str, Any], kinds: frozenset[str]) -> bool:
    manifest = params.get("manifest")
    if not isinstance(manifest, str):
        return True
    try:
        docs = [doc for doc in yaml.safe_load_all(manifest) if doc is not None]
    except (yaml.YAMLError, RecursionError):
        return True
    if not docs:
        return True
    return _holds_credential(docs, kinds)


def manifest_holds_credential(params: Mapping[str, Any]) -> bool:
    """True when the ``k8s.apply`` manifest in *params* holds a credential kind.

    Any document of a multi-document manifest counts, and any item of a
    list object. A manifest MEHO cannot read counts too: it is not text, it is not
    YAML or JSON, a document has no kind, or it has more than
    :data:`MAX_CHECKED_OBJECTS` objects.
    """
    return _manifest_holds(params, CREDENTIAL_KINDS)


def manifest_needs_a_person(params: Mapping[str, Any]) -> bool:
    """True when the ``k8s.apply`` manifest in *params* holds one of :data:`PERSON_KINDS`.

    A Secret, an object that runs a pod, an object that gives rights, or an
    object that makes or reads a Secret. The same reading as
    :func:`manifest_holds_credential`: a manifest MEHO cannot read counts.
    """
    return _manifest_holds(params, PERSON_KINDS)


# A k8s.apply of a Secret writes a credential. An apply of a pod or of an
# object that runs one runs any command with the Secrets it mounts; an apply
# of a role or a binding gives rights; an apply of an External Secret, a
# Sealed Secret or a Certificate makes a Secret. Such a call waits for a
# person on every call; an apply of other kinds keeps the rules of k8s.apply.
declare_credential_check("k8s.apply", manifest_needs_a_person)

# The manifest of an apply of a Secret holds the Secret's values. A stored
# request keeps it only in the encrypted handoff of the run
# (meho_backplane.operations.approval_queue.create_pending_request).
declare_hidden_stored_param("k8s.apply", "manifest", manifest_holds_credential)
