# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operation rows of the journal, filesystem usage and directory listing reads.

The handlers and checks are in :mod:`meho_backplane.connectors.linux.ops_inspect`.
"""

from __future__ import annotations

from meho_backplane.connectors.linux.ops import MAX_TAIL_LINES, SSH_TRANSPORT_NOTE, LinuxOp
from meho_backplane.connectors.linux.ops_host import UNIT_NAME_PATTERN
from meho_backplane.connectors.linux.ops_inspect import (
    JOURNAL_PRIORITIES,
    JOURNAL_TIME_PATTERN,
    MAX_DIR_DEPTH,
)

__all__ = ["INSPECT_OPS"]


_JOURNAL_TAIL_OP = LinuxOp(
    op_id="linux.journal.tail",
    handler_attr="journal_tail",
    summary="Read the last entries of the systemd journal (kernel, nfsd, services).",
    description=(
        "Reads the last entries of the systemd journal (default 200, max 10000), for one unit, "
        "a time window or a lowest priority. Returns {rows, total, truncated}; each row is "
        "{ts, unit, priority, message}. Read-only. Errors if journalctl is missing or unreadable."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "minLength": 1,
                "pattern": UNIT_NAME_PATTERN,
                "description": "Only this systemd unit, e.g. nfs-server.service.",
            },
            "since": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": JOURNAL_TIME_PATTERN,
                "description": "Start of the window, e.g. '2 hours ago' or '2026-11-10 08:00:00'.",
            },
            "until": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": JOURNAL_TIME_PATTERN,
                "description": "End of the window, same forms as since.",
            },
            "priority": {
                "type": "string",
                "enum": sorted(JOURNAL_PRIORITIES),
                "description": "Lowest priority to show: 0-7 or emerg, alert, crit, err, ...",
            },
            "lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TAIL_LINES,
                "description": "Number of entries (default 200).",
            },
        },
        "additionalProperties": False,
        "examples": [{"priority": "err", "since": "1 hour ago"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ts": {"type": ["string", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "priority": {"type": ["integer", "null"]},
                        "message": {"type": ["string", "null"]},
                    },
                },
            },
            "total": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "warning": {"type": "string"},
        },
        "required": ["rows", "total"],
        "additionalProperties": True,
    },
    group_key="log",
    tags=("read-only", "log", "journal", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read kernel, nfsd, XFS or service messages that are only in the "
            "systemd journal, for example after a storage or NFS fault. For a plain log "
            "file use linux.log.tail. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "unit": "One systemd unit name.",
            "since": "Journal time, e.g. '30 min ago'.",
            "priority": "err shows errors and worse.",
        },
        "output_shape": (
            "{rows, total, truncated}; rows are {ts, unit, priority, message}, oldest first. "
            "If warning is set, the journal may be incomplete (for example the login can "
            "only see its own messages). Large answers spill to a result_query handle."
        ),
        "next_step": (
            "If a unit name shows in the rows, call linux.service.status for it. For a "
            "plain log file call linux.log.tail."
        ),
    },
)

_FS_USAGE_OP = LinuxOp(
    op_id="linux.fs.usage",
    handler_attr="fs_usage",
    summary="Read the size, used and free bytes and inodes of the filesystem that holds a path.",
    description=(
        "Runs df for one path under the directory allow-list and returns the filesystem, type, "
        "size, used and free bytes and the inode counts. Read-only. A missing path gives an error."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute path inside the filesystem to measure, e.g. /srv/export.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
        "examples": [{"path": "/srv/export"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "filesystem": {"type": "string"},
            "fstype": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "used_bytes": {"type": "integer"},
            "avail_bytes": {"type": "integer"},
            "inodes_total": {"type": ["integer", "null"]},
            "inodes_used": {"type": ["integer", "null"]},
        },
        "required": [
            "path",
            "filesystem",
            "fstype",
            "size_bytes",
            "used_bytes",
            "avail_bytes",
        ],
        "additionalProperties": True,
    },
    group_key="storage",
    tags=("read-only", "storage", "capacity", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see whether a filesystem is full (space or inodes), for example when "
            "writes fail with ENOSPC or an NFS export stops accepting data. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {"path": "A path on the filesystem to check, e.g. an export path."},
        "output_shape": (
            "Flat dict with byte counts. used_bytes plus avail_bytes can be less than "
            "size_bytes because of reserved blocks."
        ),
        "next_step": (
            "If a filesystem is nearly full, call linux.dir.list on its mount point to find"
            " large or leftover files. For mounts call linux.mount.list."
        ),
    },
)

_DIR_LIST_OP = LinuxOp(
    op_id="linux.dir.list",
    handler_attr="dir_list",
    summary="List the names, types, sizes, times, modes and owners in a directory.",
    description=(
        "Lists one directory (depth 1 or 2) under the directory allow-list. Returns {rows, "
        "total, path, exists, truncated, incomplete}; each row is {name, type, size_bytes, "
        "mtime, mode, owner}. Read-only. At most 5000 rows."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute directory path under the directory allow-list.",
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DIR_DEPTH,
                "description": "1 lists the directory; 2 also lists one level below (default 1).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Also list names that start with a dot (default false).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
        "examples": [{"path": "/srv/export", "depth": 1}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "size_bytes": {"type": ["integer", "null"]},
                        "mtime": {"type": ["string", "null"]},
                        "mode": {"type": "string"},
                        "owner": {"type": "string"},
                    },
                },
            },
            "total": {"type": "integer"},
            "path": {"type": "string"},
            "exists": {"type": "boolean"},
            "truncated": {"type": "boolean"},
            "incomplete": {"type": "boolean"},
        },
        "required": ["rows", "total", "path", "exists", "truncated", "incomplete"],
        "additionalProperties": True,
    },
    group_key="file",
    tags=("read-only", "file", "directory", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which files are in a directory, for example to find leftovers of a "
            "failed deploy in an export path. To read a file's content use linux.file.read. "
            + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "path": "Absolute directory path.",
            "depth": "1 or 2.",
            "include_hidden": "true to list dot files.",
        },
        "output_shape": (
            "{rows, total, path, exists, truncated, incomplete}; rows are sorted by name; "
            "name is relative to path. If truncated or incomplete is true the list is not "
            "the whole directory. Large lists spill to a result_query handle."
        ),
        "next_step": (
            "To read one file in the listing call linux.file.read. To see how full the "
            "filesystem is call linux.fs.usage."
        ),
    },
)


#: The journal, filesystem usage and directory listing tier composed onto ``LINUX_OPS``.
INSPECT_OPS: tuple[LinuxOp, ...] = (_JOURNAL_TAIL_OP, _FS_USAGE_OP, _DIR_LIST_OP)
