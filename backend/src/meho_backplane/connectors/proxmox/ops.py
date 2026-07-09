# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op metadata + the passthrough allowlist for :class:`ProxmoxConnector` (#2238).

Proxmox VE exposes a broad, uniform REST surface under ``/api2/json`` — one
connector cannot hand-curate every node / VM / cluster endpoint the way the
argocd or holodeck connectors curate their handful of ops. Instead this
connector ships a **generic ``<METHOD> <path>`` passthrough**, split into a
read op (``proxmox.api.get`` — GET/HEAD, ``safety_level="safe"``) and a write
op (``proxmox.api.write`` — POST/PUT/DELETE, ``safety_level="dangerous"`` +
``requires_approval=True``). The agent composes the relative path
(``nodes/pve/qemu/100/status/start``, ``cluster/resources``, ``version``, …);
the connector prepends the constant base and dispatches.

Passthrough safety — two-layer allowlist
=========================================

A ``<METHOD> <path>`` passthrough is **not** an open proxy. Two independent
layers gate every call, modelled on holodeck's read-only ``kubectl`` guard
(:mod:`meho_backplane.connectors.holodeck.ops_read`):

1. **Schema layer** — ``parameter_schema.properties.path`` carries a
   ``pattern`` (:data:`API_PATH_PATTERN`) anchored ``\\A … \\Z`` that admits
   only a relative path built from the safe character class
   (:data:`_PATH_CHARS`), rejects a leading ``/`` (no absolute / protocol-
   relative URL), and rejects any ``..`` segment (no traversal off the API
   base). ``method`` is pinned to a per-op enum. The dispatcher's
   :func:`~meho_backplane.operations._validate.validate_params` runs this
   before the handler is reached; a bad shape surfaces as
   ``result_invalid_params``.
2. **Handler layer** — before the HTTP call lands, the handler re-validates
   via :func:`validate_api_path` / :func:`validate_method`, which raise
   :class:`ProxmoxPathError` / :class:`ProxmoxMethodError` (both
   ``ValueError`` subclasses → ``result_connector_error``). This is the
   **authoritative** gate: a future schema widening cannot silently re-open
   the hole, and the method allowlist is what enforces the read/write split
   at the transport boundary (GET/HEAD can never reach the write handler and
   vice versa).

Constant path base (not a parameter)
====================================

:data:`API_BASE` (``/api2/json``) is prepended by the handler and is **never**
a parameter — the op cannot be coerced into dialing an operator- or
attacker-chosen mount, and the pooled ``httpx`` client is host-pinned to the
target's ``base_url`` regardless. This is the ``constant-not-parameter path
base`` discipline holodeck applies to its ``du`` growth dirs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "API_BASE",
    "API_PATH_PATTERN",
    "PROXMOX_READ_OPS",
    "PROXMOX_WHEN_TO_USE_BY_GROUP",
    "READ_METHODS",
    "WRITE_METHODS",
    "ProxmoxMethodError",
    "ProxmoxOp",
    "ProxmoxPathError",
    "join_api_path",
    "validate_api_path",
    "validate_method",
]

#: The constant Proxmox REST base every op path is mounted under. Prepended
#: by the handlers; never a parameter (see module docstring).
API_BASE = "/api2/json"

#: Safe character class for a relative API path segment string. Letters,
#: digits, and the punctuation Proxmox paths actually use: ``.`` / ``_`` /
#: ``-`` (names, versions), ``/`` (segment separator), ``@`` (``user@realm``
#: in ``access/**`` paths), ``:`` (the colon-delimited ``UPID`` in
#: ``nodes/{node}/tasks/{upid}/status``). Deliberately excludes whitespace,
#: ``%`` (no percent-encoding injection), ``?`` / ``&`` / ``#`` (query and
#: fragment travel in the separate ``query`` param), and shell/URL
#: metacharacters.
_PATH_CHARS = r"A-Za-z0-9._@:/-"

#: JSON-schema ``pattern`` for the ``path`` parameter. Anchored ``\A … \Z``
#: (jsonschema's ``pattern`` is an unanchored search, so the anchors are
#: load-bearing). Negative lookaheads reject a leading ``/`` (absolute /
#: protocol-relative) and any ``..`` traversal segment; the character class
#: forbids newlines, so a multi-line injection cannot smuggle a second value.
API_PATH_PATTERN = rf"\A(?!/)(?!.*\.\.)[{_PATH_CHARS}]+\Z"

#: Compiled handler-layer twin of :data:`API_PATH_PATTERN`.
_API_PATH_RE: re.Pattern[str] = re.compile(API_PATH_PATTERN)

#: Idempotent read verbs the ``proxmox.api.get`` passthrough admits.
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

#: State-changing verbs the ``proxmox.api.write`` passthrough admits. Every
#: one is approval-gated at registration (``requires_approval=True``).
WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE"})


