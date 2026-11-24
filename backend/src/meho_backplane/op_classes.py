# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op classes a connector declares for its own operations.

The broadcast classifier (:func:`meho_backplane.broadcast.events.classify_op`)
decides how much of a call MEHO shows on the feed, in the approval queue,
in the preview, in the audit row and in the flight recorder. Most
operations get their class from a pattern rule on the op id. The
operations that need an exact class, for example a write whose request
carries a secret, declare it here.

A connector declares the classes in one module of its own package,
``connectors/<name>/op_classes.py``, and imports that module from its
``__init__.py``. So the classes are known as soon as the connector
package is imported, which the server does for every connector at
start. No core file lists another connector's op ids.

This module imports nothing from MEHO, so a connector and the classifier
can both import it without a cycle.

Example::

    from meho_backplane.op_classes import declare_op_classes

    declare_op_classes(
        credential_write=("example.user.set_password",),
        write=("example.cache.flush",),
    )
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

__all__ = [
    "DECLARABLE_OP_CLASSES",
    "call_always_waits",
    "declare_credential_capable",
    "declare_credential_capable_match",
    "declare_credential_check",
    "declare_hidden_stored_param",
    "declare_no_secret_value",
    "declare_op_classes",
    "declare_per_call_approval",
    "declare_root_shell",
    "declare_safe_writes",
    "declare_sandbox_delete_grant",
    "declare_secret_field",
    "declare_wait_when_set",
    "declared_op_class",
    "declared_op_ids",
    "hidden_stored_param",
    "is_credential_capable",
    "is_root_shell",
    "is_safe_write",
    "needs_per_call_approval",
    "params_make_credential_call",
    "returns_no_secret_value",
    "sandbox_delete_grant_allowed",
    "secret_field_paths",
    "waits_when_set",
]

#: The classes a connector may declare. ``credential_read``: the answer
#: holds a secret. ``credential_mint``: the answer holds a new secret.
#: ``credential_write``: the request holds a secret, or the operation sends
#: one to the target. ``write``: the operation changes the target, but its
#: op id does not end in a write word the pattern rules know.
DECLARABLE_OP_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write", "write"}
)

_DECLARED: dict[str, str] = {}
_SAFE_WRITES: set[str] = set()
_ROOT_SHELL: set[str] = set()
_PER_CALL_APPROVAL: set[str] = set()
_NO_SECRET_VALUE: set[str] = set()
_WAIT_WHEN_SET: dict[str, frozenset[str]] = {}
_CREDENTIAL_CAPABLE: set[str] = set()
_CREDENTIAL_CAPABLE_MATCHES: list[Callable[[str], bool]] = []
_SANDBOX_DELETE_GRANT: set[str] = set()
_CREDENTIAL_CHECKS: dict[str, Callable[[Mapping[str, Any]], bool]] = {}
_HIDDEN_STORED_PARAMS: dict[str, tuple[str, Callable[[Mapping[str, Any]], bool]]] = {}
_SECRET_FIELDS: dict[str, set[tuple[str, ...]]] = {}


def declare_op_classes(
    *,
    credential_read: Iterable[str] = (),
    credential_mint: Iterable[str] = (),
    credential_write: Iterable[str] = (),
    write: Iterable[str] = (),
) -> None:
    """Declare the class of the given op ids.

    Safe to call twice with the same ids and classes, so a re-import of
    the connector module changes nothing. Raises :exc:`ValueError` when an
    op id already has a different class: two declarations for one op id
    must not disagree.
    """
    for op_class, op_ids in (
        ("credential_read", credential_read),
        ("credential_mint", credential_mint),
        ("credential_write", credential_write),
        ("write", write),
    ):
        for op_id in op_ids:
            known = _DECLARED.get(op_id)
            if known is not None and known != op_class:
                raise ValueError(
                    f"op id {op_id!r} is already declared as {known!r}; "
                    f"cannot redeclare it as {op_class!r}"
                )
            _DECLARED[op_id] = op_class


def declared_op_class(op_id: str) -> str | None:
    """Return the declared class of *op_id*, or ``None`` when none was declared."""
    return _DECLARED.get(op_id)


