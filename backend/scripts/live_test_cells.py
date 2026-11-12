# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Text for the "Tested live on" cells of the connector pages.

The text comes from the live test record, ``docs/compatibility/live-tests.yaml``.
It uses only the standard library and PyYAML, so it is easy to test.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from scripts.validate_live_tests import changelog_releases, load_record

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LEVEL_WORDS = {"read": "reads", "change": "changes"}

NOT_TESTED = "not yet tested live"
NOT_RECORDED = "not yet recorded"


def _release_key(release: str) -> tuple[int, ...]:
    return tuple(int(part) for part in release.split("."))


class LiveTestRecord:
    """The record plus the newest MEHO release, read from the repository."""

    def __init__(self, data: dict[str, Any], latest_release: str) -> None:
        self.connectors: dict[str, Any] = data.get("connectors") or {}
        self.stale_after: int = data["stale_after_minor_releases"]
        self.latest_release = latest_release

    @classmethod
    def from_repo(cls, repo_root: Path) -> LiveTestRecord:
        data = load_record((repo_root / "docs/compatibility/live-tests.yaml").read_text("utf-8"))
        releases = changelog_releases((repo_root / "CHANGELOG.md").read_text("utf-8"))
        return cls(data, max(releases, key=_release_key))

    def _is_old(self, release: str) -> bool:
        tested, latest = _release_key(release), _release_key(self.latest_release)
        if tested[0] != latest[0]:
            return tested[0] < latest[0]
        return latest[1] - tested[1] > self.stale_after

    def _one_test(self, test: dict[str, Any]) -> str:
        day: dt.date = test["date"]
        notes = [f"{_MONTHS[day.month - 1]} {day.year}"]
        if self._is_old(test["meho_release"]):
            notes.append(f"last tested with MEHO v{test['meho_release']}")
        level = _LEVEL_WORDS[test["level"]]
        return f"{test['product_version']}: {level} ({', '.join(notes)})"

    def _one_connector(self, connector_id: str) -> str:
        entry = self.connectors.get(connector_id)
        if entry is None:
            return NOT_RECORDED
        if not entry["tests"]:
            return NOT_TESTED
        return "; ".join(self._one_test(test) for test in entry["tests"])

    def tested_live_cell(self, connector_ids: list[str]) -> str:
        """Return the cell text for a row with one or more connector ids."""
        if len(connector_ids) == 1:
            return self._one_connector(connector_ids[0])
        return " ".join(f"`{cid}`: {self._one_connector(cid)}." for cid in connector_ids)
