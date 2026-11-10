# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Journal, filesystem usage and directory listing reads for ``linux-ssh``.

Three ``safe`` read verbs that let an operator diagnose a storage or NFS
incident over the governed channel, without opening an unaudited console:

* ``linux.journal.tail`` (group ``log``) -- ``journalctl -o json`` with an
  optional unit, time window and priority. Returns ``{rows, total}``;
  each row is ``{ts, unit, priority, message}``. Kernel, nfsd and block
  layer messages live only in the journal on a systemd host.
* ``linux.fs.usage`` (group ``storage``) -- ``df`` for the one filesystem
  that holds a path. Returns a flat dict with the size, used and free
  bytes and the inode counts.
* ``linux.dir.list`` (group ``file``) -- the names, types, sizes, times,
  modes and owners in a directory (depth 1 or 2). Returns ``{rows, total,
  path, exists, truncated, incomplete}``.

Every operator value is checked before a command is built and is
``shlex.quote``d into a fixed command. The paths of ``fs.usage`` and
``dir.list`` must lie under the directory allow-list
(:func:`dir_list_roots`): the read roots of ``file.read`` plus common
export and data roots, and any root added with the
``LINUX_DIR_LIST_ROOTS`` environment variable of the backplane (absolute
paths, separated by ``:``). ``dir.list`` also checks the real path of the
directory it opened, so a symlink cannot lead out of the allow-list.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import (
    DEFAULT_TAIL_LINES,
    LINUX_READ_ROOTS,
    MAX_TAIL_LINES,
    PathConfinementError,
    _is_denied,
    ensure_path_under_root,
    normalise_json_rows,
)
from meho_backplane.connectors.linux.ops_file import (
    CONTENT_MARKER,
    MISSING_MARKER,
    UNREADABLE_MARKER,
    _decode_reported_path,
)
from meho_backplane.connectors.linux.ops_host import validate_unit_name

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "DIR_LIST_EXTRA_ROOTS_ENV",
    "JOURNAL_PRIORITIES",
    "JOURNAL_TIME_PATTERN",
    "MAX_DIR_DEPTH",
    "LinuxInspectError",
    "build_dir_list_command",
    "build_fs_usage_command",
    "build_journal_tail_command",
    "confine_dir_path",
    "dir_list_roots",
    "linux_dir_list",
    "linux_fs_usage",
    "linux_journal_tail",
    "parse_dir_list_output",
    "parse_fs_usage_output",
    "parse_journal_output",
    "validate_journal_priority",
    "validate_journal_time",
]

#: Environment variable that adds roots to the ``dir.list`` / ``fs.usage``
#: allow-list. Absolute paths separated by ``:``. ``/`` is refused.
DIR_LIST_EXTRA_ROOTS_ENV: str = "LINUX_DIR_LIST_ROOTS"

#: Export and data roots allowed besides :data:`LINUX_READ_ROOTS`.
_DEFAULT_DATA_ROOTS: tuple[str, ...] = (
    "/srv",
    "/mnt",
    "/media",
    "/data",
    "/export",
    "/exports",
    "/opt",
)

#: Hard cap of rows ``dir.list`` returns; a bigger directory sets
#: ``truncated=true``.
MAX_DIR_LIST_ROWS: int = 5000
MAX_DIR_DEPTH: int = 2

#: Byte cap of the journal output read from the host, and of one message.
_MAX_JOURNAL_BYTES: int = 8 * 1024 * 1024
_MAX_MESSAGE_CHARS: int = 4096

#: Exit code the journal and df commands use when the binary is absent.
_NO_BINARY_EXIT: int = 127

#: Last line of the journal output: the exit code of ``journalctl``, or
#: the marker that the byte cap cut the output.
_JOURNAL_RC_PREFIX: str = "MEHO_JOURNAL_RC="
_JOURNAL_CUT_MARKER: str = "MEHO_JOURNAL_CUT"

_JOURNAL_TIME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9:+._ -]{1,64}$")
JOURNAL_TIME_PATTERN: str = r"^[A-Za-z0-9:+._ -]{1,64}$"
JOURNAL_PRIORITIES: frozenset[str] = frozenset(
    {"0", "1", "2", "3", "4", "5", "6", "7"}
    | {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}
)

_FIND_RC_PREFIX: str = "MEHO_FIND_RC="
_FIND_RC_RE: re.Pattern[str] = re.compile(r"^MEHO_FIND_RC=(\d+)\n?$")
_FIND_FORMAT: str = r"%y\t%s\t%T@\t%m\t%u\t%P\0"
_TYPE_NAMES: dict[str, str] = {"f": "file", "d": "directory", "l": "symlink"}


