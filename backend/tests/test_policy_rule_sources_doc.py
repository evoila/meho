# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The rule sources in the code and in the docs are the same list.

The test reads the values of :data:`~meho_backplane.policy.evaluate.RuleSource`
and the source column of the "Rule sources" table in
``docs/codebase/policy-evaluator.md``, and fails when they differ.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from meho_backplane.policy.evaluate import RuleSource

_DOC = Path(__file__).resolve().parents[2] / "docs" / "codebase" / "policy-evaluator.md"


def doc_rule_sources(text: str) -> list[str]:
    """The first column of the table under the heading "Rule sources"."""
    section = text.split("### Rule sources", 1)[1]
    sources: list[str] = []
    in_table = False
    for line in section.splitlines():
        if line.startswith("| Source |"):
            in_table = True
            continue
        if not in_table or line.startswith("|---"):
            continue
        if not line.startswith("|"):
            break
        match = re.match(r"\|\s*`([a-z_]+)`\s*\|", line)
        assert match, line
        sources.append(match.group(1))
    return sources


def test_the_rule_sources_in_the_docs_are_the_ones_in_the_code() -> None:
    documented = doc_rule_sources(_DOC.read_text(encoding="utf-8"))
    assert len(documented) == len(set(documented))
    assert set(documented) == set(get_args(RuleSource))
