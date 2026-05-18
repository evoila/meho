# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""bind9 config-read op -- ``bind9.config.show``.

G3.4-T2 (#588) of Initiative #367. Read ``named.conf`` or any included
fragment, behind a path-safety filter that refuses paths outside the
bind config root.

T4 (#590) will append the config-write ops (``bind9.config.apply_views``
/ ``bind9.config.apply_file`` / ``bind9.config.backup`` /
``bind9.config.reload``) to this module against the same registration
shape.

Path-safety -- the load-bearing primitive
-----------------------------------------

The op exposes ``cat <file>`` on a remote nameserver. Without a filter
the agent could ask for ``/etc/passwd``, ``/etc/shadow``, or any other
file the SSH user can read; that's a clear regression on the consumer's
``scripts/bind9-dns.sh`` wrapper which at least scoped its reads to the
bind subtree by construction. The replacement encodes the same scoping
at the connector boundary:

1. ``allowed_root`` -- the directory of the bind config root, taken
   from the fingerprint's ``extras["named_conf_path"]`` (Debian default
   ``/etc/bind/``, RHEL default ``/etc/named/``, chrooted bind
   ``/var/cache/bind/``). The handler reads the fingerprint once per
   call -- the value is cached on the SSH adapter's connection so the
   per-call cost is one TCP round-trip in steady state.
2. ``ensure_path_under_root`` -- the pure filter. Accepts an absolute
   path that is **lexically** under ``allowed_root`` (no ``..``
   segments, no symlinks-out-of-root walk), and a relative path that
   resolves under ``allowed_root`` via :func:`posixpath.normpath`.
   Rejects everything else with a structured error that carries the
   sanitised attempt (the agent gets to know the value was refused
   without ever seeing the file's contents).

Why lexical, not realpath: a realpath check would require a second SSH
round-trip to resolve symlinks server-side, doubling the wire cost.
The threat model is operator-typed paths in agent prompts, not a
hostile operator carefully placing a symlink to ``/etc/shadow`` inside
``/etc/bind/`` -- if the latter exists, the operator already owns the
server. The lexical check rejects ``../`` ladders and absolute paths
outside the root, which is the right granularity for the agent-typed
risk.

References
----------

* Parent task: G3.4-T2 (#588).
* Parent Initiative: G3.4 (#367) (WI4 read ops; the path-safety filter
  is the canonical example of the connector encoding a safety
  invariant at the API boundary).
"""

from __future__ import annotations

import posixpath
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.bind9.ops import Bind9Op

if TYPE_CHECKING:
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "BIND9_CONFIG_SHOW_LLM_INSTRUCTIONS",
    "BIND9_CONFIG_SHOW_PARAMETER_SCHEMA",
    "CONFIG_OPS",
    "ConfigPathRejectedError",
    "bind9_config_show",
    "ensure_path_under_root",
]


class ConfigPathRejectedError(ValueError):
    """The requested path failed the path-safety filter.

    Subclassing :class:`ValueError` (not :class:`PermissionError`) keeps
    the dispatcher's ``invalid_params`` envelope mapping clean -- the
    error surfaces under ``extras.exception_class="ConfigPathRejectedError"``
    so callers can render an actionable hint. The error message
    deliberately echoes the *sanitised* path the filter rejected (not
    the raw operator input) so log scraping does not surface
    weaponised values verbatim.
    """


def ensure_path_under_root(requested: str, allowed_root: str) -> str:
    """Return an absolute remote path under *allowed_root* or raise.

    The pure filter that backs ``bind9.config.show``. Takes:

    * *requested* -- the operator-typed path. May be absolute or
      relative; relative paths resolve against *allowed_root*.
    * *allowed_root* -- the absolute bind config directory, taken from
      the fingerprint's ``extras["named_conf_path"]`` -- the *directory*
      part, not the file (a fingerprint of ``/etc/bind/named.conf``
      yields ``/etc/bind`` as the root).

    Returns the canonicalised absolute path the handler should ``cat``.
    Raises :class:`ConfigPathRejectedError` when the path lands outside
    the root, when it includes shell-control bytes, or when either
    input is degenerate.

    The check is lexical (uses :func:`posixpath.normpath` to collapse
    ``..`` and ``.`` segments) -- the module docstring spells out why
    we don't realpath-resolve via a second SSH round-trip.

    POSIX-only: bind9 ships on Linux + the BSDs; ``ntpath`` cases never
    apply, and using :mod:`pathlib` would auto-detect the *local*
    platform (which is irrelevant; the path resolution is for the
    *remote* nameserver's filesystem).
    """
    if not allowed_root or not allowed_root.startswith("/"):
        raise ConfigPathRejectedError(
            f"allowed_root {allowed_root!r} must be an absolute POSIX path "
            f"(starting with '/'); refusing to filter"
        )
    if not requested:
        raise ConfigPathRejectedError("requested path is empty")
    # Reject embedded shell-control bytes early -- the handler quotes the
    # final path with shlex.quote, but a NUL / newline inside the path
    # would survive quoting via shell-string-rules in some shells. Easier
    # and safer to refuse them outright.
    if any(ch in requested for ch in ("\x00", "\n", "\r")):
        raise ConfigPathRejectedError("requested path contains control character; refusing")

    # Build the candidate absolute path. ``posixpath.join`` with an
    # absolute second argument returns the second argument verbatim --
    # which is exactly what we want: an absolute *requested* skips the
    # join, and a relative *requested* joins under the root.
    candidate = posixpath.normpath(posixpath.join(allowed_root, requested))
    # The root itself is also normalised so the comparison is
    # apples-to-apples (a trailing slash on *allowed_root* otherwise
    # gives a spurious match).
    canonical_root = posixpath.normpath(allowed_root)

    # The candidate must equal the root *or* be one of its descendants.
    # The trailing-slash sentinel forces the descendant check to require
    # a path separator after the root -- otherwise ``/etc/bindroot``
    # would be accepted as "under" ``/etc/bind``.
    if candidate != canonical_root and not candidate.startswith(canonical_root + "/"):
        raise ConfigPathRejectedError(
            f"path {candidate!r} is outside the bind config root {canonical_root!r}"
        )
    return candidate


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def bind9_config_show(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.config.show``.

    Two-step:

    1. Fingerprint the target so the handler knows the bind config
       root. The SSH adapter's connection pool reuses a single TCP
       session, so this is a cheap pair of ``_run_command`` calls
       (``named -v`` + ``cat /etc/os-release``).
    2. Run the path-safety filter against the operator-typed path,
       then ``cat`` the resolved path.

    Returns ``{file, content}`` on success. The path-safety filter
    raises :class:`ConfigPathRejectedError` (a :class:`ValueError`
    subclass) for traversal attempts; the dispatcher's
    ``connector_error`` branch maps that to a structured envelope with
    no file content leaked. A unit test (and an acceptance criterion
    on Issue #588) asserts the envelope carries no content payload on
    a rejection.
    """
    requested: str = params["path"]
    fingerprint = await connector.fingerprint(target)
    # The fingerprint extras carry the absolute ``named.conf`` path; the
    # *directory* of that file is the bind config root. The fallback
    # ``/etc/bind`` is the Debian-family default, which is the only
    # shape T1 advertises today -- but we read through the fingerprint
    # so a future RHEL-family detection (``/etc/named``) automatically
    # widens the allowed root.
    named_conf_path = fingerprint.extras.get("named_conf_path") or "/etc/bind/named.conf"
    if not isinstance(named_conf_path, str):
        raise ConfigPathRejectedError(
            f"fingerprint extras.named_conf_path is not a string: {named_conf_path!r}"
        )
    allowed_root = posixpath.dirname(named_conf_path) or "/etc/bind"

    resolved = ensure_path_under_root(requested, allowed_root)

    # shlex.quote handles any embedded special chars (single quotes,
    # spaces, ...); the ensure_path_under_root filter rejected control
    # bytes already, so the resulting quoted form is safe to splice
    # into the SSH command line.
    proc = await connector._run_command(target, f"cat {shlex.quote(resolved)}", raw_jwt="")
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""

    return {"file": resolved, "content": content}


# ---------------------------------------------------------------------------
# Parameter schema + LLM instructions
# ---------------------------------------------------------------------------


BIND9_CONFIG_SHOW_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Path to read. May be absolute (must be lexically under "
                "the bind config root from fingerprint's "
                "``extras.named_conf_path`` directory) or relative "
                "(resolved against the same root). Traversal paths "
                "(``../...``) and absolute paths outside the root are "
                "rejected with a structured ``invalid_params`` error "
                "carrying no file content. Examples: ``named.conf``, "
                "``views/external.conf``, ``/etc/bind/named.conf.local``."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


_BIND9_CONFIG_SHOW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {
            "type": "string",
            "description": "The resolved absolute path on the remote host.",
        },
        "content": {
            "type": "string",
            "description": "File content verbatim, as bind9 sees it.",
        },
    },
    "required": ["file", "content"],
    "additionalProperties": False,
}


BIND9_CONFIG_SHOW_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Call when the operator asks 'show me named.conf' or 'what's "
        "in the views fragment X?'. Read-only. Path-safety is enforced "
        "at the API boundary: paths outside the bind config root "
        "(``extras.named_conf_path`` directory from the connector's "
        "fingerprint) are refused with no file content leaked. Use "
        "relative paths whenever possible -- they're inherently scoped "
        "and the agent doesn't have to remember the absolute root "
        "layout."
    ),
    "parameter_hints": {
        "path": (
            "Required. Absolute (under the bind config root) or "
            "relative (resolved against it). The connector resolves "
            "the root via fingerprint; the operator doesn't need to "
            "specify it."
        ),
    },
    "output_shape": (
        "On success: {'file': <resolved abs path>, 'content': <raw "
        "text>}. On rejection: a connector_error OperationResult with "
        "extras.exception_class='ConfigPathRejectedError' and no "
        "``content`` key on the envelope."
    ),
}


