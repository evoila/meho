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

from meho_backplane.broadcast.events import is_secret_name_value_pair, is_secret_param_name
from meho_backplane.connectors.kubernetes.manifest_yaml import (
    ManifestYamlError,
    load_manifest_documents,
)
from meho_backplane.op_classes import (
    declare_credential_capable,
    declare_hidden_stored_param,
    declare_no_secret_value,
    declare_op_classes,
    declare_sandbox_delete_grant,
)
from meho_backplane.redaction import get_default_policy, redact
from meho_backplane.redaction.command_line import (
    HIDDEN,
    PASSWORD_P_PROGRAMS,
    hide_command_text,
    hide_flag_value,
    hide_in_text,
    is_secret_shaped_name,
    reads_shell_script,
    takes_password_after_p,
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
        docs = [doc for doc in load_manifest_documents(manifest) if doc is not None]
    except ManifestYamlError:
        return True
    if not docs:
        return True
    return _any_object_counts(docs, counts)


def _is_credential_kind(doc: Mapping[str, Any]) -> bool:
    return str(doc["kind"]).lower() in CREDENTIAL_KINDS


#: The fields of an object whose keys name the values they hold. In a Secret
#: these hold its values: the approval card hides each of them too
#: (``ops_write_preview``).
SECRET_VALUE_FIELDS: Final[tuple[str, ...]] = ("data", "stringData", "binaryData")


def _is_secret_shaped(value: Any) -> bool:
    """True for a text that the card hides by its shape.

    That is a password in a URL or a token with a known vendor prefix
    (:func:`~meho_backplane.redaction.command_line.hide_in_text`), or a value
    the shared redaction hides with its default rules (an ``Authorization``
    header, for example).
    """
    if not isinstance(value, str) or not value:
        return False
    return hide_in_text(value) != value or redact(value, get_default_policy()).redacted != value


#: Kubernetes fields with a secret-shaped name that hold a yes/no setting,
#: never a secret. The card shows their value, and they do not count.
PLAIN_SETTINGS: Final[frozenset[str]] = frozenset({"automountServiceAccountToken"})


def _is_shown_scalar(value: Any) -> bool:
    """True for a value the card hides under a secret name: a text that is not empty,
    a number, a yes/no value or a date. A map, a list and an empty value are not."""
    if isinstance(value, str):
        return value != ""
    return value is not None and not isinstance(value, Mapping | list)


def _has_secret_named_value(node: Mapping[str, Any]) -> bool:
    """True when a key of *node* with a secret name holds a text, a number or a yes/no value.

    The name rule is that of
    :func:`~meho_backplane.broadcast.events.is_secret_param_name`. The
    ``value`` of a ``{name, value}`` pair whose name it marks counts too. A
    field of :data:`PLAIN_SETTINGS` does not count.
    """
    if is_secret_name_value_pair(node) and _is_shown_scalar(node.get("value")):
        return True
    return any(
        isinstance(key, str)
        and key not in PLAIN_SETTINGS
        and is_secret_param_name(key)
        and _is_shown_scalar(value)
        for key, value in node.items()
    )


#: A container's ``command`` and ``args`` lists. In this order they make one
#: command line, the way Kubernetes runs them.
_COMMAND_LISTS: Final[tuple[str, ...]] = ("command", "args")


def _argument_text(part: Any) -> str | None:
    """The text of one argument: a text, or a number, a yes/no value or a date as YAML read it.

    ``None`` for a map, a list or an empty value, which is no argument.
    """
    if isinstance(part, str):
        return part
    if part is None or isinstance(part, Mapping | list):
        return None
    return str(part)


def _hide_apply_item(
    text: str, previous: str, *, p_program: bool, p_any_program: bool
) -> tuple[str, str | None]:
    """One item of a ``k8s.apply`` command line with its secret values hidden.

    A value glued to ``-p`` (``-pVALUE``) is hidden only in a command of a
    program that takes a password there
    (:func:`~meho_backplane.redaction.command_line.takes_password_after_p`,
    *p_program* for the items before this one). The argument after ``-p``
    is hidden there too, and with *p_any_program* (the card) after any
    program. ``-uuser:password`` hides the password. Returned with the text
    is the secret flag that waits for the next item.
    """
    shell = reads_shell_script(previous)
    if not shell and text.startswith("-p") and (p_program or (p_any_program and text == "-p")):
        return (text, "secret") if text == "-p" else (f"-p{HIDDEN}", None)
    return hide_command_text(
        hide_in_text(text),
        None,
        shell=shell,
        p_programs=PASSWORD_P_PROGRAMS,
        p_any_program=p_any_program,
        p_program_before=p_program and not shell,
    )


def hide_command_secrets(
    parts: list[Any], *, p_after_programs: bool = False, p_any_program: bool = False
) -> list[Any]:
    """Copy one command line (``command`` + ``args``) with its secret values hidden.

    The words stay, so the approver still reads what runs. Hidden are: what
    follows a ``NAME=`` (or ``--name=``) with a secret-shaped name, the
    password in a URL, tokens with a known vendor prefix, the argument after
    a secret-shaped flag (``--password x``, ``-p x``) and the password in
    ``user:password`` after ``-u`` or ``--user``. A flag and its value may be
    two list items or two words of one string. An argument YAML reads as a
    number, a yes/no value or a date is read as its text, so
    ``--password 123456`` is hidden too; an argument with nothing hidden
    keeps its type.

    Each list item is one argument of the program, so a secret value in it
    runs to the end of the item, spaces and shell operators included
    (``PGPASSWORD=x;tail`` is hidden whole). Only the item right after
    ``-c``, alone or in a short option group (``sh -ec``, ``bash -lc``),
    is a shell command string: there a value is one shell word, quotes and
    backslash escapes included (``"a\\" b"``, ``a\\ b``), and an unquoted
    shell operator ends it (``PGPASSWORD=x;id`` keeps ``id``).

    *p_after_programs* is the rule of ``k8s.apply`` (:func:`_hide_apply_item`):
    a value glued to ``-p`` is hidden after a program that takes a password
    there (``mysql -pVALUE``), a password glued to ``-u`` is hidden
    (``curl -uuser:password``), and the item after a secret flag is its
    value also when it starts with a dash (``--password -x``). Alone, it is
    the rule of the check of the stored request: ``-p`` is a secret flag
    only after such a program (``mysql -p x``; ``mkdir -p /data`` stays).
    With *p_any_program* it is the rule of the approval card: the argument
    after ``-p`` is hidden after any program, as in the Job previews
    (``mkdir -p /data`` shows ``-p`` and the hidden mark).
    """
    hidden: list[Any] = []
    pending: str | None = None
    previous = ""
    p_program = False
    for part in parts:
        text = _argument_text(part)
        if text is None:
            hidden.append(part)
            previous = ""
            continue
        if pending is not None and (
            p_after_programs or not (isinstance(part, str) and part.startswith("-"))
        ):
            shown = hide_flag_value(pending, text)
            pending = None
        elif p_after_programs:
            shown, pending = _hide_apply_item(
                text, previous, p_program=p_program, p_any_program=p_any_program
            )
        else:
            shown, pending = hide_command_text(
                hide_in_text(text), pending, shell=reads_shell_script(previous)
            )
        if not reads_shell_script(previous) and takes_password_after_p(text):
            p_program = True
        hidden.append(part if shown == text else shown)
        previous = text
    return hidden


def _has_secret_command_line(node: Any) -> bool:
    """True when the command line of *node* holds a value :func:`hide_command_secrets` hides."""
    if not isinstance(node, Mapping):
        return False
    line = [part for key in _COMMAND_LISTS if isinstance(node.get(key), list) for part in node[key]]
    return bool(line) and hide_command_secrets(line, p_after_programs=True) != line


def _is_nested_secret(node: Any) -> bool:
    """True for a Secret object held inside another object (a template, for example).

    It has the kind ``Secret`` and either its own ``metadata`` or a ``data``,
    ``stringData`` or ``binaryData`` field. A reference to a Secret (for
    example an entry of a ServiceAccount's ``secrets``, with ``apiVersion``,
    ``kind`` and ``name``) has neither, and does not count.
    """
    return (
        isinstance(node, Mapping)
        and isinstance(node.get("kind"), str)
        and node["kind"].lower() in CREDENTIAL_KINDS
        and any(field in node for field in ("metadata", *SECRET_VALUE_FIELDS))
    )


def _is_secret_env_entry(entry: Any) -> bool:
    """True for an ``env`` entry with a secret name and a value of its own.

    A secret name is one of the command-line rule
    (:func:`~meho_backplane.redaction.command_line.is_secret_shaped_name`):
    it holds PASS, PWD, SECRET, TOKEN, KEY, CREDENTIAL or AUTH
    (``PGPASSWORD``, ``API_TOKEN``), or the name rule marks it.
    """
    return (
        isinstance(entry, Mapping)
        and is_secret_shaped_name(str(entry.get("name", "")))
        and entry.get("value") not in (None, "")
    )


def _children(node: Any, *, skip_items: bool) -> list[Any]:
    if isinstance(node, Mapping):
        return [value for key, value in node.items() if not (skip_items and key == "items")]
    return list(node) if isinstance(node, list) else []


class _SecretValueCheck:
    """The storage rule for one manifest: True when an object holds a value it counts.

    Such a manifest is kept only in the encrypted hand-off of the run. The
    approval card hides each value this rule counts, and more (every ``env``
    value, for example). An object holds such a value when:

    * it holds a Secret object at any depth (:func:`_is_nested_secret`), or
      a key of its ``data``, ``stringData`` or ``binaryData`` has a secret
      name (the name rule of
      :func:`~meho_backplane.broadcast.events.scrub_secret_named_values`);
    * a key with a secret name at any depth holds a text, a number or a
      yes/no value (:func:`_has_secret_named_value`);
    * an ``env`` entry at any depth, the object itself included, has a
      secret name of the command-line rule and a value
      (:func:`_is_secret_env_entry`);
    * the ``command`` and ``args`` of a map at any depth, read as one
      command line, hold a value :func:`hide_command_secrets` hides with
      ``p_after_programs`` (the argument after ``--password``, for example;
      ``-p`` only after a program that takes a password there);
    * a text at any depth has a secret shape (:func:`_is_secret_shaped`).

    Each value of the manifest is read once, and the names of each
    ``data``, ``stringData``, ``binaryData`` and ``env`` value are read
    once, also when YAML aliases name them many times. The ``items`` of a
    list object are read as objects of their own.
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
        if any(self._has_secret_key(doc.get(field)) for field in SECRET_VALUE_FIELDS):
            return True
        if not self._first_visit(doc, self._seen):
            return False
        if (
            self._has_secret_env(doc)
            or _has_secret_command_line(doc)
            or _has_secret_named_value(doc)
        ):
            return True
        stack = _children(doc, skip_items=_is_list_kind(str(doc["kind"])))
        while stack:
            node = stack.pop()
            if not self._first_visit(node, self._seen):
                continue
            if (
                _is_secret_shaped(node)
                or self._has_secret_env(node)
                or _is_nested_secret(node)
                or _has_secret_command_line(node)
                or (isinstance(node, Mapping) and _has_secret_named_value(node))
            ):
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
#: The answer to such an apply on an install without the hand-off key.
HANDOFF_KEY_REFUSAL: Final[str] = (
    "MEHO cannot keep this request: MEHO keeps this manifest only in the encrypted "
    "hand-off, because it holds a secret value or MEHO cannot read it, and the hand-off "
    "needs a key. Set APPROVAL_HANDOFF_ENCRYPTION_KEY on the MEHO server (in the Helm "
    "chart: approvalHandoff), then send the call again."
)

declare_hidden_stored_param(
    "k8s.apply", "manifest", manifest_holds_credential, refusal=HANDOFF_KEY_REFUSAL
)
