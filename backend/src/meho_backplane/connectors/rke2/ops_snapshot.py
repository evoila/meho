# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safe (non-gated) managed-etcd snapshot op for :class:`Rke2SshConnector`.

G-Node/RKE2-T4 (#2431) -- ``rke2.etcd-snapshot.save`` triggers an
on-demand RKE2 managed-etcd snapshot over SSH. It is the lone
**non-gated** op in the Initiative #2172 surface: ``safety_level="safe"``,
``requires_approval=False`` -- deliberately, because it is read-only with
respect to *running* cluster state (it copies the embedded etcd store to
a file on disk) and its result carries only a snapshot name + path, never
secret material.

Design notes
------------

* **Precondition guard (fail-closed).** A managed-etcd snapshot is only
  meaningful on a *server* node running *embedded* etcd. The guard runs
  first and refuses -- with a structured
  :class:`Rke2SnapshotPreconditionError` -- when the node configures an
  external ``datastore-endpoint`` (snapshots are unavailable there) or
  when the embedded-etcd data directory is absent (an agent node, or a
  server not yet initialised). No snapshot command runs on a refusal.

* **Name bounding (fail-closed, defence-in-depth).** The single optional
  ``name`` parameter is charset-bounded to ``^[A-Za-z0-9._-]+$`` at the
  JSON-schema boundary AND re-checked in the handler before any command
  is composed, mirroring the proxmox name-bounding mold. The value is
  ``shlex.quote``'d into the argv regardless. No other flag is exposed;
  the ``rke2`` binary is invoked by absolute path.

* **Privilege.** The ``rke2`` binary and the config / etcd paths are
  root-owned, so every remote command runs under ``sudo -n`` (non-
  interactive). ``sudo -n`` needs no password on a root or NOPASSWD-sudo
  operator account (the expected RKE2 node access model) and fails
  *closed* -- exiting non-zero with ``sudo: a password is required`` --
  rather than hanging when a password would be needed. The op carries no
  secret in its argv, so the password-hiding
  :meth:`Bind9Connector._remote_bash_with_sudo` mold (built for ops that
  interpolate a credential) is not needed here; keeping the op self-
  contained also lets this PR union cleanly with the concurrent
  approval-gated write-op tasks (#2429/#2430) that add that primitive.

* **No secret in the result.** ``rke2 etcd-snapshot save`` logs
  ``Snapshot <name> saved.``; the handler parses that name and returns
  ``{snapshot_name, path, exit_status}``. The snapshot *file* holds etcd
  bootstrap data, but the result envelope (and thus the audit
  ``raw_payload``) does not, so no redaction pin is required.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.rke2.ops import SSH_TRANSPORT_NOTE, Rke2Op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.rke2.connector import Rke2SshConnector, Target

__all__ = [
    "SNAPSHOT_DEFAULT_DIR",
    "SNAPSHOT_OPS",
    "Rke2SnapshotError",
    "Rke2SnapshotNameError",
    "Rke2SnapshotPreconditionError",
    "parse_saved_snapshot_name",
    "rke2_etcd_snapshot_save",
]


#: Absolute path to the RKE2 binary the installer drops on server nodes.
#: Always invoked by absolute path so the op never depends on the login
#: shell PATH (the fingerprint probe already tolerates a PATH-less binary,
#: but a maintenance op must be deterministic).
_RKE2_BIN: str = "/var/lib/rancher/rke2/bin/rke2"

#: RKE2's default managed-etcd snapshot directory
#: (``${data-dir}/db/snapshots`` with ``data-dir=/var/lib/rancher/rke2``).
#: Used to compose the returned ``path`` from the parsed snapshot name.
SNAPSHOT_DEFAULT_DIR: str = "/var/lib/rancher/rke2/server/db/snapshots"

#: The RKE2 config file the guard inspects for an external datastore.
_RKE2_CONFIG_PATH: str = "/etc/rancher/rke2/config.yaml"

#: The embedded-etcd data directory. Its presence is the server + embedded
#: signal the guard requires before allowing a snapshot.
_RKE2_ETCD_DIR: str = "/var/lib/rancher/rke2/server/db/etcd"

#: The charset an operator-supplied snapshot ``name`` is bounded to, at
#: both the schema boundary (``pattern``) and in the handler (re-check).
_SNAPSHOT_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+$")

#: ``rke2 etcd-snapshot save`` logs ``Snapshot <name> saved.`` (the name
#: only, not a path) via logrus. The regex recovers ``<name>`` from
#: stdout or stderr; the returned ``path`` is composed from it.
_SNAPSHOT_SAVED_RE: re.Pattern[str] = re.compile(r"Snapshot\s+(\S+)\s+saved")

#: Precondition guard: emit a single sentinel token describing the node's
#: snapshot eligibility. ``external-datastore`` when a ``datastore-endpoint``
#: is configured (snapshots meaningless), ``no-embedded-etcd`` when the
#: embedded-etcd data dir is absent (agent node / uninitialised server),
#: ``ok`` otherwise. Fixed constant -- no operator input is interpolated.
_GUARD_SCRIPT: str = (
    f'if grep -Eq "^[[:space:]]*datastore-endpoint" {shlex.quote(_RKE2_CONFIG_PATH)} '
    f"2>/dev/null; then echo external-datastore; "
    f"elif [ ! -d {shlex.quote(_RKE2_ETCD_DIR)} ]; then echo no-embedded-etcd; "
    f"else echo ok; fi"
)

#: The guard command as run over SSH -- ``sudo -n`` because the inspected
#: paths are root-owned (config.yaml is ``0600 root:root``).
_GUARD_CMD: str = "sudo -n -- sh -c " + shlex.quote(_GUARD_SCRIPT)


class Rke2SnapshotNameError(ValueError):
    """A supplied snapshot ``name`` violates ``^[A-Za-z0-9._-]+$``."""


class Rke2SnapshotPreconditionError(RuntimeError):
    """The node is not an embedded-etcd server -- a snapshot is refused."""


class Rke2SnapshotError(RuntimeError):
    """``rke2 etcd-snapshot save`` exited non-zero."""


def parse_saved_snapshot_name(output: str) -> str | None:
    """Return the snapshot name from ``rke2 etcd-snapshot save`` output.

    ``rke2`` logs ``Snapshot <name> saved.`` on success (name only, not a
    path). Returns the parsed ``<name>`` or ``None`` when the line is
    absent (e.g. an output format drift).

    Examples
    --------

    >>> parse_saved_snapshot_name("INFO[0000] Snapshot on-demand-srv-0-171 saved.")
    'on-demand-srv-0-171'
    >>> parse_saved_snapshot_name("") is None
    True
    """
    match = _SNAPSHOT_SAVED_RE.search(output)
    return match.group(1) if match else None


def _validate_name(name: Any) -> str | None:
    """Fail-closed re-check of the optional ``name`` param.

    Returns the validated name (or ``None`` when unset). Raises
    :class:`Rke2SnapshotNameError` for a non-string or a value outside
    ``^[A-Za-z0-9._-]+$`` -- the same bound the schema enforces, re-applied
    in code because the schema is advisory once params reach the handler.
    """
    if name is None:
        return None
    if not isinstance(name, str) or not _SNAPSHOT_NAME_RE.match(name):
        raise Rke2SnapshotNameError(
            "snapshot name must match ^[A-Za-z0-9._-]+$ (letters, digits, "
            "dot, underscore, hyphen); no path separators or whitespace"
        )
    return name


async def rke2_etcd_snapshot_save(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.etcd-snapshot.save``.

    Runs the precondition guard, then triggers an on-demand managed-etcd
    snapshot via ``sudo -n /var/lib/rancher/rke2/bin/rke2 etcd-snapshot
    save`` (plus ``--name <NAME>`` when supplied). Returns
    ``{snapshot_name, path, exit_status}``; the snapshot name/path are the
    only non-``exit_status`` fields and carry no secret material.

    Raises :class:`Rke2SnapshotNameError` (bad name),
    :class:`Rke2SnapshotPreconditionError` (non-server / external-datastore
    node) or :class:`Rke2SnapshotError` (non-zero ``rke2`` exit); the
    dispatcher maps each to a non-ok ``connector_error`` result. Transport
    / auth failures propagate the same way (#986).
    """
    name = _validate_name(params.get("name"))

    guard = await connector._run_command(target, _GUARD_CMD, operator=operator)
    guard_raw = guard.stdout if hasattr(guard, "stdout") else ""
    verdict = guard_raw.strip() if isinstance(guard_raw, str) else ""
    if verdict == "external-datastore":
        raise Rke2SnapshotPreconditionError(
            "this RKE2 node configures an external datastore-endpoint; "
            "managed-etcd snapshots apply to embedded etcd only"
        )
    if verdict != "ok":
        raise Rke2SnapshotPreconditionError(
            "this RKE2 node is not an embedded-etcd server "
            "(no server/db/etcd data directory); cannot take a snapshot"
        )

    argv = [_RKE2_BIN, "etcd-snapshot", "save"]
    if name is not None:
        argv += ["--name", name]
    save_cmd = "sudo -n -- " + " ".join(shlex.quote(arg) for arg in argv)
    proc = await connector._run_command(target, save_cmd, operator=operator, timeout=120.0)
    exit_status = getattr(proc, "exit_status", None)

    stdout_raw = proc.stdout if hasattr(proc, "stdout") else ""
    stderr_raw = proc.stderr if hasattr(proc, "stderr") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    stderr = stderr_raw if isinstance(stderr_raw, str) else ""

    if exit_status not in (0, None) and exit_status != 0:
        raise Rke2SnapshotError(
            f"rke2 etcd-snapshot save exited {exit_status}: {(stderr or stdout).strip()[:400]}"
        )

    snapshot_name = parse_saved_snapshot_name(stderr) or parse_saved_snapshot_name(stdout)
    path = f"{SNAPSHOT_DEFAULT_DIR}/{snapshot_name}" if snapshot_name else None
    return {
        "snapshot_name": snapshot_name,
        "path": path,
        "exit_status": exit_status,
    }


_RKE2_ETCD_SNAPSHOT_SAVE_OP = Rke2Op(
    op_id="rke2.etcd-snapshot.save",
    handler_attr="etcd_snapshot_save",
    summary="Trigger an on-demand RKE2 managed-etcd snapshot on a server node.",
    description=(
        "Runs ``rke2 etcd-snapshot save`` on an RKE2 server node over SSH "
        "to capture an on-demand snapshot of the embedded etcd datastore "
        "under ``/var/lib/rancher/rke2/server/db/snapshots``. An optional "
        "``name`` (charset-bounded to ``^[A-Za-z0-9._-]+$``) sets the "
        "snapshot base name; RKE2 appends the node + timestamp. A "
        "fail-closed precondition guard refuses a node that is not an "
        "embedded-etcd server (agent node, or one configuring an external "
        "``datastore-endpoint`` -- snapshots do not apply there). The "
        "result carries the snapshot name + path only; the snapshot FILE "
        "holds etcd bootstrap data but the result does not. Safe tier, "
        "non-gated: it copies etcd to disk without mutating running "
        "cluster state."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9._-]+$",
                "description": (
                    "Optional snapshot base name (letters, digits, dot, "
                    "underscore, hyphen). RKE2 appends node + timestamp. "
                    "Omit for the default ``on-demand`` prefix."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "snapshot_name": {"type": ["string", "null"]},
            "path": {"type": ["string", "null"]},
            "exit_status": {"type": ["integer", "null"]},
        },
        "required": ["snapshot_name", "path", "exit_status"],
        "additionalProperties": False,
    },
    group_key="rke2-etcd-snapshot",
    tags=("etcd", "snapshot", "maintenance", "rke2"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to capture an on-demand managed-etcd snapshot on an RKE2 "
            "server node before a risky change (a config edit, a token "
            "rotation, an upgrade). Returns the snapshot name + on-disk "
            "path; it never returns etcd contents. Refuses non-server / "
            "external-datastore nodes with a structured error. Safe and "
            "non-mutating to running cluster state. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "name": (
                "Optional base name matching ^[A-Za-z0-9._-]+$; RKE2 "
                "appends the node identity and a timestamp. Omit to use "
                "the default ``on-demand`` prefix."
            ),
        },
        "output_shape": (
            "``{snapshot_name, path, exit_status}``. ``snapshot_name`` is "
            "the full RKE2-assigned name (base + node + timestamp) parsed "
            "from the save log; ``path`` is it joined under "
            "``/var/lib/rancher/rke2/server/db/snapshots``; both are null "
            "if the save log line could not be parsed (with exit_status 0)."
        ),
    },
)


#: The safe (non-gated) snapshot tier. Composed alongside the read ops +
#: the approval-gated write ops in
#: :func:`meho_backplane.connectors.rke2.ops._rke2_ops`.
SNAPSHOT_OPS: tuple[Rke2Op, ...] = (_RKE2_ETCD_SNAPSHOT_SAVE_OP,)