# ---------------------------------------------------------------------------
# Op metadata table
# ---------------------------------------------------------------------------


CONFIG_OPS: tuple[Bind9Op, ...] = (
    Bind9Op(
        op_id="bind9.config.show",
        handler_attr="bind9_config_show",
        summary="Read named.conf or an included fragment; path-safety-filtered.",
        description=(
            "Reads a bind9 config file over SSH and returns its content. "
            "Path-safety: only files under the bind config root "
            "(``extras.named_conf_path`` directory from the connector's "
            "fingerprint) are accessible. Traversal paths "
            "(``../../etc/passwd``, absolute ``/etc/passwd``, etc.) "
            "return a structured ``connector_error`` envelope with "
            "``exception_class='ConfigPathRejectedError'`` and no file "
            "content leaked. Use to inspect ``named.conf``, included "
            "fragments (``named.conf.local``, ``named.conf.options``), "
            "or per-view configuration the operator manages under "
            "``views/``. Read-only; pairs with ``bind9.zone.list`` for "
            "the operator question 'how is this nameserver configured?'."
        ),
        parameter_schema=BIND9_CONFIG_SHOW_PARAMETER_SCHEMA,
        response_schema=_BIND9_CONFIG_SHOW_RESPONSE_SCHEMA,
        group_key="config",
        tags=("read-only", "config"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=BIND9_CONFIG_SHOW_LLM_INSTRUCTIONS,
    ),
)
