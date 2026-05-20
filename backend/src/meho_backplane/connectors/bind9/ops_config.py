# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: per-op modules colocate handler + parameter_schema +
# LLM_INSTRUCTIONS + Bind9Op metadata for all of `bind9.config.*`'s
# operator/agent surface; splitting would scatter the load-bearing
# atomic-apply + audit-payload bindings across multiple translation
# units. Function-size warnings on the config-write handlers accepted:
# each is the linear "resolve path → atomic_apply → bind audit
# contextvars → return" sequence the docstrings spell out.

"""bind9 config-read + config-write op group.

G3.4-T2 (#588) shipped ``bind9.config.show`` -- the read op behind a
path-safety filter that refuses paths outside the bind config root.

G3.4-T4 (#590) layers the four config-write ops onto the same module:

* ``bind9.config.apply_views`` -- replace the entire views subtree (a
  set of per-view fragment files plus the zonefiles they reference)
  with the caller's proposed tree, atomically.
* ``bind9.config.apply_file`` -- replace one config fragment (e.g.
  ``named.conf.local``, ``named.conf.options``) with the caller's
  proposed content, atomically.
* ``bind9.config.backup`` -- archive ``/etc/bind/`` to a timestamped
  ``.tar.gz`` under ``/var/backups/meho-bind9/`` and return the
  backup ID + a listing of existing backups (the JSONFlux reducer
  handles the > 20-entry threshold, not this handler -- the K8s
  ``ops_core.py`` decision per Issue #322 is the precedent T2's
  ``ops_zone`` adopted and this op extends).
* ``bind9.config.reload`` -- ``rndc reload``; reports the structured
  success/failure envelope with the named status before and after.

Atomic-apply reuse (T3 #589 ``_atomic.py``)
-------------------------------------------

``apply_views`` and ``apply_file`` route every wire-modifying step
through :func:`~meho_backplane.connectors.bind9._atomic.atomic_apply`
unchanged. The handlers supply:

* The **staged payload** -- either a single file (``apply_file``,
  via ``staged_bytes``) or a tar.gz archive (``apply_views``, via
  ``staged_tar_bytes``).
* The **validate command** -- ``named-checkconf -p > /dev/null`` for
  both ops (the whole-config parse is the correct gate when the
  change may touch ``named.conf`` itself or a fragment file that
  ``named-checkzone`` cannot evaluate in isolation).
* The **verify predicate** -- a post-reload sanity check tailored to
  what the change is supposed to make resolvable (a representative
  ``dig`` for ``apply_views``, ``named-checkconf > /dev/null`` for
  ``apply_file``).

No new rollback logic lives in this module. The primitive's
snapshot-tar restore handles the byte-identical rollback contract
for both shapes -- including the multi-file ``apply_views`` case
(which would otherwise leave orphan files behind a naive
``tar -xzf``; the primitive's ``find $BIND_ROOT -mindepth 1 -delete``
clear-before-extract sequence covers it).

``backup`` and ``reload`` do NOT route through atomic-apply. ``backup``
is additive (it writes a new archive under ``/var/backups/`` and
does not mutate ``/etc/bind/``), so a rollback contract is moot;
``reload`` is a single ``rndc reload`` invocation with no staging,
no validate-before-commit window, and a structured exit code as
its only failure surface.

Path-safety -- the load-bearing primitive
-----------------------------------------

The read op exposes ``cat <file>`` on a remote nameserver; without a
filter the agent could ask for ``/etc/passwd``. The same filter
(``ensure_path_under_root``) is reused by ``apply_file`` to gate the
target-path argument so an operator-typed value cannot deposit a
file outside the bind config root.

Why lexical, not realpath: a realpath check would require a second
SSH round-trip to resolve symlinks server-side, doubling the wire
cost. The threat model is operator-typed paths in agent prompts,
not a hostile operator carefully placing a symlink to ``/etc/shadow``
inside ``/etc/bind/`` -- if the latter exists, the operator already
owns the server. The lexical check rejects ``../`` ladders and
absolute paths outside the root, which is the right granularity
for the agent-typed risk.

References
----------

* Parent task (read op): G3.4-T2 (#588).
* Parent task (config-write op group): G3.4-T4 (#590).
* Parent Initiative: G3.4 (#367) (WI4 read+write ops; WI5
  atomic-apply reuse; WI7 agent-warning; WI8 audit envelope).
* Atomic-apply primitive: G3.4-T3 (#589) ``_atomic.py``.
* JSONFlux substrate for the backup listing:
  :mod:`meho_backplane.operations.reducer`. The K8s precedent is
  documented in ``connectors/kubernetes/ops_core.py``.
* ISC bind9 9.18 reference docs:
  https://bind9.readthedocs.io/en/v9.18/manpages.html#named-checkconf,
  https://bind9.readthedocs.io/en/v9.18/manpages.html#rndc.
"""

from __future__ import annotations

import io
import posixpath
import secrets
import shlex
import tarfile
import time
from typing import TYPE_CHECKING, Any

import structlog.contextvars

from meho_backplane.connectors.bind9._atomic import atomic_apply
from meho_backplane.connectors.bind9.ops import Bind9Op

if TYPE_CHECKING:
    from meho_backplane.connectors.bind9.connector import Bind9Connector

