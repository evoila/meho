# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vendor action names that mean "delete" (#4632).

MEHO decides "this operation is a delete" in several places: the ingest
parser, the standing-grant checks, the governed-step list, the flight
recorder and the review list for stored deletes. Two signs are easy to
read off an op id: the HTTP method ``DELETE`` and dotted names such as
``*.delete``. The third sign lives here: the vendor action names the
parser treats as deletes (``Destroy``, ``Delete``, ``Remove``,
``Unregister``), also after a vSAN prefix such as ``VsanCluster``
(``VsanClusterRemoveShare``), and the query ``action=delete`` of a delete
sent as ``POST`` (``POST:/hvc/links/{link}?action=delete``, #4973).

VI-JSON (the JSON form of vCenter's older object API) exposes every method
as a ``POST`` to ``/<Type>/{moId}/<Method>``, so
``POST:/VirtualMachine/{moId}/Destroy_Task`` deletes a VM while its HTTP
method says nothing. The action name is the last part of the path.

The dotted delete names (``*.delete``, ``*.destroy``, ``*.remove``,
``*.purge``) and the method sign ``DELETE:*`` live here too, as the glob
patterns the standing-grant check uses. The sandbox-mode decision
(``docs/decisions/sandbox-mode.md``) uses the same signs.

The write words are the wider list: every whole word in an op id that
says the operation changes something. The contract check in CI uses it to
refuse a ``safe`` operation whose id says it writes. Every delete sign is
also a write word; ``tests/test_delete_names.py`` proves that.

This module imports nothing from MEHO so the flight-recorder library and
the parser can import it without pulling in the dispatcher or settings.
The word lists are deliberately small and exist only here.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "DELETE_ACTION_QUERY",
    "DELETE_SHAPED_OP_ID_PATTERNS",
    "DESTRUCTIVE_ACTION_PREFIXES",
    "VENDOR_ACTION_PREFIXES",
    "WRITE_WORDS",
    "destructive_action_name_in_op_id",
    "destructive_action_name_in_path",
    "is_destructive_action_name",
    "write_word_in_op_id",
]

#: The action-name prefixes that mean "delete". Connector-owned safety
#: floors cover vendor-specific additions after this generic rule.
DESTRUCTIVE_ACTION_PREFIXES: Final[tuple[str, ...]] = ("Destroy", "Delete", "Remove", "Unregister")

#: Vendor prefixes a delete word may follow (#4973). vSAN methods start with
#: one: ``VsanClusterRemoveShare``, ``VsanVitRemoveIscsiLUN``,
#: ``VsanPerfDeleteStatsObject``, ``VsanDeleteObjects_Task``. The match is
#: case-sensitive, like the delete words.
VENDOR_ACTION_PREFIXES: Final[tuple[str, ...]] = ("Vsan", "VsanCluster", "VsanPerf", "VsanVit")

#: The query part that marks a delete sent as ``POST`` on an action-style
#: REST API, for example ``POST /hvc/links/{link}?action=delete`` (#4973).
#: Matched as one whole ``&``-separated part, so ``action=delete-tags`` does
#: not count.
DELETE_ACTION_QUERY: Final[str] = "action=delete"

#: Delete-shaped op-id patterns a service-principal standing grant may never
#: cover outside sandbox mode (#3151). Case-sensitive ``fnmatchcase`` globs
#: over the exact op id: raw HTTP DELETE ops (``DELETE:*``) plus the dotted
#: typed delete/destroy/remove/purge families.
DELETE_SHAPED_OP_ID_PATTERNS: Final[tuple[str, ...]] = (
    "DELETE:*",
    "*.delete",
    "*.destroy",
    "*.remove",
    "*.purge",
)

#: Whole words in a typed op id that say the operation changes something.
#: Matched case-insensitively on the parts between ``.``, ``_`` and ``-``,
#: so "set" inside "settings" and "run" inside "runtime" do not count.
WRITE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "add",
        "apply",
        "assign",
        "cordon",
        "create",
        "delete",
        "deploy",
        "destroy",
        "disable",
        "drain",
        "elevate",
        "enable",
        "evict",
        "exec",
        "grant",
        "import",
        "install",
        "move",
        "patch",
        "power",
        "purge",
        "put",
        "reboot",
        "register",
        "remove",
        "renew",
        "reset",
        "restart",
        "restore",
        "resume",
        "revert",
        "revoke",
        "rotate",
        "run",
        "save",
        "scale",
        "set",
        "shutdown",
        "start",
        "stop",
        "suspend",
        "sync",
        "unassign",
        "uninstall",
        "unregister",
        "update",
        "upload",
        "write",
    }
)

_ACTION_NAME_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[._/-]")
_OP_ID_WORD_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[._-]")


def _starts_with_delete_word(segment: str) -> bool:
    """Return whether *segment* starts with a delete word that ends at a word boundary."""
    for prefix in DESTRUCTIVE_ACTION_PREFIXES:
        if not segment.startswith(prefix):
            continue
        suffix = segment[len(prefix) :]
        if not suffix or suffix.startswith("_") or suffix[:1].isupper():
            return True
    return False


def is_destructive_action_name(value: str) -> bool:
    """Return whether *value* names a delete action.

    Splitting preserves ``VirtualMachine.Destroy_Task`` while accepting the
    compound VIM spelling ``DeleteDatastoreFile_Task``. A prefix must end at
    a separator, an upper-case word boundary, or the end of the segment so a
    noun such as ``Destroyer`` is not promoted accidentally. A delete word
    right after a vendor prefix (:data:`VENDOR_ACTION_PREFIXES`) counts too:
    ``VsanClusterRemoveShare`` is a delete, ``VsanVitGetIscsiLUNs`` is not.
    """
    for segment in _ACTION_NAME_SPLIT_RE.split(value):
        if _starts_with_delete_word(segment):
            return True
        for vendor_prefix in VENDOR_ACTION_PREFIXES:
            if segment.startswith(vendor_prefix) and _starts_with_delete_word(
                segment[len(vendor_prefix) :]
            ):
                return True
    return False


def destructive_action_name_in_path(path: str) -> str | None:
    """Return the delete action name in an ingested path, or ``None``.

    The action name is the last part of the path without its query, the same
    part the parser checks at ingest. When that part is not a delete, a query
    with the whole part :data:`DELETE_ACTION_QUERY` returns that query part.
    """
    raw_path, _, query = path.partition("?")
    action_name = raw_path.rsplit("/", maxsplit=1)[-1]
    if action_name and is_destructive_action_name(action_name):
        return action_name
    if DELETE_ACTION_QUERY in query.split("&"):
        return DELETE_ACTION_QUERY
    return None


def destructive_action_name_in_op_id(op_id: str) -> str | None:
    """Return the delete action name in an ingested op id, or ``None``.

    An ingested op id is ``METHOD:/path``; see
    :func:`destructive_action_name_in_path` for the path rule. Dotted typed
    op ids (``k8s.secret.delete``) have no path and return
    ``None``; they keep their own name patterns.
    """
    _method, separator, path = op_id.partition(":")
    if not separator or not path.startswith("/"):
        return None
    return destructive_action_name_in_path(path)


def write_word_in_op_id(op_id: str) -> str | None:
    """Return the first whole word in a typed op id that means "write", or ``None``.

    ``rke2.etcd-snapshot.save`` returns ``"save"``; ``vm.settings.get`` and
    ``node.runtime.show`` return ``None`` because "set" and "run" are only
    parts of a longer word.
    """
    for word in _OP_ID_WORD_SPLIT_RE.split(op_id):
        if word.lower() in WRITE_WORDS:
            return word
    return None