class LinuxInspectError(RuntimeError):
    """A read of this module could not run or its answer is unclear.

    The dispatcher maps it to a ``connector_error`` result, so a failed
    read is never served as an empty answer.
    """


def dir_list_roots() -> tuple[str, ...]:
    """Return the allow-list roots of ``dir.list`` and ``fs.usage``.

    The read roots, the default data roots and the roots in
    :data:`DIR_LIST_EXTRA_ROOTS_ENV`. A root in the variable that is not
    an absolute path, or is ``/``, raises :class:`ValueError` (fail
    closed: a wrong setting must not widen the list).
    """
    extra: list[str] = []
    for item in os.environ.get(DIR_LIST_EXTRA_ROOTS_ENV, "").split(":"):
        item = item.strip()
        if not item:
            continue
        canonical = posixpath.normpath(item)
        if not item.startswith("/") or not canonical.strip("/"):
            raise ValueError(
                f"{DIR_LIST_EXTRA_ROOTS_ENV} entry {item!r} must be an absolute path other than '/'"
            )
        extra.append(canonical)
    return (*LINUX_READ_ROOTS, *_DEFAULT_DATA_ROOTS, *extra)


def confine_dir_path(requested: Any, *, allow_filesystem_root: bool = False) -> str:
    """Return the canonical form of *requested* when it is under the allow-list, or raise.

    Same lexical check as the file reads (control characters and ``..``
    refused, key and password paths refused). ``/`` itself is allowed
    only with *allow_filesystem_root* (``fs.usage`` of the root
    filesystem; ``dir.list`` never lists ``/``).
    """
    if not isinstance(requested, str) or not requested.startswith("/"):
        raise PathConfinementError(f"path {requested!r} must be an absolute POSIX path")
    if any(ch in requested for ch in ("\x00", "\n", "\r")):
        raise PathConfinementError("requested path contains a control character; refusing")
    canonical = posixpath.normpath(requested)
    if allow_filesystem_root and canonical == "/":
        return "/"
    confined: str | None = None
    for root in dir_list_roots():
        try:
            confined = ensure_path_under_root(requested, root)
            break
        except PathConfinementError:
            continue
    if confined is None:
        raise PathConfinementError(f"path {requested!r} is outside every allowed directory root")
    if _is_denied(confined):
        raise PathConfinementError(
            f"path {confined!r} holds keys, tokens or password hashes; it is not read"
        )
    return confined


# ---------------------------------------------------------------------------
# linux.journal.tail
# ---------------------------------------------------------------------------


def validate_journal_time(value: Any) -> str:
    """Return *value* if it is a plain journal time expression, else raise.

    Letters, digits, space and ``: + . _ -`` only (``2 hours ago``,
    ``2026-11-10 08:00:00``, ``-1h``), at most 64 characters.
    """
    if not isinstance(value, str) or not _JOURNAL_TIME_RE.fullmatch(value) or not value.strip():
        raise ValueError(f"time {value!r} is not a valid journal time expression")
    return value.strip()


def validate_journal_priority(value: Any) -> str:
    """Return *value* (``0``-``7`` or a syslog level name) lower-cased, else raise."""
    text = str(value).strip().lower()
    if text not in JOURNAL_PRIORITIES:
        raise ValueError(f"priority {value!r} is not 0-7 or a syslog level name")
    return text


def _journal_lines(raw: Any) -> int:
    if raw is None:
        return DEFAULT_TAIL_LINES
    value = int(raw)
    if value < 1:
        raise ValueError("lines must be >= 1")
    return min(value, MAX_TAIL_LINES)