__all__ = [
    "BIND9_CONFIG_APPLY_FILE_LLM_INSTRUCTIONS",
    "BIND9_CONFIG_APPLY_FILE_PARAMETER_SCHEMA",
    "BIND9_CONFIG_APPLY_VIEWS_LLM_INSTRUCTIONS",
    "BIND9_CONFIG_APPLY_VIEWS_PARAMETER_SCHEMA",
    "BIND9_CONFIG_BACKUP_LLM_INSTRUCTIONS",
    "BIND9_CONFIG_BACKUP_PARAMETER_SCHEMA",
    "BIND9_CONFIG_RELOAD_LLM_INSTRUCTIONS",
    "BIND9_CONFIG_RELOAD_PARAMETER_SCHEMA",
    "BIND9_CONFIG_SHOW_LLM_INSTRUCTIONS",
    "BIND9_CONFIG_SHOW_PARAMETER_SCHEMA",
    "CONFIG_OPS",
    "ConfigPathRejectedError",
    "bind9_config_apply_file",
    "bind9_config_apply_views",
    "bind9_config_backup",
    "bind9_config_reload",
    "bind9_config_show",
    "ensure_path_under_root",
    "pack_views_tar",
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
# Tar-packing helper for apply_views (pure, unit-testable)
# ---------------------------------------------------------------------------


def pack_views_tar(
    files: dict[str, str],
    *,
    bind_root: str = "/etc/bind",
) -> bytes:
    """Pack *files* into a tar.gz archive whose members live under *bind_root*.

    *files* maps **relative** paths under ``bind_root`` to their UTF-8
    content strings. Every relative path is sanitised via
    :func:`ensure_path_under_root` so an operator-typed
    ``../../etc/passwd`` cannot smuggle a member outside the bind
    config root.

    The returned bytes are a gzip-compressed POSIX tar archive whose
    member names are **absolute** (rooted at ``/``); the atomic-apply
    primitive's stage step extracts with ``tar -xzf - -C /`` so an
    absolute archive overlays the live bind tree exactly. Empty
    *files* returns an empty (but valid) archive.

    Uses :mod:`tarfile` rather than shelling out to system ``tar`` so
    the archive build is exercised by the unit suite without an SSH
    target.

    Raises :class:`ConfigPathRejectedError` if any input path fails
    the lexical filter; the agent gets a structured rejection with
    the sanitised attempt and no archive is produced.
    """
    buf = io.BytesIO()
    # ``gzip`` mode compresses on the fly; the resulting bytes are
    # ready to feed into ``tar -xzf -`` on the remote side.
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for relative_path, content in files.items():
            # The lexical filter handles ``../`` traversal, absolute
            # paths outside the root, control characters, and empty
            # inputs. ``allowed_root`` matches the primitive's
            # ``bind_root`` exactly so the validation lines up with
            # where the archive will actually extract.
            absolute_path = ensure_path_under_root(relative_path, bind_root)
            # Strip the leading "/" because tarfile's member names are
            # conventionally relative to the archive root; ``tar -xzf
            # -C /`` is the matching extract flag. A leading-"/"
            # member triggers tar's "removing leading '/' from member
            # names" warning otherwise, which under ``set -e -o
            # pipefail`` does NOT fail the pipeline (tar exits 0) but
            # pollutes the stderr stream the FAILED_DETAIL capture
            # surfaces.
            archive_member = absolute_path.lstrip("/")
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name=archive_member)
            info.size = len(payload)
            # Pin the mode bits at 0o644 so a future operator-typed
            # archive cannot smuggle a setuid bit through this op.
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shared helpers for write handlers
# ---------------------------------------------------------------------------


async def _resolve_bind_root(connector: Bind9Connector, target: Any) -> str:
    """Return the absolute bind config root for *target*.

    Mirrors :func:`bind9_config_show`'s fingerprint-driven lookup.
    Pulled into a helper so every write handler resolves the root
    the same way: read the fingerprint, take the directory part of
    ``extras["named_conf_path"]``, fall back to ``/etc/bind``.
    """
    fingerprint = await connector.fingerprint(target)
    named_conf_path = fingerprint.extras.get("named_conf_path") or "/etc/bind/named.conf"
    if not isinstance(named_conf_path, str):
        raise ConfigPathRejectedError(
            f"fingerprint extras.named_conf_path is not a string: {named_conf_path!r}"
        )
    return posixpath.dirname(named_conf_path) or "/etc/bind"


def _sudo_password_for_target(target: Any) -> str:
    """Read the sudo password from the target's ``secret_ref``.

    Re-uses the contract from
    :mod:`meho_backplane.connectors.bind9.ops_record` -- the secret
    keys (``sudo_password`` then ``password``) and the
    :class:`ValueError` on missing-credential surface are identical.
    Hoisted here as well so the config-write handlers don't take a
    cross-module import dependency on a private helper.
    """
    secret_ref = getattr(target, "secret_ref", None) or {}
    if not isinstance(secret_ref, dict):
        raise ValueError(f"target.secret_ref must be a dict; got {type(secret_ref).__name__}")
    password = secret_ref.get("sudo_password") or secret_ref.get("password")
    if not password:
        raise ValueError(
            "target.secret_ref carries no sudo_password / password; "
            "bind9 config-write ops require a sudo credential"
        )
    return str(password)


# Global-atomic-apply warning every write op surfaces in its description
# and ``llm_instructions``. The exact tokens "global" and "atomic" are
# the load-bearing signals an agent reads before contemplating a DNS-
# layer change; the registration-shape tests assert their presence.
_WRITE_WARNING = (
    "WARNING: this change is global and atomic. The atomic-apply "
    "primitive snapshots ``/etc/bind/`` to a per-call tar, stages the "
    "proposed content, runs ``named-checkconf``, ``rndc reload``, and a "
    "post-reload verify predicate; on any failure the pre-op tree is "
    "restored byte-identical. On success the change is live for every "
    "consumer of this nameserver -- DNS has no per-caller scoping. "
    "``safety_level`` is ``dangerous`` (a bad views file can dark the "
    "whole resolver); the production-path gate is G7/G10 policy "
    "territory keyed on this value."
)


