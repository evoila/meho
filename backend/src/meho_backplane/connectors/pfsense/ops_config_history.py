# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense safe read of the configuration change history (``pfsense.config.history``).

pfSense keeps every saved configuration as a revision: the live file
``/cf/conf/config.xml`` and one backup file per older revision,
``/cf/conf/backup/config-<unix time>.xml``. Each file has a top-level
``<revision>`` block with ``time``, ``description`` and ``username``. The
firewall keeps ``<system><backupcount>`` backups (30 when it is not set).

The op has two modes:

* **No ``revision``:** one row per kept revision, newest first: ``time``,
  ``time_utc``, ``user``, ``description``, ``size`` and ``current`` (true for
  the live file). ``since`` (a unix time) keeps only revisions from that time
  on. The answer also says how many backups the firewall keeps, so the reader
  knows how far back the list goes. The command reads only the ``<revision>``
  block of each file, never the whole file.
* **With ``revision`` (its time):** what that revision changed, as a diff
  against the revision before it. Both files are read, both have their known
  secrets removed by the same rules as ``pfsense.config.show``
  (:func:`~meho_backplane.connectors.pfsense.redaction.redact_config_xml`), and
  only then are they compared. The diff is cut by top-level section
  (``filter``, ``system``, ...). The answer lists the changed sections with
  their added and removed line counts. The diff text is returned when it is
  at most :data:`MAX_DIFF_CHARS` characters; otherwise it is left out, and
  the caller asks for one section with ``section``.

Because secrets are removed before the comparison, a changed secret value
does not show in the diff. A section whose only change is a hidden value is
still listed, with ``only_hidden_values_changed: true`` and no diff lines.
That flag is the only thing the comparison of the raw text adds to the answer.

Every error text is fixed text. It never quotes a file.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring
from defusedxml.ElementTree import tostring as xml_tostring

from meho_backplane.connectors.pfsense.redaction import (
    REDACTED,
    ConfigRedactionError,
    looks_like_secret,
    redact_config_xml,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "MAX_DIFF_CHARS",
    "PfSenseConfigHistoryError",
    "compare_revisions",
    "parse_history_listing",
    "pfsense_config_history",
]

#: pfSense keeps this many backups when ``<system><backupcount>`` is not set.
DEFAULT_BACKUP_COUNT = 30

#: The largest diff text the op returns in one answer. A stored result is read
#: in parts of 256 KiB; this keeps one answer well below that.
MAX_DIFF_CHARS = 100_000

_CONTEXT_LINES = 2
_SECTION_RE = re.compile(r"[A-Za-z0-9_.:#-]{1,64}")
_FILE_RE = re.compile(r"config\.xml|backup/config-\d{1,12}\.xml")
_FILE_TIME_RE = re.compile(r"config-(\d{1,12})\.xml")
_FILE_MARK = "@@FILE "
_COUNT_MARK = "@@BACKUPCOUNT "

# One fixed command, no caller text in it. For the live file and each backup:
# the file name, its size, and the first <revision> block (at most 12 lines).
# Then the backup count of the live file.
_LIST_CMD = (
    "cd /cf/conf || exit 1; "
    "for f in config.xml backup/config-*.xml; do "
    '[ -f "$f" ] || continue; '
    'echo "@@FILE $f $(wc -c < "$f" | tr -d \' \')"; '
    "sed -n '/<revision>/,/<\\/revision>/p' \"$f\" | head -n 12; "
    "done; "
    "echo \"@@BACKUPCOUNT $(sed -n 's:.*<backupcount>\\(.*\\)</backupcount>.*:\\1:p' "
    'config.xml | head -n 1)"'
)


class PfSenseConfigHistoryError(ValueError):
    """The history cannot be read or compared. The text never holds file content."""


@dataclass(frozen=True)
class _Revision:
    """One kept revision, as read from the listing command."""

    time: int
    file: str
    size: int | None
    user: str | None
    description: str | None
    current: bool


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Return the stripped text, ``None`` when empty, ``REDACTED`` for a secret shape."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return REDACTED if looks_like_secret(value) else value


def _parse_revision_block(block: str) -> tuple[int | None, str | None, str | None]:
    """Return ``(time, user, description)`` from a ``<revision>`` block text."""
    if not block.strip():
        return None, None, None
    try:
        root = fromstring(block, forbid_dtd=True)
    except (ParseError, ValueError):
        return None, None, None
    time_text = (root.findtext("time") or "").strip()
    time_value = int(time_text) if time_text.isdigit() and len(time_text) <= 12 else None
    return time_value, _clean(root.findtext("username")), _clean(root.findtext("description"))


