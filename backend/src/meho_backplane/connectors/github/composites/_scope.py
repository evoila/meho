# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep the five GitHub ticket writes inside the target's repository and board.

The ticket writes (file an issue, comment on an issue, link a sub-issue, add
an item to a board, set a board field) are ``safe``: they run at once, with
no approval. That is true only while each write stays inside the one
repository and the one board its target names
(``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``).

* The **repository** comes from the target's ``host``:
  ``api.github.com/repos/<owner>/<repo>`` or ``<owner>/<repo>``.
* The **board** comes from the target's ``extras["project_id"]``: the node id
  of the Projects-v2 board (``project_id`` from ``gh.composite.project_view``).

A write to another repository or board is refused with
:class:`TicketWriteScopeError` before any call to GitHub. So is a write on a
target that names no repository, or a board write on a target that names no
board; the message says what to add to the target.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.connectors.github.connector import _extract_repo_path

__all__ = [
    "BOARD_EXTRAS_KEY",
    "TicketWriteScopeError",
    "require_target_board",
    "require_target_repo",
    "target_repo",
]

#: The target ``extras`` key that holds the node id of the target's board.
BOARD_EXTRAS_KEY: Final = "project_id"

# A GitHub owner login. It has no dot, so a bare host such as
# ``api.github.com/<owner>`` is never read as ``owner/repo``.
_OWNER: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class TicketWriteScopeError(ValueError):
    """A ticket write would leave the repository or board of its target."""


def _target_name(target: Any) -> str:
    return str(getattr(target, "name", None) or "<unnamed>")


def target_repo(target: Any) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` that the target's ``host`` names, or ``None``."""
    host = getattr(target, "host", None)
    if not isinstance(host, str):
        return None
    path = _extract_repo_path(host.strip().strip("/"))
    if path is None:
        return None
    owner, _, repo = path.partition("/")
    if not _OWNER.fullmatch(owner) or not repo:
        return None
    return owner, repo


def require_target_repo(target: Any, owner: object, repo: object) -> None:
    """Refuse unless ``owner/repo`` is the repository the target names.

    GitHub names are not case-sensitive, so the compare is not either.
    """
    named = target_repo(target)
    if named is None:
        raise TicketWriteScopeError(
            f"target {_target_name(target)!r} names no repository, so it cannot run "
            "GitHub ticket writes; set the target's host to "
            "'api.github.com/repos/<owner>/<repo>'"
        )
    asked = f"{owner}/{repo}"
    if asked.lower() != f"{named[0]}/{named[1]}".lower():
        raise TicketWriteScopeError(
            f"repository {asked!r} is not the repository of target "
            f"{_target_name(target)!r} ({named[0]}/{named[1]}); a ticket write may "
            "change only the target's own repository"
        )


def require_target_board(target: Any, project_id: object) -> None:
    """Refuse unless *project_id* is the node id of the target's board."""
    extras = getattr(target, "extras", None)
    named = extras.get(BOARD_EXTRAS_KEY) if isinstance(extras, Mapping) else None
    if not isinstance(named, str) or not named.strip():
        raise TicketWriteScopeError(
            f"target {_target_name(target)!r} names no board, so it cannot run board "
            f"writes; set extras.{BOARD_EXTRAS_KEY} on the target to the board's node id "
            "(project_id from gh.composite.project_view)"
        )
    if project_id != named.strip():
        raise TicketWriteScopeError(
            f"board {project_id!r} is not the board of target {_target_name(target)!r}; "
            "a board write may change only the target's own board"
        )