# ---------------------------------------------------------------------------
# bind9.config.apply_file
# ---------------------------------------------------------------------------


async def bind9_config_apply_file(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.config.apply_file`` -- atomic single-fragment write.

    Sequence:

    1. Resolve the bind root from the target's fingerprint.
    2. Path-filter the requested target path -- traversal / outside-root
       inputs are rejected pre-stage with no remote IO past the
       fingerprint call.
    3. :func:`atomic_apply` stages the new content (single-file mode),
       runs ``named-checkconf -p > /dev/null``, ``rndc reload``, and the
       verify predicate; rolls back on any failure.

    The verify predicate is ``named-checkconf > /dev/null`` -- after the
    live reload the config tree must still parse cleanly. This is the
    "live-loaded-and-still-coherent" sanity check; rolling back on a
    failed verify guards against a fragment that parses in isolation
    but breaks the global config (e.g. a duplicate-zone declaration
    only visible after include-stitching).

    Returns ``{file, op_class, result_state_before, result_state_after}``.
    """
    requested: str = params["path"]
    content: str = params["content"]

    bind_root = await _resolve_bind_root(connector, target)
    resolved_path = ensure_path_under_root(requested, bind_root)
    sudo_password = _sudo_password_for_target(target)

    # Verify predicate: the live-loaded config must still parse. A
    # bad fragment that passes the staged-tree parse but breaks the
    # live tree (e.g. a duplicate-zone declaration only visible
    # after include-stitching with sibling fragments) trips this.
    verify_cmd = "named-checkconf > /dev/null"

    apply_result = await atomic_apply(
        connector,
        target,
        raw_jwt="",
        sudo_password=sudo_password,
        audit_slice_path=resolved_path,
        # zone_name="" -- this is a config write, not a zonefile write.
        # The SOA-bump rollback path is guarded against an empty zone
        # name and stays inert.
        zone_name="",
        staged_bytes=content.encode("utf-8"),
        validate_command="named-checkconf -p > /dev/null",
        verify_command=verify_cmd,
        bind_root=bind_root,
    )

    # Chassis audit enrichment — bind ``audit_state_before`` /
    # ``audit_state_after`` so the dispatcher's audit-row payload
    # carries the config-slice snapshots (mirrors the FastAPI
    # middleware's ``audit_*`` contextvar pattern; the merger lives
    # in operations/_audit.py:_resolve_audit_extras_from_contextvars).
    structlog.contextvars.bind_contextvars(
        audit_state_before=apply_result.state_before,
        audit_state_after=apply_result.state_after,
    )
    return {
        "file": resolved_path,
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
    }


BIND9_CONFIG_APPLY_FILE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Target path of the fragment to replace. May be absolute "
                "(must be lexically under the bind config root from the "
                "fingerprint) or relative (resolved against the same "
                "root). Traversal / outside-root inputs are rejected "
                "pre-stage with ``ConfigPathRejectedError``. Examples: "
                "``named.conf.local``, ``named.conf.options``, "
                "``views/external.conf``."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Proposed file content as UTF-8 text. Replaces the "
                "target file's bytes verbatim. The atomic-apply "
                "primitive's snapshot covers the pre-op shape so a "
                "bad payload rolls back to byte-identical."
            ),
        },
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


_BIND9_CONFIG_APPLY_FILE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "op_class": {"type": "string", "enum": ["write"]},
        "result_state_before": {"type": "string"},
        "result_state_after": {"type": "string"},
    },
    "required": ["file", "op_class", "result_state_before", "result_state_after"],
    "additionalProperties": False,
}


BIND9_CONFIG_APPLY_FILE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Replace one bind9 config fragment (``named.conf.local``, "
        "``named.conf.options``, ``views/external.conf``, etc.) "
        "atomically. " + _WRITE_WARNING + " Use ``bind9.config.show`` "
        "first to capture the current content (an agent doing an "
        "iterative edit should compute the new content from the old, "
        "not blind-write)."
    ),
    "parameter_hints": {
        "path": (
            "Required. Target fragment path. Absolute (under the bind "
            "config root) or relative (resolved against it). Traversal "
            "inputs are rejected pre-stage."
        ),
        "content": (
            "Required. Proposed file content as UTF-8 text. Replaces "
            "the target file's bytes verbatim."
        ),
    },
    "output_shape": (
        "{'file': <resolved abs path>, 'op_class': 'write', "
        "'result_state_before': <prior file text>, "
        "'result_state_after': <post-write file text>}."
    ),
}


# ---------------------------------------------------------------------------
# bind9.config.apply_views
# ---------------------------------------------------------------------------


async def bind9_config_apply_views(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.config.apply_views`` -- atomic multi-file tree write.

    Replaces the views subtree (plus any zonefile fragments those
    views reference) atomically. The caller supplies a mapping of
    **relative** paths (under the bind config root) to their content
    strings; the handler packs them into a tar.gz archive client-side
    and ships it through the atomic-apply primitive's multi-file
    staging shape.

    Sequence:

    1. Resolve the bind root from the target's fingerprint.
    2. Pack *files* into a tar archive via :func:`pack_views_tar`.
       Each relative path passes through :func:`ensure_path_under_root`
       so an operator-typed traversal is rejected pre-stage.
    3. :func:`atomic_apply` extracts the archive over the live bind
       tree, runs ``named-checkconf -p > /dev/null``, ``rndc reload``,
       and the verify predicate; rolls back on any failure.

    The verify predicate is caller-supplied as ``verify_fqdn`` (the
    representative FQDN that must resolve through one of the new views).
    Omitting it falls back to ``named-checkconf > /dev/null`` (the
    same predicate ``apply_file`` uses); a deployed views change that
    parses but doesn't actually resolve is then a config-correctness
    bug for the caller to catch on their side, but the rollback shape
    stays sound either way.

    ``audit_slice_path`` is the **primary** path the caller flagged
    via ``primary_path`` (defaults to the first key in *files* sorted
    lexicographically) -- the audit row gets the pre/post-op content
    of that one file, the rest are reconstructable from the staged
    tar if needed for replay. ``primary_path`` **must** resolve to one
    of the staged files; an out-of-set value is rejected before any
    remote write so a successful reload cannot be followed by a
    cat-the-missing-slice failure that would force the caller to
    retry an already-applied change.

    Returns ``{primary_path, files, op_class, result_state_before,
    result_state_after}``; ``files`` is the sorted list of resolved
    absolute paths the archive deposited.
    """
    files: dict[str, str] = params["files"]
    verify_fqdn: str | None = params.get("verify_fqdn")
    primary_param: str | None = params.get("primary_path")

    if not files:
        raise ValueError("bind9.config.apply_views requires a non-empty 'files' mapping")

    bind_root = await _resolve_bind_root(connector, target)
    # Pack first so a traversal input fails the op pre-stage with the
    # structured ConfigPathRejectedError -- no remote IO past the
    # fingerprint call.
    tar_bytes = pack_views_tar(files, bind_root=bind_root)

    # Compute the resolved absolute paths for the audit row -- the
    # archive's member names are derived from the same filter.
    resolved_paths: list[str] = sorted(ensure_path_under_root(p, bind_root) for p in files)
    if primary_param is not None:
        primary_path = ensure_path_under_root(primary_param, bind_root)
        # Guard against the double-apply trap: the atomic-apply primitive
        # captures ``state_after`` by ``cat``-ing the audit-slice path on
        # the success path (step 7). If the operator points
        # ``primary_path`` at a file NOT in the staged ``files`` mapping,
        # the post-reload ``cat`` either hits a file the staged tar
        # didn't touch (informational mismatch -- the audit row reports
        # an unrelated file) or hits a missing path (cat fails, the
        # primitive raises after the live reload already succeeded, the
        # caller retries, and the change is applied twice). Either case
        # is broken; the only safe contract is "primary_path must
        # reference one of the staged files". Rejecting at the handler
        # boundary is cheaper than defending the primitive's success
        # path and keeps the audit-replay invariant intact (the slice
        # bytes are always one of the bytes the op shipped).
        if primary_path not in set(resolved_paths):
            raise ValueError(
                f"bind9.config.apply_views: primary_path {primary_param!r} must reference "
                f"one of the staged files; got resolved={primary_path!r}, "
                f"staged={resolved_paths!r}"
            )
    else:
        primary_path = resolved_paths[0]

    sudo_password = _sudo_password_for_target(target)

    # Verify predicate: prefer a representative dig if the caller named
    # one; otherwise fall back to the "config still parses live" check.
    # A `+short` dig that resolves anything (the actual rdata is the
    # caller's call to assert post-hoc) is a stronger signal than the
    # parse check alone -- it proves the views are wired through to a
    # resolver answer for the new tree, not just syntactically intact.
    if verify_fqdn is not None:
        quoted_fqdn = shlex.quote(verify_fqdn)
        verify_cmd = f'[ -n "$(dig @localhost {quoted_fqdn} +short 2>/dev/null)" ]'
    else:
        verify_cmd = "named-checkconf > /dev/null"

    apply_result = await atomic_apply(
        connector,
        target,
        raw_jwt="",
        sudo_password=sudo_password,
        audit_slice_path=primary_path,
        zone_name="",  # multi-file config-write; no SOA-bump rollback.
        staged_tar_bytes=tar_bytes,
        validate_command="named-checkconf -p > /dev/null",
        verify_command=verify_cmd,
        bind_root=bind_root,
    )

    # Chassis audit enrichment — see bind9_config_apply_file for rationale.
    structlog.contextvars.bind_contextvars(
        audit_state_before=apply_result.state_before,
        audit_state_after=apply_result.state_after,
    )
    return {
        "primary_path": primary_path,
        "files": resolved_paths,
        "op_class": "write",
        "result_state_before": apply_result.state_before,
        "result_state_after": apply_result.state_after,
    }


BIND9_CONFIG_APPLY_VIEWS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "object",
            "description": (
                "Mapping of **relative** paths (under the bind config "
                "root) to their UTF-8 content. Every key is passed "
                "through the path-safety filter; traversal / "
                "outside-root inputs are rejected pre-stage. The "
                "archive overlays the live tree atomically; files "
                "absent from the mapping but present on the target are "
                "preserved (the primitive extracts over the existing "
                "tree, it does not clear it first)."
            ),
            "additionalProperties": {"type": "string"},
            "minProperties": 1,
        },
        "primary_path": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. The fragment whose pre/post-op content the "
                "audit row should capture. Defaults to the first key "
                "in ``files`` sorted lexicographically. **Must** "
                "reference one of the keys in ``files`` -- a value "
                "outside the staged set is rejected before any remote "
                "write to keep the audit-replay invariant intact."
            ),
        },
        "verify_fqdn": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Optional. A representative FQDN that must resolve "
                "through one of the new views post-reload. The verify "
                "predicate is ``dig @localhost <fqdn> +short`` returning "
                "non-empty. Omit to fall back to ``named-checkconf > "
                "/dev/null`` (the live-loaded config still parses)."
            ),
        },
    },
    "required": ["files"],
    "additionalProperties": False,
}


