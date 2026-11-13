# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safe (non-gated) managed-etcd snapshot ops for :class:`Rke2SshConnector`.

Two safe, ``requires_approval=False`` snapshot ops share this module and
the same embedded-etcd-server precondition guard:

* ``rke2.etcd-snapshot.save`` (G-Node/RKE2-T4 #2431) -- triggers an
  on-demand RKE2 managed-etcd snapshot over SSH. Safe / non-gated because
  it is read-only with respect to *running* cluster state (it copies the
  embedded etcd store to a file on disk); its result carries only a
  snapshot name + path, never secret material. It is *active* (it writes
  a file), so it is deliberately NOT ``read-only``-tagged.
* ``rke2.etcd-snapshot.list`` (#2853) -- enumerates the managed-etcd
  snapshots that already exist on a server node (``rke2 etcd-snapshot
  list``). It is a genuine read (it enumerates, mutates nothing), so it
  carries the ``read-only`` tag. It verifies a fresh post-rotation
  snapshot landed -- the ``claude-rdc-hetzner-dc#615`` rotation-runbook
  step that ``.save`` alone could not confirm.

A snapshot listing carries names / locations / sizes / timestamps only,
never etcd contents, so -- like ``.save`` -- no result-envelope secret can
leak.

Design notes
------------

* **Precondition guard (fail-closed), shared by both ops.** A managed-etcd
  snapshot (save or list) is only meaningful on a *server* node running
  *embedded* etcd. :func:`_run_precondition_guard` runs first and refuses
  -- with a structured :class:`Rke2SnapshotPreconditionError` -- when the
  node configures an external ``datastore-endpoint`` (snapshots are
  unavailable there) or when the embedded-etcd data directory is absent
  (an agent node, or a server not yet initialised). No snapshot command
  runs on a refusal.

* **Name bounding (fail-closed, defence-in-depth).** The single optional
  ``name`` parameter is charset-bounded to ``^[A-Za-z0-9._-]+$`` at the
  JSON-schema boundary AND re-checked in the handler before any command
  is composed, mirroring the proxmox name-bounding mold. The value is
  ``shlex.quote``'d into the argv regardless. No other flag is exposed;
  the ``rke2`` binary is invoked by absolute path.

* **Privilege.** The ``rke2`` binary and the config / etcd paths are
  root-owned, and ``/var/lib/rancher/rke2/server/`` is ``0700 root:root``.
  The guard first checks the login's user id. A root login runs both
  remote commands via plain :meth:`SshConnector._run_command`. A normal
  user (the usual target setup) gets the verdict ``needs-root``; the ops
  then run the guard and the ``rke2`` command through the sanctioned
  safe-sudo primitive
  (:func:`~meho_backplane.connectors.rke2._sudo.run_remote_bash_with_sudo`),
  using the sudo password from the target's Vault secret. This file never
  builds a ``sudo`` argv itself, so it stays clear of the repo-wide
  sudo-guard invariant. A normal user whose secret has no password gets a
  clear "needs root" error, never "not an embedded-etcd server".

* **No secret in the result.** ``rke2 etcd-snapshot save`` logs
  ``Snapshot <name> saved.``; the handler parses that name and returns
  ``{snapshot_name, path, exit_status}``. ``rke2 etcd-snapshot list``
  prints a ``Name / Location / Size / Created`` table; the handler parses
  it into ``{snapshots: [{name, location, size_bytes, created_at}, ...]}``.
  The snapshot *files* hold etcd bootstrap data, but neither result
  envelope (and thus neither audit ``raw_payload``) does, so no redaction
  pin is required.

* **List output parsing (version-drift-resilient).** RKE2's own docs
  (``docs.rke2.io/datastore/backup_restore``) document only the fixed
  ``Name / Location / Size / Created`` table for ``etcd-snapshot list``;
  a ``-o json`` flag was requested upstream (``k3s-io/k3s#5130``) but its
  schema and per-version availability are NOT documented, so this handler
  parses the documented table rather than an unconfirmed JSON shape. The
  ``Location`` column varies across versions (``local`` / a ``file://``
  URL / a bare path / an ``s3://`` URL) and the ``Size`` column may be a
  raw byte count or a human string, so each row is matched by a regex that
  anchors the ``Created`` column as an ISO-8601 timestamp (which skips the
  header line regardless of casing) and passes ``location`` through
  verbatim; ``size_bytes`` is the integer byte count when the ``Size``
  column is a bare integer and ``null`` otherwise (fail-closed, never a
  guessed conversion).
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.rke2._sudo import run_remote_bash_with_sudo
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
    "parse_snapshot_list",
    "rke2_etcd_snapshot_list",
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

#: One row of ``rke2 etcd-snapshot list``'s ``Name / Location / Size /
#: Created`` table. ``name`` and ``location`` are single whitespace-free
#: tokens (a charset-bounded snapshot name; a ``local`` / ``file://`` /
#: bare-path / ``s3://`` location -- none contain spaces). ``size`` is
#: lazy so a human-readable ``Size`` column (``50 MiB``) is captured whole
#: even though the documented format is a bare byte count. ``created`` is
#: anchored to an ISO-8601 timestamp, which is what lets this same regex
#: skip the header row (its ``Created`` label is not a timestamp) without
#: depending on the header's casing.
_SNAPSHOT_LIST_ROW_RE: re.Pattern[str] = re.compile(
    r"^(?P<name>\S+)\s+(?P<location>\S+)\s+(?P<size>.+?)\s+"
    r"(?P<created>\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+)\s*$"
)

#: Precondition guard: emit a single sentinel token describing the node's
#: snapshot eligibility. ``needs-root`` when the login is not root (a normal
#: user cannot look inside the ``0700`` server directory, so the other
#: verdicts would be wrong), ``external-datastore`` when a ``datastore-endpoint``
#: is configured (snapshots meaningless), ``no-embedded-etcd`` when the
#: embedded-etcd data dir is absent (agent node / uninitialised server),
#: ``ok`` otherwise. Fixed constant -- no operator input is interpolated.
_GUARD_SCRIPT: str = (
    'if [ "$(id -u)" != 0 ]; then echo needs-root; '
    f'elif grep -Eq "^[[:space:]]*datastore-endpoint" {shlex.quote(_RKE2_CONFIG_PATH)} '
    f"2>/dev/null; then echo external-datastore; "
    f"elif [ ! -d {shlex.quote(_RKE2_ETCD_DIR)} ]; then echo no-embedded-etcd; "
    f"else echo ok; fi"
)

#: The guard command as run over SSH with a root login (config.yaml is
#: ``0600 root:root``; the etcd dir is root-owned), via plain
#: ``_run_command``. A non-root login gets ``needs-root`` and the ops re-run
#: it through the safe-sudo primitive (the script, not this ``sh -c`` form).
_GUARD_CMD: str = "sh -c " + shlex.quote(_GUARD_SCRIPT)


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


def parse_snapshot_list(output: str) -> list[dict[str, Any]]:
    """Parse ``rke2 etcd-snapshot list`` table output into snapshot rows.

    RKE2 prints a ``Name / Location / Size / Created`` table. Each data row
    is turned into ``{"name", "location", "size_bytes", "created_at"}``:

    * ``name`` / ``location`` -- passed through verbatim (``location`` is
      whatever the vendor emits: ``local``, a ``file://`` URL, a bare path,
      or an ``s3://`` URL).
    * ``size_bytes`` -- the integer byte count when the ``Size`` column is
      a bare integer (the documented format), else ``None`` -- never a
      guessed unit conversion.
    * ``created_at`` -- the ISO-8601 timestamp string.

    The header row and any non-matching line (a banner, a blank line, a
    "no snapshots" notice) are skipped: only lines whose final column is an
    ISO-8601 timestamp are treated as rows, so the header's ``Created``
    label -- whatever its casing -- never parses as a snapshot.

    Examples
    --------

    >>> rows = parse_snapshot_list(
    ...     "Name  Location  Size  Created\\n"
    ...     "on-demand-srv-0-1  local  52428800  2026-08-06T09:12:03Z\\n"
    ... )
    >>> rows == [{
    ...     "name": "on-demand-srv-0-1", "location": "local",
    ...     "size_bytes": 52428800, "created_at": "2026-08-06T09:12:03Z",
    ... }]
    True
    """
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = _SNAPSHOT_LIST_ROW_RE.match(line.strip())
        if match is None:
            continue
        size_raw = match.group("size").strip()
        rows.append(
            {
                "name": match.group("name"),
                "location": match.group("location"),
                "size_bytes": int(size_raw) if size_raw.isdigit() else None,
                "created_at": match.group("created"),
            }
        )
    return rows


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


async def _resolve_sudo_password(
    connector: Rke2SshConnector, target: Target, operator: Operator | None
) -> str | None:
    """Return the sudo password from the target's Vault secret, or ``None``.

    Keys on ``sudo_password`` first, then the SSH ``password`` (the same
    convention as ``rke2.token.rotate``).
    """
    secret = await connector._resolve_secret(target, operator)
    password = secret.get("sudo_password") or secret.get("password")
    return strip_credential_value(password) if password else None


async def _run_guard(
    connector: Rke2SshConnector,
    target: Target,
    operator: Operator | None,
    sudo_password: str | None,
) -> Any:
    """Run the guard script plain, or under sudo when *sudo_password* is set."""
    if sudo_password is None:
        return await connector._run_command(target, _GUARD_CMD, operator=operator)
    return await run_remote_bash_with_sudo(
        connector, target, _GUARD_SCRIPT, operator=operator, sudo_password=sudo_password
    )


async def _run_precondition_guard(
    connector: Rke2SshConnector,
    target: Target,
    operator: Operator | None = None,
) -> str | None:
    """Run the shared embedded-etcd-server precondition guard.

    Both snapshot ops (``.save`` and ``.list``) enforce the identical
    precondition through this one function. Returns the sudo password to
    run the ``rke2`` command with, or ``None`` when the login is root and
    the command runs plain. Otherwise raises:

    * :class:`Rke2SnapshotError` when the guard itself could not run over
      SSH. ``_run_command`` wraps ``conn.run(check=False)``, so a
      transport / SSH / shell failure returns a non-zero exit with
      (typically) empty stdout; the guard prints a sentinel token and
      exits 0 on every *real* verdict, so a non-zero exit is unambiguously
      an infrastructure failure (also a failed ``sudo``). Interpreting that
      empty verdict as "not an embedded-etcd server" would mislabel it.
    * :class:`Rke2SnapshotPreconditionError` on ``needs-root`` with no sudo
      password, on ``external-datastore`` (snapshots do not apply to an
      external datastore-endpoint) or any other non-``ok`` verdict (agent
      node / uninitialised server).
    """
    sudo_password: str | None = None
    guard = await _run_guard(connector, target, operator, None)
    if _guard_verdict(guard) == "needs-root":
        sudo_password = await _resolve_sudo_password(connector, target, operator)
        if sudo_password is None:
            raise Rke2SnapshotPreconditionError(
                "this operation needs root; the target logs in as a normal user "
                "and its Vault secret has no sudo_password or password"
            )
        guard = await _run_guard(connector, target, operator, sudo_password)
    guard_exit = getattr(guard, "exit_status", None)
    if guard_exit not in (0, None):
        guard_err = getattr(guard, "stderr", "") if hasattr(guard, "stderr") else ""
        guard_err_txt = guard_err.strip()[:400] if isinstance(guard_err, str) else ""
        raise Rke2SnapshotError(
            "the snapshot precondition guard failed to run over SSH "
            f"(exit {guard_exit}): {guard_err_txt or 'no stderr'}"
        )
    verdict = _guard_verdict(guard)
    if verdict == "external-datastore":
        raise Rke2SnapshotPreconditionError(
            "this RKE2 node configures an external datastore-endpoint; "
            "managed-etcd snapshots apply to embedded etcd only"
        )
    if verdict != "ok":
        raise Rke2SnapshotPreconditionError(
            "this RKE2 node is not an embedded-etcd server "
            "(no server/db/etcd data directory); cannot use managed-etcd snapshots"
        )
    return sudo_password


def _guard_verdict(guard: Any) -> str:
    """Return the stripped guard sentinel token, or ``""`` when unreadable."""
    guard_raw = guard.stdout if hasattr(guard, "stdout") else ""
    return guard_raw.strip() if isinstance(guard_raw, str) else ""


async def _run_snapshot_command(
    connector: Rke2SshConnector,
    target: Target,
    cmd: str,
    operator: Operator | None,
    sudo_password: str | None,
    timeout: float,
) -> Any:
    """Run the ``rke2`` command plain, or under sudo when *sudo_password* is set."""
    if sudo_password is None:
        return await connector._run_command(target, cmd, operator=operator, timeout=timeout)
    return await run_remote_bash_with_sudo(
        connector, target, cmd, operator=operator, sudo_password=sudo_password, timeout=timeout
    )


async def rke2_etcd_snapshot_save(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.etcd-snapshot.save``.

    Runs the precondition guard, then triggers an on-demand managed-etcd
    snapshot via ``/var/lib/rancher/rke2/bin/rke2 etcd-snapshot save``
    (plus ``--name <NAME>`` when supplied), as root: plain for a root login,
    through the safe-sudo primitive for a normal user. Returns
    ``{snapshot_name, path, exit_status}``; the snapshot name/path are the
    only non-``exit_status`` fields and carry no secret material.

    Raises :class:`Rke2SnapshotNameError` (bad name),
    :class:`Rke2SnapshotPreconditionError` (non-server / external-datastore
    node) or :class:`Rke2SnapshotError` (non-zero ``rke2`` exit); the
    dispatcher maps each to a non-ok ``connector_error`` result. Transport
    / auth failures propagate the same way (#986).
    """
    name = _validate_name(params.get("name"))

    sudo_password = await _run_precondition_guard(connector, target, operator)

    argv = [_RKE2_BIN, "etcd-snapshot", "save"]
    if name is not None:
        argv += ["--name", name]
    # Plain for a root login, the safe-sudo primitive otherwise (no hand-rolled
    # ``sudo`` argv here). ``shlex.quote`` bounds the argv defensively
    # even though ``name`` is already charset-validated.
    save_cmd = " ".join(shlex.quote(arg) for arg in argv)
    proc = await _run_snapshot_command(connector, target, save_cmd, operator, sudo_password, 120.0)
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


async def rke2_etcd_snapshot_list(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.etcd-snapshot.list``.

    Runs the shared embedded-etcd-server precondition guard, then
    enumerates existing managed-etcd snapshots via
    ``/var/lib/rancher/rke2/bin/rke2 etcd-snapshot list``, as root: plain for a
    root login, through the safe-sudo primitive for a normal user (as ``.save``).
    Takes no operator parameters (the local snapshot store only; S3 listing
    is out of scope). Returns
    ``{"snapshots": [{name, location, size_bytes, created_at}, ...]}`` --
    a set-shaped payload the central JSONFlux reducer materialises into a
    result handle above its row / byte threshold. The listing carries no
    etcd contents, so nothing in the result envelope is secret.

    Raises :class:`Rke2SnapshotPreconditionError` (non-server /
    external-datastore node -- the same guard ``.save`` runs) or
    :class:`Rke2SnapshotError` (non-zero ``rke2`` exit); the dispatcher
    maps each to a non-ok ``connector_error`` result. Transport / auth
    failures propagate the same way (#986).
    """
    del params  # no operator params; the local snapshot store is fixed

    sudo_password = await _run_precondition_guard(connector, target, operator)

    # Plain for a root login, the safe-sudo primitive otherwise, as ``.save``.
    # ``shlex.quote`` bounds the fixed argv defensively (a
    # no-op here, since no operator input is interpolated).
    list_cmd = " ".join(shlex.quote(arg) for arg in [_RKE2_BIN, "etcd-snapshot", "list"])
    proc = await _run_snapshot_command(connector, target, list_cmd, operator, sudo_password, 60.0)
    exit_status = getattr(proc, "exit_status", None)

    stdout_raw = proc.stdout if hasattr(proc, "stdout") else ""
    stderr_raw = proc.stderr if hasattr(proc, "stderr") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    stderr = stderr_raw if isinstance(stderr_raw, str) else ""

    if exit_status not in (0, None) and exit_status != 0:
        raise Rke2SnapshotError(
            f"rke2 etcd-snapshot list exited {exit_status}: {(stderr or stdout).strip()[:400]}"
        )

    return {"snapshots": parse_snapshot_list(stdout)}


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


_RKE2_ETCD_SNAPSHOT_LIST_OP = Rke2Op(
    op_id="rke2.etcd-snapshot.list",
    handler_attr="etcd_snapshot_list",
    summary="List the managed-etcd snapshots on an RKE2 server node.",
    description=(
        "Runs ``rke2 etcd-snapshot list`` on an RKE2 server node over SSH "
        "to enumerate the managed-etcd snapshots that already exist under "
        "``/var/lib/rancher/rke2/server/db/snapshots`` (plus any remote "
        "store the node reports). Returns one row per snapshot with its "
        "name, location, size in bytes, and creation timestamp -- never "
        "etcd contents. The same fail-closed precondition guard as "
        "``rke2.etcd-snapshot.save`` refuses a node that is not an "
        "embedded-etcd server (agent node, or one configuring an external "
        "``datastore-endpoint``). Safe tier, non-gated, and genuinely "
        "read-only (it enumerates, mutating nothing). Use it to confirm a "
        "fresh snapshot landed after a token rotation / config change, or "
        "to inventory recovery points before a risky op."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "snapshots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "location": {"type": "string"},
                        "size_bytes": {"type": ["integer", "null"]},
                        "created_at": {"type": "string"},
                    },
                    "required": ["name", "location", "size_bytes", "created_at"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["snapshots"],
        "additionalProperties": False,
    },
    group_key="rke2-etcd-snapshot",
    tags=("read-only", "etcd", "snapshot", "rke2"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to inventory the managed-etcd snapshots on an RKE2 server "
            "node: which snapshots exist, how old and how large they are. "
            "The mandatory post-check of a safe token rotation -- confirm a "
            "FRESH snapshot landed after the rotation, since a snapshot "
            "taken before it was made with the retired token. Returns rows "
            "of ``{name, location, size_bytes, created_at}``; never etcd "
            "contents. Refuses non-server / external-datastore nodes with a "
            "structured error. Safe and read-only. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "``{snapshots: [{name, location, size_bytes, created_at}, "
            "...]}``. A set-shaped payload: above the reducer threshold it "
            "is returned as a JSONFlux result handle -- page it with "
            "``result_query(handle_id, offset, limit)`` and scan the rows "
            "(e.g. for the newest ``created_at``) rather than expecting every "
            "row inline. "
            "``location`` is whatever RKE2 reports (``local`` / a "
            "``file://`` URL / a bare path / an ``s3://`` URL). "
            "``size_bytes`` is the integer byte count, or null when RKE2 "
            "printed a non-integer size. ``created_at`` is an ISO-8601 "
            "timestamp. An empty ``snapshots`` list means the node has no "
            "snapshots (NOT an error)."
        ),
    },
)


#: The safe (non-gated) snapshot tier: the *active* ``.save`` op (copies
#: etcd to disk -- not ``read-only``-tagged) and the genuinely read-only
#: ``.list`` op (enumerates existing snapshots). Composed alongside the
#: read ops + the approval-gated write ops in
#: :func:`meho_backplane.connectors.rke2.ops._rke2_ops`.
SNAPSHOT_OPS: tuple[Rke2Op, ...] = (
    _RKE2_ETCD_SNAPSHOT_SAVE_OP,
    _RKE2_ETCD_SNAPSHOT_LIST_OP,
)