class ProxmoxPathError(ValueError):
    """Raised by :func:`validate_api_path` when a passthrough path is unsafe.

    Subclass of :class:`ValueError` so the dispatcher's
    ``result_connector_error`` envelope picks it up uniformly. The message
    names *why* the path was refused (leading slash, ``..`` traversal, or a
    disallowed character) without echoing sensitive context.
    """


class ProxmoxMethodError(ValueError):
    """Raised by :func:`validate_method` when a verb is outside the op's set.

    Enforces the read/write split at the handler boundary: the read
    passthrough admits only :data:`READ_METHODS`, the write passthrough only
    :data:`WRITE_METHODS`. A verb outside the op's allowed set fails closed.
    """


def validate_api_path(path: str) -> str:
    """Return *path* unchanged when it is a safe relative API path, else raise.

    The authoritative handler-layer gate (the schema ``pattern`` is the
    guardrail). Rejects an empty path, a leading ``/`` (absolute /
    protocol-relative URL), any ``..`` traversal segment, an empty ``//``
    segment, and any character outside :data:`_PATH_CHARS`. Raises
    :class:`ProxmoxPathError` on any violation.
    """
    if not path:
        raise ProxmoxPathError("proxmox api path must not be empty")
    if path.startswith("/"):
        raise ProxmoxPathError(
            f"proxmox api path must be relative to /api2/json (no leading '/'); got {path!r}"
        )
    if ".." in path:
        raise ProxmoxPathError(
            f"proxmox api path must not contain a '..' traversal segment; got {path!r}"
        )
    if "//" in path:
        raise ProxmoxPathError(
            f"proxmox api path must not contain an empty '//' segment; got {path!r}"
        )
    if not _API_PATH_RE.match(path):
        raise ProxmoxPathError(
            "proxmox api path carries a disallowed character (allowed: letters, "
            f"digits, and '._@:/-'); got {path!r}"
        )
    return path


def validate_method(method: str, allowed: frozenset[str]) -> str:
    """Return the upper-cased *method* when it is in *allowed*, else raise.

    :class:`ProxmoxMethodError` when the verb is outside the op's set — the
    handler-layer half of the read/write split.
    """
    verb = method.upper()
    if verb not in allowed:
        raise ProxmoxMethodError(
            f"proxmox method {verb!r} is not permitted for this op (allowed: {sorted(allowed)})"
        )
    return verb


def join_api_path(path: str) -> str:
    """Prepend the constant :data:`API_BASE` to a validated relative *path*."""
    return f"{API_BASE}/{path}"