_BIND9_CONFIG_APPLY_VIEWS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "primary_path": {"type": "string"},
        "files": {
            "type": "array",
            "items": {"type": "string"},
        },
        "op_class": {"type": "string", "enum": ["write"]},
        "result_state_before": {"type": "string"},
        "result_state_after": {"type": "string"},
    },
    "required": [
        "primary_path",
        "files",
        "op_class",
        "result_state_before",
        "result_state_after",
    ],
    "additionalProperties": False,
}


BIND9_CONFIG_APPLY_VIEWS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Replace a multi-file views subtree (per-view fragment files "
        "plus the zonefiles they reference) atomically. "
        + _WRITE_WARNING
        + " The archive overlays the live tree -- absent files are "
        "preserved, present files are replaced byte-for-byte. Pair with "
        "a ``verify_fqdn`` for the strongest verify (a dig that resolves "
        "through one of the new views proves the deployment landed; "
        "without it the verify falls back to the static parse check)."
    ),
    "parameter_hints": {
        "files": (
            "Required. Mapping of relative paths (under the bind config "
            "root) to UTF-8 content strings. Traversal inputs are "
            "rejected pre-stage."
        ),
        "primary_path": (
            "Optional. Which file the audit row should capture pre/post. "
            "Defaults to the first sorted key in ``files``."
        ),
        "verify_fqdn": (
            "Optional. A FQDN that must resolve through one of the new "
            "views post-reload. Strengthens the verify gate."
        ),
    },
    "output_shape": (
        "{'primary_path', 'files': [<resolved abs paths>], "
        "'op_class': 'write', 'result_state_before', "
        "'result_state_after'}. ``result_state_*`` captures the "
        "primary file's bytes; the rest of the tree is reconstructable "
        "from the staged tar if needed for replay."
    ),
}