def build_journal_tail_command(params: dict[str, Any], max_bytes: int = _MAX_JOURNAL_BYTES) -> str:
    """Build the fixed ``journalctl -o json`` command from validated params.

    Every value is validated first and ``shlex.quote``d. ``--since=`` and
    ``--until=`` carry their value in the same word, so a value that
    starts with ``-`` can never be read as an option. ``--all`` keeps
    fields over 4096 bytes (without it ``journalctl`` prints them as
    ``null``). The exit code of ``journalctl`` is printed after its
    output, because the pipe returns the exit code of its last command.
    The output is cut at *max_bytes*, and then at the last whole line, so
    a cut never splits a UTF-8 character; a cut ends with the cut marker.
    The exit code ``127`` says ``journalctl`` is absent.
    """
    parts = [
        "journalctl",
        "-o",
        "json",
        "--all",
        "--no-pager",
        "-n",
        str(_journal_lines(params.get("lines"))),
    ]
    if params.get("unit") is not None:
        parts += ["-u", shlex.quote(validate_unit_name(params["unit"]))]
    if params.get("since") is not None:
        parts.append("--since=" + shlex.quote(validate_journal_time(params["since"])))
    if params.get("until") is not None:
        parts.append("--until=" + shlex.quote(validate_journal_time(params["until"])))
    if params.get("priority") is not None:
        parts += ["-p", validate_journal_priority(params["priority"])]
    cap = int(max_bytes)
    # In the C locale awk counts bytes. Only the last line of a cut
    # output has no line break, so only it brings the count over the cap.
    cut = (
        f"LC_ALL=C awk -v max={cap} "
        f"'{{ n += length($0) + 1; "
        f'if (n > max) {{ print "{_JOURNAL_CUT_MARKER}"; exit }} print }}\''
    )
    return (
        f"command -v journalctl >/dev/null 2>&1 || exit {_NO_BINARY_EXIT}; "
        f"{{ {' '.join(parts)}; printf '\\n{_JOURNAL_RC_PREFIX}%s\\n' \"$?\"; }} "
        f"| head -c {cap} | {cut}"
    )


def _journal_message(value: Any) -> str | None:
    """Return the ``MESSAGE`` field as text.

    ``journalctl`` prints a field that is not valid UTF-8 as a list of
    bytes, and a field that is in the entry more than once as a list of
    its values (joined here with line breaks).
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(b, int) for b in value):
        return bytes(b for b in value if 0 <= b < 256).decode("utf-8", "replace")
    if isinstance(value, list):
        return "\n".join(part for part in (_journal_message(v) for v in value) if part is not None)
    return str(value)


def _bounded_message(message: str | None) -> tuple[str | None, bool]:
    if message is not None and len(message) > _MAX_MESSAGE_CHARS:
        return message[:_MAX_MESSAGE_CHARS], True
    return message, False


def _journal_row(line: str) -> dict[str, Any]:
    """Turn one ``journalctl -o json`` line into a ``{ts, unit, priority, message}`` row.

    ``message_truncated`` is ``True`` when the message was cut at
    :data:`_MAX_MESSAGE_CHARS` characters.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        entry = None
    if not isinstance(entry, dict):
        raw, cut = _bounded_message(line)
        return {
            "ts": None,
            "unit": None,
            "priority": None,
            "message": raw,
            "message_truncated": cut,
        }
    ts: str | None = None
    try:
        ts = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000, UTC).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        ts = None
    unit = entry.get("_SYSTEMD_UNIT") or entry.get("UNIT") or entry.get("SYSLOG_IDENTIFIER")
    try:
        priority: int | None = int(entry["PRIORITY"])
    except (KeyError, TypeError, ValueError):
        priority = None
    message, cut = _bounded_message(_journal_message(entry.get("MESSAGE")))
    return {
        "ts": ts,
        "unit": unit if isinstance(unit, str) else None,
        "priority": priority,
        "message": message,
        "message_truncated": cut,
    }


def parse_journal_output(stdout: str) -> tuple[list[dict[str, Any]], bool]:
    """Parse the journal output into ``(rows, truncated)``, or raise.

    The output ends with the exit code of ``journalctl`` or, when the
    byte cap cut it, with the cut marker (``truncated`` is ``True``). An
    exit code other than ``0``, or an output with neither line, raises
    :class:`LinuxInspectError`: a failed read is never served as rows.
    A line that is not JSON is kept as a row with the raw text in
    ``message``.
    """
    lines = [line for line in stdout.split("\n") if line.strip()]
    last = lines.pop() if lines else ""
    if last == _JOURNAL_CUT_MARKER:
        return [_journal_row(line) for line in lines], True
    if not last.startswith(_JOURNAL_RC_PREFIX):
        raise LinuxInspectError("the journal read did not finish cleanly; the journal was not read")
    if last != f"{_JOURNAL_RC_PREFIX}0":
        raise LinuxInspectError(
            f"journalctl failed (exit {last[len(_JOURNAL_RC_PREFIX) :]}); the journal was not read"
        )
    return [_journal_row(line) for line in lines], False