def parse_history_listing(output: str) -> tuple[list[_Revision], int | None]:
    """Parse the output of the listing command.

    Returns ``(revisions, backup_count)``: the revisions newest first (one
    per time; the live file wins a tie) and the stored ``backupcount``, or
    ``None`` when the config does not set it.

    Raises :class:`PfSenseConfigHistoryError` when the output holds no file
    at all, so a broken read never looks like "no history".
    """
    entries: list[tuple[str, int | None, list[str]]] = []
    backup_count: int | None = None
    for line in output.splitlines():
        if line.startswith(_FILE_MARK):
            parts = line[len(_FILE_MARK) :].split()
            name = parts[0] if parts else ""
            size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            entries.append((name, size, []))
        elif line.startswith(_COUNT_MARK):
            text = line[len(_COUNT_MARK) :].strip()
            backup_count = int(text) if text.isdigit() and 0 < len(text) <= 6 else None
        elif entries:
            entries[-1][2].append(line)
    if not entries:
        raise PfSenseConfigHistoryError(
            "the firewall returned no configuration files; no history was returned"
        )

    by_time: dict[int, _Revision] = {}
    for name, size, block_lines in entries:
        if not _FILE_RE.fullmatch(name):
            continue
        time_value, user, description = _parse_revision_block("\n".join(block_lines))
        if time_value is None:
            match = _FILE_TIME_RE.search(name)
            if match is None:
                continue  # the live file without a revision time: nothing to list
            time_value = int(match.group(1))
        by_time.setdefault(
            time_value,
            _Revision(
                time=time_value,
                file=name,
                size=size,
                user=user,
                description=description,
                current=name == "config.xml",
            ),
        )
    revisions = sorted(by_time.values(), key=lambda rev: rev.time, reverse=True)
    return revisions, backup_count


def _time_utc(value: int) -> str | None:
    try:
        return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _revision_row(rev: _Revision) -> dict[str, Any]:
    return {
        "time": rev.time,
        "time_utc": _time_utc(rev.time),
        "user": rev.user,
        "description": rev.description,
        "size": rev.size,
        "current": rev.current,
    }


def _sections(xml_text: str) -> dict[str, str]:
    """Return the top-level sections of a config as ``{name: text}``.

    The ``<revision>`` block is left out (it differs in every revision and is
    reported separately). A name that occurs twice gets ``#2``, ``#3`` ...
    Raises :class:`PfSenseConfigHistoryError` with fixed text when the file
    is not a pfSense config.
    """
    try:
        root = fromstring(xml_text, forbid_dtd=True)
    except (ParseError, ValueError) as exc:
        raise PfSenseConfigHistoryError(
            f"a configuration file could not be parsed ({type(exc).__name__}); no diff was returned"
        ) from None
    if root.tag != "pfsense":
        raise PfSenseConfigHistoryError(
            "a configuration file has no <pfsense> root element; no diff was returned"
        )
    sections: dict[str, str] = {}
    for child in root:
        if not isinstance(child.tag, str) or child.tag == "revision":
            continue
        name = child.tag
        count = 2
        while name in sections:
            name = f"{child.tag}#{count}"
            count += 1
        sections[name] = xml_tostring(child, encoding="unicode").strip()
    return sections


def _diff_lines(name: str, old: str, new: str, old_time: int, new_time: int) -> list[str]:
    return list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{name}@{old_time}",
            tofile=f"{name}@{new_time}",
            lineterm="",
            n=_CONTEXT_LINES,
        )
    )


