# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Kubernetes connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.

Every ``k8s.apply`` waits for a person, for every kind of object: there is
no list of kinds that keep the rules of the operation. So does every
``k8s.exec``, ``k8s.delete``, ``k8s.label``, ``k8s.annotate`` and
``k8s.namespace.create``. No standing grant, no agent grant and no run
approval covers these calls. In sandbox mode a delete grant still covers
``k8s.delete``, and only that one of them. A stored request keeps the
manifest of a Secret, or of an object with a secret-looking value, only in
the encrypted handoff of the run.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

import yaml

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.op_classes import (
    declare_credential_capable,
    declare_hidden_stored_param,
    declare_no_secret_value,
    declare_op_classes,
    declare_sandbox_delete_grant,
)
from meho_backplane.redaction.command_line import hide_in_text

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

# Every call of these operations waits for a person, and no standing grant,
# agent grant or run approval covers it:
# - k8s.exec: a command in a container can read or write any credential the
#   container holds.
# - k8s.apply: what an apply does to an object depends on the object as it is
#   on the cluster and on the fields other writers own, not only on the
#   manifest.
# - k8s.delete: it deletes a Pod, a Job or a ReplicaSet.
# - k8s.label and k8s.annotate: on a Namespace they can set or remove the
#   labels that decide how the cluster checks the pods in it.
# - k8s.namespace.create: it makes a new namespace, and MEHO sets no labels
#   on it.
declare_credential_capable(
    ("k8s.exec", "k8s.apply", "k8s.delete", "k8s.label", "k8s.annotate", "k8s.namespace.create")
)

# In sandbox mode a live delete grant still covers k8s.delete. No other
# grant covers it, and a delete grant covers none of the other calls above.
declare_sandbox_delete_grant(("k8s.delete",))

#: Kinds whose objects hold a credential, in lower case. A ServiceAccount
#: token is a Secret too.
CREDENTIAL_KINDS: Final[frozenset[str]] = frozenset({"secret"})

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


#: The fields of an object whose keys name the values they hold.
_NAMED_VALUE_FIELDS: Final[tuple[str, ...]] = ("data", "stringData", "binaryData")


def _is_secret_shaped(value: Any) -> bool:
    """True for a text with a secret shape of its own (a password in a URL, a known token)."""
    return isinstance(value, str) and hide_in_text(value) != value


def _is_nested_secret(node: Any) -> bool:
    """True for a Secret object held inside another object (a template, for example).

    It has the kind ``Secret`` and either the core ``apiVersion`` ``v1`` or a
    ``data``, ``stringData`` or ``binaryData`` field. A mapping that only
    names a Secret (a reference with a ``kind`` and a ``name``) does not count.
    """
    return (
        isinstance(node, Mapping)
        and isinstance(node.get("kind"), str)
        and node["kind"].lower() in CREDENTIAL_KINDS
        and (node.get("apiVersion") == "v1" or any(f in node for f in _NAMED_VALUE_FIELDS))
    )


def _is_secret_env_entry(entry: Any) -> bool:
    """True for an ``env`` entry with a secret name and a value of its own."""
    return (
        isinstance(entry, Mapping)
        and is_secret_param_name(str(entry.get("name", "")))
        and entry.get("value") not in (None, "")
    )


def _children(node: Any, *, skip_items: bool) -> list[Any]:
    if isinstance(node, Mapping):
        return [value for key, value in node.items() if not (skip_items and key == "items")]
    return list(node) if isinstance(node, list) else []


class _SecretValueCheck:
    """A check for one manifest: True when an object holds a secret-looking value.

    An object holds one when it holds a Secret object at any depth
    (:func:`_is_nested_secret`), when a key of its ``data``, ``stringData`` or
    ``binaryData`` has a secret name (the name rule of
    :func:`~meho_backplane.broadcast.events.scrub_secret_named_values`), when
    an ``env`` entry at any depth, the object itself included, has a secret
    name and a value, or when a text at any depth has a secret shape
    (:func:`~meho_backplane.redaction.command_line.hide_in_text`). Each value
    of the manifest is read once, and the names of each ``data``,
    ``stringData``, ``binaryData`` and ``env`` value are read once, also when YAML aliases
    name them many times. The ``items`` of a list object are read as objects
    of their own.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()
        self._names_read: set[int] = set()

    def _first_visit(self, value: Any, visited: set[int]) -> bool:
        if id(value) in visited:
            return False
        visited.add(id(value))
        return True

    def _has_secret_key(self, values: Any) -> bool:
        return (
            isinstance(values, Mapping)
            and self._first_visit(values, self._names_read)
            and any(is_secret_param_name(str(k)) for k in values)
        )

    def _has_secret_env(self, node: Any) -> bool:
        env = node.get("env") if isinstance(node, Mapping) else None
        return (
            isinstance(env, list)
            and self._first_visit(env, self._names_read)
            and any(_is_secret_env_entry(entry) for entry in env)
        )

    def __call__(self, doc: Mapping[str, Any]) -> bool:
        if any(self._has_secret_key(doc.get(field)) for field in _NAMED_VALUE_FIELDS):
            return True
        if not self._first_visit(doc, self._seen):
            return False
        if self._has_secret_env(doc):
            return True
        stack = _children(doc, skip_items=_is_list_kind(str(doc["kind"])))
        while stack:
            node = stack.pop()
            if not self._first_visit(node, self._seen):
                continue
            if _is_secret_shaped(node) or self._has_secret_env(node) or _is_nested_secret(node):
                return True
            stack.extend(_children(node, skip_items=False))
        return False


def manifest_holds_credential(params: Mapping[str, Any]) -> bool:
    """True when the ``k8s.apply`` manifest in *params* holds a credential.

    A manifest holds one when an object is of a credential kind or holds a
    secret-looking value (:class:`_SecretValueCheck`). Any document of a
    multi-document manifest counts, and any item of a list object. A
    manifest MEHO cannot read counts too: it is not text, it is not YAML or
    JSON, a document has no kind, or it has more than
    :data:`MAX_CHECKED_OBJECTS` objects.
    """
    holds_secret_value = _SecretValueCheck()
    return _manifest_has(params, lambda doc: _is_credential_kind(doc) or holds_secret_value(doc))


# The manifest of an apply of a Secret, or of an object with a secret-looking
# value, holds that value. A stored request keeps it only in the encrypted
# handoff of the run
# (meho_backplane.operations.approval_queue.create_pending_request).
declare_hidden_stored_param("k8s.apply", "manifest", manifest_holds_credential)