async def linux_journal_tail(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.journal.tail`` -- read the systemd journal."""
    cmd = build_journal_tail_command(params)
    proc = await connector._run_command(target, cmd, operator=operator)
    if getattr(proc, "exit_status", 0) == _NO_BINARY_EXIT:
        raise LinuxInspectError("host has no journalctl; cannot read the journal")
    if getattr(proc, "exit_status", 0) not in (0, None):
        raise LinuxInspectError("journalctl did not finish cleanly; the journal was not read")
    raw_out = getattr(proc, "stdout", None)
    raw_err = getattr(proc, "stderr", None)
    warning = raw_err.strip()[:500] if isinstance(raw_err, str) else ""
    try:
        rows, truncated = parse_journal_output(raw_out if isinstance(raw_out, str) else "")
    except LinuxInspectError as exc:
        if warning:
            raise LinuxInspectError(f"{exc}; journalctl said: {warning}") from None
        raise
    if warning and not rows:
        raise LinuxInspectError(f"journalctl returned no entries and said: {warning}")
    result = normalise_json_rows(rows)
    result["truncated"] = truncated
    if warning:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# linux.fs.usage
# ---------------------------------------------------------------------------


def build_fs_usage_command(confined_path: str) -> str:
    """Build the fixed ``df`` command for an already confined path."""
    return (
        f"command -v df >/dev/null 2>&1 || exit {_NO_BINARY_EXIT}; "
        "df -B1 --output=source,fstype,size,used,avail,itotal,iused -- "
        f"{shlex.quote(confined_path)}"
    )


def _opt_int(token: str) -> int | None:
    """Return *token* as an int, or ``None`` when df printed ``-``."""
    try:
        return int(token)
    except ValueError:
        return None


def parse_fs_usage_output(stdout: str) -> dict[str, Any]:
    """Parse ``df --output`` output into the flat usage dict, or raise.

    The header line is skipped; the one data line is read from the right
    (five numbers, then the type, then the source, which may hold spaces).
    A missing line or a number that is not a number raises
    :class:`LinuxInspectError`; ``-`` (a filesystem without inodes) is
    ``null``.
    """
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if len(lines) != 2:
        raise LinuxInspectError("df gave an answer that cannot be read")
    tokens = lines[1].split()
    if len(tokens) < 7:
        raise LinuxInspectError("df gave an answer that cannot be read")
    numbers = [_opt_int(tok) for tok in tokens[-5:]]
    if any(n is None for n in numbers[:3]):
        raise LinuxInspectError("df gave an answer that cannot be read")
    return {
        "filesystem": " ".join(tokens[:-6]),
        "fstype": tokens[-6],
        "size_bytes": numbers[0],
        "used_bytes": numbers[1],
        "avail_bytes": numbers[2],
        "inodes_total": numbers[3],
        "inodes_used": numbers[4],
    }


async def linux_fs_usage(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.fs.usage`` -- free and used space of one filesystem."""
    confined = confine_dir_path(params["path"], allow_filesystem_root=True)
    proc = await connector._run_command(target, build_fs_usage_command(confined), operator=operator)
    if getattr(proc, "exit_status", 0) == _NO_BINARY_EXIT:
        raise LinuxInspectError("host has no df; cannot read filesystem usage")
    if getattr(proc, "exit_status", 0) not in (0, None):
        raise LinuxInspectError(f"df failed for {confined!r}; usage was not read")
    raw = getattr(proc, "stdout", None)
    return {"path": confined, **parse_fs_usage_output(raw if isinstance(raw, str) else "")}


# ---------------------------------------------------------------------------
# linux.dir.list
# ---------------------------------------------------------------------------


def _dir_depth(raw: Any) -> int:
    if raw is None:
        return 1
    value = int(raw)
    if not 1 <= value <= MAX_DIR_DEPTH:
        raise ValueError(f"depth must be between 1 and {MAX_DIR_DEPTH}")
    return value


def build_dir_list_command(
    confined_path: str, depth: int, include_hidden: bool, cap: int = MAX_DIR_LIST_ROWS
) -> str:
    """Build the fixed single-round-trip ``dir.list`` command.

    The shell changes into the directory with ``cd -P``, prints the real
    path it is in (as hex, like ``file.read``), the content marker, and
    then the ``find`` records (NUL-ended, so a name with a line break
    stays one record) cut at ``cap + 1`` records. The exit code of
    ``find`` follows the records, so a listing that hit an unreadable
    sub-directory is known. *depth* and *cap* are ints; the path is
    ``shlex.quote``d.
    """
    quoted = shlex.quote(confined_path)
    hidden = "" if include_hidden else "-name '.*' -prune -o "
    find = (
        f"find . -mindepth 1 -maxdepth {int(depth)} {hidden}"
        f"-printf {shlex.quote(_FIND_FORMAT)} 2>/dev/null"
    )
    return (
        f"if [ ! -e {quoted} ]; then printf '%s\\n' {shlex.quote(MISSING_MARKER)}; exit 0; fi; "
        f"cd -P -- {quoted} 2>/dev/null || "
        f"{{ printf '%s\\n' {shlex.quote(UNREADABLE_MARKER)}; exit 0; }}; "
        "pwd -P | od -An -v -tx1 | tr -d ' \\n'; echo; "
        f"printf '%s\\n' {shlex.quote(CONTENT_MARKER)}; "
        f"{{ {find}; printf '{_FIND_RC_PREFIX}%s\\n' \"$?\"; }} | head -z -n {int(cap) + 2}"
    )


def _dir_row(record: str) -> dict[str, Any] | None:
    parts = record.split("\t", 5)
    if len(parts) != 6:
        return None
    kind, size, mtime, mode, owner, name = parts
    try:
        size_bytes: int | None = int(size)
    except ValueError:
        size_bytes = None
    try:
        mtime_iso: str | None = datetime.fromtimestamp(float(mtime), UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        mtime_iso = None
    return {
        "name": name,
        "type": _TYPE_NAMES.get(kind, "other"),
        "size_bytes": size_bytes,
        "mtime": mtime_iso,
        "mode": mode,
        "owner": owner,
    }


def parse_dir_list_output(
    stdout: str, confined_path: str, cap: int = MAX_DIR_LIST_ROWS
) -> dict[str, Any]:
    """Parse the ``dir.list`` output into its parts, or raise when it is unclear.

    Returns ``{exists, real_path, rows, truncated, incomplete}``. A missing
    directory has ``exists=False`` and no rows. An unreadable directory,
    an answer without the marker, or a listing that has neither the
    ``find`` exit code nor more rows than the cap raises
    :class:`LinuxInspectError` (or :class:`PathConfinementError` for an
    unclear real path): a half answer is never returned as a listing.
    """
    if stdout.startswith(MISSING_MARKER):
        return {
            "exists": False,
            "real_path": None,
            "rows": [],
            "truncated": False,
            "incomplete": False,
        }
    if stdout.startswith(UNREADABLE_MARKER):
        raise LinuxInspectError(f"the directory {confined_path!r} exists but cannot be entered")
    marker_at = stdout.find(CONTENT_MARKER)
    header = stdout[:marker_at].split("\n") if marker_at != -1 else []
    if len(header) != 2 or header[1] != "" or not header[0]:
        raise LinuxInspectError(f"could not tell which directory was opened for {confined_path!r}")
    real_path = _decode_reported_path(header[0], confined_path)
    body = stdout[marker_at + len(CONTENT_MARKER) :]
    body = body[1:] if body.startswith("\n") else body
    pieces = body.split("\0")
    match = _FIND_RC_RE.match(pieces[-1])
    records = pieces[:-1]
    truncated = len(records) > cap
    if match is None and not truncated:
        raise LinuxInspectError(f"the listing of {confined_path!r} did not finish cleanly")
    rows = [row for row in (_dir_row(rec) for rec in records[:cap]) if row is not None]
    if len(rows) != len(records[:cap]):
        raise LinuxInspectError(
            f"the listing of {confined_path!r} holds a line that cannot be read"
        )
    rows.sort(key=lambda r: r["name"])
    incomplete = bool(match and int(match.group(1)) != 0)
    return {
        "exists": True,
        "real_path": real_path,
        "rows": rows,
        "truncated": truncated,
        "incomplete": incomplete,
    }


async def linux_dir_list(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.dir.list`` -- list one directory under the allow-list.

    The typed path is confined before any SSH. The real path of the
    directory the host opened (symlinks followed) goes through the same
    check, and the rows are returned only when it passes.
    """
    confined = confine_dir_path(params["path"])
    depth = _dir_depth(params.get("depth"))
    include_hidden = bool(params.get("include_hidden", False))
    cmd = build_dir_list_command(confined, depth, include_hidden)
    proc = await connector._run_command(target, cmd, operator=operator)
    if getattr(proc, "exit_status", 0) not in (0, None):
        raise LinuxInspectError(f"the listing of {confined!r} did not finish cleanly")
    raw = getattr(proc, "stdout", None)
    parsed = parse_dir_list_output(raw if isinstance(raw, str) else "", confined)
    if parsed["real_path"] is not None:
        confine_dir_path(parsed["real_path"])
    return {
        **normalise_json_rows(parsed["rows"]),
        "path": confined,
        "exists": parsed["exists"],
        "truncated": parsed["truncated"],
        "incomplete": parsed["incomplete"],
    }
