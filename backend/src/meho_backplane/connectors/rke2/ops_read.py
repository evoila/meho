# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only tier for :class:`Rke2SshConnector` (G-Node/RKE2-T1 #2221).

Hosts the connector's safe, non-mutating read ops:

* ``rke2.posture.show`` -- the config-surface security posture (below).
* ``rke2.node.service.status`` -- the systemd service-state probe added by
  Initiative #2833 (#2852): one read-only ``systemctl show`` round-trip
  over the fixed ``(rke2-server, rke2-agent)`` unit pair, reporting each
  unit's load/active/sub state, start time, and restart count so an
  operator can answer "is ``rke2-server`` up, since when, and is it
  crash-looping?" when the Kubernetes API itself is down. See
  :func:`rke2_service_status`.

``rke2.posture.show`` reports the security posture of an RKE2 node's
config surface without ever reading secret material:

* **Config-file modes** -- the octal permission bits + owner/group of the
  RKE2 config files under ``/etc/rancher/rke2/`` (``config.yaml`` and the
  admin kubeconfig ``rke2.yaml``). A world-readable ``rke2.yaml`` or a
  drifted ``config.yaml`` mode is the posture signal the operator wants.
* **Token-key presence (redacted)** -- whether the on-disk server join
  token at ``/var/lib/rancher/rke2/server/token`` exists and its mode,
  **without reading the token value**. The handler only ``stat``s the
  path; the file content is never fetched, so no secret can leak into
  the result envelope, the audit ``raw_payload``, or the logs. Every
  token entry carries ``redacted: true`` to make the guarantee explicit
  to agents reading the schema.

All measured paths are fixed constants in this module -- there is **no**
operator-supplied path parameter, so there is no path-traversal or
shell-injection surface.