@dataclass(frozen=True)
class ProxmoxOp:
    """Metadata for one proxmox op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar splats the dataclass into the helper without
    per-op boilerplate. ``handler_attr`` is the attribute on
    :class:`~meho_backplane.connectors.proxmox.connector.ProxmoxConnector`
    exposing the async handler; the registrar resolves the bound method at
    registration so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk
    recovers the callable from the persisted ``module.ClassName.method`` path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Reusable schema fragments
# ---------------------------------------------------------------------------

_PATH_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": API_PATH_PATTERN,
    "description": (
        "Relative Proxmox API path under /api2/json (no leading slash, no "
        "'..'). Examples: 'version', 'cluster/status', 'cluster/resources', "
        "'nodes', 'nodes/{node}/qemu', 'nodes/{node}/qemu/{vmid}/status/"
        "current'."
    ),
}

_QUERY_PROPERTY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "description": (
        "Optional query-string parameters, sent as the URL query. Flat "
        "scalar values (or lists, repeated per key)."
    ),
}


# ---------------------------------------------------------------------------
# Curated when_to_use blurbs
# ---------------------------------------------------------------------------

_WHEN_TO_USE_IDENTITY = (
    "Use for Proxmox VE identity questions before any drill-in: 'which PVE "
    "version / build is this cluster running?' The proxmox.about op returns "
    "vendor/product/version, the repoid build hash, and per-node online "
    "status (parsed from GET /version + GET /nodes). Call it first to "
    "confirm the target is reachable and authenticated."
)

_WHEN_TO_USE_API_READ = (
    "Use to READ any Proxmox VE REST resource by relative path via a single "
    "generic GET/HEAD op (proxmox.api.get): the node inventory (nodes), "
    "cluster health (cluster/status), the resource inventory "
    "(cluster/resources), per-VM status "
    "(nodes/{node}/qemu/{vmid}/status/current), storage, tasks, and so on. "
    "The path is validated against a strict allowlist and mounted under the "
    "fixed /api2/json base. Read-only — no state changes here (use "
    "proxmox.api.write for those). Pair with proxmox.task.status to follow a "
    "UPID returned by a write."
)

_WHEN_TO_USE_TASK = (
    "Use to read — or poll to completion — the status of a Proxmox "
    "background task by its UPID (proxmox.task.status). A write op "
    "(clone/start/stop/delete) returns a UPID for a long-running task; this "
    "op reads GET /nodes/{node}/tasks/{upid}/status to learn whether it is "
    "still running or has stopped, and with wait=true blocks (bounded) until "
    "the task reaches a terminal 'stopped' status, returning the exitstatus."
)

#: Curated ``when_to_use`` blurb per read/task op group. Write-op groups
#: carry a ``-write`` suffix and live in
#: :data:`~meho_backplane.connectors.proxmox.ops_write.PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP`
#: so the two maps never collide when the connector merges them.
PROXMOX_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "proxmox-identity": _WHEN_TO_USE_IDENTITY,
    "proxmox-api-read": _WHEN_TO_USE_API_READ,
    "proxmox-tasks": _WHEN_TO_USE_TASK,
}


# ---------------------------------------------------------------------------
# Read op registration table
# ---------------------------------------------------------------------------

PROXMOX_READ_OPS: tuple[ProxmoxOp, ...] = (
    ProxmoxOp(
        op_id="proxmox.about",
        handler_attr="about",
        summary="Proxmox VE identity: version, repoid build hash, per-node status.",
        description=(
            "Reads GET /api2/json/version and GET /api2/json/nodes and returns "
            "{vendor, product, version, release, repoid, nodes:[{node, "
            "status}]}. The canonical fingerprint an agent calls first to "
            "confirm the cluster is reachable and the credential authenticates."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        response_schema=None,
        group_key="proxmox-identity",
        tags=("read", "proxmox", "identity"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_IDENTITY,
            "output_shape": (
                "{vendor, product, version, release, repoid, reachable, "
                "nodes: [{node, status}, ...]}."
            ),
        },
    ),
    ProxmoxOp(
        op_id="proxmox.api.get",
        handler_attr="api_get",
        summary="Generic read (GET/HEAD) of any Proxmox REST path (allowlisted).",
        description=(
            "Issues a GET (or HEAD) against a relative Proxmox API path mounted "
            "under the fixed /api2/json base and returns the parsed 'data' "
            "payload. The path is gated by a two-layer allowlist (schema "
            "pattern + fail-closed handler) — a relative path only, no '..', "
            "safe characters only. Read-only: this op registers "
            "safety_level=safe and can never reach the write handler. Use for "
            "nodes, cluster/status, cluster/resources, version, per-VM status, "
            "storage, task lists, and any other GET resource."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "path": _PATH_PROPERTY,
                "method": {
                    "type": "string",
                    "enum": sorted(READ_METHODS),
                    "description": "Read verb (default GET).",
                },
                "query": _QUERY_PROPERTY,
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        response_schema=None,
        group_key="proxmox-api-read",
        tags=("read", "proxmox", "passthrough"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_API_READ,
            "parameter_hints": {
                "path": "Relative to /api2/json, e.g. 'cluster/resources'.",
                "method": "GET (default) or HEAD.",
                "query": "e.g. {'type': 'vm'} on cluster/resources.",
            },
            "output_shape": (
                "The endpoint's 'data' payload verbatim (object or list); HEAD "
                "returns {reachable, status_code}."
            ),
        },
    ),
    ProxmoxOp(
        op_id="proxmox.task.status",
        handler_attr="task_status",
        summary="Read or poll a Proxmox background task (UPID) to completion.",
        description=(
            "Reads GET /api2/json/nodes/{node}/tasks/{upid}/status for a task "
            "returned by a write op. Returns {upid, node, status, exitstatus, "
            "type, ...}. With wait=true it polls (bounded by "
            "poll_timeout_seconds) until status reaches the terminal "
            "'stopped' value, so an agent can follow a clone/start/stop/delete "
            "to completion and read its exitstatus ('OK' on success)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "node": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": API_PATH_PATTERN,
                    "description": "The PVE node name the task ran on.",
                },
                "upid": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": API_PATH_PATTERN,
                    "description": "The task UPID (colon-delimited) from a write op's result.",
                },
                "wait": {
                    "type": "boolean",
                    "description": (
                        "Poll until the task reaches terminal 'stopped' "
                        "status (default false: single read)."
                    ),
                },
                "poll_timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1800,
                    "description": "Max seconds to poll when wait=true (default 300).",
                },
            },
            "required": ["node", "upid"],
            "additionalProperties": False,
        },
        response_schema=None,
        group_key="proxmox-tasks",
        tags=("read", "proxmox", "task"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_TASK,
            "parameter_hints": {
                "node": "The node from the UPID (also the 2nd colon field of the UPID).",
                "upid": "The full UPID string a write op returned.",
                "wait": "true to block until the task finishes.",
            },
            "output_shape": (
                "{upid, node, status ('running'|'stopped'), exitstatus, type, "
                "timed_out}. exitstatus=='OK' means success."
            ),
        },
    ),
)
