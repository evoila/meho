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

import json
import re
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
from meho_backplane.redaction.command_line import (
    hide_command_text,
    hide_in_text,
    is_secret_shaped_name,
    reads_shell_script,
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


#: The fields whose keys name the values they hold, in an object or at any
#: depth below it.
_NAMED_VALUE_FIELDS: Final[frozenset[str]] = frozenset({"data", "stringData", "binaryData"})

#: The fields that hold the arguments of a program.
_COMMAND_FIELDS: Final[frozenset[str]] = frozenset({"command", "args"})

#: The most nodes one manifest check reads, the text of a JSON annotation
#: included. A manifest with more counts as one MEHO cannot read.
_MAX_NODES: Final[int] = 2_000_000

#: A line of a file text: ``name: value``, ``name=value``, ``"name": "value",``.
_FILE_LINE = re.compile(
    r"""^[\s\-"'{\[,]*(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]\s*(?P<value>\S.*)$"""
)

#: A variable name written the way a shell variable is: capital letters.
_SHELL_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*")


def _is_secret_shaped(value: Any) -> bool:
    """True for a text with a secret shape of its own (a password in a URL, a known token)."""
    return isinstance(value, str) and hide_in_text(value) != value


def _has_value(value: Any) -> bool:
    """True for a text that is not blank, or a number: a value of its own."""
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, int | float)


def _is_secret_env_entry(entry: Any) -> bool:
    """True for an ``env`` entry with a secret-shaped name and a value of its own."""
    return (
        isinstance(entry, Mapping)
        and is_secret_shaped_name(str(entry.get("name", "")))
        and _has_value(entry.get("value"))
    )


def _command_holds_secret(parts: Any) -> bool:
    """True when a ``command`` or ``args`` list has a secret flag or a secret-named assignment.

    Each item is one argument. The item after ``-c`` is a shell command
    string. A flag and its value may be two items.
    """
    items = parts if isinstance(parts, list) else [parts]
    pending: str | None = None
    previous = ""
    for part in items:
        if not isinstance(part, str):
            previous = ""
            continue
        if pending is not None and not part.startswith("-"):
            return True
        text, pending = hide_command_text(
            hide_in_text(part), pending, shell=reads_shell_script(previous)
        )
        if text != part:
            return True
        previous = part
    return False


def _file_text_holds_secret(text: str) -> bool:
    """True when a line of a file text names a secret and gives it a value.

    A capital-letter name is read the way a shell variable is
    (``DB_PASS=x``); any other name by the name rule of
    :func:`~meho_backplane.broadcast.events.scrub_secret_named_values`.
    """
    for line in text.splitlines():
        found = _FILE_LINE.match(line)
        if found is None:
            continue
        name = found["name"]
        is_secret = (
            is_secret_shaped_name if _SHELL_VARIABLE.fullmatch(name) else is_secret_param_name
        )
        if is_secret(name) and found["value"].strip(" \t,;\"'{}[]") != "":
            return True
    return False


def _children(node: Any, *, skip_items: bool) -> list[Any]:
    if isinstance(node, Mapping):
        return [value for key, value in node.items() if not (skip_items and key == "items")]
    return list(node) if isinstance(node, list) else []


def _annotation_objects(annotations: Mapping[Any, Any]) -> list[Any] | None:
    """The JSON objects in the texts of *annotations*; ``None`` when one is too deep to read."""
    found: list[Any] = []
    for value in annotations.values():
        if not isinstance(value, str) or value.lstrip()[:1] not in ("{", "["):
            continue
        try:
            found.append(json.loads(value))
        except RecursionError:
            return None
        except ValueError:
            continue
    return found


def _mapping_holds_secret(node: Mapping[Any, Any], named_maps_read: set[int]) -> bool:
    """True when a key of *node* or a value of one of its keys holds a secret (see the check).

    A ``data``, ``stringData`` or ``binaryData`` map is read once, also when
    many objects name it through a YAML alias.
    """
    for key, value in node.items():
        name = str(key)
        if name in _NAMED_VALUE_FIELDS and isinstance(value, Mapping):
            if id(value) in named_maps_read:
                continue
            named_maps_read.add(id(value))
            if any(is_secret_param_name(str(k)) for k in value):
                return True
            if name != "binaryData" and any(
                isinstance(v, str) and _file_text_holds_secret(v) for v in value.values()
            ):
                return True
        elif is_secret_param_name(name) and _has_value(value):
            return True
        if name == "env" and isinstance(value, list):
            if any(_is_secret_env_entry(entry) for entry in value):
                return True
        elif name in _COMMAND_FIELDS and _command_holds_secret(value):
            return True
    return False


class _SecretValueCheck:
    """A check for one manifest: True when an object holds a secret-looking value.

    Every key and value at any depth is read with the same rules, in the
    object itself and below it:

    * a key of a ``data``, ``stringData`` or ``binaryData`` map has a secret
      name (the name rule of
      :func:`~meho_backplane.broadcast.events.scrub_secret_named_values`);
    * any other key has a secret name and a value of its own (a text or a
      number);
    * an ``env`` entry has a secret-shaped name
      (:func:`~meho_backplane.redaction.command_line.is_secret_shaped_name`,
      also a secret word inside a longer name) and a value;
    * a ``command`` or ``args`` item has a secret flag or a secret-named
      assignment (:func:`~meho_backplane.redaction.command_line.hide_command_text`);
    * a line of a text in ``data`` or ``stringData`` names a secret and gives
      it a value;
    * a text in ``annotations`` that is JSON is read the same way;
    * a text has a secret shape
      (:func:`~meho_backplane.redaction.command_line.hide_in_text`).

    A value of another type than a rule expects never raises: the rule does
    not apply and the walk goes on. Each value of the manifest is read once,
    also when YAML aliases name it many times. The ``items`` of a list object
    are read as objects of their own.
    """

    def __init__(self) -> None:
        self._seen: set[int] = set()
        self._named_maps_read: set[int] = set()
        self._nodes = 0
        # Parsed annotation objects stay alive for the whole check, so an id() in
        # the seen sets is never reused by a later object.
        self._parsed: list[Any] = []

    def _first_visit(self, value: Any) -> bool:
        if id(value) in self._seen:
            return False
        self._seen.add(id(value))
        return True

    def __call__(self, doc: Mapping[str, Any]) -> bool:
        if not self._first_visit(doc):
            return False
        stack: list[Any] = [doc]
        skip_items = _is_list_kind(str(doc["kind"]))
        while stack:
            node = stack.pop()
            self._nodes += 1
            if self._nodes > _MAX_NODES or _is_secret_shaped(node):
                return True
            if isinstance(node, Mapping):
                if _mapping_holds_secret(node, self._named_maps_read):
                    return True
                annotations = node.get("annotations")
                if isinstance(annotations, Mapping):
                    parsed = _annotation_objects(annotations)
                    if parsed is None:
                        return True
                    self._parsed.extend(parsed)
                    stack.extend(parsed)
            for child in _children(node, skip_items=skip_items and node is doc):
                if self._first_visit(child):
                    stack.append(child)
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