def compare_revisions(
    old: tuple[str, str],
    new: tuple[str, str],
    *,
    old_time: int,
    new_time: int,
) -> list[dict[str, Any]]:
    """Compare two revisions section by section; return one entry per changed section.

    *old* and *new* are ``(raw, cleaned)`` pairs of config texts. A section is
    changed when its raw text differs. Its ``diff`` is made from
    the cleaned texts (known secrets removed), so a hidden value never shows.
    A section that changed only in hidden values has no diff lines and
    ``only_hidden_values_changed: True``. The raw texts are never returned.

    Each entry: ``{section, added, removed, only_hidden_values_changed, diff}``
    where ``diff`` is the unified diff text (``""`` when there are no lines).
    """
    old_raw_sections = _sections(old[0])
    new_raw_sections = _sections(new[0])
    old_sections = _sections(old[1])
    new_sections = _sections(new[1])
    names = list(new_raw_sections) + [n for n in old_raw_sections if n not in new_raw_sections]
    changed: list[dict[str, Any]] = []
    for name in names:
        if old_raw_sections.get(name) == new_raw_sections.get(name):
            continue
        lines = _diff_lines(
            name, old_sections.get(name, ""), new_sections.get(name, ""), old_time, new_time
        )
        added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        changed.append(
            {
                "section": name,
                "added": added,
                "removed": removed,
                "only_hidden_values_changed": not lines,
                "diff": "\n".join(lines),
            }
        )
    return changed


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _int_param(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999_999_999_999:
        raise ValueError(f"{key} must be a unix time (a whole number of seconds); got {value!r}")
    return value


async def _read_listing(
    self: PfSenseConnector, target: Any, operator: Operator | None
) -> tuple[list[_Revision], int | None]:
    proc = await self._run_command(target, _LIST_CMD, operator=operator)
    raw = proc.stdout or ""
    stdout = raw if isinstance(raw, str) else ""
    if proc.exit_status != 0:
        raise RuntimeError(f"reading the configuration history failed: exit {proc.exit_status}")
    return parse_history_listing(stdout)


async def _read_file(
    self: PfSenseConnector, target: Any, rev: _Revision, operator: Operator | None
) -> str:
    if not _FILE_RE.fullmatch(rev.file):  # cannot happen: parse_history_listing checked it
        raise PfSenseConfigHistoryError("a revision file name is not valid")
    proc = await self._run_command(target, f"cat /cf/conf/{rev.file}", operator=operator)
    raw = proc.stdout or ""
    stdout = raw if isinstance(raw, str) else ""
    if proc.exit_status != 0 or not stdout.strip():
        raise RuntimeError(f"reading revision {rev.time} failed: exit {proc.exit_status}")
    return stdout


async def _clean_text(raw: str) -> str:
    try:
        cleaned, _count = await asyncio.to_thread(redact_config_xml, raw)
    except Exception as exc:  # fail closed: any fault withholds the diff
        reason = str(exc) if isinstance(exc, ConfigRedactionError) else type(exc).__name__
        raise PfSenseConfigHistoryError(
            f"secret removal failed ({reason}); no diff was returned"
        ) from None
    return cleaned


async def pfsense_config_history(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the configuration change history of a pfSense firewall.

    Op-id: ``pfsense.config.history``. Without ``revision``: ``{rows, total,
    kept, backup_count, backup_count_is_default}``. With ``revision``: the
    changed sections and the diff against the revision before it. See the
    module docstring.
    """
    since = _int_param(params, "since")
    revision = _int_param(params, "revision")
    section = params.get("section")
    if section is not None and (not isinstance(section, str) or not _SECTION_RE.fullmatch(section)):
        raise ValueError(f"section must be a section name such as 'filter'; got {section!r}")
    if revision is None and section is not None:
        raise ValueError("section can only be used together with revision")
    if revision is not None and since is not None:
        raise ValueError("since is for the list; do not combine it with revision")

    revisions, stored_count = await _read_listing(self, target, operator)

    if revision is None:
        rows = [_revision_row(rev) for rev in revisions if since is None or rev.time >= since]
        return {
            "rows": rows,
            "total": len(rows),
            "kept": len(revisions),
            "backup_count": stored_count if stored_count is not None else DEFAULT_BACKUP_COUNT,
            "backup_count_is_default": stored_count is None,
        }

    index = next((i for i, rev in enumerate(revisions) if rev.time == revision), None)
    if index is None:
        raise PfSenseConfigHistoryError(
            f"revision {revision} is not kept on this firewall; list the history to see "
            f"the {len(revisions)} kept revisions"
        )
    new_rev = revisions[index]
    result: dict[str, Any] = {
        "revision": new_rev.time,
        "revision_utc": _time_utc(new_rev.time),
        "user": new_rev.user,
        "description": new_rev.description,
    }
    if index + 1 >= len(revisions):
        return {
            **result,
            "previous_revision": None,
            "changed_sections": [],
            "changed_total": 0,
            "diff": None,
            "diff_omitted": False,
            "note": "this is the oldest kept revision, so there is nothing to compare it with",
        }
    old_rev = revisions[index + 1]
    changed = await _changed_sections(self, target, operator, old_rev, new_rev)
    return {**result, **_diff_answer(changed, section, revision, old_rev.time)}


async def _changed_sections(
    self: PfSenseConnector,
    target: Any,
    operator: Operator | None,
    old_rev: _Revision,
    new_rev: _Revision,
) -> list[dict[str, Any]]:
    """Read both files, remove the known secrets, and compare them section by section."""
    new_raw = await _read_file(self, target, new_rev, operator)
    old_raw = await _read_file(self, target, old_rev, operator)
    new_clean = await _clean_text(new_raw)
    old_clean = await _clean_text(old_raw)
    return await asyncio.to_thread(
        compare_revisions,
        (old_raw, old_clean),
        (new_raw, new_clean),
        old_time=old_rev.time,
        new_time=new_rev.time,
    )


def _diff_answer(
    changed: list[dict[str, Any]], section: str | None, revision: int, previous: int
) -> dict[str, Any]:
    """Build the diff part of the answer, for all changed sections or just *section*."""
    summary = [{k: v for k, v in entry.items() if k != "diff"} for entry in changed]
    if section is not None:
        changed = [entry for entry in changed if entry["section"] == section]
        if not changed:
            names = ", ".join(entry["section"] for entry in summary) or "none"
            raise PfSenseConfigHistoryError(
                f"section {section!r} did not change in revision {revision}; "
                f"changed sections: {names}"
            )
    diff_text = "\n".join(entry["diff"] for entry in changed if entry["diff"])
    omitted = len(diff_text) > MAX_DIFF_CHARS
    note = (
        "Known secret values are removed before the comparison, so a changed secret "
        "does not show in the diff."
    )
    if omitted:
        note += (
            f" The diff is longer than {MAX_DIFF_CHARS} characters, so it is left out. "
            "Ask for one changed section with the section parameter."
        )
    return {
        "previous_revision": previous,
        "changed_sections": summary,
        "changed_total": len(summary),
        "diff": None if omitted else diff_text,
        "diff_omitted": omitted,
        "note": note,
    }
