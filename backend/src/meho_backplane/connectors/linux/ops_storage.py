# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Storage inspection read verb for :class:`LinuxSshConnector` (#3360).

``linux.mount.list`` (group ``storage``) -- a ``safe``, read-only view of
the host's mount table plus its NFS-export inspection, the minimum a day-0
recipe needs to answer "is the base NFS export live; is the expected mount
present". Takes no operator parameters (a fixed command). Returns
``{rows, total}`` where each row carries a ``kind`` discriminator:

* ``kind="mount"`` -- a mount entry (``source`` / ``target`` / ``fstype`` /
  ``options``), from ``findmnt`` (preferred) or ``mount``.
* ``kind="export"`` -- an exported path (``path`` / ``clients``), from
  ``exportfs -s`` (preferred) or ``showmount -e``.

Block-topology and capacity reporting (``df`` / ``lsblk``) is out of scope
-- a separate op if a concrete need arises. The row list spills to a
``result_query`` handle above the JSONFlux threshold.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import (
    SSH_TRANSPORT_NOTE,
    LinuxOp,
    normalise_json_rows,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "MOUNT_LIST_COMMAND",
    "SECTION_MARKER",
    "STORAGE_OPS",
    "linux_mount_list",
    "parse_export_line",
    "parse_mount_line",
    "parse_mount_table",
]

#: Prefix marking a section boundary in the command's single-round-trip
#: output (``MEHO_SECTION=mounts`` / ``MEHO_SECTION=exports``).
SECTION_MARKER: str = "MEHO_SECTION="

#: The fixed, operator-input-free storage inspection command. ``findmnt``
#: raw mode (``-rno``) prints a stable 4-column table; ``mount`` is the
#: fallback. ``exportfs -s`` prints the active NFS exports; ``showmount -e
#: localhost`` is the fallback (its header line is skipped by the parser).
MOUNT_LIST_COMMAND: str = (
    f"printf '%s\\n' {SECTION_MARKER!r}mounts; "
    "findmnt -rno SOURCE,TARGET,FSTYPE,OPTIONS 2>/dev/null || mount 2>/dev/null; "
    f"printf '%s\\n' {SECTION_MARKER!r}exports; "
    "exportfs -s 2>/dev/null || showmount -e localhost 2>/dev/null || true"
)

#: ``mount(8)`` line shape: ``SOURCE on TARGET type FSTYPE (OPTIONS)``.
_MOUNT8_RE: re.Pattern[str] = re.compile(
    r"^(?P<source>.+?) on (?P<target>.+?) type (?P<fstype>\S+) \((?P<options>.*)\)\s*$"
)


def parse_mount_line(line: str) -> dict[str, Any] | None:
    """Parse one mount line into a ``kind="mount"`` row, or ``None`` if blank.

    Handles both the ``mount(8)`` shape (``SOURCE on TARGET type FSTYPE
    (OPTIONS)``) and the ``findmnt -rno`` raw shape (four
    whitespace-separated columns). An unparseable non-blank line still
    yields a row carrying the raw text under ``source`` so nothing is
    silently dropped.
    """
    stripped = line.strip()
    if not stripped:
        return None

    match = _MOUNT8_RE.match(line)
    if match is not None:
        return {
            "kind": "mount",
            "source": match.group("source").strip(),
            "target": match.group("target").strip(),
            "fstype": match.group("fstype").strip(),
            "options": match.group("options").strip(),
        }

    # findmnt raw: SOURCE TARGET FSTYPE OPTIONS (options never contain a
    # bare space; findmnt escapes them), so split on whitespace, 4 fields.
    parts = stripped.split(maxsplit=3)
    return {
        "kind": "mount",
        "source": parts[0] if len(parts) > 0 else stripped,
        "target": parts[1] if len(parts) > 1 else None,
        "fstype": parts[2] if len(parts) > 2 else None,
        "options": parts[3] if len(parts) > 3 else None,
    }


def parse_export_line(line: str) -> dict[str, Any] | None:
    """Parse one NFS-export line into a ``kind="export"`` row, or ``None``.

    Handles ``exportfs -s`` (``/path client(options)``) and ``showmount
    -e`` (``/path client1,client2``). The ``showmount`` header line
    (``Export list for ...``) and blank lines yield ``None``. The first
    whitespace token is the exported path; the remainder is the client
    spec (``None`` when no client is listed).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("Export list for"):
        return None
    if not stripped.startswith("/"):
        return None
    path, _, clients = stripped.partition(" ")
    clients = clients.strip()
    return {"kind": "export", "path": path, "clients": clients or None}


def parse_mount_table(stdout: str) -> list[dict[str, Any]]:
    """Parse the sectioned command output into the merged mount + export rows."""
    section: str | None = None
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if line.startswith(SECTION_MARKER):
            section = line[len(SECTION_MARKER) :].strip()
            continue
        if section == "mounts":
            row = parse_mount_line(line)
            if row is not None:
                rows.append(row)
        elif section == "exports":
            row = parse_export_line(line)
            if row is not None:
                rows.append(row)
    return rows


async def linux_mount_list(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.mount.list`` -- mount table + NFS-export inspection."""
    del params  # declared empty in schema; intentionally ignored
    proc = await connector._run_command(target, MOUNT_LIST_COMMAND, operator=operator)
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    rows = parse_mount_table(stdout)
    return normalise_json_rows(rows)


_MOUNT_LIST_OP = LinuxOp(
    op_id="linux.mount.list",
    handler_attr="mount_list",
    summary="List the host's mount table and NFS exports.",
    description=(
        "Runs ``findmnt`` (or ``mount``) plus ``exportfs -s`` (or "
        "``showmount -e``) and returns ``{rows, total}`` where each row "
        "carries a ``kind`` discriminator: ``mount`` (``source`` / "
        "``target`` / ``fstype`` / ``options``) or ``export`` (``path`` / "
        "``clients``). Takes no parameters. Read-only -- it never mounts, "
        "unmounts, or edits an export. Block-topology / capacity (``df`` / "
        "``lsblk``) is out of scope. Above the JSONFlux threshold the rows "
        "spill to a handle paged with ``result_query(handle_id, offset, "
        "limit)``."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": {"type": "object"}},
            "total": {"type": "integer"},
        },
        "required": ["rows", "total"],
        "additionalProperties": True,
    },
    group_key="storage",
    tags=("read-only", "storage", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to confirm the base NFS export is live and the expected "
            "mount is present -- e.g. the first-boot script was meant to "
            "export a share and mount a datastore. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "``{rows, total}``; each row's ``kind`` is ``mount`` or "
            "``export``. Large tables spill to a ``result_query`` handle."
        ),
    },
)


#: The storage read tier composed onto ``LINUX_OPS``.
STORAGE_OPS: tuple[LinuxOp, ...] = (_MOUNT_LIST_OP,)
