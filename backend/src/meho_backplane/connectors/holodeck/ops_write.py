# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated remediation write ops for :class:`HolodeckConnector` (G3.18-T2 #2154).

G3.8 (#371) shipped the connector plus 8 read ops; every one is
``requires_approval=False`` and read-only -- ``holodeck.k8s.exec``
re-validates its verb against a read-only safelist, so ``kubectl delete``
cannot pass. The VCF-9.x backup-fill outage (Initiative #2145) needed three
narrow writes the connector could not express, so recovery fell back to
local root SSH with no MEHO audit row. This module closes that gap with
three **tightly bounded** remediation writes:

==============================  ===========  =========================================
op_id                           safety       action
==============================  ===========  =========================================
``holodeck.k8s.pods.gc``        dangerous    ``kubectl delete pods --field-selector
                                             status.phase=<Failed|Succeeded>`` only
``holodeck.backups.prune``      dangerous    delete backups under ``/var/backups``,
                                             keep newest ``keep_newest``
``holodeck.images.import``      dangerous    ``ctr -n k8s.io images import <tar>``
==============================  ===========  =========================================

Approval routing (load-bearing)
===============================

Every op registers ``requires_approval=True``. The dispatcher's policy gate
(:func:`~meho_backplane.operations._validate.policy_gate`) routes a
``USER``-principal dispatch of a ``requires_approval`` op to the human
approve-queue (``needs-approval``) -- G11.7-T1 (#1401) -- rather than
hard-denying it, and floors an agent dispatch to ``needs-approval``
regardless of safety level. The handler bodies below therefore run only on
the ``_approved=True`` resume path, after a human approves the parked
request. The audit row is written by the dispatcher on that execute path
(the same ``DISPATCH`` ``AuditLog`` row every op gets). The
``requires_approval=True`` descriptor flag is the only thing the connector
controls, and it is what engages the queue.

Bounding posture (reject-before-compose)
=========================================

Each handler validates its inputs against a fixed allowlist and refuses any
value that escapes the allowed prefix / pattern **before** composing the
shell command, then :func:`shlex.quote`-quotes every interpolated token.
This mirrors the read-op ``_COMPONENT_SAFE_RE`` / ``parse_kubectl_command``
posture: the substrate is dumb (a single unchained command), the bound is a
positive allowlist, and the reject happens before any SSH traffic. No
operator-supplied value ever reaches ``self._run_command`` without passing a
bound.

``kubectl delete`` field-selector note
=======================================

``kubectl``'s ``--field-selector`` supports only ``=`` / ``==`` / ``!=`` --
**not** the set-based ``in (Failed,Succeeded)`` form (that operator exists
only for label ``--selector``; see the kubectl reference). The issue table's
``status.phase in (Failed,Succeeded)`` shorthand is therefore realised as
one equality delete per terminal phase, run in sequence, which yields the
same bound: only ``Failed`` and ``Succeeded`` pods are ever targeted, and
there is no name or label surface to widen it. Reference:
https://kubernetes.io/docs/reference/kubectl/generated/kubectl_delete/ .

References
----------

* Task: https://github.com/evoila/meho/issues/2154
* Parent initiative: https://github.com/evoila/meho/issues/2145
* Write-op mold: argocd ``ops_write.py`` (#1405), keycloak (#1406).
* Approval routing: ``policy_gate`` / G11.7-T1 #1401.
* Path-safety precedent: ``_COMPONENT_SAFE_RE`` (``ops_read.py``).
* ``shlex.quote``:
  https://docs.python.org/3/library/shlex.html#shlex.quote
* ``ctr images import``: ``ctr -n k8s.io images import <tar>`` (containerd
  CLI; the ``-n k8s.io`` namespace makes the image visible to kubelet).
* OS command-injection defence (never build commands via string concat of
  untrusted input):
  https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.holodeck.ops import SSH_TRANSPORT_NOTE, HolodeckOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

__all__ = [
    "HOLODECK_WHEN_TO_USE_WRITE_BY_GROUP",
    "WRITE_OPS",
    "HolodeckWriteSafetyError",
    "holodeck_backups_prune",
    "holodeck_images_import",
    "holodeck_k8s_pods_gc",
]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: The only pod ``status.phase`` values ``holodeck.k8s.pods.gc`` may target.
#: Both are terminal (the pod has run to completion / failure); deleting them
#: reclaims garbage without touching a live workload. ``Running`` /
#: ``Pending`` / ``Unknown`` are deliberately absent and rejected.
_TERMINAL_POD_PHASES: tuple[str, ...] = ("Failed", "Succeeded")
_TERMINAL_POD_PHASE_SET: frozenset[str] = frozenset(_TERMINAL_POD_PHASES)

#: Kubernetes namespace slug allowlist (RFC-1123 label subset). A namespace
#: is optional on ``pods.gc``; when supplied it is bound to this pattern so
#: the ``-n <ns>`` token cannot smuggle a flag or a shell metacharacter.
_NAMESPACE_SAFE_RE: re.Pattern[str] = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

#: The directory tree ``holodeck.backups.prune`` is confined to. Every
#: resolved backup path must stay strictly within this prefix; a value that
#: normalises to the directory itself, to an ancestor, or outside the tree
#: (via ``..`` traversal or an absolute-path escape) is rejected.
_BACKUPS_ROOT: str = "/var/backups"

#: ``holodeck.images.import`` accepts only a seed-image tarball living
#: directly under ``/root/containerd-images/`` with a ``.tar`` suffix. The
#: single ``*`` is a filename glob, not a path separator -- a nested path or
#: a traversal in the basename is rejected.
_IMAGE_TAR_DIR: str = "/root/containerd-images"
_IMAGE_TAR_BASENAME_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9._-]+\.tar")


class HolodeckWriteSafetyError(ValueError):
    """Raised when a write op's input escapes its declared bound.

    Subclass of :class:`ValueError` so the dispatcher's
    ``result_connector_error`` envelope picks it up uniformly. The message
    names the offending category (phase / path / tar) without echoing the
    full operator-supplied value back into user-visible surfaces.
    """


def _bound_phases(params: dict[str, Any]) -> list[str]:
    """Resolve + validate the requested terminal phases for ``pods.gc``.

    ``phases`` is optional; when omitted the op targets both terminal
    phases (``Failed`` + ``Succeeded``). When supplied it must be a
    non-empty list whose every element is in :data:`_TERMINAL_POD_PHASE_SET`.
    Any other value -- a live phase (``Running``), a wildcard, a
    non-string -- raises :exc:`HolodeckWriteSafetyError`. Returns the phases
    in the canonical :data:`_TERMINAL_POD_PHASES` order so the composed
    command is deterministic.
    """
    raw = params.get("phases")
    if raw is None:
        return list(_TERMINAL_POD_PHASES)
    if not isinstance(raw, list) or not raw:
        raise HolodeckWriteSafetyError(
            "phases must be a non-empty list drawn from ('Failed', 'Succeeded')"
        )
    requested = set()
    for phase in raw:
        if not isinstance(phase, str) or phase not in _TERMINAL_POD_PHASE_SET:
            raise HolodeckWriteSafetyError(
                f"pods.gc targets only terminal phases; allowed: {sorted(_TERMINAL_POD_PHASE_SET)}"
            )
        requested.add(phase)
    return [p for p in _TERMINAL_POD_PHASES if p in requested]


def _bound_namespace(params: dict[str, Any]) -> str | None:
    """Validate the optional ``namespace`` slug for ``pods.gc``.

    Returns ``None`` (cluster-default namespace) when unset, or the slug
    when it fully matches :data:`_NAMESPACE_SAFE_RE`. Any other shape raises
    :exc:`HolodeckWriteSafetyError`.
    """
    namespace = params.get("namespace")
    if namespace is None:
        return None
    if not isinstance(namespace, str) or not _NAMESPACE_SAFE_RE.fullmatch(namespace):
        raise HolodeckWriteSafetyError(
            "namespace must be an RFC-1123 label ([a-z0-9-], <=63 chars)"
        )
    return namespace


def bound_backup_path(raw_path: Any) -> str:
    """Return the canonical backup path, or raise if it escapes ``/var/backups``.

    A backup path is accepted only when, after POSIX normalisation, it lies
    **strictly inside** :data:`_BACKUPS_ROOT` -- i.e. its normalised form
    starts with ``/var/backups/`` and is not the root directory itself. A
    relative path is resolved against the root before normalisation, so
    ``daily/db.sql.gz`` becomes ``/var/backups/daily/db.sql.gz``; a
    traversal (``../etc/passwd``, ``daily/../../etc``) or an absolute escape
    (``/etc/shadow``) normalises outside the tree and is rejected. The
    returned path is *not* yet shell-quoted -- the caller quotes it.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HolodeckWriteSafetyError("backup path must be a non-empty string")
    candidate = raw_path if raw_path.startswith("/") else posixpath.join(_BACKUPS_ROOT, raw_path)
    normalised = posixpath.normpath(candidate)
    root_prefix = _BACKUPS_ROOT + "/"
    if not normalised.startswith(root_prefix) or normalised == _BACKUPS_ROOT:
        raise HolodeckWriteSafetyError(f"backup path must resolve strictly within {_BACKUPS_ROOT}/")
    return normalised


def bound_image_tar(raw_path: Any) -> str:
    """Return the canonical seed-image tar path, or raise if out of bounds.

    Accepts only a ``.tar`` file living directly under
    :data:`_IMAGE_TAR_DIR`. The path is POSIX-normalised, its directory must
    equal :data:`_IMAGE_TAR_DIR` exactly (no nesting, no traversal), and its
    basename must match :data:`_IMAGE_TAR_BASENAME_RE`. Mirrors the
    ``/root/containerd-images/*.tar`` glob bound from the issue. The
    returned path is not yet shell-quoted.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HolodeckWriteSafetyError("tar path must be a non-empty string")
    normalised = posixpath.normpath(raw_path)
    directory, basename = posixpath.split(normalised)
    if directory != _IMAGE_TAR_DIR or not _IMAGE_TAR_BASENAME_RE.fullmatch(basename):
        raise HolodeckWriteSafetyError(
            f"tar path must match {_IMAGE_TAR_DIR}/*.tar (single .tar file, no nesting)"
        )
    return normalised


# ---------------------------------------------------------------------------
# Handlers (bound-method shims on HolodeckConnector, resume path only)
# ---------------------------------------------------------------------------


async def _run_text(
    self: HolodeckConnector,
    target: Any,
    cmd: str,
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Run *cmd* over plain SSH, return ``{stdout, stderr, exit_status}``.

    Shared thin layer for the three write handlers: run the (already-bounded,
    already-quoted) command via the base adapter's ``_run_command`` and shape
    the completed process into the connector's standard result envelope.
    Stderr is capped at 4096 chars (the ``PwshRunError`` convention).
    """
    proc = await self._run_command(target, cmd, operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
    if not isinstance(stdout, str):
        stdout = ""
    if not isinstance(stderr, str):
        stderr = ""
    if len(stderr) > 4096:
        stderr = stderr[:4096]
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_status": getattr(proc, "exit_status", None),
    }


async def holodeck_k8s_pods_gc(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Garbage-collect terminal (Failed / Succeeded) pods on the in-appliance K8s.

    Op-id: ``holodeck.k8s.pods.gc``. Runs one
    ``kubectl delete pods --field-selector status.phase=<phase>`` per
    requested terminal phase (default: both ``Failed`` and ``Succeeded``).
    There is **no** name, label-selector, or arbitrary-field surface -- the
    only knobs are the terminal-phase set (validated against
    :data:`_TERMINAL_POD_PHASE_SET`) and an optional bounded ``namespace``.
    A phase outside the terminal set, or a malformed namespace, is rejected
    before any SSH traffic.

    Runs only on the ``_approved=True`` resume path (the op registers
    ``requires_approval=True``). Returns one result per phase under
    ``deletes`` plus the composed commands for auditability.
    """
    try:
        phases = _bound_phases(params)
        namespace = _bound_namespace(params)
    except HolodeckWriteSafetyError as exc:
        return {"deleted": False, "error": f"k8s.pods.gc safety check: {exc}"}

    ns_arg = f" -n {shlex.quote(namespace)}" if namespace is not None else ""
    deletes: list[dict[str, Any]] = []
    try:
        for phase in phases:
            selector = shlex.quote(f"status.phase={phase}")
            cmd = f"kubectl delete pods{ns_arg} --field-selector {selector}"
            outcome = await _run_text(self, target, cmd, operator)
            deletes.append({"phase": phase, "command": cmd, **outcome})
    except Exception as exc:
        return {"deleted": False, "error": str(exc), "deletes": deletes}
    return {"deleted": True, "phases": phases, "namespace": namespace, "deletes": deletes}


async def holodeck_backups_prune(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Prune backups under ``/var/backups``, keeping the newest ``keep_newest``.

    Op-id: ``holodeck.backups.prune``. *params*:

    * ``keep_newest`` -- required int >= 1; how many newest entries to keep.
    * ``path`` -- optional sub-path under ``/var/backups`` (default the root
      itself). Every candidate is resolved via :func:`bound_backup_path`,
      which rejects any value that escapes ``/var/backups/**`` (traversal or
      absolute-path escape).

    The prune lists the directory newest-first and removes everything past
    the keep window. The pipeline is composed from bounded, shell-quoted
    tokens; the directory listing runs under ``find`` with ``-maxdepth 1`` so
    the prune never recurses into a subtree. Runs only on the
    ``_approved=True`` resume path.
    """
    keep_newest = params.get("keep_newest")
    if not isinstance(keep_newest, int) or isinstance(keep_newest, bool) or keep_newest < 1:
        return {"pruned": False, "error": "keep_newest must be an integer >= 1"}

    target_dir = params.get("path", _BACKUPS_ROOT)
    if target_dir == _BACKUPS_ROOT:
        resolved_dir = _BACKUPS_ROOT
    else:
        try:
            resolved_dir = bound_backup_path(target_dir)
        except HolodeckWriteSafetyError as exc:
            return {"pruned": False, "error": f"backups.prune safety check: {exc}"}

    quoted_dir = shlex.quote(resolved_dir)
    # List regular files newest-first (mtime), drop the newest keep_newest,
    # delete the rest. All tokens are literals or bounded/quoted; the ``+N``
    # tail offset is 1-based, hence keep_newest + 1.
    tail_from = keep_newest + 1
    cmd = (
        f"find {quoted_dir} -maxdepth 1 -type f -printf '%T@ %p\\n' "
        f"| sort -rn | tail -n +{tail_from} | cut -d' ' -f2- "
        f"| xargs -r -d '\\n' rm -f --"
    )
    try:
        outcome = await _run_text(self, target, cmd, operator)
    except Exception as exc:
        return {"pruned": False, "error": str(exc)}
    return {
        "pruned": True,
        "directory": resolved_dir,
        "keep_newest": keep_newest,
        "command": cmd,
        **outcome,
    }


async def holodeck_images_import(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Import a seed-image tarball into containerd's ``k8s.io`` namespace.

    Op-id: ``holodeck.images.import``. Runs
    ``ctr -n k8s.io images import <tar>`` where ``<tar>`` must match
    ``/root/containerd-images/*.tar`` (validated via
    :func:`bound_image_tar`). The ``-n k8s.io`` namespace makes the imported
    image visible to kubelet. *params* carries ``tar_path``. Runs only on
    the ``_approved=True`` resume path.
    """
    try:
        tar_path = bound_image_tar(params.get("tar_path"))
    except HolodeckWriteSafetyError as exc:
        return {"imported": False, "error": f"images.import safety check: {exc}"}

    cmd = f"ctr -n k8s.io images import {shlex.quote(tar_path)}"
    try:
        outcome = await _run_text(self, target, cmd, operator)
    except Exception as exc:
        return {"imported": False, "error": str(exc)}
    return {"imported": True, "tar_path": tar_path, "command": cmd, **outcome}


# ---------------------------------------------------------------------------
# Curated when_to_use blurbs (one per write op group)
# ---------------------------------------------------------------------------

_WHEN_TO_USE_K8S_WRITE = (
    "Use to reclaim terminal (Failed / Succeeded) pods on the HoloRouter's "
    "in-appliance K8s cluster: ``holodeck.k8s.pods.gc`` runs "
    "``kubectl delete pods --field-selector status.phase=Failed`` (and "
    "``=Succeeded``). It is approval-gated -- a dispatch parks for a human "
    "to approve before anything is deleted -- and it CANNOT target a named "
    "pod, a label selector, or a live phase (Running / Pending). The right "
    "op when terminal-pod garbage is filling the node and the operator wants "
    "a bounded cleanup, not an arbitrary ``kubectl delete``. " + SSH_TRANSPORT_NOTE
)

_WHEN_TO_USE_BACKUPS_WRITE = (
    "Use to prune old backup artefacts under ``/var/backups`` on the "
    "HoloRouter: ``holodeck.backups.prune keep_newest=N`` keeps the newest N "
    "entries and removes the rest. Approval-gated. Every candidate path is "
    "confined to ``/var/backups/**`` -- a traversal or absolute-path escape "
    "is rejected before anything is removed. The right op when a backup-fill "
    "outage needs disk reclaimed without hand-run root SSH. " + SSH_TRANSPORT_NOTE
)

_WHEN_TO_USE_IMAGES_WRITE = (
    "Use to seed a container image into the HoloRouter's containerd from a "
    "pre-staged tarball: ``holodeck.images.import tar_path=/root/"
    "containerd-images/<name>.tar`` runs ``ctr -n k8s.io images import`` so "
    "the image becomes visible to kubelet. Approval-gated; the tar path must "
    "match ``/root/containerd-images/*.tar``. The right op when recovering a "
    "cluster whose registry is unreachable and images must be side-loaded. " + SSH_TRANSPORT_NOTE
)

#: Curated ``when_to_use`` blurb per write op group. Keys carry a ``-write``
#: suffix so they never collide with the read-op group keys the connector's
#: ``register_operations`` walk merges alongside them.
HOLODECK_WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "k8s-write": _WHEN_TO_USE_K8S_WRITE,
    "backups-write": _WHEN_TO_USE_BACKUPS_WRITE,
    "images-write": _WHEN_TO_USE_IMAGES_WRITE,
}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_WRITE_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


WRITE_OPS: tuple[HolodeckOp, ...] = (
    HolodeckOp(
        op_id="holodeck.k8s.pods.gc",
        handler_attr="k8s_pods_gc",
        summary="Delete terminal (Failed/Succeeded) pods on the in-appliance K8s (approval-gated).",
        description=(
            "Runs ``kubectl delete pods --field-selector status.phase=Failed`` "
            "and/or ``status.phase=Succeeded`` on the HoloRouter's "
            "in-appliance Kubernetes cluster to reclaim terminal-pod garbage. "
            "There is NO name, label-selector, or arbitrary-field surface: "
            "the only inputs are the terminal-phase set (default both; each "
            "element must be Failed or Succeeded) and an optional bounded "
            "namespace. A live phase (Running/Pending) or a malformed "
            "namespace is rejected before any SSH traffic. kubectl's "
            "--field-selector supports only equality, so one delete runs per "
            "phase. safety_level=dangerous, requires_approval=True -- parks "
            "for human approval before deleting anything."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "phases": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_TERMINAL_POD_PHASES)},
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": (
                        "Terminal pod phases to garbage-collect. Only "
                        "'Failed' and 'Succeeded' are allowed; omit to target "
                        "both. Running/Pending/Unknown are rejected."
                    ),
                },
                "namespace": {
                    "type": "string",
                    "pattern": r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$",
                    "description": (
                        "Optional Kubernetes namespace to scope the GC to "
                        "(RFC-1123 label). Omit for the cluster default."
                    ),
                },
            },
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="k8s-write",
        tags=("write", "k8s", "kubectl", "gc", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_K8S_WRITE,
            "parameter_hints": {
                "phases": "Omit to GC both Failed and Succeeded; never accepts Running.",
                "namespace": "Optional; RFC-1123 label. Omit for the default namespace.",
            },
            "output_shape": (
                "{deleted, phases, namespace, deletes: [{phase, command, "
                "stdout, stderr, exit_status}]}. One deletes[] entry per "
                "phase. On a bound violation: {deleted: false, error}."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.backups.prune",
        handler_attr="backups_prune",
        summary="Prune old backups under /var/backups keeping newest-N (approval-gated).",
        description=(
            "Removes backup artefacts under /var/backups on the HoloRouter, "
            "keeping the newest ``keep_newest`` entries (by mtime). An "
            "optional ``path`` sub-directory is resolved and confined to "
            "/var/backups/** -- any traversal or absolute-path escape is "
            "rejected before anything is removed. The listing runs at "
            "-maxdepth 1 so the prune never recurses into a subtree. "
            "safety_level=dangerous, requires_approval=True -- parks for "
            "human approval before deleting anything."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "keep_newest": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "How many newest backup entries to KEEP; everything "
                        "older is removed. Required."
                    ),
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional sub-path under /var/backups (absolute or "
                        "relative to it). Must resolve strictly within "
                        "/var/backups/**. Omit to prune the root."
                    ),
                },
            },
            "required": ["keep_newest"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="backups-write",
        tags=("write", "backups", "prune", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_BACKUPS_WRITE,
            "parameter_hints": {
                "keep_newest": "Count of newest entries to keep (>=1); the rest are removed.",
                "path": "Optional; must stay within /var/backups/**. Omit to prune the root.",
            },
            "output_shape": (
                "{pruned, directory, keep_newest, command, stdout, stderr, "
                "exit_status}. On a bound violation: {pruned: false, error}."
            ),
        },
    ),
    HolodeckOp(
        op_id="holodeck.images.import",
        handler_attr="images_import",
        summary="Import a seed-image tarball into containerd (k8s.io) (approval-gated).",
        description=(
            "Runs ``ctr -n k8s.io images import <tar>`` on the HoloRouter to "
            "side-load a container image from a pre-staged tarball so kubelet "
            "can see it. The tar path must match /root/containerd-images/*.tar "
            "(single .tar file, no nesting, no traversal) -- any other path "
            "is rejected before any SSH traffic. safety_level=dangerous, "
            "requires_approval=True -- parks for human approval before "
            "importing."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "tar_path": {
                    "type": "string",
                    "pattern": r"^/root/containerd-images/[A-Za-z0-9._-]+\.tar$",
                    "description": (
                        "Absolute path to the image tarball; must match "
                        "/root/containerd-images/*.tar."
                    ),
                },
            },
            "required": ["tar_path"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="images-write",
        tags=("write", "images", "containerd", "ctr", "holodeck"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_IMAGES_WRITE,
            "parameter_hints": {
                "tar_path": "Must be /root/containerd-images/<name>.tar; nothing else is accepted.",
            },
            "output_shape": (
                "{imported, tar_path, command, stdout, stderr, exit_status}. "
                "On a bound violation: {imported: false, error}."
            ),
        },
    ),
)
