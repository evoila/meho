# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No guide or codebase doc describes an agent as a way to approve your own change.

A request of an agent counts as a request of the people behind it
(``docs/codebase/approvals.md``, "The people behind a request"). So the
guides and the codebase docs must not offer an agent as the answer for a
one-person install. The check reads the guides (``docs-site/``), the
codebase docs (``docs/codebase/``) and ``docs/RELEASING.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Words of the old recipe.
_OLD_RECIPE = (
    "agent-requester",
    "agent requester",
    "set up the agent requester",
    "approve as yourself",
    "which identity clears the gate",
)


def _docs() -> list[Path]:
    found = sorted((_REPO_ROOT / "docs-site").rglob("*.md"))
    found += sorted((_REPO_ROOT / "docs" / "codebase").rglob("*.md"))
    found.append(_REPO_ROOT / "docs" / "RELEASING.md")
    return found


def test_the_docs_are_found() -> None:
    assert len(_docs()) > 50


@pytest.mark.parametrize("words", _OLD_RECIPE)
def test_no_doc_offers_an_agent_as_a_way_to_approve_your_own_change(words: str) -> None:
    hits = [
        f"{path.relative_to(_REPO_ROOT)}:{number}"
        for path in _docs()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if words in line.lower()
    ]
    assert hits == [], f"old recipe words {words!r} in: {hits}"


def test_the_guide_says_an_agent_is_never_a_way_to_approve_your_own_change() -> None:
    guide = (_REPO_ROOT / "docs-site" / "guides" / "approvals-and-break-glass.md").read_text(
        encoding="utf-8"
    )
    assert "An agent is never a way to\napprove your own change." in guide
