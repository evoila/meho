# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only posture tier for :class:`Rke2SshConnector` (G-Node/RKE2-T1 #2221).

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
precondition guard.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.rke2.ops import SSH_TRANSPORT_NOTE, Rke2Op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.rke2.connector import Rke2SshConnector, Target

__all__ = [
    "POSTURE_CONFIG_PATHS",
    "READ_OPS",
    "RKE2_TOKEN_PATH",
    "STATUS_ABSENT",
    "STATUS_PRESENT",
    "STATUS_UNKNOWN",
    "Rke2PostureProbeError",
    "build_posture_probe_command",
    "parse_posture",
    "parse_posture_probe_output",
    "rke2_posture_show",
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
    """
    del params  # declared empty in schema; the measured paths are fixed
    paths = (*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    cmd = build_posture_probe_command(paths)
    proc = await connector._run_command(target, cmd, operator=operator)
    exit_status = getattr(proc, "exit_status", None)
    if exit_status not in (0, None):
        stderr_raw = getattr(proc, "stderr", "")
        stderr_txt = stderr_raw.strip()[:400] if isinstance(stderr_raw, str) else ""
        hint = " (no `stat` on the node)" if exit_status == _PROBE_NO_STAT_EXIT else ""
        raise Rke2PostureProbeError(
            f"the RKE2 posture probe failed to run over SSH (exit {exit_status})"
            f"{hint}: {stderr_txt or 'no stderr'}"
        )
    stdout_raw = proc.stdout if hasattr(proc, "stdout") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    probe_map = parse_posture_probe_output(stdout)
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


#: The read-only posture tier ops. ``rke2.about`` (the identity canary)
#: is composed alongside these in
#: :func:`meho_backplane.connectors.rke2.ops._rke2_ops`.
READ_OPS: tuple[Rke2Op, ...] = (_RKE2_POSTURE_OP,)
