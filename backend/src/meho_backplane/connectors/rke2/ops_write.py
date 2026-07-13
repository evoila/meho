# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated RKE2 server-token rotation over SSH (G-Node/RKE2-T2 #2429).

The first write op on the ``rke2-ssh`` connector: ``rke2.token.rotate``
rotates the RKE2 server join token cluster-wide. A cluster-wide credential
change, so ``safety_level="dangerous"`` + ``requires_approval=True`` (the
holodeck / argocd / bind9 write mold): the dispatcher's policy gate parks a
USER dispatch at ``needs-approval`` and floors an agent, and the handler
body below runs only on the ``_approved=True`` resume path.

The load-bearing constraint (THE audit rule)
=============================================

The dispatcher persists the **raw** handler result on the audit row
(``dispatcher.py`` ``raw_payload=redaction.raw``); connector-boundary
redaction never scrubs ``raw_payload``. So the only reliable control is that
this handler **never returns the token** -- old or new, not the value, not
in any field. Both tokens are handled so they never reach a result surface:

* The **OLD** token is read on-disk server-side from
  ``/var/lib/rancher/rke2/server/token`` *inside the sudo script* (a shell
  ``$(cat ...)``), so its value never enters Python and never enters the
  result.
* The **NEW** token is minted server-side here (:func:`secrets.token_hex`),
  written to **Vault** under the operator's identity, and only a
  **pointer** to the Vault location plus non-secret metadata
  (``rotated`` / ``node`` / ``exit_status``) is returned. The minted value
  is interpolated into the sudo script (streamed on stdin, never argv /
  history / log) but never into the returned dict, the logs, or an operator
  param -- ``ApprovalRequest.params`` is stored verbatim for resume, so the
  op takes **no** token param at all.

The op is additionally pinned in
:data:`~meho_backplane.broadcast.events._CREDENTIAL_MINT_OPS` (defence in
depth: ``.rotate`` would otherwise classify ``other`` and broadcast full
params) and registers a non-secret park-time preview builder
(``ops_write_preview``).

Fingerprint gate (reject-before-rotate)
=======================================

A botched rotate wedges every future node-join (rancher/rke2#5785), so the
handler refuses to proceed unless, checked over read-only SSH **before** any
mutation:

* the node is an RKE2 **server** (``rke2-server.service`` is installed),
* ``rke2-server`` is **active**, and
* the running RKE2 version is at or above the per-minor CVE-fix floor and is
  **not** the known-bad ``v1.27.10+rke2r1`` build (rancher/rke2#6250).

References
----------

* Task: https://github.com/evoila/meho/issues/2429
* Parent initiative: https://github.com/evoila/meho/issues/2172
* Write-op mold: ``holodeck/ops_write.py`` (#2154), bind9 safe-sudo (#697).
* Approval routing: ``policy_gate`` / G11.7-T1 #1401.
* raw_payload sink: ``dispatcher.py`` / ``middleware.py``.
* ``rke2 token rotate`` / join-token wedge: rancher/rke2#5785, #6250.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.rke2._sudo import run_remote_bash_with_sudo
from meho_backplane.connectors.rke2.ops import SSH_TRANSPORT_NOTE, Rke2Op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.rke2.connector import Rke2SshConnector

__all__ = [
    "RKE2_TOKEN_PATH",
    "RKE2_WHEN_TO_USE_WRITE_BY_GROUP",
    "WRITE_OPS",
    "Rke2WriteError",
    "rke2_token_rotate",
    "rke2_version_rotate_verdict",
]


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


class Rke2WriteError(ValueError):
    """Raised when a write op cannot proceed (fingerprint gate, missing creds).

    Subclass of :class:`ValueError` so the dispatcher's
    ``result_connector_error`` envelope picks it up uniformly. The message
    names the failed gate without echoing any secret material.
    """


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


# ---------------------------------------------------------------------------
# Curated when_to_use + op metadata
# ---------------------------------------------------------------------------

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

#: Curated ``when_to_use`` per write group. The ``-write`` suffix keeps it
#: from colliding with the read-op group keys the registration walk merges.
RKE2_WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "rke2-token-write": _WHEN_TO_USE_TOKEN_WRITE,
}


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


WRITE_OPS: tuple[Rke2Op, ...] = (
    Rke2Op(
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
    ),
)