**Every path reports one of three states, never two (#2698).** ``stat``
exits non-zero and writes to stderr for *both* a missing file (``ENOENT``)
and a parent directory it cannot traverse (``EACCES``), so reading stdout
alone cannot tell them apart. Collapsing both to ``present: false`` made
the op lie in the unsafe direction: on a stock hardened server node the
``0700 root:root`` ``/var/lib/rancher/rke2/server/`` directory hides a
join token that *does* exist from a non-root SSH user, and a rotation
runbook pre-checking with this op was told there was nothing to rotate.

The probe therefore resolves each path individually and reports:

* ``present`` / ``present: true`` -- ``stat`` succeeded; mode + owner/group
  are real measurements.
* ``absent`` / ``present: false`` -- ``stat`` failed **and** the parent
  directory is traversable, so the path genuinely does not exist. This is
  the "no server token" signal on an agent node.
* ``unknown`` / ``present: null`` -- the parent directory cannot be
  traversed (or no verdict came back for the path), so existence is
  **undetermined**. An honest "could not determine" is strictly more
  useful to an operator or agent than a confident wrong ``false``.

Traversability is answered by the remote shell's own ``[ -x ]`` test
rather than by parsing ``stat``'s diagnostic text -- the kernel is asked
the actual question, so the verdict carries no dependency on coreutils
message wording or the node's locale. A consequence worth stating: every
real posture verdict now exits 0, so a **non-zero exit is unambiguously an
infrastructure failure** (no ``stat`` on the node, broken shell) and
raises :class:`Rke2PostureProbeError` instead of being silently reported
as posture -- the same discipline ``ops_snapshot`` applies to its
precondition guard. When the peer reports *no* exit status at all, the
output vouches for the run instead: one marker line per measured path
means the probe finished, and anything less raises.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

import yaml

from meho_backplane.connectors.rke2.ops import SSH_TRANSPORT_NOTE, Rke2Op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.rke2.connector import Rke2SshConnector, Target

__all__ = [
    "POSTURE_CONFIG_PATHS",
    "READ_OPS",
    "REDACTED_SENTINEL",
    "RKE2_TOKEN_PATH",
    "SECRET_CONFIG_KEYS",
    "SERVICE_STATUS_PROPERTIES",
    "SERVICE_UNITS",
    "STATUS_ABSENT",
    "STATUS_PRESENT",
    "STATUS_UNKNOWN",
    "Rke2PostureProbeError",
    "Rke2ServiceStatusProbeError",
    "build_posture_probe_command",
    "build_service_status_command",
    "parse_posture",
    "parse_posture_probe_output",
    "parse_service_status",
    "redact_config_content",
    "rke2_config_get",
    "rke2_posture_show",
    "rke2_service_status",
]


#: RKE2 config files whose modes the posture tier reports. Path-bounded
#: to ``/etc/rancher/rke2/*`` per the ratified Initiative #2172 design.
#: ``config.yaml`` is the server/agent config; ``rke2.yaml`` is the
#: cluster-admin kubeconfig RKE2 writes on server nodes.
POSTURE_CONFIG_PATHS: tuple[str, ...] = (
    "/etc/rancher/rke2/config.yaml",
    "/etc/rancher/rke2/rke2.yaml",
)

#: The on-disk server join token. Its **presence + mode** are reported;
#: its **value is never read** (redacted by construction). This is the
#: same path the future ``rke2.token.rotate`` write op reads the OLD
#: token from (Initiative #2172).
RKE2_TOKEN_PATH: str = "/var/lib/rancher/rke2/server/token"

#: ``stat`` succeeded -- the path exists and its mode/owner/group are real
#: measurements.
STATUS_PRESENT: str = "present"

#: ``stat`` failed *and* the parent directory is traversable, so the path
#: genuinely does not exist (the agent-node "no server token" signal).
STATUS_ABSENT: str = "absent"

#: Existence is undetermined -- the parent directory cannot be traversed by
#: the SSH user, or the probe returned no verdict for the path. Never
#: reported as ``absent`` (#2698).
STATUS_UNKNOWN: str = "unknown"

#: Per-path probe markers. ``S`` carries a full ``stat`` line; ``A`` and
#: ``U`` carry only the path.
_MARKER_STAT: str = "S"
_MARKER_ABSENT: str = "A"
_MARKER_UNREADABLE: str = "U"

#: Exit code the probe uses when the node has no ``stat`` binary. Any
#: non-zero exit is an infrastructure failure, not a posture verdict.
_PROBE_NO_STAT_EXIT: int = 127

#: The per-path body of the probe, run once per measured path with ``$p``
#: bound to it. ``stat`` first; on failure the parent directory's
#: traversability (``[ -x ]`` -- the kernel's own answer, no reliance on
#: ``stat``'s message wording or the node locale) decides ``absent`` vs
#: ``unknown``. Kept as its own constant so the ``${p%/*}`` parameter
#: expansion needs no brace-escaping at the f-string that assembles the
#: command.
_PROBE_BODY: str = (
    "if o=$(stat -c '%n|%a|%U|%G' -- \"$p\" 2>/dev/null); "
    f"then printf '{_MARKER_STAT}|%s\\n' \"$o\"; "
    'elif [ -x "${p%/*}" ]; '
    f"then printf '{_MARKER_ABSENT}|%s\\n' \"$p\"; "
    f"else printf '{_MARKER_UNREADABLE}|%s\\n' \"$p\"; fi"
)


class Rke2PostureProbeError(RuntimeError):
    """The posture probe itself failed to run (no ``stat``, broken shell).

    Distinct from any posture verdict: the dispatcher maps it to a
    ``connector_error`` result (the #986 discipline) so an infrastructure
    failure is never served as a file-presence answer (#2698).
    """


def build_posture_probe_command(paths: tuple[str, ...]) -> str:
    """Build the single-round-trip POSIX ``sh`` posture probe for *paths*.

    Emits one marker line per path (see :func:`parse_posture_probe_output`).
    Every path is ``shlex.quote``d, and the caller passes module constants
    only -- there is no operator input in the command. The probe exits
    ``0`` on every real verdict, so a non-zero exit means the probe could
    not run at all.
    """
    quoted = " ".join(shlex.quote(path) for path in paths)
    return (
        f"command -v stat >/dev/null 2>&1 || exit {_PROBE_NO_STAT_EXIT}; "
        f"for p in {quoted}; do {_PROBE_BODY}; done"
    )


def _normalise_mode(raw: str) -> str:
    """Left-pad a ``stat %a`` octal mode to 4 digits (``600`` -> ``0600``).

    ``stat -c '%a'`` drops the leading zero for the common ``0600`` /
    ``0644`` modes; the operator reads posture more clearly with the
    canonical 4-digit octal form. Non-numeric input (never expected from
    ``%a``) is returned unchanged.
    """
    stripped = raw.strip()
    if stripped.isdigit() and len(stripped) < 4:
        return stripped.zfill(4)
    return stripped


def parse_posture_probe_output(stdout: str) -> dict[str, dict[str, str | None]]:
    """Parse the probe's marker lines into a path -> verdict map.

    Recognised lines are ``S|<path>|<octal-mode>|<owner>|<group>`` (stat
    succeeded), ``A|<path>`` (absent) and ``U|<path>`` (parent not
    traversable). Anything else is skipped -- defensive against a stray
    banner line from a login shell, and a skipped path resolves to
    ``unknown`` rather than ``absent``. Modes are normalised to the
    4-digit octal form.
    """
    result: dict[str, dict[str, str | None]] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        marker, sep, rest = stripped.partition("|")
        if not sep or not rest:
            continue
        if marker == _MARKER_STAT:
            parts = rest.split("|")
            if len(parts) != 4:
                continue
            path, mode, owner, group = parts
            result[path] = {
                "status": STATUS_PRESENT,
                "mode": _normalise_mode(mode),
                "owner": owner,
                "group": group,
            }
        elif marker in (_MARKER_ABSENT, _MARKER_UNREADABLE):
            status = STATUS_ABSENT if marker == _MARKER_ABSENT else STATUS_UNKNOWN
            result[rest] = {"status": status, "mode": None, "owner": None, "group": None}
    return result


def _unknown_entry(path: str, detail: str) -> dict[str, Any]:
    """Build the undetermined-existence entry for *path*."""
    return {
        "path": path,
        "present": None,
        "status": STATUS_UNKNOWN,
        "mode": None,
        "owner": None,
        "group": None,
        "detail": detail,
    }


def _stat_entry(path: str, probe_map: dict[str, dict[str, str | None]]) -> dict[str, Any]:
    """Build the per-path posture entry from the parsed probe verdicts."""
    info = probe_map.get(path)
    if info is None:
        return _unknown_entry(path, "the posture probe returned no verdict for this path")
    if info["status"] == STATUS_UNKNOWN:
        parent = path.rpartition("/")[0] or "/"
        return _unknown_entry(
            path,
            f"the SSH user cannot traverse {parent}, so existence is undetermined; "
            "the path may exist and be unreadable",
        )
    present = info["status"] == STATUS_PRESENT
    return {
        "path": path,
        "present": present,
        "status": info["status"],
        "mode": info["mode"],
        "owner": info["owner"],
        "group": info["group"],
        "detail": None,
    }


def parse_posture(
    probe_map: dict[str, dict[str, str | None]],
    config_paths: tuple[str, ...],
    token_path: str,
) -> dict[str, Any]:
    """Compose the posture envelope from the parsed probe verdicts.

    Returns ``{"config_files": [...], "token": {...}}``. Every entry
    carries the same key set -- ``path`` / ``present`` / ``status`` /
    ``mode`` / ``owner`` / ``group`` / ``detail`` -- so a consumer can
    rely on one shape per path. ``present`` is ``null`` exactly when
    ``status`` is ``unknown``, and ``detail`` says why. The token entry
    additionally carries ``redacted: true`` -- the token **value** is
    never read, only its presence + mode, so no secret material appears
    in the envelope.
    """
    config_files = [_stat_entry(path, probe_map) for path in config_paths]
    token = _stat_entry(token_path, probe_map)
    token["redacted"] = True
    return {"config_files": config_files, "token": token}


async def rke2_posture_show(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.posture.show``.

    ``stat``s the fixed RKE2 config paths + the on-disk join-token path in
    a single SSH round-trip and returns the redacted posture envelope. No
    param is consumed -- the measured paths are code constants, never
    operator input. Transport / auth failures propagate to the dispatcher,
    which maps them to a ``connector_error`` result (the #986 discipline);
    a merely-absent file surfaces as ``present: false``, and a path whose
    parent the SSH user cannot traverse surfaces as ``present: null`` /
    ``status: unknown`` -- never as a confident ``false`` (#2698).

    Raises :class:`Rke2PostureProbeError` when the probe exits non-zero.
    The probe answers every real posture question with exit 0, so -- by
    the same reasoning ``ops_snapshot`` applies to its precondition guard
    -- a non-zero exit is a transport / shell / missing-``stat`` failure,
    and serving it as posture would mislabel an infrastructure failure as
    a file-presence verdict.

    ``exit_status`` is ``None`` when the peer closed the channel without
    sending an exit status at all (``asyncssh``'s documented third case,
    alongside an int and ``-1`` for signal death). That is not proof of
    failure -- some SSH implementations omit ``exit-status`` while still
    delivering full output -- so completeness of the output decides: the
    probe emits exactly one marker line per measured path, so a verdict
    for every path is independent evidence that it ran to completion.
    Missing verdicts with no exit status to vouch for the run raise,
    because neither the run nor the paths can be accounted for.
    """
    del params  # declared empty in schema; the measured paths are fixed
    paths = (*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    cmd = build_posture_probe_command(paths)
    proc = await connector._run_command(target, cmd, operator=operator)
    exit_status = getattr(proc, "exit_status", None)
    stdout_raw = proc.stdout if hasattr(proc, "stdout") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    probe_map = parse_posture_probe_output(stdout)
    if exit_status != 0 and not (exit_status is None and probe_map.keys() >= set(paths)):
        stderr_raw = getattr(proc, "stderr", "")
        stderr_txt = stderr_raw.strip()[:400] if isinstance(stderr_raw, str) else ""
        hint = " (no `stat` on the node)" if exit_status == _PROBE_NO_STAT_EXIT else ""
        if exit_status is None:
            hint = (
                " (no exit status reported, and the probe returned no verdict for "
                f"{len(set(paths) - probe_map.keys())} of {len(paths)} measured paths)"
            )
        raise Rke2PostureProbeError(
            f"the RKE2 posture probe failed to run over SSH (exit {exit_status})"
            f"{hint}: {stderr_txt or 'no stderr'}"
        )
    return parse_posture(probe_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)


_RKE2_POSTURE_OP = Rke2Op(
    op_id="rke2.posture.show",
    handler_attr="posture_show",
    summary="Report RKE2 config-file modes + join-token presence (values redacted).",
    description=(
        "Runs a single ``stat``-based probe over the RKE2 config files "
        "under ``/etc/rancher/rke2/`` (``config.yaml`` and the admin "
        "kubeconfig ``rke2.yaml``) plus the on-disk server join token at "
        "``/var/lib/rancher/rke2/server/token``. Returns each path's "
        "octal mode + owner/group and a three-state verdict: ``present``, "
        "``absent`` (the file really is not there) or ``unknown`` (the "
        "SSH user cannot traverse the parent directory, so existence is "
        "undetermined -- typically a stock ``0700 root:root`` "
        "``/var/lib/rancher/rke2/server/`` reached as a non-root user). "
        "``unknown`` is never reported as ``absent``. The join-token "
        "entry reports presence + mode ONLY -- the token value is never "
        "read, so no secret material appears in the result. Use it to "
        "audit config-file permission drift (e.g. a world-readable "
        "kubeconfig) and to confirm the server token exists before a "
        "rotation. No params; safe and read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "config_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "present": {"type": ["boolean", "null"]},
                        "status": {
                            "type": "string",
                            "enum": [STATUS_PRESENT, STATUS_ABSENT, STATUS_UNKNOWN],
                        },
                        "mode": {"type": ["string", "null"]},
                        "owner": {"type": ["string", "null"]},
                        "group": {"type": ["string", "null"]},
                        "detail": {"type": ["string", "null"]},
                    },
                    "required": ["path", "present", "status"],
                    "additionalProperties": False,
                },
            },
            "token": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "present": {"type": ["boolean", "null"]},
                    "status": {
                        "type": "string",
                        "enum": [STATUS_PRESENT, STATUS_ABSENT, STATUS_UNKNOWN],
                    },
                    "mode": {"type": ["string", "null"]},
                    "owner": {"type": ["string", "null"]},
                    "group": {"type": ["string", "null"]},
                    "detail": {"type": ["string", "null"]},
                    "redacted": {"type": "boolean"},
                },
                "required": ["path", "present", "status", "redacted"],
                "additionalProperties": False,
            },
        },
        "required": ["config_files", "token"],
        "additionalProperties": False,
    },
    group_key="posture",
    tags=("read-only", "posture", "rke2", "security"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to audit an RKE2 node's config-surface posture: the "
            "permission modes of ``/etc/rancher/rke2/config.yaml`` and "
            "the admin kubeconfig ``rke2.yaml``, and whether the on-disk "
            "server join token exists (with its mode). The token VALUE is "
            "never read -- only presence + mode. Treat ``status: "
            "unknown`` as 'not determined', NOT as 'absent': before "
            "concluding a server node has no join token to rotate, "
            "re-check under an identity that can traverse "
            "``/var/lib/rancher/rke2/server/``. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "``{config_files: [{path, present, status, mode, owner, group, "
            "detail}], token: {..., redacted: true}}`` -- every entry "
            "carries the same keys. ``mode`` is the 4-digit octal form "
            "(e.g. ``0600``). ``status`` is ``present`` (measured), "
            "``absent`` (``present: false`` -- the path really is not "
            "there) or ``unknown`` (``present: null`` -- the parent "
            "directory is not traversable by the SSH user, so existence "
            "is undetermined; ``detail`` says why). ``present`` is null "
            "exactly when ``status`` is ``unknown``, so a falsy check on "
            "``present`` alone CANNOT distinguish absent from "
            "undetermined -- branch on ``status``. ``token.redacted`` is "
            "always true -- the value is never fetched."
        ),
    },
)


# ---------------------------------------------------------------------------
# Service-state read (``rke2.node.service.status``, Initiative #2833 / #2852)
# ---------------------------------------------------------------------------

#: The RKE2 systemd units a node may run. A control-plane node runs
#: ``rke2-server``; a worker runs ``rke2-agent``. A node runs exactly one,
#: so probing both is how the op reports which role the node has -- the same
#: closed pair the approval-gated restart op allow-lists, here read-only.
#: Fixed constants: no operator-supplied unit, so no arbitrary-unit or
#: shell-injection surface.
SERVICE_UNITS: tuple[str, ...] = ("rke2-server", "rke2-agent")

#: The systemd unit properties the status probe reads per unit. All are
#: read-only introspection (``systemctl show`` never mutates). ``LoadState``
#: separates an installed unit from ``not-found``; ``ActiveState`` /
#: ``SubState`` are the running signal; ``ExecMainStartTimestamp`` answers
#: "active since when"; ``NRestarts`` is the crash-loop counter.
SERVICE_STATUS_PROPERTIES: tuple[str, ...] = (
    "LoadState",
    "ActiveState",
    "SubState",
    "ExecMainStartTimestamp",
    "NRestarts",
)

#: systemd's "this unit is not installed on this node" ``LoadState`` value.
#: ``systemctl show`` prints it on stdout and still exits 0
#: (systemd/systemd#1105), so it -- not the exit code -- is how the op tells
#: a node's role apart and reports both units honestly.
_LOAD_STATE_NOT_FOUND: str = "not-found"

#: Marker line the probe prints before each unit's ``systemctl show`` block
#: so the parser can attribute the ``KEY=VALUE`` lines that follow to a unit.
#: ``systemctl show`` never emits a ``UNIT`` property, so the marker cannot
#: collide with real output.
_UNIT_MARKER: str = "UNIT"

#: Exit code the probe uses when the node has no ``systemctl``. Any non-zero
#: exit is an infrastructure failure, not a service-state verdict.
_PROBE_NO_SYSTEMCTL_EXIT: int = 127


class Rke2ServiceStatusProbeError(RuntimeError):
    """The service-status probe itself failed to run (no ``systemctl``, broken shell).

    Distinct from any service-state verdict: the dispatcher maps it to a
    ``connector_error`` result (the #986 discipline) so an infrastructure
    failure is never served as a service-state answer.
    """


def build_service_status_command(units: tuple[str, ...]) -> str:
    """Build the single-round-trip ``systemctl show`` service-status probe.

    Emits a ``UNIT=<name>`` marker then that unit's ``KEY=VALUE`` property
    block for each unit, in one SSH round-trip. Every unit name is a fixed
    module constant (no operator input) and is ``shlex.quote``d defensively.
    ``--all`` un-suppresses the zero/empty properties within the ``-p``
    selection (``systemctl show`` drops ``NRestarts=0`` and an empty
    ``ExecMainStartTimestamp`` otherwise), so a healthy unit reports
    ``restart_count: 0`` rather than a missing field. ``systemctl show`` is
    read-only and exits 0 even for a ``not-found`` unit, so the ``|| true``
    per unit means the only non-zero exit is the ``systemctl``-absent guard
    -- which lets the handler tell an infrastructure failure apart from a
    real verdict.
    """
    props = ",".join(SERVICE_STATUS_PROPERTIES)
    quoted = " ".join(shlex.quote(unit) for unit in units)
    return (
        f"command -v systemctl >/dev/null 2>&1 || exit {_PROBE_NO_SYSTEMCTL_EXIT}; "
        f"for u in {quoted}; do "
        f"printf '{_UNIT_MARKER}=%s\\n' \"$u\"; "
        f'systemctl show --all -p {props} "$u" 2>/dev/null || true; '
        "done"
    )


def _parse_restart_count(raw: str | None) -> int | None:
    """Parse a ``systemctl`` ``NRestarts`` value into an int, else ``None``.

    A missing or non-integer value resolves to ``None`` rather than raising --
    a suppressed or unparseable counter must not fail the whole probe.
    """
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _service_unit_entry(unit: str, fields: dict[str, str]) -> dict[str, Any]:
    """Compose one unit's status entry from its parsed ``KEY=VALUE`` fields.

    A ``LoadState=not-found`` unit is not installed on this node, so every
    live-state field is ``null``: systemd still prints ``inactive`` /
    ``dead`` defaults for an absent unit, and surfacing those would misreport
    "not present here" as "present but stopped". A missing ``LoadState``
    (unusual) resolves to ``null`` rather than ``not-found`` -- undetermined,
    not a confident absence.
    """
    load_state = fields.get("LoadState") or None
    if load_state == _LOAD_STATE_NOT_FOUND:
        return {
            "unit": unit,
            "load_state": _LOAD_STATE_NOT_FOUND,
            "active_state": None,
            "sub_state": None,
            "since": None,
            "restart_count": None,
        }
    return {
        "unit": unit,
        "load_state": load_state,
        "active_state": fields.get("ActiveState") or None,
        "sub_state": fields.get("SubState") or None,
        "since": fields.get("ExecMainStartTimestamp", "").strip() or None,
        "restart_count": _parse_restart_count(fields.get("NRestarts")),
    }


def parse_service_status(stdout: str) -> list[dict[str, Any]]:
    """Parse the probe's ``UNIT=`` / ``KEY=VALUE`` stream into per-unit entries.

    Each ``UNIT=<name>`` marker opens a block; the ``systemctl show``
    ``KEY=VALUE`` lines that follow belong to it until the next marker. The
    ``partition("=")`` split reuses the ``ops_write._parse_preflight`` idiom,
    generalised from one flat map to one map per unit. Entries preserve the
    order the units were probed in. Lines before the first marker, and lines
    with no ``=``, are ignored -- defensive against a login-shell banner.
    """
    entries: list[dict[str, Any]] = []
    current_unit: str | None = None
    current_fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key == _UNIT_MARKER:
            if current_unit is not None:
                entries.append(_service_unit_entry(current_unit, current_fields))
            current_unit = value.strip()
            current_fields = {}
        elif current_unit is not None:
            current_fields[key] = value.strip()
    if current_unit is not None:
        entries.append(_service_unit_entry(current_unit, current_fields))
    return entries


async def rke2_service_status(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.node.service.status``.

    Runs one read-only ``systemctl show`` probe over SSH across the fixed
    ``(rke2-server, rke2-agent)`` unit pair and returns their live systemd
    state -- ``load_state`` / ``active_state`` / ``sub_state``, the start
    timestamp, and the restart count -- without mutating anything. No param
    is consumed; the probed units are code constants, never operator input.

    Answers "is ``rke2-server`` up when the Kubernetes API is down?": a node
    runs exactly one of the two units and the other reports
    ``load_state: not-found``. ``restart_count`` (systemd ``NRestarts``)
    paired with a non-``active`` ``active_state`` is the crash-loop signal.

    Raises :class:`Rke2ServiceStatusProbeError` when the probe exits non-zero
    (no ``systemctl`` on the node, broken shell). ``systemctl show`` exits 0
    for every real verdict including a ``not-found`` unit
    (systemd/systemd#1105), so a non-zero exit is an infrastructure failure,
    not service state -- the same discipline ``rke2.posture.show`` applies.
    ``exit_status`` is ``None`` when the peer sent no exit status at all
    (asyncssh's documented third case); that is tolerated when at least one
    unit block came back, because the probe emits a marker per unit and a
    parsed entry is independent evidence it ran. Transport / auth failures
    propagate to the dispatcher's ``connector_error`` branch (#986).
    """
    del params  # declared empty in schema; the probed units are fixed
    cmd = build_service_status_command(SERVICE_UNITS)
    proc = await connector._run_command(target, cmd, operator=operator)
    exit_status = getattr(proc, "exit_status", None)
    stdout_raw = proc.stdout if hasattr(proc, "stdout") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    units = parse_service_status(stdout)
    if exit_status not in (0, None) or not units:
        stderr_raw = getattr(proc, "stderr", "")
        stderr_txt = stderr_raw.strip()[:400] if isinstance(stderr_raw, str) else ""
        hint = " (no `systemctl` on the node)" if exit_status == _PROBE_NO_SYSTEMCTL_EXIT else ""
        raise Rke2ServiceStatusProbeError(
            f"the RKE2 service-status probe failed to run over SSH "
            f"(exit {exit_status}){hint}: {stderr_txt or 'no stderr'}"
        )
    return {"units": units}


_RKE2_SERVICE_STATUS_OP = Rke2Op(
    op_id="rke2.node.service.status",
    handler_attr="service_status",
    summary="Report RKE2 systemd unit state (rke2-server/rke2-agent) without mutating.",
    description=(
        "Runs a single read-only ``systemctl show`` probe over SSH across "
        "the fixed ``rke2-server`` / ``rke2-agent`` unit pair and returns "
        "each unit's live systemd state: ``load_state`` (``loaded`` vs "
        "``not-found``), ``active_state`` / ``sub_state`` (the running "
        "signal), ``since`` (the systemd ``ExecMainStartTimestamp`` -- active "
        "since when), and ``restart_count`` (systemd ``NRestarts`` -- the "
        "crash-loop counter). A node runs exactly one of the two units; the "
        "other reports ``load_state: not-found`` (every other field null), so "
        "the result also tells the caller the node's role. Answers 'is "
        "rke2-server actually up?' when the Kubernetes API server itself is "
        "down and ``kubernetes.*`` ops cannot help. ``systemctl show`` never "
        "mutates -- there is NO restart/start/stop here (that is the "
        "approval-gated ``rke2.node.service.restart``). No params; safe and "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "units": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "unit": {"type": "string"},
                        "load_state": {"type": ["string", "null"]},
                        "active_state": {"type": ["string", "null"]},
                        "sub_state": {"type": ["string", "null"]},
                        "since": {"type": ["string", "null"]},
                        "restart_count": {"type": ["integer", "null"]},
                    },
                    "required": ["unit", "load_state"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["units"],
        "additionalProperties": False,
    },
    group_key="rke2-service-read",
    tags=("read-only", "service", "systemd", "rke2"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to check whether an RKE2 node's systemd service is up "
            "WITHOUT changing anything -- especially 'is rke2-server active, "
            "since when, and is it crash-looping?' when the Kubernetes API is "
            "unreachable and node-level ops like ``kubernetes.node.list`` "
            "cannot answer (the API server is what is down). Probes the fixed "
            "``rke2-server`` / ``rke2-agent`` pair; the unit reporting "
            "``load_state: not-found`` is simply not installed on this node, "
            "which is how the op reveals the node's role. This is READ-ONLY: "
            "to actually restart a unit use the approval-gated "
            "``rke2.node.service.restart``. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "``{units: [{unit, load_state, active_state, sub_state, since, "
            "restart_count}]}`` -- one entry per probed unit. ``load_state`` "
            "is ``loaded`` for an installed unit or ``not-found`` when the "
            "unit is not present on this node (then every other field is "
            "null). ``active_state`` (e.g. ``active`` / ``inactive`` / "
            "``failed`` / ``activating``) with ``sub_state`` (e.g. "
            "``running`` / ``dead`` / ``auto-restart``) is the running "
            "signal; ``since`` is the systemd ``ExecMainStartTimestamp`` "
            "string (e.g. ``Fri 2026-08-01 09:12:03 UTC``), null when the "
            "unit has no recorded start; ``restart_count`` is systemd "
            "``NRestarts`` (0 or more) -- a non-zero value paired with a "
            "non-``active`` state signals a crash loop."
        ),
    },
)


# ---------------------------------------------------------------------------
# rke2.node.config.get -- redacted config.yaml content read (#2854)
# ---------------------------------------------------------------------------

#: The value substituted for a fully-redacted secret key. Matches the
#: names-only-disclosure discipline ``changed_config_keys`` established on
#: the write side: the operator learns the key is set without seeing it.
REDACTED_SENTINEL: str = "***redacted***"

#: RKE2 ``config.yaml`` top-level keys whose value is a secret in itself and
#: is replaced wholesale with :data:`REDACTED_SENTINEL`. Grounded against the
#: RKE2 server-config reference (docs.rke2.io/reference/server_config): the
#: two join tokens the connector's own write-side docstring names
#: (``token`` / ``agent-token``, ``ops_write.py``) plus the three etcd S3
#: snapshot credentials (mapped to ``AWS_ACCESS_KEY_ID`` /
#: ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``). Matching is
#: case-sensitive because RKE2 only honours the exact lowercase-hyphenated
#: flag names, so a mis-cased key is not a live credential. Sibling ``*-file``
#: keys and ``private-registry`` are on-disk paths, not secret values, and are
#: deliberately NOT redacted -- the operator asked to verify them.
SECRET_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "agent-token",
        "etcd-s3-access-key",
        "etcd-s3-secret-key",
        "etcd-s3-session-token",
    }
)

#: The one config key whose value is a datastore DSN that may embed
#: ``user:password@`` userinfo (external MySQL/Postgres/NATS datastore). Only
#: the userinfo segment is masked -- the operator's stated need is to verify
#: the host/port/database, which stays visible.
_DATASTORE_ENDPOINT_KEY: str = "datastore-endpoint"

#: Matches the ``scheme://userinfo@`` prefix of a DSN so the userinfo (which
#: may itself contain ``:``) can be masked without disturbing the host/port/
#: path that follow. ``[^/@\s]+`` stops at the first ``@`` and never crosses a
#: ``/``, so a credential-less endpoint (no ``@``) is left untouched.
_DSN_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")


def _mask_dsn_userinfo(value: Any) -> tuple[Any, bool]:
    """Mask the ``user:pass@`` userinfo of a datastore DSN string.

    Returns ``(masked_value, changed)``. A non-string value or a DSN with no
    userinfo is returned unchanged with ``changed=False``; the host/port/
    database portion is always preserved so the operator can still verify it.
    """
    if not isinstance(value, str):
        return value, False
    masked, count = _DSN_USERINFO_RE.subn(rf"\g<scheme>{REDACTED_SENTINEL}@", value)
    return masked, count > 0


def redact_config_content(content: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a redacted copy of *content* plus the sorted redacted key names.

    Secret-bearing top-level keys (:data:`SECRET_CONFIG_KEYS`) have their value
    replaced by :data:`REDACTED_SENTINEL`; ``datastore-endpoint`` has only its
    DSN userinfo masked. The input mapping is not mutated. A key lands in the
    returned name list only when it was actually present and masked, mirroring
    the write side's ``changed_config_keys`` names-only discipline -- so the
    secret VALUE never appears in the result envelope (and therefore never in
    the audit ``raw_payload``, which stores the raw handler result), while its
    NAME is disclosed so the operator knows the key is set.
    """
    redacted = dict(content)
    touched: set[str] = set()
    for key, value in content.items():
        if key in SECRET_CONFIG_KEYS:
            redacted[key] = REDACTED_SENTINEL
            touched.add(key)
        elif key == _DATASTORE_ENDPOINT_KEY:
            masked, changed = _mask_dsn_userinfo(value)
            if changed:
                redacted[key] = masked
                touched.add(key)
    return redacted, sorted(touched)


async def rke2_config_get(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.node.config.get`` -- redacted config.yaml read (#2854).

    Reads a single bounded ``/etc/rancher/rke2/*.yaml`` file over SSH with the
    same ``cat`` + :func:`yaml.safe_load` step ``rke2.node.config.update``
    already runs, and returns the parsed top-level mapping with secret-bearing
    keys redacted (:func:`redact_config_content`) -- so an operator can verify
    ``tls-san`` / ``datastore-endpoint`` / ``node-taint`` before or after a
    config patch without an untracked ``ssh cat`` that would spill the join
    token into shell history.

    The path is confined by :func:`~meho_backplane.connectors.rke2.ops_write.bound_config_path`
    (the same ``/etc/rancher/rke2/*.yaml`` filter the write op uses), so a
    traversal attempt is rejected before any SSH round trip. Non-mapping or
    unparseable YAML surfaces a structured ``error`` rather than crashing the
    dispatch. Never mutates the node.
    """
    from meho_backplane.connectors.rke2.ops_write import (
        Rke2WriteSafetyError,
        bound_config_path,
    )

    try:
        path = bound_config_path(params.get("path"))
    except Rke2WriteSafetyError as exc:
        return {"error": f"config.get path check: {exc}"}

    quoted_path = shlex.quote(path)
    read_cmd = f"if [ -e {quoted_path} ]; then cat -- {quoted_path}; fi"
    read_proc = await connector._run_command(target, read_cmd, operator=operator)
    if getattr(read_proc, "exit_status", None) not in (0, None):
        return {"path": path, "error": "failed to read the config file"}

    stdout_raw = read_proc.stdout if hasattr(read_proc, "stdout") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    try:
        parsed = yaml.safe_load(stdout)
    except yaml.YAMLError as exc:
        return {"path": path, "error": f"config file is not valid YAML: {exc}"}
    content: dict[str, Any] = parsed if parsed is not None else {}
    if not isinstance(content, dict):
        return {"path": path, "error": "config file is not a YAML mapping"}

    redacted, redacted_keys = redact_config_content(content)
    return {"path": path, "content": redacted, "redacted_keys": redacted_keys}


_RKE2_CONFIG_GET_OP = Rke2Op(
    op_id="rke2.node.config.get",
    handler_attr="config_get",
    summary="Read an RKE2 config.yaml file's content with join/S3 secrets redacted.",
    description=(
        "Reads a single ``/etc/rancher/rke2/*.yaml`` file (default "
        "``config.yaml``) over SSH -- the same ``cat`` + YAML parse "
        "``rke2.node.config.update`` runs -- and returns the parsed top-level "
        "mapping so an operator can verify ``tls-san`` / ``datastore-endpoint`` "
        "/ ``node-taint`` before or after a config patch. Secret-bearing keys "
        "are REDACTED, not withheld: ``token`` / ``agent-token`` and the etcd "
        "S3 credentials (``etcd-s3-access-key`` / ``etcd-s3-secret-key`` / "
        "``etcd-s3-session-token``) are replaced with ``***redacted***``, and "
        "``datastore-endpoint`` has only its ``user:pass@`` DSN userinfo "
        "masked (host/port/db preserved). The masked key NAMES are listed in "
        "``redacted_keys``. The path is confined to ``/etc/rancher/rke2/*.yaml`` "
        "(traversal rejected before any SSH); non-mapping / invalid YAML "
        "returns a structured ``error``. safety_level=safe, "
        "requires_approval=False, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "pattern": r"^/etc/rancher/rke2/[^\x00\n\r]*\.yaml$",
                "description": (
                    "Absolute path to the RKE2 config file to read. Must be a "
                    ".yaml file under /etc/rancher/rke2/ (default "
                    "/etc/rancher/rke2/config.yaml). Omit for the default."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "object"},
            "redacted_keys": {"type": "array", "items": {"type": "string"}},
            "error": {"type": "string"},
        },
        "required": [],
        "additionalProperties": True,
    },
    group_key="rke2-config-read",
    tags=("read-only", "rke2", "config"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to read an RKE2 node's config.yaml content and verify "
            "non-secret settings (``tls-san``, ``datastore-endpoint`` host, "
            "``node-taint``, ``cni``, ...) before or after "
            "``rke2.node.config.update`` -- e.g. re-confirming a "
            "post-token-rotation config edit landed on every server. Prefer "
            "this over an untracked ``ssh cat`` of the file: it is governed "
            "(policy/audit/broadcast) and it REDACTS the join tokens + etcd "
            "S3 credentials so they never reach the transcript. It reports "
            "content, not permission modes -- use ``rke2.posture.show`` for "
            "file modes / token presence. Transport: plain SSH (read-only "
            "``cat`` of one bounded ``/etc/rancher/rke2/*.yaml`` path)."
        ),
        "parameter_hints": {
            "path": (
                "Optional; a .yaml file under /etc/rancher/rke2/ (e.g. a "
                "config.yaml.d drop-in). Omit for /etc/rancher/rke2/config.yaml."
            ),
        },
        "output_shape": (
            "``{path, content: {<parsed keys>}, redacted_keys: [names]}``. "
            "``content`` is the parsed top-level YAML mapping with "
            "``token`` / ``agent-token`` / ``etcd-s3-*`` keys shown as "
            "``***redacted***`` and ``datastore-endpoint`` userinfo masked; "
            "``redacted_keys`` lists exactly which keys were masked (empty "
            "when the file holds no secrets). A missing or empty file yields "
            "``content: {}``. On a path-bound violation or unparseable YAML: "
            "``{error}`` (plus ``path`` when the path was valid)."
        ),
    },
)


#: The read-only tier ops (posture + service status + redacted config
#: read). ``rke2.about`` (the identity canary) is composed alongside
#: these in :func:`meho_backplane.connectors.rke2.ops._rke2_ops`.
READ_OPS: tuple[Rke2Op, ...] = (
    _RKE2_POSTURE_OP,
    _RKE2_SERVICE_STATUS_OP,
    _RKE2_CONFIG_GET_OP,
)
