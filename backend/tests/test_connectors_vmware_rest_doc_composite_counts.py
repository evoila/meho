# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep the composite counts in ``docs/codebase/connectors-vmware-rest.md`` true.

The doc states how many vmware-rest composites exist, and how many are reads
and writes, in a few places. Those numbers went stale more than once when new
composites landed. This test reads each count from the doc and compares it to
the ``_COMPOSITES`` registry tuple, so a new composite fails CI until the doc
is updated too.

A read is a composite with ``requires_approval=False``. That includes
``vm.guest.file.read``, which is ``caution`` but does not need approval.
"""

from __future__ import annotations

import pathlib
import re
from collections import Counter

import pytest

from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES

_DOC = pathlib.Path(__file__).resolve().parents[2] / "docs/codebase/connectors-vmware-rest.md"

_TOTAL = len(_COMPOSITES)
_READS = sum(1 for spec in _COMPOSITES if not spec.requires_approval)
_WRITES = _TOTAL - _READS
_WRITE_LEVELS = Counter(spec.safety_level for spec in _COMPOSITES if spec.requires_approval)


def _doc_text() -> str:
    # Join wrapped lines so a phrase split across two lines still matches.
    return " ".join(_DOC.read_text(encoding="utf-8").split())


#: (pattern, expected numbers in group order). Each pattern must match the doc
#: at least once, and every match must carry the expected numbers.
_COUNT_PHRASES: list[tuple[str, tuple[int, ...]]] = [
    (r"plus (\d+) hand-authored composites", (_TOTAL,)),
    (r"cross-spec workflows: (\d+) read composites", (_READS,)),
    (r"\) and (\d+) write composites \(G3\.1-T6", (_WRITES,)),
    (
        r"`_COMPOSITES` tuple of (\d+) `_CompositeSpec` rows \((\d+) read .*?"
        r"\+ (\d+) dangerous/destructive writes \+ (\d+) caution writes\)",
        (
            _TOTAL,
            _READS,
            _WRITE_LEVELS["dangerous"] + _WRITE_LEVELS["destructive"],
            _WRITE_LEVELS["caution"],
        ),
    ),
    (r"the (\d+) `vmware\.composite\.\*` rows", (_TOTAL,)),
    (r"`source_kind=\"composite\"` — (\d+) reads \(", (_READS,)),
    (r"and (\d+) writes, all `requires_approval=True`", (_WRITES,)),
    (r"\((\d+) `safety_level=\"dangerous\"`;", (_WRITE_LEVELS["dangerous"],)),
    (r"; (\d+) `safety_level=\"caution\"` —", (_WRITE_LEVELS["caution"],)),
    (r"and (\d+) `safety_level=\"destructive\"` —", (_WRITE_LEVELS["destructive"],)),
    (r"The (\d+) composites \((\d+) reads \+ (\d+) writes\)", (_TOTAL, _READS, _WRITES)),
    (r"The (\d+) composites are hand-authored aggregators", (_TOTAL,)),
]


@pytest.mark.parametrize(("pattern", "expected"), _COUNT_PHRASES)
def test_doc_composite_count_matches_registry(pattern: str, expected: tuple[int, ...]) -> None:
    matches = re.findall(pattern, _doc_text())
    assert matches, f"count phrase not found in {_DOC.name}: {pattern!r}"
    for match in matches:
        found = tuple(int(n) for n in ((match,) if isinstance(match, str) else match))
        assert found == expected, (
            f"{_DOC.name} says {found} for {pattern!r}, but the _COMPOSITES registry "
            f"gives {expected}. Update the doc."
        )


def test_registry_split_is_consistent() -> None:
    """Every write needs approval and is not ``safe``; the levels add up."""
    assert sum(_WRITE_LEVELS.values()) == _WRITES
    assert "safe" not in _WRITE_LEVELS