def declare_safe_writes(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as ``safe`` writes: they need no approval, but they change things.

    Only a connector that registers such a write calls this, for example the
    GitHub ticket writes (``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``).
    A health check never runs a safe write, and a satellite runner never takes
    one as a read. Safe to call twice with the same ids.
    """
    _SAFE_WRITES.update(op_ids)


def is_safe_write(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_safe_writes`."""
    return op_id in _SAFE_WRITES


def declare_root_shell(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as operations that run a command as root on the host.

    A connector calls this for each operation that changes a host by running
    a command or script as root, for example over SSH with ``sudo``. A run
    approval never covers such an operation: a person approves each call
    (``docs/codebase/run-approvals.md``). The mark is separate from the op
    class, because an op id has only one class. Safe to call twice with the
    same ids.
    """
    _ROOT_SHELL.update(op_ids)


def is_root_shell(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_root_shell`."""
    return op_id in _ROOT_SHELL


def declare_per_call_approval(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as operations that a person approves call by call.

    Neither a standing grant nor a run approval covers such an operation:
    each call waits for its own approval. A connector calls this for an
    operation whose call gives rights, for example adding a member to a
    directory group. Safe to call twice with the same ids.
    """
    _PER_CALL_APPROVAL.update(op_ids)


def needs_per_call_approval(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_per_call_approval`."""
    return op_id in _PER_CALL_APPROVAL


def declare_no_secret_value(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as credential reads whose answer holds no secret value.

    A ``credential_read`` operation is at least ``caution`` and waits for a
    person's approval (:mod:`meho_backplane.operations.credential_read`).
    A read in that class whose answer holds no secret value is the
    exception: for example one that returns only key names, or only "same"
    or "different". Its connector marks it here, in code. A spec import or
    a tenant override cannot set the mark.

    Mark only a read whose answer holds no secret value also when the call
    asks for ``reveal_secret``. Safe to call twice with the same ids.
    """
    _NO_SECRET_VALUE.update(op_ids)


def returns_no_secret_value(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_no_secret_value`."""
    return op_id in _NO_SECRET_VALUE


def declare_wait_when_set(op_id: str, param_names: Iterable[str]) -> None:
    """Mark params of *op_id* that make a call wait for a person when they are set.

    A call of *op_id* that sets one of *param_names* waits for a person's
    approval on every call: neither a standing grant nor a run approval
    covers it. A call without them keeps the rules of the operation. A
    param counts as set when its value is not ``None`` and not an empty
    map or list. Safe to call twice; the names add up.
    """
    _WAIT_WHEN_SET[op_id] = _WAIT_WHEN_SET.get(op_id, frozenset()) | frozenset(param_names)


def waits_when_set(op_id: str, params: Mapping[str, Any]) -> bool:
    """Return ``True`` when *params* set a param marked with :func:`declare_wait_when_set`."""
    return any(
        params.get(name) is not None and params.get(name) not in ({}, [])
        for name in _WAIT_WHEN_SET.get(op_id, ())
    )


def declare_credential_capable(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as operations that can read, write or make a credential.

    For an operation whose call can do anything the caller asks, for
    example run a command in a container or send a raw write to a vendor
    API. The op class stays as it is, so the approval card and the
    broadcast still show the call. The credential rule holds for every
    call: a person approves each one, and no standing grant, no agent
    grant and no run approval covers it
    (:func:`~meho_backplane.operations.service_grants.credential_grant_reason`).
    Safe to call twice with the same ids.
    """
    _CREDENTIAL_CAPABLE.update(op_ids)


def declare_sandbox_delete_grant(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as ops a sandbox delete grant may cover, although they always wait.

    An op marked here waits for a person on every call because its
    connector marked it with :func:`declare_credential_capable`. In sandbox
    mode a live delete grant may still cover it, and only a delete grant:
    no standing grant, no agent grant and no run approval does. Mark only
    an op that has a delete sign and no credential class. Raises
    :exc:`ValueError` when an op id has a declared credential class
    (:func:`declared_op_class`). Safe to call twice with the same ids.
    """
    op_ids = tuple(op_ids)
    for op_id in op_ids:
        op_class = declared_op_class(op_id)
        if op_class is not None and op_class.startswith("credential_"):
            raise ValueError(
                f"op id {op_id!r} is declared as {op_class!r}; "
                "a sandbox delete grant never covers a credential operation"
            )
    _SANDBOX_DELETE_GRANT.update(op_ids)


def sandbox_delete_grant_allowed(op_id: str) -> bool:
    """Return ``True`` when *op_id* is marked with :func:`declare_sandbox_delete_grant`.

    ``False`` for an op id that has a declared credential class, also when
    the class was declared after the mark.
    """
    op_class = declared_op_class(op_id)
    if op_class is not None and op_class.startswith("credential_"):
        return False
    return op_id in _SANDBOX_DELETE_GRANT


def declare_credential_capable_match(match: Callable[[str], bool]) -> None:
    """Mark every op id for which *match* returns ``True`` as credential capable.

    The same rule as :func:`declare_credential_capable`, for a family of op
    ids that a connector cannot list one by one, for example the operations
    an ingest makes from a vendor API file (``POST:/api/v1/...``). A match
    that raises counts as ``True``. Safe to call twice with the same
    function.
    """
    if match not in _CREDENTIAL_CAPABLE_MATCHES:
        _CREDENTIAL_CAPABLE_MATCHES.append(match)


def _matches_credential_capable(op_id: str) -> bool:
    for match in _CREDENTIAL_CAPABLE_MATCHES:
        try:
            if match(op_id):
                return True
        except Exception:
            return True
    return False


def is_credential_capable(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* as credential capable.

    By :func:`declare_credential_capable` or by a match of
    :func:`declare_credential_capable_match`.
    """
    return op_id in _CREDENTIAL_CAPABLE or _matches_credential_capable(op_id)


def declare_credential_check(op_id: str, check: Callable[[Mapping[str, Any]], bool]) -> None:
    """Register *check* to tell from the params whether a call of *op_id* handles a credential.

    *check* gets the params of one call and returns ``True`` when the call
    writes or makes a credential, for example a manifest that holds a
    Kubernetes Secret. Such a call waits for a person, and no standing
    grant, no agent grant and no run approval covers it. A call for which
    *check* returns ``False`` keeps the rules of the operation. A new
    check for the same op id replaces the old one.
    """
    _CREDENTIAL_CHECKS[op_id] = check


def params_make_credential_call(op_id: str, params: Mapping[str, Any]) -> bool:
    """Return ``True`` when the check of :func:`declare_credential_check` flags *params*.

    A check that raises counts as ``True``: a call MEHO cannot read waits.
    """
    check = _CREDENTIAL_CHECKS.get(op_id)
    if check is None:
        return False
    try:
        return bool(check(params))
    except Exception:
        return True


def call_always_waits(op_id: str, params: Mapping[str, Any]) -> bool:
    """Return ``True`` when a call of *op_id* with *params* waits for a person every time.

    The call of an operation marked credential capable, a call whose params
    a credential check flags (:func:`params_make_credential_call`), and a
    call that sets a param of :func:`declare_wait_when_set`. A sensor never
    runs such a call: it runs with no person.
    """
    return (
        is_credential_capable(op_id)
        or params_make_credential_call(op_id, params)
        or waits_when_set(op_id, params)
    )


def declare_hidden_stored_param(
    op_id: str, name: str, check: Callable[[Mapping[str, Any]], bool]
) -> None:
    """Hide the param *name* of a stored request of *op_id* when *check* flags its params.

    A stored approval request keeps its params only until the run starts or
    the request ends without a run. Then MEHO replaces the value of *name*
    with a marker when *check* returns ``True`` for the params, the same way
    it replaces a secret-named value. For example the manifest of a
    ``k8s.apply`` that holds a Kubernetes Secret. A new entry for the same
    op id replaces the old one.
    """
    _HIDDEN_STORED_PARAMS[op_id] = (name, check)


def hidden_stored_param(op_id: str, params: Mapping[str, Any]) -> str | None:
    """The param of *params* that :func:`declare_hidden_stored_param` hides, or ``None``.

    ``None`` when *op_id* has no entry, *params* has no such param, or the
    check returns ``False``. A check that raises hides the param.
    """
    entry = _HIDDEN_STORED_PARAMS.get(op_id)
    if entry is None or entry[0] not in params:
        return None
    name, check = entry
    try:
        flagged = bool(check(params))
    except Exception:
        flagged = True
    return name if flagged else None


def declare_secret_field(op_id: str, *path: str) -> None:
    """Mark the field at *path* of the params of *op_id* as one that may hold a secret.

    *path* is one name for a top-level param, or several names for a field
    inside an object (``"config", "text"``). A call to *op_id* that sets such a
    field waits for a person like any call, and MEHO keeps the field only in
    the encrypted one-time store of the request: the stored request shows a
    marker. Use it for a free text field that a caller may type a secret
    into, such as a note, a config text or a command line. An op id can mark
    many fields. Marking a field twice changes nothing.
    """
    if not path or not all(isinstance(part, str) and part for part in path):
        raise ValueError("a secret field needs a path of one or more names")
    _SECRET_FIELDS.setdefault(op_id, set()).add(tuple(path))


def secret_field_paths(op_id: str, params: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """The paths of :func:`declare_secret_field` that *params* sets, longest first.

    A field counts when it is present and its value is not ``None``.
    """
    found: list[tuple[str, ...]] = []
    for path in _SECRET_FIELDS.get(op_id, ()):
        node: Any = params
        for part in path:
            if not isinstance(node, Mapping) or part not in node:
                break
            node = node[part]
        else:
            if node is not None:
                found.append(path)
    return sorted(found, key=lambda path: (-len(path), path))


def declared_op_ids(op_class: str) -> frozenset[str]:
    """Return every op id declared with *op_class*."""
    return frozenset(op_id for op_id, known in _DECLARED.items() if known == op_class)


def _reset_declared_op_classes_for_testing() -> None:
    """Forget every declaration. Test-only."""
    _DECLARED.clear()
    _SAFE_WRITES.clear()
    _ROOT_SHELL.clear()
    _PER_CALL_APPROVAL.clear()
    _NO_SECRET_VALUE.clear()
    _WAIT_WHEN_SET.clear()
    _CREDENTIAL_CAPABLE.clear()
    _CREDENTIAL_CAPABLE_MATCHES.clear()
    _SANDBOX_DELETE_GRANT.clear()
    _CREDENTIAL_CHECKS.clear()
    _HIDDEN_STORED_PARAMS.clear()
    _SECRET_FIELDS.clear()
