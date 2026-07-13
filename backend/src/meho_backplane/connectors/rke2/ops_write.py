# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated node-write ops for :class:`Rke2SshConnector` (G-Node/RKE2-T3 #2430).

The first two **write** ops of Initiative #2172, both ``dangerous`` /
``requires_approval=True`` and in the shared ``rke2-node-write`` group:

* ``rke2.node.service.restart`` -- ``systemctl restart <UNIT>`` for a UNIT in
  the two-entry :data:`_RESTARTABLE_UNIT_SET` allow-list (a schema ``enum``
  re-checked in the handler; the proxmox method-allowlist mold -- schema
  advisory, frozenset authoritative), health-gated on ``systemctl is-active``.
  No arbitrary unit / ``systemctl`` action.
* ``rke2.node.config.update`` -- a **backplane-owned key merge** of a bounded
  ``/etc/rancher/rke2/*.yaml`` file (:func:`ensure_config_path_under_root`,
  the bind9 ``ensure_path_under_root`` mold). The handler reads + parses the
  current YAML in-process, applies the operator's key-level patch, validates
  it re-parses, and writes it back atomically (temp under
  ``/etc/rancher/rke2``, ``chmod 0600`` + ``chown root:root``, ``mv``) -- no
  host-side ``sed`` / ``yq`` clobber, no arbitrary-file-write primitive. It
  does **not** restart (config is inert until one); it returns
  ``restart_required: true`` and changed key **names** only.

Approval routing: the policy gate routes a ``USER``/agent dispatch of a
``requires_approval`` op to ``needs-approval`` (G11.7-T1 #1401), so the
handlers below run only on the ``_approved=True`` resume path.

Privilege model: the connector operates as ``root`` over SSH (the T1 posture
tier ``stat``s ``0600 root:root`` token files), so writes run via
``_run_command`` without a separate sudo-password stream.

Secret discipline: an RKE2 ``config.yaml`` body carries ``token:`` /
``agent-token:`` join credentials, so ``config.update`` is pinned in
:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS` and its result +
approval preview surface key **names** only -- never a value, never the body.

References: Task #2430; Initiative #2172; molds proxmox/bind9/holodeck
``ops_write.py``; Rancher RKE2 STIG V-254564 (``config.yaml`` is ``0600``).
"""

from __future__ import annotations

import base64
import posixpath
import shlex
from typing import TYPE_CHECKING, Any

import yaml

from meho_backplane.connectors.rke2.ops import SSH_TRANSPORT_NOTE, Rke2Op

if TYPE_CHECKING:
    from meho_backplane.connectors.rke2.connector import Rke2SshConnector, Target
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "RKE2_WHEN_TO_USE_WRITE_BY_GROUP",
    "WRITE_OPS",
    "ConfigPathRejectedError",
    "Rke2WriteSafetyError",
    "apply_config_patch",
    "bound_config_path",
    "changed_config_keys",
    "ensure_config_path_under_root",
    "rke2_config_update",
    "rke2_service_restart",
]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: The only systemd units ``rke2.node.service.restart`` may restart. Both are
#: the RKE2 node daemons; a server node runs ``rke2-server`` and an agent node
#: ``rke2-agent``. Every other unit name is rejected -- this is a typed
#: restart, not a generic ``systemctl`` op.
_RESTARTABLE_UNITS: tuple[str, ...] = ("rke2-agent", "rke2-server")
_RESTARTABLE_UNIT_SET: frozenset[str] = frozenset(_RESTARTABLE_UNITS)

#: The directory tree ``rke2.node.config.update`` is confined to. The RKE2
#: server/agent config (``config.yaml``) and its drop-ins (``config.yaml.d/
#: *.yaml``) all live under this prefix; a path that normalises outside it is
#: rejected before any SSH traffic.
_RKE2_CONFIG_ROOT: str = "/etc/rancher/rke2"

#: The config file edited when the operator supplies no ``path``.
_DEFAULT_CONFIG_PATH: str = "/etc/rancher/rke2/config.yaml"

#: Accepted ``semantics`` values. ``merge`` applies the operator's patch on
#: top of the current top-level keys; ``replace`` swaps the whole config for
#: the patch object.
_CONFIG_SEMANTICS: tuple[str, ...] = ("merge", "replace")

#: Success sentinel the atomic-write script emits on its last line. The
#: leading marker comment lets the recorded fake-shell harness prefix-match
#: the write command without decoding the base64 body.
_CONFIG_WRITE_MARKER: str = "# meho-rke2-config-update"
_CONFIG_WRITE_SENTINEL: str = "===RKE2_CONFIG_WRITTEN==="


class Rke2WriteSafetyError(ValueError):
    """A write op's input escaped its declared bound.

    Subclass of :class:`ValueError` so the dispatcher's
    ``result_connector_error`` envelope picks it up uniformly. The message
    names the offending category (unit / semantics / patch) without echoing
    the full operator-supplied value.
    """


class ConfigPathRejectedError(Rke2WriteSafetyError):
    """The requested config path failed the ``/etc/rancher/rke2/*`` filter.

    The message echoes the *sanitised* candidate path, never the raw input and
    never the file body, so log scraping surfaces no secret material.
    """


def bound_unit(params: dict[str, Any]) -> str:
    """Return the validated restart unit, or raise :exc:`Rke2WriteSafetyError`.

    ``unit`` must be a member of :data:`_RESTARTABLE_UNIT_SET`; this handler-side
    re-check is authoritative (the proxmox method-allowlist mold -- the schema
    ``enum`` is advisory).
    """
    unit = params.get("unit")
    if not isinstance(unit, str) or unit not in _RESTARTABLE_UNIT_SET:
        raise Rke2WriteSafetyError(
            f"unit must be one of {sorted(_RESTARTABLE_UNIT_SET)}; refusing to restart"
        )
    return unit


def ensure_config_path_under_root(requested: str, allowed_root: str) -> str:
    """Return an absolute ``*.yaml`` path under *allowed_root*, or raise.

    Lexical confinement (the bind9 ``ensure_path_under_root`` mold): the
    candidate is ``posixpath.normpath``-collapsed and must lie strictly inside
    *allowed_root* (a trailing-slash sentinel stops ``/etc/rancher/rke2-evil``
    matching) and carry a ``.yaml`` suffix; control bytes are refused. Raises
    :class:`ConfigPathRejectedError` (a :class:`ValueError`) on any violation.
    """
    if not allowed_root.startswith("/"):
        raise ConfigPathRejectedError(
            f"allowed_root {allowed_root!r} must be an absolute POSIX path; refusing to filter"
        )
    if not isinstance(requested, str) or not requested.strip():
        raise ConfigPathRejectedError("requested path is empty")
    if any(ch in requested for ch in ("\x00", "\n", "\r")):
        raise ConfigPathRejectedError("requested path contains a control character; refusing")

    candidate = posixpath.normpath(posixpath.join(allowed_root, requested))
    canonical_root = posixpath.normpath(allowed_root)
    if not candidate.startswith(canonical_root + "/"):
        raise ConfigPathRejectedError(
            f"path {candidate!r} is outside the RKE2 config root {canonical_root!r}"
        )
    if not candidate.endswith(".yaml"):
        raise ConfigPathRejectedError(
            f"path {candidate!r} must be a .yaml file under {canonical_root}/"
        )
    return candidate


def bound_config_path(raw_path: Any) -> str:
    """Return the confined config path (``None`` -> default ``config.yaml``)."""
    if raw_path is None:
        return _DEFAULT_CONFIG_PATH
    return ensure_config_path_under_root(raw_path, _RKE2_CONFIG_ROOT)


def bound_semantics(raw: Any) -> str:
    """Return the validated merge semantics (default ``"merge"``)."""
    if raw is None:
        return "merge"
    if raw not in _CONFIG_SEMANTICS:
        raise Rke2WriteSafetyError(f"semantics must be one of {list(_CONFIG_SEMANTICS)}")
    return str(raw)


def bound_patch(raw: Any) -> dict[str, Any]:
    """Return the validated patch object (non-empty, string keys)."""
    if not isinstance(raw, dict) or not raw:
        raise Rke2WriteSafetyError("patch must be a non-empty object of key -> value")
    for key in raw:
        if not isinstance(key, str):
            raise Rke2WriteSafetyError("patch keys must be strings")
    return raw


def apply_config_patch(
    current: dict[str, Any], patch: dict[str, Any], semantics: str
) -> dict[str, Any]:
    """Apply *patch* to *current* per *semantics*, returning a new mapping.

    ``merge`` is a shallow top-level key merge (each patch key overlays/adds;
    other keys preserved -- the ratified "key-level edit"); ``replace`` makes
    the config *patch* verbatim. Neither input is mutated.
    """
    if semantics == "replace":
        return dict(patch)
    return {**current, **patch}


def changed_config_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Return the sorted top-level key names whose value changed (names only).

    A key is "changed" when added, removed, or its value differs. Names only --
    no value leaks into the result envelope, audit ``raw_payload``, or feed.
    """
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def _build_atomic_write_script(path: str, content: str) -> str:
    """Render the root-owned atomic config-write bash script.

    The content is base64-encoded (safe to splice into the command line) and
    decoded into a temp file in the same directory as *path* (so the final
    ``mv`` is an atomic same-filesystem rename), ``chmod 0600`` +
    ``chown root:root`` before the rename. Every path is ``shlex.quote``-quoted.
    """
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    quoted_path = shlex.quote(path)
    quoted_dir = shlex.quote(posixpath.dirname(path))
    quoted_b64 = shlex.quote(b64)
    return (
        f"{_CONFIG_WRITE_MARKER}\n"
        "set -eu\n"
        "umask 077\n"
        f'tmp="$(mktemp {quoted_dir}/.meho-config.XXXXXX)"\n'
        f'printf %s {quoted_b64} | base64 -d > "$tmp"\n'
        'chmod 0600 "$tmp"\n'
        'chown root:root "$tmp"\n'
        f'mv -f "$tmp" {quoted_path}\n'
        f"echo '{_CONFIG_WRITE_SENTINEL}'\n"
    )


# ---------------------------------------------------------------------------
# Handlers (bound-method shims on Rke2SshConnector, resume path only)
# ---------------------------------------------------------------------------


def _stdout(proc: Any) -> str:
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    return raw if isinstance(raw, str) else ""


def _stderr(proc: Any) -> str:
    raw = (proc.stderr or "") if hasattr(proc, "stderr") else ""
    text = raw if isinstance(raw, str) else ""
    return text[:4096]


async def rke2_service_restart(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Any = None,
) -> dict[str, Any]:
    """Handler for ``rke2.node.service.restart`` (resume path only).

    Re-validates the unit against :data:`_RESTARTABLE_UNIT_SET` before any SSH
    traffic (out-of-list -> error envelope, no command sent), restarts it, and
    health-gates on ``systemctl is-active <UNIT> == "active"``.
    """
    try:
        unit = bound_unit(params)
    except Rke2WriteSafetyError as exc:
        return {"restarted": False, "error": f"service.restart safety check: {exc}"}

    quoted_unit = shlex.quote(unit)
    node = getattr(target, "name", None)

    restart_cmd = f"systemctl restart {quoted_unit}"
    restart_proc = await connector._run_command(
        target, restart_cmd, operator=operator, timeout=120.0
    )
    restarted = getattr(restart_proc, "exit_status", None) == 0
    if not restarted:
        return {
            "restarted": False,
            "unit": unit,
            "action": "restart",
            "node": node,
            "error": _stderr(restart_proc) or "systemctl restart returned a non-zero status",
        }

    active_cmd = f"systemctl is-active {quoted_unit}"
    active_proc = await connector._run_command(target, active_cmd, operator=operator)
    is_active = _stdout(active_proc).strip() == "active"
    return {
        "restarted": True,
        "unit": unit,
        "action": "restart",
        "node": node,
        "is_active": is_active,
    }


async def rke2_config_update(
    connector: Rke2SshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Any = None,
) -> dict[str, Any]:
    """Handler for ``rke2.node.config.update`` (resume path only).

    Backplane-owned key merge of a bounded ``/etc/rancher/rke2/*.yaml`` file:
    read + parse the current YAML, apply the key-level patch, validate it
    re-parses, write it back atomically (``0600 root:root``). Does **not**
    restart -- returns ``restart_required: true`` + changed key **names** only.
    """
    try:
        path = bound_config_path(params.get("path"))
        patch = bound_patch(params.get("patch"))
        semantics = bound_semantics(params.get("semantics"))
    except Rke2WriteSafetyError as exc:
        return {"updated": False, "error": f"config.update safety check: {exc}"}

    quoted_path = shlex.quote(path)
    read_cmd = f"if [ -e {quoted_path} ]; then cat -- {quoted_path}; fi"
    read_proc = await connector._run_command(target, read_cmd, operator=operator)
    if getattr(read_proc, "exit_status", None) not in (0, None):
        return {"updated": False, "path": path, "error": "failed to read the current config file"}

    try:
        current_raw = yaml.safe_load(_stdout(read_proc))
    except yaml.YAMLError as exc:
        return {"updated": False, "path": path, "error": f"current config is not valid YAML: {exc}"}
    current: dict[str, Any] = current_raw if current_raw is not None else {}
    if not isinstance(current, dict):
        return {"updated": False, "path": path, "error": "current config is not a YAML mapping"}

    merged = apply_config_patch(current, patch, semantics)
    try:
        new_text = yaml.safe_dump(merged, default_flow_style=False, sort_keys=False)
        reparsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        return {
            "updated": False,
            "path": path,
            "error": f"merged config failed to serialise: {exc}",
        }
    if reparsed != merged:
        return {
            "updated": False,
            "path": path,
            "error": "merged config did not round-trip as YAML",
        }

    write_proc = await connector._run_command(
        target, _build_atomic_write_script(path, new_text), operator=operator, timeout=60.0
    )
    if getattr(write_proc, "exit_status", None) != 0:
        return {
            "updated": False,
            "path": path,
            "error": _stderr(write_proc) or "atomic config write returned a non-zero status",
        }

    return {
        "updated": True,
        "path": path,
        "restart_required": True,
        "changed_keys": changed_config_keys(current, merged),
    }


# ---------------------------------------------------------------------------
# Approval-park preview builders (blast-radius shapes; never file body/values)
# ---------------------------------------------------------------------------


async def _rke2_service_restart_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``rke2.node.service.restart`` -- the systemd-unit
    blast-radius shape. Declines (``None``) an out-of-list unit so a preview
    never advertises an out-of-bounds one."""
    unit = ctx.params.get("unit")
    if not isinstance(unit, str) or unit not in _RESTARTABLE_UNIT_SET:
        return None
    return {
        "resource": "systemd_unit",
        "unit": unit,
        "action": "restart",
        "node": getattr(ctx.target, "name", None),
    }


async def _rke2_config_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``rke2.node.config.update`` -- the config-file
    blast-radius shape (path, semantics, patch **key names**), never values or
    the body. Runs even for the ``credential_write`` class: a bespoke builder
    owns its field discipline (#1857) and this one only surfaces key names."""
    patch = ctx.params.get("patch")
    if not isinstance(patch, dict):
        return None
    path = ctx.params.get("path") or _DEFAULT_CONFIG_PATH
    semantics = ctx.params.get("semantics") or "merge"
    return {
        "resource": "config_file",
        "path": path,
        "semantics": semantics,
        "key_names": sorted(str(k) for k in patch),
    }


def register_rke2_write_previews() -> None:
    """Register the two write-op approval-park preview builders (import-time).

    Mirrors the vault ``kv_write_preview`` import side-effect so the builders
    are wired before the first dispatch parks. Idempotent.
    """
    from meho_backplane.operations._preview import register_preview_builder

    register_preview_builder("rke2.node.service.restart", _rke2_service_restart_preview)
    register_preview_builder("rke2.node.config.update", _rke2_config_update_preview)


# ---------------------------------------------------------------------------
# Curated when_to_use blurb (shared rke2-node-write group)
# ---------------------------------------------------------------------------

_WHEN_TO_USE_NODE_WRITE = (
    "Use for the two approval-gated RKE2 node-write ops, both dangerous and "
    "parked for human approval before anything changes: "
    "``rke2.node.service.restart`` restarts EXACTLY one of ``rke2-server`` / "
    "``rke2-agent`` (no other unit, no arbitrary systemctl action) and "
    "health-gates on ``systemctl is-active``; ``rke2.node.config.update`` "
    "edits a ``/etc/rancher/rke2/*.yaml`` file via a backplane-owned key "
    "merge (read + parse + patch + write back 0600 root:root atomically -- "
    "no host-side sed/yq) and returns restart_required=true WITHOUT "
    "restarting, so batch multiple config edits behind a single restart "
    "approval. Config changes are inert until a restart. " + SSH_TRANSPORT_NOTE
)

#: Curated ``when_to_use`` blurb per write-op group. The ``rke2-node-write``
#: key never collides with the read-op group keys (``identity`` / ``posture``)
#: the connector's ``register_operations`` walk merges alongside it.
RKE2_WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "rke2-node-write": _WHEN_TO_USE_NODE_WRITE,
}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_RKE2_SERVICE_RESTART_OP = Rke2Op(
    op_id="rke2.node.service.restart",
    handler_attr="service_restart",
    summary="Restart an allow-listed RKE2 unit (rke2-server/rke2-agent) (approval-gated).",
    description=(
        "Runs ``systemctl restart <UNIT>`` over SSH, where UNIT must be "
        "``rke2-server`` or ``rke2-agent`` -- any other unit is rejected at the "
        "schema enum AND re-checked in the handler before any SSH traffic "
        "(fail-closed); there is NO arbitrary-unit / arbitrary-action surface. "
        "It then health-gates on ``systemctl is-active <UNIT>``. A restart is "
        "disruptive -- it reloads the otherwise-inert on-disk config and briefly "
        "interrupts the control-plane / kubelet. safety_level=dangerous, "
        "requires_approval=True -- parks for human approval before restarting."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "enum": list(_RESTARTABLE_UNITS),
                "description": (
                    "The RKE2 systemd unit to restart. Only 'rke2-server' "
                    "(server nodes) or 'rke2-agent' (agent nodes) are allowed."
                ),
            },
        },
        "required": ["unit"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "restarted": {"type": "boolean"},
            "unit": {"type": ["string", "null"]},
            "action": {"type": "string"},
            "node": {"type": ["string", "null"]},
            "is_active": {"type": "boolean"},
            "error": {"type": "string"},
        },
        "required": ["restarted"],
        "additionalProperties": True,
    },
    group_key="rke2-node-write",
    tags=("write", "rke2", "systemd", "restart"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": _WHEN_TO_USE_NODE_WRITE,
        "parameter_hints": {
            "unit": "Required; exactly 'rke2-server' or 'rke2-agent'. Nothing else is accepted.",
        },
        "output_shape": (
            "{restarted, unit, action: 'restart', node, is_active}. On a bound "
            "violation or a failed restart: {restarted: false, ..., error}."
        ),
    },
)


_RKE2_CONFIG_UPDATE_OP = Rke2Op(
    op_id="rke2.node.config.update",
    handler_attr="config_update",
    summary="Backplane-owned key merge of a bounded RKE2 config file (approval-gated).",
    description=(
        "Edits a ``/etc/rancher/rke2/*.yaml`` file (default ``config.yaml``; "
        "drop-ins ``config.yaml.d/*.yaml``) via a backplane-owned merge: the "
        "connector reads + parses the current YAML in-process, applies the "
        "operator's key-level ``patch`` ('merge' overlays keys; 'replace' swaps "
        "the whole config), validates it re-parses, and writes it back "
        "atomically (temp under /etc/rancher/rke2, chmod 0600, chown root:root, "
        "mv). NO host-side sed/yq, NO arbitrary-file-write; the path is confined "
        "to /etc/rancher/rke2/*.yaml (traversal / escape rejected, no body "
        "leaked). Config is inert until a restart, so this op does NOT restart "
        "-- it returns restart_required=true (batch edits, then one restart) and "
        "changed key NAMES only, never values. safety_level=dangerous, "
        "requires_approval=True."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "pattern": r"^/etc/rancher/rke2/[^\x00\n\r]*\.yaml$",
                "description": (
                    "Absolute path to the RKE2 config file. Must be a .yaml "
                    "file under /etc/rancher/rke2/ (default "
                    "/etc/rancher/rke2/config.yaml). Omit for the default."
                ),
            },
            "patch": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": True,
                "description": (
                    "Key -> value edits to apply to the config. Under 'merge' "
                    "each key overlays (or adds) that top-level key; under "
                    "'replace' this object becomes the whole config."
                ),
            },
            "semantics": {
                "type": "string",
                "enum": list(_CONFIG_SEMANTICS),
                "description": (
                    "'merge' (default) overlays the patch keys onto the current "
                    "config; 'replace' swaps the whole config for the patch."
                ),
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "updated": {"type": "boolean"},
            "path": {"type": "string"},
            "restart_required": {"type": "boolean"},
            "changed_keys": {"type": "array", "items": {"type": "string"}},
            "error": {"type": "string"},
        },
        "required": ["updated"],
        "additionalProperties": True,
    },
    group_key="rke2-node-write",
    tags=("write", "rke2", "config"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": _WHEN_TO_USE_NODE_WRITE,
        "parameter_hints": {
            "path": "Optional; a .yaml file under /etc/rancher/rke2/. Omit for config.yaml.",
            "patch": "Required non-empty key->value object; the key-level edit to apply.",
            "semantics": "'merge' (default) or 'replace'.",
        },
        "output_shape": (
            "{updated, path, restart_required: true, changed_keys: [names]} -- "
            "key NAMES only, never values, never the file body. Does NOT "
            "restart. On a bound violation: {updated: false, error}."
        ),
    },
)


#: The approval-gated write ops. Composed onto :data:`RKE2_OPS` after the read
#: tier in :func:`meho_backplane.connectors.rke2.ops._rke2_ops`.
WRITE_OPS: tuple[Rke2Op, ...] = (_RKE2_SERVICE_RESTART_OP, _RKE2_CONFIG_UPDATE_OP)


register_rke2_write_previews()
