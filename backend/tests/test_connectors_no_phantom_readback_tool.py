# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.19-T1 (#1479) grep gate: no agent-facing phantom read-back tool refs.

Several connectors' ``llm_instructions`` / ``when_to_use`` / ``when_to_call``
/ ``next_step`` strings used to instruct the LLM to "use ``result_query`` /
``result_describe`` to navigate the handle" or to spill "through the shared
``HandleStore``". Neither the read-back meta-tools nor the ``HandleStore``
exist in this version (G3.1-T4 #304 was closed-superseded), so the guidance
sent the agent at a tool it can never call.

This gate walks the connectors source tree and fails if any **action
phrase** directing the agent at a phantom read-back surface reappears. It
deliberately matches *instruction* phrasing (``use result_query``,
``navigate via …``, ``through the shared HandleStore``) rather than every
mention of the token — module docstrings that *document the absence*
(``#304 was closed as superseded; no HandleStore landed``) are legitimate
historical context and must stay.
"""

from __future__ import annotations

import re
from pathlib import Path

_CONNECTORS_ROOT = Path(__file__).resolve().parent.parent / "src" / "meho_backplane" / "connectors"

#: Action phrases that direct the agent at a read-back surface that does not
#: exist. Case-insensitive. Each is an *instruction* ("use X", "navigate via
#: X", "reads it back via X", "through the shared HandleStore") — not a bare
#: mention of the token, which can legitimately document the tool's absence.
_PHANTOM_ACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"use\s+result_(?:describe|query|aggregate|export)", re.IGNORECASE),
    re.compile(r"navigate\s+(?:via|the\s+handle\s+via)\s+result_", re.IGNORECASE),
    re.compile(r"drills?\s+in(?:to)?\s+via\b[^.]*result_", re.IGNORECASE),
    re.compile(r"reads?\s+(?:them|it)?\s*back\s+via\s+result_", re.IGNORECASE),
    re.compile(r"paging\s+via\s+result_", re.IGNORECASE),
    re.compile(r"available\s+for\s+paging\s+via\s+result_", re.IGNORECASE),
    re.compile(r"through\s+the\s+shared\s+handlestore", re.IGNORECASE),
    re.compile(r"via\s+the\s+handlestore", re.IGNORECASE),
)


def test_no_connector_string_points_at_a_phantom_readback_tool() -> None:
    """No connector source line directs the agent at a non-existent read-back tool."""
    offenders: list[str] = []
    for path in sorted(_CONNECTORS_ROOT.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in _PHANTOM_ACTION_PATTERNS:
                if pattern.search(line):
                    rel = path.relative_to(_CONNECTORS_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "agent-facing strings must not instruct the LLM to use a read-back "
        "meta-tool / HandleStore that does not exist in this version:\n" + "\n".join(offenders)
    )
