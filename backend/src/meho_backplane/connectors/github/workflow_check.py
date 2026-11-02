# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find the GitHub workflows that ticket writes would start.

The five GitHub ticket writes are ``safe`` and need no approval
(``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``). That holds
only while a ticket write starts no change elsewhere. A workflow in the
target's repository that starts on an issue, comment, sub-issue or board
event breaks that rule: MEHO's ticket write would start it.

:func:`ticket_event_workflow_check` reads the files in
``.github/workflows/`` of the target's repository and reports each workflow
that starts on one of :data:`TICKET_EVENTS`. The probe of a target that names
one repository puts the result into the target's fingerprint, so the warning
shows on the target.

The check sees only workflow files. The connector's rights cannot read every
webhook and installed app, so the setup guide asks the operator to check
those once in the repository settings.

A failed read is never "no workflows": the answer then says
``workflow_check="unknown"`` with the reason.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx
import yaml

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = [
    "MAX_WORKFLOW_FILES",
    "TICKET_EVENTS",
    "ticket_event_workflow_check",
    "workflow_events",
]

#: Workflow events that a ticket write can start: issue opened, edited,
#: commented, labelled or assigned; sub-issue added; board item added or
#: changed (both the classic and the Projects-v2 board events).
TICKET_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "issues",
        "issue_comment",
        "sub_issues",
        "project",
        "project_card",
        "project_column",
        "projects_v2",
        "projects_v2_item",
    }
)

#: Most workflow files one check reads. More files make the answer incomplete.
MAX_WORKFLOW_FILES: Final[int] = 100

_WORKFLOWS_DIR: Final = ".github/workflows"
_WORKFLOW_SUFFIXES: Final[tuple[str, ...]] = (".yml", ".yaml")


def workflow_events(text: str) -> set[str]:
    """Return the event names in the ``on:`` key of one workflow file.

    ``on`` may be one event, a list of events, or a map of events. YAML 1.1
    reads a bare ``on`` key as ``True``, so both keys are looked up.

    Raises
    ------
    ValueError
        The text is not YAML, or not a map.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise ValueError("the file is not a YAML map")
    trigger = doc.get("on", doc.get(True))
    if isinstance(trigger, str):
        return {trigger}
    if isinstance(trigger, list):
        return {item for item in trigger if isinstance(item, str)}
    if isinstance(trigger, Mapping):
        return {key for key in trigger if isinstance(key, str)}
    return set()


def _decode(payload: object) -> str:
    content = payload.get("content") if isinstance(payload, Mapping) else None
    encoding = payload.get("encoding") if isinstance(payload, Mapping) else None
    if not isinstance(content, str) or encoding != "base64":
        raise ValueError("GitHub returned no file content")
    try:
        return base64.b64decode(content).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"the file content cannot be decoded: {exc}") from exc


def _warning(found: list[dict[str, Any]]) -> str | None:
    if not found:
        return None
    names = ", ".join(entry["path"] for entry in found)
    return (
        f"{len(found)} workflow(s) start on issue, comment, sub-issue or board events: "
        f"{names}. A ticket write from MEHO would start them. Remove those triggers, "
        "or do not run ticket writes on this target."
    )


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "workflow_check": "unknown",
        "workflow_check_error": reason,
        "ticket_event_workflows": None,
        "ticket_write_warning": (
            "MEHO could not check the workflows of this repository; a ticket write may "
            f"start one ({reason})."
        ),
    }


async def _list_workflow_files(
    connector: GitHubRestConnector, target: Any, operator: Operator, repo_path: str
) -> list[str] | None:
    """Return the workflow file paths, ``[]`` when the folder does not exist."""
    path = f"/repos/{quote(repo_path, safe='/')}/contents/{_WORKFLOWS_DIR}"
    try:
        listing: Any = await connector._get_json(target, path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    if not isinstance(listing, list):
        return None
    return sorted(
        entry["path"]
        for entry in listing
        if isinstance(entry, Mapping)
        and entry.get("type") == "file"
        and isinstance(entry.get("path"), str)
        and entry["path"].endswith(_WORKFLOW_SUFFIXES)
    )


async def ticket_event_workflow_check(
    connector: GitHubRestConnector, target: Any, operator: Operator, repo_path: str
) -> dict[str, Any]:
    """Report the workflows of ``repo_path`` that start on ticket events.

    Returns the fingerprint extras: ``workflow_check`` (``ok``, ``incomplete``
    or ``unknown``), ``ticket_event_workflows`` (``[{path, events}]``, or
    ``None`` when unknown), ``ticket_write_warning`` (text, or ``None``) and,
    when a read failed, ``workflow_check_error``.
    """
    try:
        files = await _list_workflow_files(connector, target, operator, repo_path)
    except (httpx.HTTPError, OSError, RuntimeError) as exc:
        return _unknown(f"reading {_WORKFLOWS_DIR} failed: {type(exc).__name__}: {exc}")
    if files is None:
        return _unknown(f"GitHub's answer for {_WORKFLOWS_DIR} is not a folder listing")

    found: list[dict[str, Any]] = []
    unread: list[str] = []
    for file_path in files[:MAX_WORKFLOW_FILES]:
        try:
            payload = await connector._get_json(
                target,
                f"/repos/{quote(repo_path, safe='/')}/contents/{quote(file_path, safe='/')}",
                operator=operator,
            )
            events = workflow_events(_decode(payload)) & TICKET_EVENTS
        except (httpx.HTTPError, OSError, RuntimeError, ValueError):
            unread.append(file_path)
            continue
        if events:
            found.append({"path": file_path, "events": sorted(events)})

    unread.extend(files[MAX_WORKFLOW_FILES:])
    result: dict[str, Any] = {
        "workflow_check": "incomplete" if unread else "ok",
        "ticket_event_workflows": found,
        "ticket_write_warning": _warning(found),
    }
    if unread:
        result["workflow_check_error"] = (
            f"{len(unread)} workflow file(s) could not be read: {', '.join(unread[:10])}"
        )
        if result["ticket_write_warning"] is None:
            result["ticket_write_warning"] = (
                "Some workflow files could not be read; a ticket write may start one of them."
            )
    return result
