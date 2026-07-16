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
shell-injection surface. The single ``stat`` round-trip reports each
existing path; paths absent from stdout are reported ``present: false``
(``stat`` writes a diagnostic to stderr and skips them, which is exactly
the "file not present" posture signal on an agent node with no server
token).
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
    "parse_posture",
    "parse_stat_output",
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


def parse_stat_output(stdout: str) -> dict[str, dict[str, str]]:
    """Parse ``stat -c '%n|%a|%U|%G'`` stdout into a path -> attrs map.

    Each valid line is ``<path>|<octal-mode>|<owner>|<group>``. Lines
    that do not split into exactly four ``|``-separated fields are
    skipped (defensive against a stray banner line). Modes are
    normalised to the 4-digit octal form.
    """
    result: dict[str, dict[str, str]] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split("|")
        if len(parts) != 4:
            continue
        path, mode, owner, group = parts
        result[path] = {
            "mode": _normalise_mode(mode),
            "owner": owner,
            "group": group,
        }
    return result


def _stat_entry(path: str, stat_map: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Build the per-path posture entry from the parsed ``stat`` map."""
    info = stat_map.get(path)
    if info is None:
        return {"path": path, "present": False, "mode": None, "owner": None, "group": None}
    return {
        "path": path,
        "present": True,
        "mode": info["mode"],
        "owner": info["owner"],
        "group": info["group"],
    }


def parse_posture(
    stat_map: dict[str, dict[str, str]],
    config_paths: tuple[str, ...],
    token_path: str,
) -> dict[str, Any]:
    """Compose the posture envelope from the parsed ``stat`` map.

    Returns ``{"config_files": [...], "token": {...}}``. Every config
    entry and the token entry carry ``present`` plus (when present)
    ``mode`` / ``owner`` / ``group``. The token entry additionally
    carries ``redacted: true`` -- the token **value** is never read, only
    its presence + mode, so no secret material appears in the envelope.
    """
    config_files = [_stat_entry(path, stat_map) for path in config_paths]
    token = _stat_entry(token_path, stat_map)
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
    a merely-absent file surfaces as ``present: false``, not an error.
    """
    del params  # declared empty in schema; the measured paths are fixed
    paths = (*POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)
    quoted = " ".join(shlex.quote(path) for path in paths)
    cmd = f"stat -c '%n|%a|%U|%G' -- {quoted}"
    proc = await connector._run_command(target, cmd, operator=operator)
    stdout_raw = proc.stdout if hasattr(proc, "stdout") else ""
    stdout = stdout_raw if isinstance(stdout_raw, str) else ""
    stat_map = parse_stat_output(stdout)
    return parse_posture(stat_map, POSTURE_CONFIG_PATHS, RKE2_TOKEN_PATH)


_RKE2_POSTURE_OP = Rke2Op(
    op_id="rke2.posture.show",
    handler_attr="posture_show",
    summary="Report RKE2 config-file modes + join-token presence (values redacted).",
    description=(
        "Runs a single ``stat`` over the RKE2 config files under "
        "``/etc/rancher/rke2/`` (``config.yaml`` and the admin kubeconfig "
        "``rke2.yaml``) plus the on-disk server join token at "
        "``/var/lib/rancher/rke2/server/token``. Returns each path's "
        "octal mode + owner/group and a ``present`` flag. The join-token "
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
                        "present": {"type": "boolean"},
                        "mode": {"type": ["string", "null"]},
                        "owner": {"type": ["string", "null"]},
                        "group": {"type": ["string", "null"]},
                    },
                    "required": ["path", "present"],
                    "additionalProperties": False,
                },
            },
            "token": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "present": {"type": "boolean"},
                    "mode": {"type": ["string", "null"]},
                    "owner": {"type": ["string", "null"]},
                    "group": {"type": ["string", "null"]},
                    "redacted": {"type": "boolean"},
                },
                "required": ["path", "present", "redacted"],
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
            "never read -- only presence + mode. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "``{config_files: [{path, present, mode, owner, group}], "
            "token: {path, present, mode, owner, group, redacted: true}}``."
            " ``mode`` is the 4-digit octal form (e.g. ``0600``). A path "
            "that does not exist reports ``present: false`` with null "
            "mode/owner/group. ``token.redacted`` is always true -- the "
            "value is never fetched."
        ),
    },
)


#: The read-only posture tier ops. ``rke2.about`` (the identity canary)
#: is composed alongside these in
#: :func:`meho_backplane.connectors.rke2.ops._rke2_ops`.
READ_OPS: tuple[Rke2Op, ...] = (_RKE2_POSTURE_OP,)
