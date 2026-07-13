# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated write ops for :class:`Rke2SshConnector` (Initiative #2172).

Three ``dangerous`` / ``requires_approval=True`` write ops share this module,
composed onto :data:`RKE2_OPS` after the read tier in
:func:`meho_backplane.connectors.rke2.ops._rke2_ops`:

``rke2.token.rotate`` (G-Node/RKE2-T2 #2429)
--------------------------------------------

Rotates the RKE2 server join token cluster-wide -- a cluster-wide credential
change (the holodeck / argocd / bind9 write mold): the dispatcher's policy
gate parks a USER dispatch at ``needs-approval`` and floors an agent, and the
handler runs only on the ``_approved=True`` resume path.

THE audit rule: the dispatcher persists the **raw** handler result on the
audit row (``dispatcher.py`` ``raw_payload=redaction.raw``); connector-boundary
redaction never scrubs ``raw_payload``. So the only reliable control is that
this handler **never returns the token** -- old or new, not the value, not in
any field. The OLD token is read on-disk server-side inside the sudo script (a
shell ``$(cat ...)``), so its value never enters Python. The NEW token is
minted server-side (:func:`secrets.token_hex`), written to **Vault** under the
operator's identity, and only a **pointer** to the Vault location plus
non-secret metadata (``rotated`` / ``node`` / ``exit_status``) is returned. The
op is pinned in
:data:`~meho_backplane.broadcast.events._CREDENTIAL_MINT_OPS` and registers a
non-secret park-time preview builder (``ops_write_preview``). A read-only
fingerprint gate refuses a non-server / inactive / below-CVE-floor node
**before** any mutation (a botched rotate wedges every future node-join,
rancher/rke2#5785, #6250). This op elevates via the sanctioned safe-sudo
primitive (:mod:`~meho_backplane.connectors.rke2._sudo`, the bind9 #697 wire
shape) so the sudo password never lands in argv / history / log.

``rke2.node.service.restart`` + ``rke2.node.config.update`` (G-Node/RKE2-T3 #2430)
---------------------------------------------------------------------------------

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
  ``restart_required: true`` and changed key **names** only. An RKE2
  ``config.yaml`` body carries ``token:`` / ``agent-token:`` join credentials,
  so ``config.update`` is pinned in
  :data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS` and its result
  + approval preview surface key **names** only.

Privilege model for the T3 node ops: the connector operates as ``root`` over
SSH (the T1 posture tier ``stat``s ``0600 root:root`` token files), so these
writes run via ``_run_command`` without a separate sudo-password stream -- the
sudo primitive is reserved for the credential-minting ``token.rotate`` flow.

References: Tasks #2429/#2430; Initiative #2172; molds proxmox/bind9/holodeck
``ops_write.py``; bind9 safe-sudo (#697); Rancher RKE2 STIG V-254564
(``config.yaml`` is ``0600``); ``rke2 token rotate`` wedge rancher/rke2#5785,
#6250; approval routing ``policy_gate`` / G11.7-T1 #1401.
"""

from __future__ import annotations

import asyncio
import base64
import posixpath
import re
import secrets
import shlex
from typing import TYPE_CHECKING, Any

import yaml

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.rke2._sudo import run_remote_bash_with_sudo
from meho_backplane.connectors.rke2.ops import SSH_TRANSPORT_NOTE, Rke2Op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.rke2.connector import Rke2SshConnector, Target
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "RKE2_TOKEN_PATH",
    "RKE2_WHEN_TO_USE_WRITE_BY_GROUP",
    "WRITE_OPS",
    "ConfigPathRejectedError",
    "Rke2WriteError",
    "Rke2WriteSafetyError",
    "apply_config_patch",
    "bound_config_path",
    "changed_config_keys",
    "ensure_config_path_under_root",
    "rke2_config_update",
    "rke2_service_restart",
    "rke2_token_rotate",
    "rke2_version_rotate_verdict",
]


# ===========================================================================
# Shared errors
# ===========================================================================


class Rke2WriteError(ValueError):
    """Raised when a write op cannot proceed (fingerprint gate, missing creds).

    Subclass of :class:`ValueError` so the dispatcher's
    ``result_connector_error`` envelope picks it up uniformly. The message
    names the failed gate without echoing any secret material.
    """


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


# ===========================================================================
# rke2.token.rotate (#2429)
# ===========================================================================

#: The on-disk RKE2 server join token. Read server-side (as root, inside the
#: sudo script) as the OLD token; its value never enters Python. Mirrors the
#: constant the read-posture tier reports presence for.
RKE2_TOKEN_PATH: str = "/var/lib/rancher/rke2/server/token"

#: Absolute path to the ``rke2`` binary the installer drops. Absolute so the
#: sudo script never depends on root's ``PATH``.
_RKE2_BIN: str = "/var/lib/rancher/rke2/bin/rke2"

#: The systemd unit an RKE2 **server** node runs. Its presence in
#: ``systemctl list-unit-files`` is the role signal; ``is-active`` on it is
#: the running signal.
_RKE2_SERVER_UNIT: str = "rke2-server.service"

#: Per-minor CVE-fix floor for a safe token rotate (rancher/rke2#6250). A
#: rotate on a below-floor server can wedge node re-joins. Keyed by
#: ``(major, minor)`` -> the minimum ``(patch, rke2r)`` that carries the fix.
_RKE2_ROTATE_FLOORS: dict[tuple[int, int], tuple[int, int]] = {
    (1, 25): (15, 2),
    (1, 26): (10, 2),
    (1, 27): (7, 2),
    (1, 28): (3, 2),
}

#: The single known-bad build that regressed rotate even though it is at/above
#: its minor floor's patch (rancher/rke2#6250). Denied explicitly.
_RKE2_KNOWN_BAD_BUILD: tuple[tuple[int, int, int], int] = ((1, 27, 10), 1)

#: A parsed RKE2 release: ``v1.28.3+rke2r2`` -> ``((1, 28, 3), 2)``.
_RKE2_RELEASE_RE: re.Pattern[str] = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\+rke2r(\d+)$")

_VERSION_PREFLIGHT_CMD: str = (
    "printf 'ACTIVE=%s\\n' \"$(systemctl is-active rke2-server 2>/dev/null || true)\"; "
    "printf 'UNIT=%s\\n' "
    '"$(systemctl list-unit-files rke2-server.service --no-legend 2>/dev/null '
    "| awk '{print $1}' | head -n1)\"; "
    "printf 'VERSION=%s\\n' \"$("
    "rke2 --version 2>/dev/null | head -n1 || "
    '/usr/local/bin/rke2 --version 2>/dev/null | head -n1 || true)"'
)

#: Non-word characters collapse to ``-`` when deriving the Vault path segment
#: from the target name -- keeps the KV path to a safe ``[A-Za-z0-9._-]`` set.
_NODE_SLUG_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]+")


def parse_rke2_release(version: str | None) -> tuple[tuple[int, int, int], int] | None:
    """Parse an RKE2 release string into ``((major, minor, patch), rke2r)``.

    Examples
    --------

    >>> parse_rke2_release("v1.28.3+rke2r2")
    ((1, 28, 3), 2)
    >>> parse_rke2_release("1.27.10+rke2r1")
    ((1, 27, 10), 1)
    >>> parse_rke2_release("v1.29.0") is None
    True
    >>> parse_rke2_release(None) is None
    True
    """
    if not version:
        return None
    match = _RKE2_RELEASE_RE.match(version.strip())
    if match is None:
        return None
    major, minor, patch, rke2r = (int(g) for g in match.groups())
    return ((major, minor, patch), rke2r)


def rke2_version_rotate_verdict(version: str | None) -> tuple[bool, str]:
    """Return ``(safe, reason)`` for rotating on RKE2 *version*.

    A version is safe when it is at/above the per-minor CVE-fix floor
    (:data:`_RKE2_ROTATE_FLOORS`) for a covered minor line, or on any newer
    minor line (>= 1.29, all patched), and is not the known-bad
    ``v1.27.10+rke2r1`` build. Below the covered range (<= 1.24, EOL /
    unpatched) or unparseable metadata is refused.
    """
    parsed = parse_rke2_release(version)
    if parsed is None:
        return False, f"unparseable RKE2 release metadata: {version!r}"
    (major, minor, patch), rke2r = parsed

    if ((major, minor, patch), rke2r) == _RKE2_KNOWN_BAD_BUILD:
        return False, (
            "v1.27.10+rke2r1 regressed token rotate (rancher/rke2#6250); "
            "upgrade to v1.27.10+rke2r2 or newer before rotating"
        )

    minor_key = (major, minor)
    floor = _RKE2_ROTATE_FLOORS.get(minor_key)
    if floor is not None:
        if (patch, rke2r) >= floor:
            return True, "at or above the CVE-fix floor for its minor line"
        floor_patch, floor_r = floor
        return False, (
            f"below the rotate CVE-fix floor for v{major}.{minor}: needs "
            f">= v{major}.{minor}.{floor_patch}+rke2r{floor_r} (rancher/rke2#6250)"
        )

    # Unlisted minor line: newer than the covered range is patched; older is
    # EOL / predates the fix.
    if (major, minor) > max(_RKE2_ROTATE_FLOORS):
        return True, "newer than the covered CVE-fix range (all builds patched)"
    return False, (
        f"v{major}.{minor} predates the rotate CVE-fix range "
        "(EOL / no fixed build); upgrade before rotating"
    )


def _node_label(target: Any) -> str:
    """Human-facing node label for the result / preview -- name, else host."""
    name = getattr(target, "name", None)
    if isinstance(name, str) and name.strip():
        return name
    host = getattr(target, "host", None)
    return host if isinstance(host, str) and host.strip() else "unknown"


def _vault_token_path(operator: Operator, target: Any) -> str:
    """Build the per-tenant KV-v2 path the minted token is stashed under.

    ``tenants/<tenant_id>/rke2/<node>/server-token`` on the default
    ``secret`` mount -- the canonical per-tenant layout (#1723) so the write
    lands where the operator's ``meho-mcp`` Vault policy already grants
    ``create``/``update``. The node segment is a ``[A-Za-z0-9._-]`` slug of
    the target name.
    """
    slug = _NODE_SLUG_RE.sub("-", _node_label(target)).strip("-") or "node"
    return f"tenants/{operator.tenant_id}/rke2/{slug}/server-token"


async def _resolve_sudo_password(
    connector: Rke2SshConnector, target: Any, operator: Operator | None
) -> str:
    """Resolve the sudo password from the target's Vault secret (#2155).

    Keys on a dedicated ``sudo_password`` field first, falling back to the
    SSH ``password`` (the consumer-wrapper convention). Raises
    :class:`Rke2WriteError` when neither is set -- the safe-sudo primitive
    requires a non-empty single-line credential and the rotate cannot
    legitimately proceed without root.
    """
    secret = await connector._resolve_secret(target, operator)
    password = secret.get("sudo_password") or secret.get("password")
    if not password:
        raise Rke2WriteError(
            "the target's Vault secret carries no sudo_password / password; "
            "rke2.token.rotate needs a sudo credential to run as root"
        )
    return strip_credential_value(password)


def _parse_preflight(stdout: str) -> dict[str, str]:
    """Parse the ``KEY=VALUE`` preflight stdout into a flat map."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


async def _fingerprint_gate(
    connector: Rke2SshConnector, target: Any, operator: Operator
) -> dict[str, Any] | None:
    """Read-only pre-rotate gate. Return an error envelope on refusal, else ``None``.

    Runs one read-only SSH round-trip and refuses a node that is not an RKE2
    server, whose ``rke2-server`` is not active, or whose RKE2 version cannot
    safely rotate -- **before** any mutation.
    """
    proc = await connector._run_command(target, _VERSION_PREFLIGHT_CMD, operator=operator)
    pre_raw = proc.stdout if hasattr(proc, "stdout") else ""
    fields = _parse_preflight(pre_raw if isinstance(pre_raw, str) else "")

    if fields.get("UNIT") != _RKE2_SERVER_UNIT:
        return {
            "rotated": False,
            "error": (
                "fingerprint gate: node is not an RKE2 server "
                "(rke2-server.service is not installed)"
            ),
            "gate": "role",
        }
    active = fields.get("ACTIVE", "")
    if active != "active":
        return {
            "rotated": False,
            "error": f"fingerprint gate: rke2-server is not active (state={active!r})",
            "gate": "service",
        }
    # Lazy import avoids a connector <-> ops_write import cycle (connector
    # imports this module's when_to_use table at its own import time).
    from meho_backplane.connectors.rke2.connector import parse_rke2_version

    version = parse_rke2_version(fields.get("VERSION", ""))
    safe, reason = rke2_version_rotate_verdict(version)
    if not safe:
        return {
            "rotated": False,
            "error": f"fingerprint gate: RKE2 version {version!r} cannot rotate -- {reason}",
            "gate": "version",
            "version": version,
        }
    return None


async def _stash_token_in_vault(
    operator: Operator, target: Any, *, new_token: str, node: str, exit_status: int | None
) -> dict[str, Any]:
    """Write the minted token to Vault and return a pointer (never the value).

    On a Vault failure the rotate already succeeded (the new token is the live
    cluster token, persisted on the node's own disk), so the partial state is
    surfaced honestly -- ``rotated: True`` with ``token_ref: None`` and a
    ``vault_error`` class name, still without the token value.
    """
    from meho_backplane.auth.vault import vault_client_for_operator

    mount = "secret"
    path = _vault_token_path(operator, target)
    try:
        async with vault_client_for_operator(operator) as client:
            write_payload = await asyncio.to_thread(
                client.secrets.kv.v2.create_or_update_secret,
                path=path,
                secret={"token": new_token},
                mount_point=mount,
            )
        kv_version = write_payload["data"]["version"]
    except Exception as exc:
        return {
            "rotated": True,
            "node": node,
            "exit_status": exit_status,
            "token_ref": None,
            "vault_error": type(exc).__name__,
        }

    return {
        "rotated": True,
        "node": node,
        "exit_status": exit_status,
        "token_ref": {
            "backend": "vault",
            "mount": mount,
            "path": path,
            "key": "token",
            "kv_version": kv_version,
        },
    }


def _build_rotate_script(new_token: str) -> str:
    """Build the sudo script: read OLD on-disk as root, run ``rke2 token rotate``.

    The OLD token is a shell variable (``$(cat ...)``) -- it never enters
    Python. The NEW token is the only interpolated value (``shlex.quote``'d),
    streamed on stdin via the safe-sudo primitive, never in argv / history /
    log.
    """
    return (
        "set -euo pipefail\n"
        "umask 077\n"
        f"TOKENFILE={shlex.quote(RKE2_TOKEN_PATH)}\n"
        'if [ ! -r "$TOKENFILE" ]; then echo "old-token-unreadable" >&2; exit 3; fi\n'
        'OLD=$(cat "$TOKENFILE")\n'
        f'{shlex.quote(_RKE2_BIN)} token rotate --token "$OLD" '
        f"--new-token {shlex.quote(new_token)}\n"
    )


async def rke2_token_rotate(
    connector: Rke2SshConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``rke2.token.rotate`` -- approval-gated server-token rotation.

    Runs only on the ``_approved=True`` resume path. Flow: fail closed
    without an operator (no Vault sink) -> resolve the sudo credential ->
    read-only fingerprint gate (server role + active service + safe version)
    -> mint a new token -> one sudo script that reads the OLD token on-disk
    and runs ``rke2 token rotate`` -> stash the NEW token in Vault -> return
    a pointer + non-secret metadata. The token value (old or new) never
    appears in the returned dict.
    """
    del params  # schema declares no params; the target addresses the node

    if operator is None:
        return {
            "rotated": False,
            "error": (
                "rke2.token.rotate requires an operator identity for the Vault "
                "token sink; refusing to rotate without a place to stash the "
                "new token"
            ),
            "gate": "operator",
        }

    try:
        sudo_password = await _resolve_sudo_password(connector, target, operator)
    except Rke2WriteError as exc:
        return {"rotated": False, "error": str(exc), "gate": "credentials"}

    gate_error = await _fingerprint_gate(connector, target, operator)
    if gate_error is not None:
        return gate_error

    # Mint + rotate. The OLD token is read on-disk as root inside the script;
    # the NEW token is minted here and only ever leaves as a Vault pointer.
    new_token = secrets.token_hex(32)
    node = _node_label(target)
    result = await run_remote_bash_with_sudo(
        connector,
        target,
        _build_rotate_script(new_token),
        operator=operator,
        sudo_password=sudo_password,
    )
    exit_status = getattr(result, "exit_status", None)
    if exit_status != 0:
        # Never surface stdout/stderr: ``rke2 token rotate`` diagnostics could
        # echo a token value, and the raw result is persisted on the audit row.
        return {
            "rotated": False,
            "error": "rke2 token rotate exited non-zero",
            "exit_status": exit_status,
            "node": node,
            "gate": "rotate",
        }

    return await _stash_token_in_vault(
        operator, target, new_token=new_token, node=node, exit_status=exit_status
    )


# ===========================================================================
# rke2.node.service.restart + rke2.node.config.update (#2430)
# ===========================================================================

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
    """Register the two node-write-op approval-park preview builders (import-time).

    Mirrors the vault ``kv_write_preview`` import side-effect so the builders
    are wired before the first dispatch parks. Idempotent. (The
    ``rke2.token.rotate`` preview is registered separately in
    :mod:`~meho_backplane.connectors.rke2.ops_write_preview`.)
    """
    from meho_backplane.operations._preview import register_preview_builder

    register_preview_builder("rke2.node.service.restart", _rke2_service_restart_preview)
    register_preview_builder("rke2.node.config.update", _rke2_config_update_preview)


# ===========================================================================
# Curated when_to_use blurbs (one per write group)
# ===========================================================================

_WHEN_TO_USE_TOKEN_WRITE: str = (
    "Use to rotate an RKE2 cluster's server join token: "
    "``rke2.token.rotate`` runs ``rke2 token rotate`` on a server node so a "
    "leaked or aging join token is replaced cluster-wide. It is "
    "approval-gated -- a dispatch parks for a human to approve before "
    "anything changes -- and takes NO token parameter: the new token is "
    "minted server-side, stashed in Vault, and only a pointer is returned "
    "(the token value never appears in the result, the audit row, or the "
    "feed). A read-only fingerprint gate refuses a non-server node, an "
    "inactive rke2-server, or a below-floor / known-bad RKE2 version before "
    "touching anything (a botched rotate wedges future node joins). The "
    "right op when the join token must be rotated on a single server node; "
    "multi-node restart choreography is a separate operator runbook. " + SSH_TRANSPORT_NOTE
)

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

#: Curated ``when_to_use`` blurb per write-op group. The ``-write`` suffix keeps
#: each key from colliding with the read-op group keys (``identity`` /
#: ``posture``) the connector's ``register_operations`` walk merges alongside.
RKE2_WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "rke2-token-write": _WHEN_TO_USE_TOKEN_WRITE,
    "rke2-node-write": _WHEN_TO_USE_NODE_WRITE,
}


# ===========================================================================
# Op metadata
# ===========================================================================

_TOKEN_ROTATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rotated": {"type": "boolean"},
        "node": {"type": ["string", "null"]},
        "exit_status": {"type": ["integer", "null"]},
        "token_ref": {
            "type": ["object", "null"],
            "properties": {
                "backend": {"type": "string"},
                "mount": {"type": "string"},
                "path": {"type": "string"},
                "key": {"type": "string"},
                "kv_version": {"type": ["integer", "null"]},
            },
            "additionalProperties": True,
        },
        "error": {"type": "string"},
        "gate": {"type": "string"},
        "vault_error": {"type": "string"},
        "version": {"type": ["string", "null"]},
    },
    "required": ["rotated"],
    "additionalProperties": True,
}


_RKE2_TOKEN_ROTATE_OP = Rke2Op(
    op_id="rke2.token.rotate",
    handler_attr="token_rotate",
    summary="Rotate the RKE2 server join token cluster-wide (approval-gated).",
    description=(
        "Runs ``rke2 token rotate`` on an RKE2 server node over SSH to "
        "replace the cluster's server join token. Takes NO parameters "
        "and NO token value: the new token is minted server-side, the "
        "OLD token is read on-disk as root inside the rotate script, and "
        "the new token is written to Vault -- only a pointer to the Vault "
        "location plus non-secret metadata (rotated / node / exit_status) "
        "is returned, so no token value ever reaches the result, the "
        "audit row, or the broadcast feed. A read-only fingerprint gate "
        "refuses a non-server node, an inactive rke2-server, or a "
        "below-floor / known-bad (v1.27.10+rke2r1) RKE2 version before "
        "any mutation, because a botched rotate wedges every future node "
        "join (rancher/rke2#5785). safety_level=dangerous, "
        "requires_approval=True -- parks for human approval first."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema=_TOKEN_ROTATE_RESPONSE_SCHEMA,
    group_key="rke2-token-write",
    tags=("write", "token", "rotate", "credential", "rke2"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_use": _WHEN_TO_USE_TOKEN_WRITE,
        "parameter_hints": {},
        "output_shape": (
            "{rotated, node, exit_status, token_ref: {backend, mount, "
            "path, key, kv_version}}. token_ref points at the Vault KV-v2 "
            "location the new token was stashed at -- the token VALUE is "
            "never returned. On a gate refusal or failure: {rotated: "
            "false, error, gate}. If the rotate succeeded but the Vault "
            "stash failed: {rotated: true, token_ref: null, vault_error}."
        ),
    },
)


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
WRITE_OPS: tuple[Rke2Op, ...] = (
    _RKE2_TOKEN_ROTATE_OP,
    _RKE2_SERVICE_RESTART_OP,
    _RKE2_CONFIG_UPDATE_OP,
)


register_rke2_write_previews()