# ---------------------------------------------------------------------------
# bind9.config.backup
# ---------------------------------------------------------------------------


# Directory under which timestamped backups live. ``/var/backups`` is
# the Debian-family convention; the connector creates the meho-scoped
# subdir on first use. ``rndc`` does not write here; only the connector
# does, so a tenant-side backup never conflicts with bind9's own log
# rotation.
_BACKUP_DIR: str = "/var/backups/meho-bind9"

# Tag-name filter -- the operator may set a friendly tag on a backup;
# the filename embeds it. Restrict the character class so a tag cannot
# inject shell metacharacters into the filename (the path lands inside
# ``shlex.quote`` regardless, but the typed-op JSON schema enforces
# the shape at the API boundary too).
_BACKUP_TAG_PATTERN: str = r"^[A-Za-z0-9._-]{1,64}$"


async def bind9_config_backup(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.config.backup``.

    ``tar -czf /var/backups/meho-bind9/<timestamp>[-<tag>].tar.gz
    /etc/bind/`` and return a structured envelope with the backup ID
    plus a listing of existing backups (so the operator can see what's
    on disk in one round-trip).

    Returns ``{backup_id, path, rows, total, op_class, state_after}``;
    ``rows`` is the listing (one row per existing ``.tar.gz`` file
    with ``{id, path, size, modified}``); ``total`` is the un-truncated
    count. A future JSONFlux reducer reads ``rows`` + ``total`` and
    swaps the row list for a result handle when ``total > 20`` -- the
    K8s precedent (``ops_core.py``) is what this op extends.

    ``op_class="write"`` because the op creates an artifact, but the
    artifact lives outside ``/etc/bind/`` so the audit row carries
    ``state_after`` (the backup ID) and no ``state_before`` -- nothing
    in the bind tree mutated.

    ``safety_level=caution`` (additive write; recoverable trivially via
    ``rm`` if the backup is unwanted).
    """
    tag: str | None = params.get("tag")
    sudo_password = _sudo_password_for_target(target)
    bind_root = await _resolve_bind_root(connector, target)

    # Compose the filename. ``time.strftime`` returns a localtime-zoned
    # timestamp -- but we want UTC so backups across timezones sort
    # deterministically; ``time.gmtime()`` is the right basis. A short
    # CSPRNG suffix breaks ties when two backups land in the same
    # second with the same tag (concurrent orchestrator calls, a
    # tight retry loop, two operators racing): ``secrets.token_hex(3)``
    # gives 6 hex chars / 24 bits of entropy -- birthday-collision
    # probability under any realistic same-second burst is negligible,
    # and the suffix is short enough that the backup ID stays readable.
    # The ID schema is opaque to callers (the only test assertion is
    # ``startswith("bind9-")``), so appending the suffix is a backward-
    # compatible refinement.
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = secrets.token_hex(3)
    filename = (
        f"bind9-{timestamp}-{tag}-{suffix}.tar.gz"
        if tag is not None
        else f"bind9-{timestamp}-{suffix}.tar.gz"
    )
    # Backup ID is the bare filename without the extension -- shorter
    # to type, unambiguous against future backups (timestamp is
    # second-granular; the random suffix breaks same-second ties).
    backup_id = filename.removesuffix(".tar.gz")
    backup_path = f"{_BACKUP_DIR}/{filename}"

    # Build the create + list script. ``set -e -o pipefail`` so any
    # failure surfaces a non-zero exit; the dispatcher wraps it into
    # a connector_error envelope. The listing is sorted by mtime
    # newest-first so the most recent backup is the head row.
    script = (
        "set -e -o pipefail\n"
        f"BACKUP_DIR={shlex.quote(_BACKUP_DIR)}\n"
        f"BACKUP_PATH={shlex.quote(backup_path)}\n"
        f"BIND_ROOT={shlex.quote(bind_root)}\n"
        # ``mkdir -p`` is idempotent; ``chmod 700`` keeps the backup
        # tree readable only by root since the tar contains the live
        # named config (which may carry view-secret keys / DNSSEC
        # material in future iterations).
        'mkdir -p "$BACKUP_DIR"\n'
        'chmod 700 "$BACKUP_DIR"\n'
        # ``-C /`` so absolute paths in the archive round-trip; the
        # rollback shape this matches is the atomic-apply primitive's
        # snapshot grammar, deliberately consistent.
        'ROOT_NO_SLASH="${BIND_ROOT#/}"\n'
        'tar -czf "$BACKUP_PATH" -C / "$ROOT_NO_SLASH"\n'
        # Emit the listing as JSONL so the parser is unambiguous --
        # ``ls -l`` output varies between coreutils versions and locales.
        # Python is the only tool we are guaranteed to have under
        # ``python3-minimal`` (the testcontainer fixture from T3
        # already pulls it in); stat-via-python keeps the parser pinned.
        "python3 - \"$BACKUP_DIR\" <<'PYEOF'\n"
        "import json, os, sys\n"
        "backup_dir = sys.argv[1]\n"
        "rows = []\n"
        "for name in os.listdir(backup_dir):\n"
        "    if not name.endswith('.tar.gz'):\n"
        "        continue\n"
        "    path = os.path.join(backup_dir, name)\n"
        "    try:\n"
        "        st = os.stat(path)\n"
        "    except OSError:\n"
        "        continue\n"
        "    rows.append({\n"
        "        'id': name.removesuffix('.tar.gz'),\n"
        "        'path': path,\n"
        "        'size': st.st_size,\n"
        "        'modified': st.st_mtime,\n"
        "    })\n"
        # Sort newest-first; the operator's mental model is "the most\n"
        # recent backup is the head of the list".\n"
        "rows.sort(key=lambda r: -r['modified'])\n"
        "print(json.dumps(rows))\n"
        "PYEOF\n"
    )

    proc = await connector._remote_bash_with_sudo(
        target,
        script,
        raw_jwt="",
        sudo_password=sudo_password,
        timeout=120.0,
    )
    exit_status = getattr(proc, "exit_status", 0)
    if exit_status != 0:
        stderr_raw = getattr(proc, "stderr", "")
        stderr_text = stderr_raw if isinstance(stderr_raw, str) else ""
        raise RuntimeError(
            f"bind9.config.backup failed (exit={exit_status}): "
            f"{stderr_text.strip() or '<no stderr>'}"
        )
    stdout_raw = getattr(proc, "stdout", "")
    stdout_text = stdout_raw if isinstance(stdout_raw, str) else ""

    # The Python listing prints exactly one JSON line; defensively
    # take the last non-empty line in case bash echoed extra
    # progress data ahead of it.
    import json

    rows: list[dict[str, Any]] = []
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError:
            continue
        else:
            break

    # Chassis audit enrichment — only ``state_after`` (no
    # state_before: nothing in /etc/bind/ mutated). The backup_id is
    # the audit row's view of "the artifact this write produced",
    # which the G8.2 audit-replay consumer cross-references with the
    # archive store. See bind9_config_apply_file for the binding
    # rationale.
    structlog.contextvars.bind_contextvars(audit_state_after=backup_id)
    return {
        "backup_id": backup_id,
        "path": backup_path,
        "rows": rows,
        "total": len(rows),
        "op_class": "write",
        "state_after": backup_id,
    }


BIND9_CONFIG_BACKUP_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tag": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": _BACKUP_TAG_PATTERN,
            "description": (
                "Optional friendly tag embedded in the backup filename "
                "after the UTC timestamp. Restricted to "
                "``[A-Za-z0-9._-]{1,64}`` so a tag value cannot inject "
                "shell metacharacters or path separators."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}


_BIND9_CONFIG_BACKUP_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "backup_id": {"type": "string"},
        "path": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "path": {"type": "string"},
                    "size": {"type": "integer"},
                    "modified": {"type": "number"},
                },
                "required": ["id", "path", "size", "modified"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "integer"},
        "op_class": {"type": "string", "enum": ["write"]},
        "state_after": {"type": "string"},
    },
    "required": ["backup_id", "path", "rows", "total", "op_class", "state_after"],
    "additionalProperties": False,
}


BIND9_CONFIG_BACKUP_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Archive ``/etc/bind/`` to a timestamped ``.tar.gz`` under "
        "``/var/backups/meho-bind9/`` before a risky change; the "
        "operator can manually ``tar -xzf`` the result if a future "
        "rollback is needed beyond the atomic-apply primitive's "
        "per-call snapshot. Additive (non-destructive); "
        "``safety_level=caution``."
    ),
    "parameter_hints": {
        "tag": ("Optional friendly tag. Limit: ``[A-Za-z0-9._-]{1,64}``."),
    },
    "output_shape": (
        "{'backup_id': <timestamp[-tag]>, 'path': <abs tar.gz path>, "
        "'rows': [{id, path, size, modified}, ...] newest-first, "
        "'total': <int>, 'op_class': 'write', 'state_after': "
        "<backup_id>}. The future JSONFlux reducer swaps ``rows`` for "
        "a result handle when ``total > 20``."
    ),
}


# ---------------------------------------------------------------------------
# bind9.config.reload
# ---------------------------------------------------------------------------


async def bind9_config_reload(
    connector: Bind9Connector,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``bind9.config.reload``.

    ``rndc reload`` -- tells the running named to re-read its config
    files and reload changed zones. Returns a structured envelope
    distinguishing success from failure: a non-zero ``rndc reload`` exit
    is **not** an exception, it's a structured ``ok: false`` row carrying
    the stderr verbatim (the operator can diagnose without parsing the
    exception class).

    Captures ``rndc status`` before and after as ``state_before`` /
    ``state_after`` so the audit row records the named instance's
    health snapshot around the reload (uptime, query count, server is
    up).

    Does **not** route through atomic-apply -- there is no staging, no
    validate-before-commit window, and no rollback contract. ``reload``
    is a single ``rndc`` invocation; the failure mode is "rndc itself
    refused" (control channel down, named mid-shutdown), not a
    rollback-able partial state.

    ``safety_level=caution`` (the live in-memory config is reloaded; if
    a sibling write op already staged a bad fragment, the reload
    surfaces it as a named-startup failure -- but the prior staged
    state was already on disk by then, so this op is the messenger,
    not the cause).
    """
    del params  # declared empty in schema; intentionally ignored
    sudo_password = _sudo_password_for_target(target)

    # ``rndc reload`` is one positional command; the wrapper captures
    # stdout + stderr + exit so the result envelope can carry whichever
    # surface the operator needs. ``set +e`` because we want the
    # non-zero rndc exit to flow through to the structured shape, not
    # bail out under ``set -e``.
    script = (
        "set +e\n"
        "STATE_BEFORE=$(rndc status 2>&1)\n"
        "RNDC_STATUS_BEFORE=$?\n"
        "RELOAD_OUT=$(rndc reload 2>&1)\n"
        "RELOAD_RC=$?\n"
        "STATE_AFTER=$(rndc status 2>&1)\n"
        "RNDC_STATUS_AFTER=$?\n"
        "echo '===STATE_BEFORE_BEGIN==='\n"
        'echo "$STATE_BEFORE"\n'
        "echo '===STATE_BEFORE_END==='\n"
        "echo '===RELOAD_OUT_BEGIN==='\n"
        'echo "$RELOAD_OUT"\n'
        "echo '===RELOAD_OUT_END==='\n"
        "echo '===STATE_AFTER_BEGIN==='\n"
        'echo "$STATE_AFTER"\n'
        "echo '===STATE_AFTER_END==='\n"
        'echo "===RELOAD_RC===$RELOAD_RC"\n'
        # rndc's status exit codes are captured so the audit row can
        # show whether the status probe itself failed (named down)
        # vs the reload itself failed (named up but rndc refused).
        'echo "===STATUS_BEFORE_RC===$RNDC_STATUS_BEFORE"\n'
        'echo "===STATUS_AFTER_RC===$RNDC_STATUS_AFTER"\n'
    )

    proc = await connector._remote_bash_with_sudo(
        target,
        script,
        raw_jwt="",
        sudo_password=sudo_password,
        timeout=60.0,
    )
    # ``_remote_bash_with_sudo`` exits 0 if the script ran (we suppressed
    # rndc's own exit via ``set +e``); a non-zero here means the wrapper
    # itself blew up (sudo refused, ssh dropped, etc.) -- that's a
    # connector-error, raise it.
    exit_status = getattr(proc, "exit_status", 0)
    if exit_status != 0:
        stderr_raw = getattr(proc, "stderr", "")
        stderr_text = stderr_raw if isinstance(stderr_raw, str) else ""
        raise RuntimeError(
            f"bind9.config.reload wrapper failed (exit={exit_status}): "
            f"{stderr_text.strip() or '<no stderr>'}"
        )
    stdout_raw = getattr(proc, "stdout", "")
    stdout_text = stdout_raw if isinstance(stdout_raw, str) else ""

    sections = _parse_reload_output(stdout_text)
    reload_rc = int(sections.get("RELOAD_RC", "1"))
    # Chassis audit enrichment — rndc-status snapshots before and
    # after the reload land on the audit row's payload. See
    # bind9_config_apply_file for the binding rationale.
    structlog.contextvars.bind_contextvars(
        audit_state_before=sections.get("STATE_BEFORE", ""),
        audit_state_after=sections.get("STATE_AFTER", ""),
    )
    return {
        "ok": reload_rc == 0,
        "rndc_reload_exit": reload_rc,
        "stderr": sections.get("RELOAD_OUT", "") if reload_rc != 0 else "",
        "stdout": sections.get("RELOAD_OUT", "") if reload_rc == 0 else "",
        "result_state_before": sections.get("STATE_BEFORE", ""),
        "result_state_after": sections.get("STATE_AFTER", ""),
        "op_class": "write",
    }


def _parse_reload_output(text: str) -> dict[str, str]:
    """Parse the sentinel-delimited output of the reload script.

    Same parser shape as :func:`_atomic._parse_pipeline_output` but
    tailored to the reload script's section names. Pulled out so the
    unit suite can exercise it directly against fixture text.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    scalars: dict[str, str] = {}
    scalar_names = ("RELOAD_RC", "STATUS_BEFORE_RC", "STATUS_AFTER_RC")
    for raw in text.splitlines(keepends=True):
        stripped = raw.rstrip("\r\n")
        matched_scalar = False
        for name in scalar_names:
            prefix = f"==={name}==="
            if stripped.startswith(prefix):
                scalars[name] = stripped[len(prefix) :].strip()
                matched_scalar = True
                break
        if matched_scalar:
            continue
        if stripped.startswith("===") and stripped.endswith("_BEGIN==="):
            current = stripped[3 : -len("_BEGIN===")]
            sections[current] = []
            continue
        if stripped.startswith("===") and stripped.endswith("_END==="):
            current = None
            continue
        if current is not None:
            sections[current].append(raw)
    result: dict[str, str] = {name: "".join(lines) for name, lines in sections.items()}
    # Bash's ``echo`` appends one newline of its own; strip exactly
    # one terminal newline per captured section so the slice
    # round-trips byte-for-byte.
    for name in list(result):
        if result[name].endswith("\n"):
            result[name] = result[name][:-1]
    result.update(scalars)
    return result


BIND9_CONFIG_RELOAD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


_BIND9_CONFIG_RELOAD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "rndc_reload_exit": {"type": "integer"},
        "stderr": {"type": "string"},
        "stdout": {"type": "string"},
        "result_state_before": {"type": "string"},
        "result_state_after": {"type": "string"},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": [
        "ok",
        "rndc_reload_exit",
        "stderr",
        "stdout",
        "result_state_before",
        "result_state_after",
        "op_class",
    ],
    "additionalProperties": False,
}


BIND9_CONFIG_RELOAD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Tell the running named to re-read its config and reload "
        "changed zones. Use after a manual edit on the target (not "
        "needed after ``config.apply_*`` ops -- those reload as part "
        "of the atomic-apply pipeline). Returns a structured envelope "
        "distinguishing success from failure; a non-zero ``rndc reload`` "
        "exit is reported as ``ok: false`` with the stderr verbatim, "
        "not raised as an exception. ``safety_level=caution`` (the "
        "live config is reloaded; a prior staged-bad fragment surfaces "
        "as a named-startup failure here)."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'ok': <bool>, 'rndc_reload_exit': <int>, 'stderr': <str>, "
        "'stdout': <str>, 'result_state_before': <rndc status pre-reload>, "
        "'result_state_after': <rndc status post-reload>, "
        "'op_class': 'write'}."
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
    Bind9Op(
        op_id="bind9.config.apply_file",
        handler_attr="bind9_config_apply_file",
        summary="Atomically replace one bind9 config fragment with rollback on failure.",
        description=(
            "Atomic single-fragment write via the T3 atomic-apply "
            "primitive. Replaces the target fragment with the proposed "
            "content; validates via ``named-checkconf -p``, reloads "
            "via ``rndc reload``, verifies the live config still parses "
            "post-reload. " + _WRITE_WARNING
        ),
        parameter_schema=BIND9_CONFIG_APPLY_FILE_PARAMETER_SCHEMA,
        response_schema=_BIND9_CONFIG_APPLY_FILE_RESPONSE_SCHEMA,
        group_key="config",
        tags=("write", "config", "atomic-apply"),
        safety_level="dangerous",
        requires_approval=False,
        llm_instructions=BIND9_CONFIG_APPLY_FILE_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.config.apply_views",
        handler_attr="bind9_config_apply_views",
        summary="Atomically replace a bind9 views subtree with rollback on failure.",
        description=(
            "Atomic multi-file tree write via the T3 atomic-apply "
            "primitive (multi-file tar mode). Packs the caller's "
            "``files`` mapping into a tar.gz archive, extracts it over "
            "the live bind tree, validates via ``named-checkconf -p``, "
            "reloads via ``rndc reload``, optionally verifies via a "
            "representative ``dig`` against ``verify_fqdn``. " + _WRITE_WARNING
        ),
        parameter_schema=BIND9_CONFIG_APPLY_VIEWS_PARAMETER_SCHEMA,
        response_schema=_BIND9_CONFIG_APPLY_VIEWS_RESPONSE_SCHEMA,
        group_key="config",
        tags=("write", "config", "atomic-apply"),
        safety_level="dangerous",
        requires_approval=False,
        llm_instructions=BIND9_CONFIG_APPLY_VIEWS_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.config.backup",
        handler_attr="bind9_config_backup",
        summary="Archive /etc/bind/ to a timestamped .tar.gz and list existing backups.",
        description=(
            "Creates a ``tar.gz`` archive of ``/etc/bind/`` under "
            "``/var/backups/meho-bind9/`` and returns the backup ID + "
            "a listing of existing backups (newest-first). Additive "
            "(non-destructive); does NOT route through atomic-apply "
            "because nothing in ``/etc/bind/`` mutates. The future "
            "JSONFlux reducer swaps the ``rows`` list for a result "
            "handle when ``total > 20``."
        ),
        parameter_schema=BIND9_CONFIG_BACKUP_PARAMETER_SCHEMA,
        response_schema=_BIND9_CONFIG_BACKUP_RESPONSE_SCHEMA,
        group_key="config",
        tags=("write", "config", "backup"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=BIND9_CONFIG_BACKUP_LLM_INSTRUCTIONS,
    ),
    Bind9Op(
        op_id="bind9.config.reload",
        handler_attr="bind9_config_reload",
        summary="rndc reload -- tell the running named to re-read its config.",
        description=(
            "Wraps ``rndc reload`` with a structured success/failure "
            "envelope. Captures ``rndc status`` before and after the "
            "reload for the audit row. Does NOT route through "
            "atomic-apply -- no staging, no validate-before-commit "
            "window, no rollback contract. A non-zero ``rndc reload`` "
            "exit is reported as ``ok: false`` with the stderr "
            "verbatim, not raised as an exception."
        ),
        parameter_schema=BIND9_CONFIG_RELOAD_PARAMETER_SCHEMA,
        response_schema=_BIND9_CONFIG_RELOAD_RESPONSE_SCHEMA,
        group_key="config",
        tags=("write", "config", "reload"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions=BIND9_CONFIG_RELOAD_LLM_INSTRUCTIONS,
    ),
)
